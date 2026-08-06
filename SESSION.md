# Session ledger — declutter rules, Watch Later player, cb:search

Written so this session can be recovered after a crash or a compaction. Facts
and decisions only; no transcripts. Update it at every chunk boundary.

Started 2026-08-05. Branch `master`.

## What the user asked for

1. **YouTube is unusable — too cluttered and loud.** The two things they
   actually do there:
   - scroll the **home feed** for suggestions to add to **Watch Later**;
   - play the **Watch Later** playlist in the background while working.
2. **Per-site declutter rules** for common sites, so the browser shows a
   simplified version. Sites named: `youtube.com`, `google.com`, Gemini,
   `claude.ai`, `chatgpt.com`, Claude's API + docs site, Cloudflare.
3. **A built-in Watch Later playlist player** — optimised and streamlined, for
   background listening while working.
4. **`cb:search` as the default search engine**, replacing DuckDuckGo. A short
   Claude answer streams at the top with an expand for a verbose version,
   followed by real results from a search API. **The user is signing up for a
   search API and will send the key** — that half is blocked until it arrives.

## Decisions taken

- **Declutter is auto-on for any matched site**, with a per-page toggle to see
  the original and a `CB_SITERULES` setting to turn the feature off. (User
  chose this over off-until-toggled.)
- **The Claude answer on `cb:search` fires on every search, streamed**, with an
  expand for the longer version. (User chose this over on-demand; it costs an
  API call per search and that tradeoff was stated and accepted.)
- **The VPS will *not* fetch and simplify pages.** Proposed by the user,
  declined with reasons, and the user did not press it:
  - the two pages that matter (home feed, Watch Later) are *signed-in* pages;
    the VPS has no Google session and would fetch a logged-out shell, and
    giving it one means shipping the user's Google cookies to a server —
    exactly what `AUDIT.md` and the local-only posture exist to prevent;
  - YouTube is a Polymer app, so a server-side GET returns an app skeleton, not
    a feed. Real content would need a headless browser on the VPS, which is the
    Playwright container already recorded as rejected in `CLAUDE.md`;
  - a VPS round trip is slower and heavier than the local alternative, which
    costs one style recalculation.
  VPN Mode narrowed the VPS to a *transport* proxy on purpose. Fetching and
  rewriting page content there is the rejected "second brain" architecture, and
  this use case supplied no new fact to reopen it.
- **Work sequentially.** The user asked for one thing at a time — no fan-out of
  subagents, no batches of speculative commands.
- **Close any browser tab opened for inspection.**

## State of the code at session start

- `claudebrowser/siterules.py` — **untracked, 225 lines, complete but wired to
  nothing.** Written in an earlier session. Per-site declutter: injects one CSS
  sheet per site (`STYLE_ID = "cb-siterules-style"`), hides rather than removes
  nodes, one sheet per *site* not per page so it survives SPA navigation.
  Public surface: `host_of(url)`, `for_url(url)`, `toggle(url)`,
  `apply_css(url)`, `RULES`, `Rule`. One rule today: `youtube`.
  Deliberate: keeps the masthead search and every menu, so "Save to Watch
  Later" still works; hides hover previews for CPU reasons.
- Nothing references `siterules` anywhere else (`grep` confirms).
- No `tests/test_siterules.py`.
- `urls.SEARCH` still defaults to `https://duckduckgo.com/?q=%s` (`urls.py:11`).

## Integration points found (verified by reading, not guessed)

- `api.py` — `Op(...)` entries in `OPS`; the `reader` op at `api.py:218` is the
  closest template (a *display* change that reports state, not content).
- `browser.py`:
  - menu table at the top, `("This page", ...)` group holds `Reader mode`
    (`browser.py:76`); `INTERNAL` tuple lists the `cb:` pages.
  - action dispatch dict at `browser.py:2372` (`"reader": self.toggle_reader`).
  - `_on_load` at `browser.py:1741` — `STARTED` / `FINISHED` only today.
  - `api_reader` at `browser.py:3513`, `toggle_reader` at `browser.py:3533`.
  - `api_eval` at `browser.py:3467` (restores a discarded tab first).
- `settings.py` — the `SETTINGS` table; every key also hand-listed in
  `tests/test_settings.EVERY_KEY`, on purpose. Adding a knob means both.
- `perf.light_enabled` (`perf.py:128`) is the pattern for a spelling-tolerant
  boolean read fresh from `envfile.setting` on each call.

## Plan

Chunk A — declutter layer
1. Extend `siterules.py`: Watch Later / playlist rules, plus rules for
   google.com, Gemini, claude.ai, chatgpt.com, Claude docs+API, Cloudflare.
   Selectors read off live pages where possible, never remembered.
