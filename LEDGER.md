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
- Chunk 3 **done**, committed as `21ae95a`. **617 tests pass** (576 + 41),
  `py_compile` clean — verified by me, not only reported. Every HIGH and MEDIUM
  finding in `AUDIT.md` is addressed. Deliberately not fixed: **L1** (`cb:tabs`
  renders private URLs) — that deck is *how you switch to a private tab*, it is
  in-memory only, and the audit classes it as shoulder-surfing. **M5** needed no
  action (clean bill of health).
  Design notes worth keeping: privacy now only travels downhill
  (`storage.child_is_private` can add it, never remove it); a tab id the playbook
  recorder cannot resolve answers *private*, i.e. it fails closed; the new
  settings use an `_only_on_words` truth function so a typo keeps the private
  answer rather than dropping the guard.
- Chunk 4 dispatched — VPN client. Was blocked on chunk 3: both own `browser.py`,
  `api.py`, `settings.py`, `pages.py`, so they could never run together.

## Chunk 6 — perceived speed and feel (user asked for this mid-session)

Hardware re-confirmed, because it decides every trade below: **Celeron N3060, 2
cores @1.6GHz, 3.8GB RAM, Braswell graphics, and no swap configured at all**
(`swapon --show` is empty). `perf.py`'s conservative defaults are therefore
*correct* and must not be loosened — smooth scrolling and WebGL stay off. The
wins available are the ones that cost no CPU.

Findings, all measured or probed, not guessed:

1. **Every navigation flashes white.** `set_background_color` is applied to the
   Claude panel (browser.py:670) and to no page view, so WebKit paints its
   default white between commit and first paint. On the default phosphor theme
   that is the single most jarring thing about using the browser. Free to fix.
2. **Autoplay is allowed.** `media-playback-requires-user-gesture` defaults to
   **False** and `perf.tune_view` never sets it. An autoplaying video on a news
   page can hold both cores on this machine. Biggest *real* CPU win left.
3. **No hover prefetch.** DNS is warmed while typing in the omnibox
   (`urls.HostWarmer`) but there is no `mouse-target-changed` handler, so a link
   the pointer is resting on pays full DNS latency on click. Must inherit the
   privacy gates: not in a private tab, not while VPN Mode is on (local
   resolution would defeat the tunnel).
4. **`Tab.scroll` is a dead field** — set to `0` at browser.py:277 and never read
   or written again. So a tab discarded under memory pressure reloads to the top
   and loses your reading position. Capturing and restoring it makes discards
   nearly invisible, which is exactly the "feel" complaint.
5. **Startup is ~7.8s of imports** (`python3 -X importtime`). Most is
   unavoidable — the WebKit2 typelib alone is ~2.6s, Gtk+Gdk ~1.4s. But
   `claudebrowser.agent` costs 0.83s cumulative and pulls in `ai` (0.81s) and
   `http.client` (0.36s), none of which is needed until the user actually asks
   for something. **Note the self-inflicted part:** chunk 3 put
   `private_ai_enabled()` / `PRIVATE_REFUSAL` in `ai.py`, and `api_tabs` calls
   it — so an ordinary tab listing now drags `http.client` and `ssl` in. Move
   those two names to a GTK-free home and `agent`/`ai` become lazy imports.
6. **Back/forward swipe gestures** (`enable-back-forward-navigation-gestures`)
   default False and are free to enable.

Rejected after probing: `enable-mediasource` and `enable-webaudio` stay on —
disabling them breaks streaming video and site audio, which is a correctness
regression sold as a speed win.

Chunk 6 owns `perf.py`, `extract.py`, `browser.py`, `settings.py`, `style.py`,
`tests/**` — so it **overlaps chunk 4 almost completely and must follow it.**

## Where this stopped

Stopped deliberately at the user's request after chunks 1-5. **Chunk 6 (feel and
perceived speed) is NOT started** — the agent was halted before it wrote anything,
so the tree is clean and the findings above are a complete, measured brief for
whoever picks it up. Nothing is half-applied.

Delivered: private-mode hardening (`21ae95a`), VPN Mode (`b36f17d`), docs. 680
tests pass; `py_compile` clean.

## Still to do

- Chunk 5 — docs. `CLAUDE.md` says "all 19 of them" settings; it is 21 after
  chunk 3 and will be 23 after chunk 4, and the new knobs are undocumented there.
  The "opt-in VPS backend" entry under *Architectures already rejected* must be
  rewritten to record that it was reopened deliberately, on the user's explicit
  instruction, and narrowed to a transport proxy.
- Hand-verification on the real display (`:0`), which no unit test can reach:
  1. `storage.apply_policy` really flipping `itp_enabled` and the accept policy on
     a live ephemeral manager.
  2. `download-started` firing for a download from an ephemeral view, and the
     cancel actually landing.
  3. Popup inheritance end to end — an OAuth-style `window.open` from a private
     tab arriving as a private tab, with the badge.
  4. `cb:private` and `cb:vpn` rendering in all three themes.
  5. `tab.view.destroy()` in `close_tab` warning-free over a close/reopen cycle.
  6. VPN Mode on: exit IP shown matches `162.35.172.112`, and a private tab opened
     while VPN Mode is on is *also* proxied (the two features must compose).
