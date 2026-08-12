# Handoff — resource modes, crash recovery, VPN

Read `AGENTS.md` first. This file is the *current session's* state: what changed,
what is verified, what is next, and which decisions are already settled. Delete
it once the work it describes has landed.

## Status

All of it is committed on the branch **`worktree-modes-crash-vpn`**, six commits
ahead of `master`, oldest first:

```
94001e6  Rename CLAUDE.md to AGENTS.md
8b593c5  VPN Mode: answer the client, and say which of four ways it failed
0637061  Never leave a tab blank when its web process dies
1802db2  Give Watch Later an icon the theme actually has
c681e57  Move status out of the omnibox and into a toast
bec4092  Resource modes: Normal, Light, Potato, and a scraper tier for Claude
```

They are ordered so every intermediate state compiles and the tree is
independently useful at each one; each was verified to compile as a cumulative
state before it was written. The two commits that share a hunk (`toast` and
`modes`, in `browser.py` and `style.py`) are ordered toast-first so neither half
is broken on its own. Nothing is left in the working tree.

874 tests pass (`CB_AUTOSTART=0 python3 -m unittest discover -s tests`) and
`python3 -m py_compile claudebrowser/*.py` is clean at the branch tip.

**Not merged.** The branch is unreviewed by the user, and the modes control and
the toast have never been looked at by human eyes (see below). Merging is a
fast-forward — `git checkout master && git merge worktree-modes-crash-vpn` — but
that is the user's call, and it is worth them driving the pills once first.

The user's running browser follows whatever is *checked out*:
`~/.local/bin/claude-browser` is a symlink into this tree, so restarting it while
this branch is checked out is enough to try the work. No installer run is needed
unless the desktop entry or icons change — and the Watch Later fix is an icon
*name* in the menu table, not a shipped icon file, so it does not.

## Done and verified

Each heading below names its commit; `git show <sha>` has the full reasoning in
the message, and the durable rules landed in `AGENTS.md` rather than here.

**Blank "New Tab" bug — fixed** (`0637061`). Was a dead WebKit web process with
no handler anywhere in the project. `Browser._on_gone` + `Tab.last_good` /
`Tab.crashed_at`. Verified live by killing the web processes of an
isolated instance: foreground tab reloaded with content intact, background tabs
kept their titles and restored on select, and a second crash inside 30s correctly
left the tab asleep instead of reload-looping. Note: the *trigger* is unproven —
`journalctl -k` needs privileges unavailable here, so an OOM kill could not be
confirmed. The recovery is trigger-independent.

**Watch Later icon — fixed** (`1802db2`). `view-media-playlist-symbolic` is not
in Adwaita. `test_style.MenuIcons` now audits every icon name in `browser.py`.

**VPN Mode — fixed, and it was the VPS, not `vpn.py`.** A conntrack exemption was
missing from `backend/firewall.sh` — the project's own SSRF chain was dropping
the proxy's *reply* packets, so the socket sat in `SYN_RECV`. Verified end to end
(traffic exits `162.35.172.112`, direct is `47.214.55.67`), with SSRF protection
re-confirmed afterwards against `nip.io` names resolving into private space.
Details in `AGENTS.md`. The fix is in `8b593c5` alongside the taxonomy, **and it
is also already live on the VPS** — it was deployed and the unit restarted during
this session, ahead of the commit. So merging this branch does not re-deploy
anything and not merging it does not un-fix the VPN; a *fresh* VPS gets it from
`backend/README.md`'s deploy.

**VPN error taxonomy — done** (`8b593c5`). `vpn.py` classifies failures as
`CONFIG` / `CONNECTION` / `BACKEND` / `VERIFY`, each with a `headline` and
`advice` in one
table, surfaced through the snapshot to cb:vpn, the pill, `cbctl vpn` and the
navigation-refusal message. 407 is deliberately `CONFIG` (the server is healthy;
the password is not). `combine_kinds` exploits the fact that three echoes share
one proxy hop. 22 tests.

