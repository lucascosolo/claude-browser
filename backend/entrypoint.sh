#!/bin/sh
# Renders tinyproxy.conf from the template so the BasicAuth credential can come
# from the environment (and therefore from a mode-600 .env on the host) instead
# of living in the repo.
set -eu

# Template ships in the image; the render target is a tmpfs, so the file holding
# the password exists only in RAM.
TMPL=/opt/cb-vpn/tinyproxy.conf.tmpl
CONF=/etc/tinyproxy/tinyproxy.conf

cp "$TMPL" "$CONF"

if [ -n "${CB_VPN_USER:-}" ] && [ -n "${CB_VPN_PASS:-}" ]; then
    # Appended rather than substituted into a placeholder: tinyproxy accepts
    # repeated BasicAuth lines, and appending keeps the secret off any line we
    # might otherwise echo while debugging the template.
    printf 'BasicAuth %s %s\n' "$CB_VPN_USER" "$CB_VPN_PASS" >> "$CONF"
else
    # Refuse rather than silently running open. On a tailnet-only bind an open
    # proxy is not catastrophic, but "authorization is tailnet AND token" is the
    # stated posture and a half-applied posture is worse than a loud failure.
    echo "cb-vpn: CB_VPN_USER/CB_VPN_PASS unset - refusing to start an unauthenticated proxy" >&2
    exit 1
fi

# Readable only by the account tinyproxy drops to (it re-reads on SIGHUP after
# dropping privileges). chmod before chown, not after: with CAP_FOWNER dropped,
# chmod is only permitted while we still own the file.
chmod 0600 "$CONF"
chown tinyproxy:tinyproxy "$CONF"

exec "$@"