2. Add `siterules.enabled()` reading `CB_SITERULES`; add the key to
   `settings.py` and to `tests/test_settings.EVERY_KEY`.
3. Wire: a `simplify` op in `api.py`; auto-apply in `browser.py`'s load
   handling; a menu entry + shortcut next to Reader mode.
4. `tests/test_siterules.py`.

Chunk B — Watch Later background player
5. A `cb:` page that plays the Watch Later queue with the page chrome gone.
6. Never discard a tab that is playing media (existing task #5).

Chunk C — `cb:search` (blocked on the API key for the results half)
7. Provider-agnostic search module + `cb:search` page; Claude answer streamed
   at the top; `urls.SEARCH` default flipped once it works.

## Measured on this machine (2026-08-05, load ~11-13 on 2 cores)

Opened `https://www.youtube.com/playlist?list=WL` in a real tab and probed it.

- `cbctl open` **timed out at 150s**; the tab did finish afterwards (title
  "Watch later - YouTube", `loading: false`).
- **Every `eval` against that tab timed out at 45s** — including a trivial
  `return location.href`. The same eval against `cb:home` in the same browser
  answered instantly, so the control plane and the GTK main loop were healthy:
  it is *YouTube's own web process* that could not service a callback.
- `cbctl machine` at the time: `level: critical`, `load 13.3 on 2 cores`,
  689MB available, swap 841/1545MB. Top consumers were this agent's own
  tooling plus `WebKitWebProcess` at ~25%.

**What this changes.** A CSS declutter sheet hides elements *after* the Polymer
app has been downloaded, parsed, hydrated and built them. It saves layout and
paint — real, but it does not save the JavaScript, and the JavaScript is what
makes this page unusable here. So:

- the declutter sheet is still worth having for the **home feed**, where the
  user's task (scroll suggestions, save to Watch Later) genuinely requires
  youtube.com's own app and session;
- but it is **not** the fix for background listening. The Watch Later player
  has to avoid loading youtube.com's application at all, which promotes it from
  "nice extra" to the primary answer for use case 2.

**Selectors for the playlist page could not be read live** because of the
above. They must be verified on a quiet machine before shipping; anything
written from memory in the meantime is marked as unverified in the source.

## How the player gets the Watch Later contents

Verified by experiment, 2026-08-05:

- A plain `urllib` GET of `https://www.youtube.com/playlist?list=<id>` returns
  ~1MB of HTML containing `var ytInitialData = {...};</script>`, which parses as
  JSON (~295KB). **No rendering, no hydration, no Polymer** — this is the cheap
  path, and it sidesteps the measured problem entirely.
- **`playlistVideoRenderer` no longer exists.** Current YouTube puts playlist
  items in **`lockupViewModel`** (19 on the test playlist), alongside
  `lockupMetadataViewModel`, `thumbnailViewModel` and `contentMetadataViewModel`.
  A parser written from memory against `playlistVideoRenderer` silently returns
  zero items — it did exactly that on the first attempt here.
- Cookies: **do not read `cookies.sqlite` from a script.** That was attempted and
  correctly blocked as credential access. The right implementation asks WebKit's
  own `CookieManager` from inside the browser process, on the main loop, which is
  also the architecturally correct answer — the player is a browser feature.

### On logging into Google / using the official API

The user offered to sign in so the browser could use Google's APIs. Worth
knowing before anyone builds that: **the YouTube Data API v3 cannot read the
Watch Later playlist.** Google removed programmatic `WL` (and `HL`) access in
2016; `playlistItems.list` with `id=WL` does not return the user's queue. So
OAuth, a Cloud project and an API key would buy nothing for *this* use case.

Two ways forward, and the first needs nothing from the user:

1. **Session fetch (recommended).** The browser already holds a signed-in
   YouTube session. Fetch the playlist HTML with those cookies in-process and
   parse `ytInitialData`. No OAuth, no API key, no Google Cloud project.
   Unofficial, so the parse can break when YouTube reshapes its data — which is
   why it belongs in a GTK-free module with tests over a saved fixture.
2. **Use a normal playlist instead of `WL`.** If the user moves to a regular
   playlist (e.g. one called "Queue"), the official API *can* read it, and that
   path is stable and supported. This is a change to the user's habit, not just
   to the code, so it is theirs to choose.

## Progress

- [x] Ledger written.
- [x] Browser started; Watch Later opened, probed, and the tab **closed again**.
- [x] Finding above recorded.
- [x] **`claudebrowser/watchlater.py` written** — GTK-free queue parser.
      `parse(html)` -> `{ok, items, truncated}`; `Item` carries `video_id`,
      `title`, `channel`, `duration`, `seconds`. Reads `lockupViewModel` by
      *walking for the leaf* rather than indexing the eleven-level path, since
      every name above it has been renamed before. Fails loudly: an unreadable
      or reshaped page returns `ok: False` with a reason, never an empty queue.
      Takes a `Cookie` header as a string — it never opens `cookies.sqlite`.
- [x] **`tests/test_watchlater.py`** — 19 tests, all passing, no display, no
      network. Fixture `tests/fixtures/playlist_page.json` is three real item
      lockups taken verbatim (noise pruned), deliberately not hand-written.
- [x] **Verified end to end against the full live page**: 19/19 videos parsed,
      every title, channel and duration correct.
- [ ] Not yet committed. Browser-side fetch and the player page still to come.

- [x] **`cb:queue` works end to end against the real signed-in queue.**
      100 items with titles, channels and durations; Play all starts the
      embedded player; no console errors. Verified in the running browser, and
      the inspection tab was closed afterwards.
- [x] Reachable from the icon rail, the menu, and **Ctrl+Alt+W**.
- [x] `CB_QUEUE_LIST` setting (default `WL`) — any playlist id works, so
      moving off Watch Later is a settings change, not a code change.
- [x] 726 tests pass; `py_compile` clean.
- [ ] Not yet committed.

### Corrections to earlier entries in this file

- **"`playlistVideoRenderer` no longer exists" was wrong.** Both shapes are
  live, and *which one you get depends on the page*: a public playlist page
  serves `lockupViewModel`, the signed-in Watch Later page serves
  `playlistVideoRenderer` (100 per page). The first parser failed against a
  public playlist; the "corrected" one then failed against the real Watch Later
  page for the mirror-image reason. Both are parsed now. The rule worth keeping
  is *never generalise YouTube's data model from one page*.
- **There is no signed-out marker on the Watch Later page.** A signed-out fetch
  is HTTP 200, empty, and carries nothing to detect — `responseContext` holds
  only `webResponseContextExtensionData`. So sign-in is judged locally, from
  whether the jar had a session cookie (`watchlater.signed_in`), and only ever
  used to explain an *empty* result.

### Bugs found and fixed while wiring this up

1. **`self._queue` was already the page-load admission FIFO** in `_admit`, and
   the new queue state clobbered it — i.e. it disabled the one mechanism
   `CLAUDE.md` credits with preventing a twenty-minute machine freeze. Renamed
   to `_watchlater`, with a comment at the definition so it cannot recur.
2. **`pages._js` is for HTML *attributes*, not `<script>` blocks.** An entity
   is decoded in an attribute and not in a script, so a queued video called
   "Rick & Morty" put a literal `&amp;` into the source and killed the page's
   whole script with `SyntaxError: Unexpected token '&'`. Added
   `pages._js_block`, with tests.
3. `cb:` pages rendered an exception with no traceback, which made (1) far
   harder to find than it should have been. `_serve_internal` now includes one.
4. Literal `%` in a page body that gets `%`-formatted. Caught by rendering the
   page in-process — which is also how *not* re-testing after editing `NAV`
   let bug (1) reach the browser.

### Still unverified

- That audio genuinely keeps playing when the tab is in the background. The
  embed loads and autoplay is set, but this needs a human ear.
- The playlist-page **declutter** selectors (task #2/#9) — still not read off a
  live page, because the machine could not run a script against YouTube.

### Reported by the user, not yet investigated

- **Password autofill does not always trigger.** Reproducing page:
  `https://api-dashboard.search.brave.com/login`. Suspects, in order: the form
  is rendered client-side *after* the load-finished hook that calls
  `_pw_autofill`; the fields sit inside a component or shadow root the injected
  selector does not reach; or the saved entry's origin
  (`search.brave.com`?) does not match `api-dashboard.search.brave.com`. Any
  fix must keep the rule that the origin is read natively from the focused
  view and never supplied by the page. Task #7.
- Brave Search appears to be where the `cb:search` API key is coming from,
  which makes that dashboard worth getting right.

### Next steps, in order

1. Browser side of the fetch: ask WebKit's `CookieManager` for youtube.com
   cookies on the main loop, build the header with
   `watchlater.cookie_header`, do the GET on a worker thread (never the main
   loop), hand the parsed queue back.
2. The `cb:` player page: the queue as a list, one embedded player, advance on
   ended. Chrome-free and cheap — the whole point is not loading YouTube's app.
3. Task #5, never discard a tab that is playing media — the player is exactly
   the tab `resources.py` would otherwise reclaim while it is playing.
4. Back to the declutter sheet: wiring (task #2), then the other sites (task
   #9). Playlist-page selectors still need verifying on a quiet machine.
5. `cb:search` — still blocked on the user's search API key for the results
   half; the Claude-answer half is not blocked.

## Verification command

    CB_AUTOSTART=0 python3 -m unittest discover -s tests

Syntax gate for the display-only modules:

    python3 -m py_compile claudebrowser/*.py