**Resource modes — `modes.py` + the toolbar control** (`bec4092`). GTK-free
policy, applied by `perf.apply_mode`, a `/mode` op in `api.py` (so `cbctl mode`
and the MCP tool are generated, not hand-added), and three segmented pills in the
toolbar. Verified live: `cbctl mode` reads/sets, persists to `CB_MODE_SITES`, and
in Potato all 50 images on a Wikipedia page reported `naturalWidth: 0`. Scraper
is defined but unimplemented — see next steps.

**Omnibox no longer shows status** (`c681e57`). `_flash` paints a `.cb-toast`
over the content instead. All 38 call sites unchanged.

**Not visually reviewed.** The pills and the toast have never been seen in a
screenshot — a root grab kept capturing the user's own browser, which sits on top
of the test window. The user is the right reviewer for the look.

## Next, in the order agreed

1. **Potato image placeholders.** The user chose placeholder boxes in the page
   with click-to-load over a toolbar notice. Unsolved design problem worth
   knowing before starting: `auto-load-images` is a *view-level* setting, so
   "load just this one image" has no direct API — clicking a placeholder probably
   has to enable images for the view and re-assign `src`. Confirm on a live page
   before committing to a design.
2. **Content filter.** `perf.BLOCKED` already exists and compiles through
   `WebKit2.UserContentFilterStore`. Expanding the list is the single biggest
   performance lever in the user's spec and is a good candidate for the
   `distributed-compute` skill — bounded, verifiable, no house style to match.
   The user has explicitly authorised that. Web fonts have **no** WebKit setting
   and must be blocked here.
3. **Scraper mode.** `modes.SCRAPER` exists and is deliberately absent from
   `modes.SLIDER`; nothing implements it yet. Plain HTTP + HTML parse, no web
   process. Follow `watchlater.py` (which already fetches with the browser's real
   cookies via WebKit's `CookieManager` on the main loop) and `search.py` (which
   shows the `vpn.api_route` pattern — a scraper fetch must not become a hole in
   the tunnel). Escalate to a real tab on *measured* evidence: near-zero text
   yield, or a Cloudflare interstitial. Many client-rendered pages ship their
   content as `__NEXT_DATA__` / `application/ld+json` / inline state, which is
   pure string work and recovers most of what JS would have given.
4. **Remaining spec items**, not yet started: context menus (Open in New Tab,
   middle-click, Search in New Tab, Claude Look Up popup — there is *no*
   context-menu handling in the project at all today); the tab-lifecycle rework
   (visible / temporarily-inactive / safe-to-purge, grace periods, per-tab CPU
   and RAM instrumentation); Cloudflare compatibility; the custom developer
   inspector; password autofill suggestions; and the `cb:search` rewrite from
   summarising results to answering the query's intent.

## Decisions already made — do not re-litigate without a new fact

- **The ladder is Normal / Light / Potato, plus a hidden Scraper.** Normal =
  "the full suite with the waste removed", and is what a site gets when it has to
  work. Potato = "HTML and basic JavaScript".
- **JS implies WebKit.** A non-WebKit tier with JavaScript was considered and
  rejected: the engine is free here (JavaScriptCore is available through
  introspection, node is installed) but a DOM faithful enough for real pages is a
  browser engine, and the cost on this hardware is layout and paint, not script.
  So the Potato/Scraper boundary *is* the JavaScript boundary.
- **The mode control is three always-visible segmented pills**, not a cycling
  chip and not a popover. Always visible because a control that hides on Normal
  is missing exactly when someone reaches for it.
- **A site is remembered only when it departs from the default**, which with the
  shipped default means "remembered when Light or Potato".
- Rejected architectures are listed in `AGENTS.md`; reopening one needs a new
  fact, not a preference.

## Working style the user asked for

Scoped, independently verifiable chunks. Pause to ask style questions rather than
guessing — the user has clear opinions and gives them readily. Do not use the
Agent tool or workflows unless asked; `distributed-compute` is authorised for
bounded work. Test against an isolated browser instance, never the user's daily
one (`AGENTS.md` says how). Documentation is `AGENTS.md`, never `CLAUDE.md`.
