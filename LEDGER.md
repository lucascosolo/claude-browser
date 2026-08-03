# Ledger — private-mode hardening + VPN Mode

Branch `worktree-private-vpn`. Append-only. Written before compaction, read after it.

## The ask

1. Harden private mode so it is *truly* hardened against all types of data saving.
2. Add a "VPN Mode" routing all browser traffic through a VPS backend.
3. Deploy that backend if it does not exist.

## Standing facts (verified this session, do not re-derive)

- Browser: Python 3 + GTK3 + WebKit2GTK **2.52.5**, API version 4.1, stdlib only.
- `WebKit2.WebContext.set_network_proxy_settings` — **exists**.
- `WebKit2.WebsiteDataManager.set_network_proxy_settings` — **exists**. This is the
  one that matters: a private tab has its own ephemeral `WebsiteDataManager`, so a
  context-level proxy alone would let private tabs bypass VPN Mode.
- `WebKit2.NetworkProxyMode` = `CUSTOM` / `DEFAULT` / `NO_PROXY`.
- VPS: Debian 13, Docker CE, cloudflared tunnel + tailscaled running.
  Public listeners: **22 and 8420 only**. Existing containers: `ai-lab-dashboard`
  (127.0.0.1:4173), `wandering-dungeon` (0.0.0.0:8420). `/srv` holds
  `ai-lab-dashboard`, `cloudflared`, `wandering-dungeon`.
- **Laptop and VPS share a tailnet.** VPS tailnet IP `100.91.16.6`, laptop
  `100.84.169.57`. This is why the proxy binds tailnet-only instead of being an
  internet-facing open proxy.
- Reaching the VPS over SSH needs `dangerouslyDisableSandbox: true` (port 22 is
  filtered by the Bash sandbox); use `~/.claude/skills/deploy/scripts/vps.sh`.

## Conflict with CLAUDE.md, resolved

`CLAUDE.md` → "Architectures already rejected" lists **"An opt-in VPS backend"**,
refused as contradicting the local-only posture. The user has now explicitly asked
for it, which overrides the note. Scope is narrower than what was rejected: a
transport proxy only — no Redis, no Postgres/pgvector, no Playwright container, no
device pairing, no JWTs, and no browsing data leaving the laptop. Update that
section rather than leaving the doc contradicting the code.

## Chunks

| # | Chunk | Files owned | State |
|---|-------|-------------|-------|
| 0 | Recon + plan | LEDGER.md | done |
| 1 | Private-mode leak audit (read-only) | — | dispatched |
| 2 | VPS backend: proxy + deploy | `backend/**`, `.claude/deploy.json` | |
| 3 | Private-mode hardening | browser.py, storage.py, playbooks.py, control.py, urls.py, settings.py, pages.py, tests | |
| 4 | VPN Mode client | vpn.py (new), browser.py, api.py, ai.py, settings.py, pages.py, tests | |
| 5 | Docs (CLAUDE.md, README.md) | CLAUDE.md, README.md | |

**Chunks 3 and 4 both own browser.py / settings.py / pages.py → they must run
sequentially, never concurrently.** Chunk 2 is disjoint and may overlap with either.

## Verification command

```bash
CB_AUTOSTART=0 python3 -m unittest discover -s tests    # 576 tests at branch point
python3 -m py_compile claudebrowser/*.py                # the only gate for the 4
                                                        # display-only modules
```

## Codex second opinion — where it disagreed, and what I did

Asked because this is structural (new subsystem) and guards a safety claim.

1. **It rejected my hand-rolled stdlib asyncio proxy. I changed course.** Its argument
   is the one I did not have: the proxy's clients are *the pages the browser loads*,
   so a bespoke HTTP parser is attack surface reachable by any hostile page — request
   smuggling, ambiguous Host/absolute-form handling, chunked/100-Continue edge cases,
   DNS rebinding. Tailnet-only exposure stops scanners, not parser bugs. Backend is
   **tinyproxy** now.
2. **My VPS-local health endpoint was wrong and is dropped.** It proves the proxy
   accepted a connection; it cannot prove the public exit address, because the VPS's
   default route, Docker NAT or provider firewall can be broken independently. The
   health check is now *the browser fetching an external HTTPS IP echo through the
   proxy* and comparing it to the expected exit IP.
3. **Tailnet membership alone is not enough authorization.** The realistic threat is
   not internet open-relay, it is an authorized-but-compromised device using the VPS
   as an SSRF relay into loopback, Docker bridges, `169.254.169.254`, or other tailnet
   nodes. Adopted: a per-install token *and* destination egress filtering, in both
   tinyproxy's filter and a `DOCKER-USER` iptables rule.
