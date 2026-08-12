#!/bin/bash
# Host-side egress filter for the cb-vpn proxy container.
#
# Why this exists on top of tinyproxy's Filter: the Filter matches on the *name*
# in the request. It cannot know that `evil.example.com` has an A record of
# 169.254.169.254, so on its own it is bypassed by one DNS entry. These rules
# match on the resolved destination address, after tinyproxy has already dialled,
# which is the only place the check is actually sound.
#
# Rules live in a dedicated CB-VPN-EGRESS chain so they can be replaced wholesale
# without touching ufw's or Docker's own chains.
#
# The chain is jumped to from two places, because container traffic reaches
# private space by two different paths:
#   DOCKER-USER  - traffic being forwarded through the host to somewhere else
#                  (other Docker networks, the LAN, link-local).
#   INPUT        - traffic addressed to an IP the host itself owns, which is
#                  never forwarded and so never sees DOCKER-USER. Without this
#                  the container could still reach the VPS's own tailnet address.
#
# Idempotent: safe to run on every service start.
set -euo pipefail

CHAIN=CB-VPN-EGRESS
SUBNET=172.31.250.0/28

DENY4=(
    127.0.0.0/8
    10.0.0.0/8
    172.16.0.0/12
    192.168.0.0/16
    169.254.0.0/16
    100.64.0.0/10
    0.0.0.0/8
)
DENY6=(
    ::1/128
    fc00::/7
    fe80::/10
)

setup() {
    local ipt=$1 subnet=$2; shift 2
    local denies=("$@")

    # Rebuild from scratch rather than appending, so a changed deny-list does not
    # leave stale accepts behind.
    $ipt -N "$CHAIN" 2>/dev/null || $ipt -F "$CHAIN"

    # First rule, before any deny: reply traffic on a connection somebody else
    # opened is not egress, and matching it against the deny-list broke the whole
    # proxy.
    #
    # The deny-list matches on *destination*, which is right for a connection the
    # container dials and wrong for one it merely answers. A client on the
    # tailnet connects to 100.91.16.6:8888; the container's SYN-ACK goes back to
    # 100.91.16.6, which is inside 100.64.0.0/10, so the rule meant to stop the
    # container reaching the tailnet rejected the proxy's own replies to it. And
    # since the published port is bound to the tailnet address, every legitimate
    # client is a tailnet client -- so the proxy accepted connections and could
    # never answer one. It showed up as a plain TCP timeout from the laptop, with
    # the container "Up", tinyproxy listening, and its log empty, because the
    # request never completed a handshake and so was never a request. The tell
    # was the socket sitting in SYN_RECV in the container's /proc/net/tcp.
    #
    # This does not loosen the SSRF filter it sits above. A connection the
    # container *initiates* into private space is ctstate NEW, matches nothing
    # here, and falls through to the same REJECT as before; only packets belonging
    # to an already-established flow are let past.
    $ipt -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

    local net
    for net in "${denies[@]}"; do
        $ipt -A "$CHAIN" -s "$subnet" -d "$net" -j REJECT --reject-with icmp-port-unreachable 2>/dev/null \
            || $ipt -A "$CHAIN" -s "$subnet" -d "$net" -j REJECT
    done
    $ipt -A "$CHAIN" -j RETURN

    # Delete-then-insert: keeps exactly one jump no matter how often this runs,
    # and re-asserts position 1 in case ufw or Docker rewrote the chain since.
    $ipt -D DOCKER-USER -j "$CHAIN" 2>/dev/null || true
    $ipt -I DOCKER-USER 1 -j "$CHAIN" 2>/dev/null || true

    $ipt -D INPUT -j "$CHAIN" 2>/dev/null || true
    $ipt -I INPUT 1 -j "$CHAIN"
}

teardown() {
    local ipt=$1
    $ipt -D DOCKER-USER -j "$CHAIN" 2>/dev/null || true
    $ipt -D INPUT -j "$CHAIN" 2>/dev/null || true
    $ipt -F "$CHAIN" 2>/dev/null || true
    $ipt -X "$CHAIN" 2>/dev/null || true
}

case "${1:-up}" in
    up)
        setup iptables  "$SUBNET" "${DENY4[@]}"
        # IPv6 is not enabled on this Docker network today, so the v6 chain is
        # empty of matching traffic. It is installed anyway: the day someone
        # flips enable_ipv6 the filter should already be there rather than
        # becoming a silent hole.
        ip6tables -N "$CHAIN" 2>/dev/null || ip6tables -F "$CHAIN"
        # Same exemption, same reason as the v4 chain above. Installed now rather
        # than the day IPv6 is enabled, so this chain cannot come alive already
        # carrying the bug the v4 one just had.
        ip6tables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
        for net in "${DENY6[@]}"; do
            ip6tables -A "$CHAIN" -d "$net" -j REJECT
        done
        ip6tables -A "$CHAIN" -j RETURN
        ip6tables -D DOCKER-USER -j "$CHAIN" 2>/dev/null || true
        ip6tables -I DOCKER-USER 1 -j "$CHAIN" 2>/dev/null || true
        ;;
    down)
        teardown iptables
        teardown ip6tables
        ;;
    show)
        echo "== iptables $CHAIN =="; iptables -S "$CHAIN"
        echo "== iptables jumps ==";  iptables -S DOCKER-USER; iptables -S INPUT | grep "$CHAIN" || true
        echo "== ip6tables $CHAIN =="; ip6tables -S "$CHAIN"
        ;;
    *)
        echo "usage: $0 {up|down|show}" >&2; exit 2 ;;
esac
