# Private-tab leak audit

Findings from a read-only audit at branch point `74f805f`. Line numbers are from
that commit. Kept in the repo because the remediation is spread over several
files and the reasoning behind each guard is not obvious from the guard itself.

## Headline

The boundary is real but partial. `store.recordable(url)` + `tab.private` gate
exactly two sinks — history and the pagetext cache, both written from
`Browser._record` (browser.py:1501-1548) — plus discard/summary (browser.py:1761)
and password *writes* (browser.py:2180). Everything else is unaware private tabs
exist: `grep -rn "private"` returns **zero hits** in `ai.py`, `agent.py`,
`playbooks.py`, `control.py`, `tabnames.py`, `personas.py`, `scrub.py`,
`urls.py`, `storage.py`, and no test covers privacy on any of them.

## HIGH

**H1 — a popup from a private tab opens a NON-private tab.** browser.py:1453-1458.
`_on_popup` calls `self.new_tab(uri, background=True)`; `private` defaults to
False. The child gets the persistent cookie jar and disk cache, **and is written
to history and pagetext**. Any OAuth popup or `window.open` silently
de-privatises, with no `private` badge to show it.

**H2 — playbook recording writes private-tab operations to disk.** control.py:173
→ playbooks.py:322-334, landing in `~/.local/share/claude-browser/playbooks.json`.
The recorder is *structurally* blind: `tab` is stripped at playbooks.py:267-269
and is never in `op.params` (api.py:87), and clients usually omit it because the
registry default is "the focused tab" (api.py:24-25,119-121). So no privacy
decision is even possible at that funnel. What lands on disk: every navigate URL
verbatim — including magic-link and `?access_token=` URLs, since
`is_secret_step` (playbooks.py:103-114) inspects only `fill` selectors and `eval`
source, never a URL and never a value — plus click selectors, non-secret fill
values, `find`/`recall` queries, and screenshot paths.

**H3 — `playbooks.json` is written world-readable.** playbooks.py:329-334 does
`tmp.write_text(...)` then `os.replace`, with no `chmod` — unlike every sibling
atomic write in envfile.py:158,213,332, which all do `tmp.chmod(0o600)`. Default
umask gives `0644`, and a crash between write and rename leaves a `.tmp` at the
same mode.

**H4 — Ask and TL;DR send a private page's URL, title and text to Anthropic.**
browser.py:2557 and browser.py:2446 both go through `_with_page`
(browser.py:2435-2441), which defaults to the current tab with no private check.
The URL is deliberately not redacted (ai.py:621-626). `scrub.py` is a PII regex
pass, not a privacy gate, and `CB_SCRUB=0` disables it entirely.

**H5 — Research ships every open tab, private ones included.** browser.py:2474
takes `list(self.tabs)` unfiltered; each is read at browser.py:2501 and emitted
with a raw URL at ai.py:690-692.

**H6 — the Ctrl+G agent drives private tabs and labels them for the model.**
agent.py:207,213-227: `navigate`, `read_page`, `page_links` act on the current
tab. `list_tabs` → `api_tabs` (browser.py:2585) → `Tab.info()`
(browser.py:265-278) returns `url`, `title` **and `"private": True`** for every
tab — one call ships every private tab's URL and title to Anthropic, annotated
with which ones are private. `open_tab` → `api_open` (browser.py:2590) passes no
`private=`, so it de-privatises like H1 *and* the load is recorded.
(Clean: no screenshots are sent; `agent.py`/`ai.py` write nothing to disk.)

**H7 — omnibox typing in a private tab prefetches DNS and reads persistent
history.** There is one window-level omnibox and `_on_omnibox_changed`
(browser.py:986-1001) has no notion of the focused tab.
- browser.py:953-984 → `urls.HostWarmer` → `context.prefetch_dns(host)` on the
  **shared, non-ephemeral** context: a DNS query leaves the machine for a host
  you have only typed.
- browser.py:995-1001 renders persistent history and bookmarks in a private
  session's dropdown.
- browser.py:1003-1060 renders **snippets of previously-read page bodies**.

## MEDIUM

**M1 — private tabs get *weaker* tracking protection than normal ones.** Probed
on this build: the ephemeral manager's `itp_enabled` is `False` while the
persistent one is `True`. `storage.make_context()` sets ITP only on the
persistent manager (storage.py:116-121); the per-view ephemeral manager never
sees it. Cross-site trackers get *more* linkage in a private tab. Confirmed
fixable — `set_itp_enabled` works on an ephemeral manager.

**M2 — `CB_COOKIES` is silently ignored in private tabs.** `storage.attach_cookies`
(storage.py:145-173) only touches the persistent manager. Sweeping the context
policy leaves the ephemeral view at `NO_THIRD_PARTY` regardless, so
`CB_COOKIES=none` — whose stated promise is "nothing is kept" — still accepts
first-party cookies in a private tab. The default case coincides, which is why it
went unnoticed.

**M3 — `api_screenshot` writes a PNG of a private page to a caller-supplied
path.** browser.py:2777-2800, no private check; `@needs_tab` accepts a private
tab id, and the path is also recorded into the playbook (H2).

**M4 — downloads are entirely unhandled.** Nothing connects
`WebKitWebContext::download-started` or `decide-destination`, yet WebKit's own
context menu exposes "download link to disk". With no application handler,
WebKitGTK's default writes into the user's Downloads directory under the
server-suggested filename — so a download from a private tab produces a permanent
file, named by the remote server, with no UI and no private check.

**M5 — `is_ephemeral=True` IS sufficient at the WebKit storage layer.** A clean
bill of health for the layer the code worried about. Probed: the ephemeral view's
manager is a distinct object with `base-data`, `disk-cache`, `local-storage`,
`indexeddb`, `itp`, `hsts`, `service-worker-registrations` and `dom-cache` all
`None`. A non-ephemeral `web_context` does not contaminate an `is_ephemeral=True`
view in 2.52. The comment at browser.py:214-218 is accurate. Favicons are a
non-issue: no favicon database directory is ever set. **The residual risk is
policy (M1, M2) and the Python layer above WebKit, not WebKit storage.**

## LOW

- **L1** `cb:tabs` renders private URLs (pages.py:864,873). In-memory only, `cb:`
  is excluded from history by `store.SKIP_SCHEMES`. Shoulder-surfing only.
- **L2** `close_tab` (browser.py:1460-1472) never calls `view.destroy()`;
  teardown of the ephemeral session is left to GC, so nothing forces a wipe at
  close.
- **L3** Autofill *reads* the keyring in private tabs — deliberate and documented
  (browser.py:2180-2182). `_on_pw_message` returns early so nothing reaches
  `vault.save`. Noted, not a defect.
- **L4** sqlite WAL residue exists but nothing from a private tab ever enters
  either database. Closed.
- **L5** No `print()` anywhere emits a URL, title or page text. Closed.
- **L6** There is no session/crash restore file at all — `_tab_states()`
  (browser.py:1727) is in-memory input to `resources.py` only. Private tabs are
  not "excluded from session restore"; there is no session restore. Closed.
- **L7** `tabnames.py` makes no network call; "AI tab naming" is a misnomer.
  Closed.
- **L8** `personas` contributes no page data. Closed.
