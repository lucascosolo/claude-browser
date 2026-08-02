# Claude Browser — working notes

A WebKitGTK browser with a control API, driven by Claude. Python 3 + GTK3 +
WebKit2GTK 4.1, standard library only. No build step, no pip, no node.

**Read this file, not README.md.** The README is the user-facing document; this
is everything an agent needs and nothing it does not.

## Layout

```
cb  cbctl  cb-mcp          the three entry points (bash, CLI, MCP over stdio)
claudebrowser/
  api.py       THE API REGISTRY -- one table describing every operation
  browser.py   the window, tabs, the Claude panel, and every api_* method
  control.py   the loopback HTTP server + the GTK main-loop bridge
  client.py    the client half: used by cbctl, cb-mcp and the launch handoff
  extract.py   JavaScript injected into pages (read, click, fill, halo)
  agent.py     the in-browser tool-use loop behind Ctrl+G
  ai.py        Anthropic Messages API over urllib; auth.py picks the credential
  tabnames.py  tab labelling (GTK-free so it is testable)
  urls.py      omnibox intent: navigate or search (GTK-free)
  store.py pages.py panel_html.py style.py perf.py envfile.py
tests/         unittest, no display needed
```

## Commands

```bash
./cb                                        # run it
./cbctl health                              # is it up
./cbctl --help                              # every subcommand, generated
python3 -m unittest discover -s tests       # 149 tests, ~35s, no display
CB_AUTOSTART=0 python3 -m unittest ...      # in tests, so cb-mcp cannot launch a real window
```

There is no linter or type checker configured. `python3 -m py_compile` is the
syntax gate.

## The rules that matter

- **`api.py` is the single description of the browser's surface.** control.py's
  routes, `cbctl`'s subcommands and `cb-mcp`'s tools are all generated from it.
  Add an operation there, once. Do not add a route, a subcommand or a tool by
  hand — that is exactly how `cb-mcp` silently lost `forward` and `wait`.
  `agent.py`'s `TOOLS` is deliberately separate: its wording is prompt
  engineering for a model picking a next step, not an API reference.
- **Never touch GTK or WebKit off the main loop.** Everything goes through
  `control.on_main_loop`. Calling in from the HTTP thread crashes in ways that
  look like unrelated rendering bugs.
- **Bind loopback only.** The control API can read any page the user is signed
  into. `127.0.0.1`, never `0.0.0.0`.
- **Selectors and values are attacker-adjacent** — they can come from a page the
  agent is reading. They go through `extract._js_str`, which escapes `<`, `>`
  and U+2028/9 on top of `json.dumps`.
- Named exports of intent in comments: explain *why*, especially where a choice
  looks arbitrary but encodes a real constraint.

## Gotchas that cost real debugging

- **A WebView's minimum height is 0.** Packing the Claude panel with a fixed
  height in a plain box let the page collapse toward nothing on a short window,
  which read as "the panel went fullscreen". It is a `Gtk.Paned` now, clamped
  from an idle scheduled by `configure-event` — a `set_position()` during the
  allocation a resize triggers is overwritten by that same allocation.
- **An ellipsizing GtkLabel has a minimum width of one ellipsis.** Set
  `width_chars` (the minimum) as well as `max_width_chars`, or GtkNotebook
  shrinks every tab to a bare `…`. This is what `tabnames.py` and the label
  construction in `_tab_label` exist to prevent.
- **`Gtk.Window.present()` takes no timestamp.** `present_with_time()` is the
  timed one, and it is the one window managers honour instead of treating the
  raise as focus stealing.
- **`set_process_model()` is a documented no-op in WebKitGTK 2.52.** Tabs share
  one web process only because each view is created *related* to the first.
  A private tab cannot be a related view, so it costs its own process.
- **`innerText` on a detached node degrades to `textContent`.** Cloning the DOM
  to strip chrome is both slower and wrong; `extract.TEXT` walks the live tree.
- **A second launch must not bind the port.** `xdg-open` runs
  `claude-browser <url>` with no idea one is running. `client.handoff()` gives
  the URL to the running window and exits 0. Breaking this breaks the browser's
  role as the system default.
- **Four registries have an opinion about "the default browser"** and do not
  consult each other: `mimeapps.list`, `xdg-settings`, XFCE's `helpers.rc`
  (which needs a hand-written `X-XFCE-Helper`), and `gio`. `install.sh
  --set-default` writes all four. Writing one leaves the system disagreeing
  with itself.
- **The settings file beats the environment**, on purpose — see the long comment
  in `envfile.py`. A desktop-launched window never sees your shell. The API key
  is never copied into `os.environ` at all.
- **Tests must not start a real browser.** `cb-mcp` autostarts one on a tool
  call; the stub in `test_offline.py` answers `/health` in the real shape so
  `client.is_running()` is satisfied, and `CB_AUTOSTART=0` is the belt.

## Conventions

- Named exports, no `__all__` games; module-level constants in caps.
- Calendar-free: everything user-visible is a live query, nothing is cached that
  the user can change (see the uncached `envfile.values()` and its comment).
- Every `api_*` method takes a trailing `done` callback and calls it exactly
  once with a JSON-serializable dict. `@needs_tab` resolves the tab id first.
- Tests are colocated by concern in `tests/`, plain `unittest`. Anything that
  can be tested without a display should live in a GTK-free module so it can be.
