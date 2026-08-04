# cb-vpn — the private-browsing exit node

An HTTP forward proxy on the VPS that claude-browser can route traffic through, so
pages see the VPS's address instead of the user's. It is
[tinyproxy](https://tinyproxy.github.io/) in a container, reachable only over the
user's tailnet, and locked down so that a page cannot use it to reach anything on
the private side of the server.

Nothing here is hand-rolled HTTP. Every page the browser loads becomes a client of
this proxy, which makes a bespoke request parser the single worst thing that could
be in this directory.

## Shape

```
laptop (100.84.169.57)
  │  http://cb:<token>@100.91.16.6:8888     ← tailnet only
  ▼
VPS 100.91.16.6 :8888  ─ docker publish, bound to the tailnet address
  │
  ▼  container cb-vpn, bridge cb-vpn 172.31.250.0/28
tinyproxy  ── Filter (deny-list on destination host)
  │
  ▼  DOCKER-USER / INPUT → CB-VPN-EGRESS  (deny-list on resolved address)
open internet, egressing as 162.35.172.112
```

## Client configuration

```
http://<CB_VPN_USER>:<CB_VPN_PASS>@100.91.16.6:8888
```

Both HTTP and HTTPS go through the same URL — HTTPS via `CONNECT`. Credentials are
on the server in `/srv/cb-vpn/.env`, mode 600, generated at first deploy:

```bash
grep CB_VPN /srv/cb-vpn/.env
```

They are not in this repo and must not be put here (`backend/.gitignore` guards it).

### Health check

There is deliberately **no health endpoint on the VPS**. A local endpoint can only
report what the server believes about itself; it cannot prove that traffic actually
leaves via the expected address. The only honest check is an end-to-end one from the
client:

```bash
curl -fsS --max-time 20 --proxy "$PROXY_URL" https://api.ipify.org
# → 162.35.172.112
```

If that returns the VPS's public IP, the whole path works: tailnet reachability,
auth, CONNECT, DNS, and egress. Anything less than that is guessing.

## Deploying

`compose-systemd`, per `.claude/deploy.json`. This directory is the source of truth
and is copied to `/srv/cb-vpn` on the server.

```bash
# from the repo root
for f in Dockerfile docker-compose.yml tinyproxy.conf filter.deny \
         entrypoint.sh firewall.sh cb-vpn.service; do
  ~/.claude/skills/deploy/scripts/vps.sh push "$PWD/backend/$f" "/srv/cb-vpn/$f"
done

~/.claude/skills/deploy/scripts/vps.sh run '
  chmod 0755 /srv/cb-vpn/firewall.sh /srv/cb-vpn/entrypoint.sh
  install -m 0644 /srv/cb-vpn/cb-vpn.service /etc/systemd/system/cb-vpn.service
  systemctl daemon-reload && systemctl enable --now cb-vpn'
```

Every `vps.sh` call needs the Bash tool's `dangerouslyDisableSandbox: true` — port
22 is filtered by the sandbox and the call otherwise hangs to timeout.

`enable` is the half that survives a reboot; `start` alone does not, and the
omission stays invisible until the box next restarts. Both are required.

### Reboot survival

The unit is `enable`d, so systemd starts it at boot. The firewall rules are **not**
stored with `iptables-persistent` — they are re-applied by the unit's
`ExecStartPre=/srv/cb-vpn/firewall.sh up` on every start. That was chosen over a
saved rule file for two reasons: this host is kept deliberately bare of packages,
and a saved ruleset goes stale the moment `firewall.sh` changes, whereas
re-applying always matches the deployed script. `PartOf=docker.service` extends the
same guarantee to a Docker restart, which rebuilds Docker's chains and would
otherwise drop the `DOCKER-USER` jump silently.

Verified by tearing the chain down and restarting the unit — it comes back.

### Rotating the token

```bash
~/.claude/skills/deploy/scripts/vps.sh run '
  PASS=$(head -c 24 /dev/urandom | base64 | tr -d "/+=" | head -c 32)
  printf "CB_VPN_USER=cb\nCB_VPN_PASS=%s\n" "$PASS" > /srv/cb-vpn/.env
  chmod 600 /srv/cb-vpn/.env
  systemctl restart cb-vpn'
```

Then update the client. The rendered `tinyproxy.conf` holding the password lives on
a tmpfs inside the container, so a restart is all that is needed to retire the old
one — there is no copy left on disk.

### Checking it

```bash
systemctl is-enabled cb-vpn && systemctl is-active cb-vpn
docker ps --filter name=cb-vpn
/srv/cb-vpn/firewall.sh show      # the egress chain and both jumps
docker logs cb-vpn                # should be near-empty; see below
```

## Security posture

Read this before assuming the proxy protects something it does not.

### What it does protect

**The user's address.** Origin servers see `162.35.172.112`, not the user's IP.

**Against being an open proxy.** Two independent gates, both required:

1. *Network.* The container port is published as `100.91.16.6:8888:8888` — the
   VPS's tailnet address. There is no route to it from the public internet;
   `curl --proxy http://162.35.172.112:8888` fails to connect. This is the boundary
   that matters, and it is one line in `docker-compose.yml`. Changing it to
   `0.0.0.0` or to a bare `8888:8888` publishes an open proxy to the internet.
2. *Token.* tinyproxy `BasicAuth` from `CB_VPN_USER`/`CB_VPN_PASS`. Missing
   credentials return `407`. The entrypoint refuses to start at all if the env vars
   are unset, rather than quietly running without auth.

**Against SSRF into the private side of the server.** Two layers, because either
alone is bypassable:

- `filter.deny` (tinyproxy `Filter`, `FilterType ere`, `FilterDefaultDeny Off`)
  matches the destination host in the request. It covers 127/8, 10/8, 172.16/12,
  192.168/16, 169.254/16 (including `169.254.169.254`), 100.64/10, `::1`, fc00::/7,
  fe80::/10, `localhost`, cloud-metadata hostnames, and the encodings that hide an
  address from a naive string check — integer literals (`http://2130706433/`) and
  leading-zero octal. Blocked requests get `403 Filtered`.
  **On its own this is bypassed by one DNS record**, since it never sees where a
  name resolves.
- `firewall.sh` installs a `CB-VPN-EGRESS` iptables chain matching the *resolved*
  destination, jumped to from `DOCKER-USER` (forwarded traffic) and `INPUT`
  (traffic to addresses the host itself owns — that path never reaches
  `DOCKER-USER`, so without the `INPUT` jump the container could still reach the
  VPS's own tailnet address). This is the layer that actually holds. A hostname
  resolving to `10.0.0.1` gets past the filter and dies here, surfacing as
  `500 Unable to connect`.

**Against port scanning.** `ConnectPort 443` / `ConnectPort 80` only. `CONNECT` to
anything else returns `403 Access violation`, so the proxy cannot be turned into a
generic TCP tunnel by whatever is running in a page.

**Against leaving a browsing log on the VPS.** `LogLevel Warning`, so tinyproxy's
per-request `CONNECT`/`Request` lines — which are the user's browsing history,
including private-mode browsing — are never emitted. No `LogFile`, so the little
that is written goes to the container's stdout ring buffer (json-file, 1 MB × 3)
rather than a file that grows unwatched. After a session of browsing, `docker logs
cb-vpn` is empty.

*The exception, stated plainly:* connection **failures** are logged at ERROR and
include the host that failed — `opensock: Could not establish a connection to
<host>:80`. So blocked and unreachable destinations do leave a trace; successfully
visited ones do not. Silencing that too would mean losing the ability to diagnose
the proxy at all, which seemed the worse trade.

**Container blast radius.** Read-only root filesystem, `cap_drop: ALL` with only
`SETUID`/`SETGID`/`CHOWN`/`DAC_OVERRIDE` added back for tinyproxy's own privilege
drop, and `no-new-privileges`. The base image is pinned by digest, and tinyproxy
comes from Debian rather than a third-party image so its security updates come from
a team already trusted for the rest of the userland.

**DNS.** The container is given `1.1.1.1`/`9.9.9.9` explicitly. The host's
`/etc/resolv.conf` points at Tailscale MagicDNS (`100.100.100.100`), which the
egress filter blocks as part of 100.64/10 — inheriting it would leave the proxy
with no DNS at all. Independently, the user's browsing lookups have no business
going through the tailnet resolver.

### What it does NOT protect

**It is not a VPN.** It proxies HTTP and HTTPS-over-CONNECT, and nothing else. No
UDP, no QUIC, no WebRTC, no DNS from the browser process itself. Anything in the
client that bypasses the proxy setting leaks the real IP, and this backend cannot
detect or prevent that — that is the browser's job.

**No traffic-analysis resistance.** One user, one exit IP, dedicated to them. It
defeats naive IP logging; it does not make the user anonymous, and anyone who can
correlate the tailnet side with the exit side sees straight through it.

**TLS is not inspected — and that is on purpose.** `CONNECT` is a blind tunnel, so
the proxy cannot see, filter, or log HTTPS URLs. The filter rules apply to the
*host* in the CONNECT line only.

**Trust in the VPS is total.** Plaintext HTTP through this proxy is readable on the
box, and whoever holds root there could start logging at any time. Tailnet
membership plus a token is the whole authorization model; anyone with both is the
user, as far as this proxy is concerned.

**The tailnet is the perimeter.** Any device on the tailnet that also has the token
can use the proxy. Revocation means rotating the token or removing the device from
the tailnet — there is no per-client identity here.
