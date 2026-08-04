# Unbuilt ideas that fit this stack

A shortlist, not a brainstorm. Everything here is buildable in what this project
already is — Python 3 + GTK3 + WebKit2GTK, standard library only, no pip, no
node, no build step. Anything that needed a different product (a Tauri/React
front end, a Rust or FastAPI service, an embedding model, an opt-in VPS with
Postgres and Playwright) has been removed rather than parked; the reasoning for
those refusals lives in `CLAUDE.md` under *Architectures already rejected*, so
it does not get re-proposed here every six months.

Ideas that have since been built — reader mode, the page-text cache, `recall`
full-text search, the visible agent cursor, the outbound PII scrubber, playbooks,
Claude personas, discard-path tab summaries, private-mode hardening and VPN
Mode — are documented in `README.md` and `CLAUDE.md` and are no longer
suggestions.

## Perceived speed and feel

Measured on the target machine, not guessed: **Celeron N3060, 2 cores @1.6GHz,
3.8GB RAM, and no swap configured at all** (`swapon --show` is empty). That is
what decides every trade here — `perf.py`'s conservative defaults are *correct*
and must not be loosened to buy smoothness. Smooth scrolling and WebGL stay off.
What is left are the wins that cost no CPU.

- **Every navigation flashes white.** `set_background_color` is applied to the
  Claude panel and to no page view, so WebKit paints its default white between
  commit and first paint. On the phosphor default that is the single most jarring
  thing about using the browser, and it is free to fix.
- **Autoplay is allowed.** `media-playback-requires-user-gesture` defaults to
  `False` and `perf.tune_view` never sets it. On two 1.6GHz cores an autoplaying
  video is not a nuisance, it is the page. This is the biggest real CPU win left.
- **No hover prefetch.** DNS is warmed while typing in the omnibox
  (`urls.HostWarmer`) but there is no `mouse-target-changed` handler, so a link
  the pointer is resting on pays full DNS latency on click. It has to inherit the
  privacy gates: not in a private tab, and not while VPN Mode is on, where a local
  resolution would defeat the tunnel.
- **`Tab.scroll` is a dead field** — set to `0` and never read or written again,
  so a tab discarded under memory pressure reloads to the top and loses your
  reading position. Capturing and restoring it makes discards nearly invisible.
- **Back/forward swipe gestures** (`enable-back-forward-navigation-gestures`)
  default `False` and are free to enable.
- **Startup is ~7.8s of imports** (`python3 -X importtime`), most of it
  unavoidable — the WebKit2 typelib alone is ~2.6s and Gtk+Gdk ~1.4s. But
  `claudebrowser.agent` costs 0.83s cumulative and pulls in `ai` (0.81s) and
  `http.client` (0.36s), none of which is needed until the user actually asks for
  something. Note the self-inflicted part: the private-mode work put
  `private_ai_enabled()` / `PRIVATE_REFUSAL` in `ai.py`, and `api_tabs` calls it,
  so an ordinary tab listing now drags `http.client` and `ssl` in. Move those two
  names to a GTK-free home and `agent`/`ai` become lazy imports.

Rejected after probing: `enable-mediasource` and `enable-webaudio` stay **on**.
Disabling them breaks streaming video and site audio, which is a correctness
regression sold as a speed win.

## An editable outbound prompt preview

The scrubber half of this is built: `scrub.py` redacts emails, phone numbers,
Luhn-valid cards, mod-97-valid IBANs, SSNs and account-adjacent digit runs out
of every page that goes to Anthropic, `CB_SCRUB` turns it off, and the panel
says how many of each it removed. Clearing the page-text cache is built too, on
`cb:data` and as `cbctl clear pagetext`.

What is left is the second half: a preview step in the Claude panel for the
large sends — a full page dump, Research across every open tab — showing the
redacted text as it will be sent, editable before it goes. That is the part that
answers "what is about to leave this machine" rather than "what left it", and it
is a panel interaction rather than a text-processing problem: the scrubber
already produces the text it would show.

Worth deciding first: whether the preview is opt-in (a toggle, off by default,
because a confirmation step on every question would be nagging) or triggered by
size, and whether an edit to the preview is one-shot or remembered for that
page.

## Smaller things, in rough order of value

- **Read-later cache from what is already stored.** The page-text cache holds
  the prose of everything read; reader mode can already render an article. An
  offline "read later" view is mostly a `cb:` page over `pagetext`, not a new
  subsystem.
- **Inline hover badges.** A one-line TL;DR on hovering a link, served only from
  the page-text cache — never a live API call on hover, which would be a request
  per mouse movement.
- **Recall in the omnibox.** The address bar ranks history and bookmarks by
  title and URL. `recall` searches the full text of the same pages. Merging the
  two makes "the page about rate limits I read yesterday" findable by a phrase
  that never appeared in its title.
- **Battery and idle awareness in the resource guard.** `resources.py` already
  reads `/proc`; background work (summaries, indexing) could be gated on being
  plugged in and idle, the same way tab discarding is gated on swap-in rate.
- **Lazy images and iframes.** A `loading="lazy"` pass and a low-quality
  placeholder for offscreen images, on hardware where a page's image decode is a
  real cost.

- **Parameters for playbooks.** A recorded run hard-codes the URL and the search
  term it used. A playbook worth keeping takes them as arguments, so
  `playbook-run report --date 2026-08` is one playbook rather than thirty. The
  recorder already knows which parameter each value came from; what is missing
  is a way to name one and substitute it at replay.