4. **"VPN Mode" is not an honest name for an HTTP proxy — partially accepted.** The
   user asked for that label, so it stays as the label, but every surface states
   plainly what it does not cover. It is a browser proxy with a VPS exit; it does not
   carry other applications, WebRTC/UDP media, or system traffic.
5. **True fail-closed needs an OS-level egress kill switch**, not a Python boolean —
   a compromised page or WebKit subprocess can open a direct socket regardless. In-app
   we do the strongest best-effort (never fall back to DEFAULT/NO_PROXY, verify via
   external echo before reporting "on", block navigation when the probe fails, disable
   WebRTC and DNS prefetch). The nftables/cgroup kill switch ships as an **opt-in
   script**, and the wording says "best-effort" wherever the hard guarantee is absent.

Where it was wrong: it warned a data manager "must be supplied when constructing the
WebContext" and cannot be retrofitted. In this build `WebKit2.WebView.get_website_data_manager()`
**does** exist (probed), so a private view's ephemeral manager is reachable and
`set_network_proxy_settings` can be applied to it directly. Its underlying point still
holds — the proxy must be set before the first navigation in that view.

### API facts probed on this build (WebKit2 4.1 / 2.52.5)

All present: `WebView.get_website_data_manager`, `WebsiteDataManager.set_network_proxy_settings`,
`WebContext.set_network_proxy_settings`, `WebContext.prefetch_dns`,
`WebContext.set_favicon_database_directory`, and `WebKit2.Settings` properties
`enable-webrtc`, `enable-media-stream`, `enable-dns-prefetching`,
`enable-hyperlink-auditing`, `enable-page-cache`. `NetworkProxySettings.new()` accepts
a URI with embedded credentials.

## Log

- Recon complete; worktree `private-vpn` created off `74f805f`.
- Audit agent dispatched (read-only inventory of private-mode leaks).
- Codex consulted on backend shape (hand-rolled stdlib proxy vs tinyproxy/3proxy,
  tailnet-only authz, fail-closed, honesty of the name "VPN Mode").
- Baseline verified: **576 tests pass** at branch point, `python3 -m py_compile
  claudebrowser/*.py` clean. A real display (`:0`) is available, so both features
  can be checked in the running app and not only in tests.
- Chunk 1 **done** — leak audit landed, written up in `AUDIT.md`. Verdict: the
  WebKit storage layer is sound (`is_ephemeral=True` really does keep cookies,
  localStorage, IndexedDB, service workers, HSTS and ITP off disk — probed), but
  the *policy* layer and the whole Python layer above WebKit are unaware private
  tabs exist. 7 high, 5 medium.
- Chunk 2 dispatched — tinyproxy backend, tailnet-bound, SSRF-filtered.
- Chunk 3 dispatched — private hardening. Decisions fixed in the brief rather than
  left open: AI paths **refuse by default** on a private tab (`CB_PRIVATE_AI`,
  default off, to re-enable); downloads from a private tab are cancelled
  (`CB_PRIVATE_DOWNLOADS`, default off); `api_tabs` hides private tabs from the
  agent entirely rather than merely labelling them.
- Chunk 2 **done** — `cb-vpn` deployed to `/srv/cb-vpn`, tinyproxy 1.11.2 on a
  digest-pinned trixie-slim base, under systemd unit `cb-vpn.service`
  (enabled + active). Client proxy URL: `http://cb:<pass>@100.91.16.6:8888`;
  the password lives only in `/srv/cb-vpn/.env` (mode 600) and is **not** in the
  repo. Verified independently by me, not just by the agent:
  direct exit IP `47.214.55.67` → proxied `162.35.172.112`; metadata and loopback
  SSRF both 403; unauthenticated refused; public `162.35.172.112:8888` unreachable.
  Two independent egress layers — tinyproxy's host filter, and a `CB-VPN-EGRESS`
  iptables chain that catches what the name filter cannot (a hostname resolving
  into RFC1918, e.g. `10-0-0-1.nip.io`, is stopped only by the second layer).
  The chain is jumped from `INPUT` as well as `DOCKER-USER`, because traffic to an
  IP the host itself owns is never forwarded and so never reaches `DOCKER-USER`.
  Caveat to carry into the docs: connection *failures* are logged with the
  hostname; successful browsing is not logged at all.
- Chunk 4 (VPN client) is **blocked** on chunk 3 — both own `browser.py`,
  `api.py`, `settings.py`, `pages.py`.
