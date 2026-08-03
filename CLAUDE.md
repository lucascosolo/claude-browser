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
  resources.py THE RESOURCE POLICY -- when to wait, refuse, or drop a tab
  storage.py   the web context: persistent cookies, disk cache, clearing
  findbar.py   Ctrl+F, driving WebKit's per-view FindController
  tabnames.py  tab labelling (GTK-free so it is testable)
  urls.py      omnibox intent: navigate or search (GTK-free)
  reader.py    reader mode: article extraction + reading typography (GTK-free)
  passwords.py saved logins in the system keyring + the injected form script
  store.py pages.py panel_html.py style.py perf.py envfile.py
tests/         unittest, no display needed
```

## Commands

```bash
./cb                                        # run it
./cbctl health                              # is it up
./cbctl machine                             # what the resource guard thinks
./cbctl --help                              # every subcommand, generated
python3 -m unittest discover -s tests       # 250 tests, ~25s, no display
CB_AUTOSTART=0 python3 -m unittest ...      # in tests, so cb-mcp cannot launch a real window
```

Environment knobs the guard and storage read: `CB_MAX_TABS` (agent tab ceiling,
default 10), `CB_COOKIES` (`nothird`/`all`/`none`), `CB_ITP`, `CB_MEM_LIMIT`,
`CB_PACE` (agent pacing multiplier, default 1; `0`/`off` removes the pauses,
higher slows the cursor down, clamped to 5).

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
- **A page is never asked what origin it is.** Autofill is driven from the native
  side against `view.get_uri()`; the injected script only rings a doorbell and
  the native side reads the credential back out of the *focused* view. The
  content manager is shared by every tab, so a `script-message-received` signal
  cannot say who sent it — trusting a page-supplied origin would hand a
  background tab any saved password it cared to name.
- **Secrets go to the Secret Service, never to a file this project invents.** A
  password file of our own would need a master key, and the only place to put
  one is another file beside it.
- **Page loads are queued, never parallel.** `Browser._admit` puts every
  API-initiated load in a FIFO and runs one or two at a time. This is not
  politeness — five simultaneous loads on two cores peak their memory in the
  same second, and that is what froze the machine for twenty minutes. Anything
  that navigates from the API goes through `_admit`, or it is outside the only
  thing preventing a repeat.
- **Only memory may refuse; CPU may only slow things down.** A developer laptop
  idles at a load average of ten. A CPU-driven refusal means a browser that
  never opens a tab again. See the long note in `resources.admit`.
- **The browser owns its web context, and it is not the default one.**
  `storage.make_context_once()` builds it with explicit data directories,
  because a WebContext's data manager is fixed at construction and the default
  one persists nothing. Never call `WebKit2.WebContext.get_default()` —
  `WebView.new_with_user_content_manager()` does so internally, which is why
  `Tab` uses the property constructor instead.
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
- **`popover > contents` does not exist in GTK3.** It is the GTK4 node, and it is
  what every current styling answer tells you to use. In GTK3 those rules match
  nothing, so the menu paints as bare text floating over the page with no card
  behind it and no error anywhere. Style `popover.cb-menu` itself.
- **Clearing `background` on a themed button is not enough.** The stock theme
  draws its bevel with a `background-image` gradient *and* an inset `box-shadow`.
  Set both to `none` or a "flat" text button keeps a ghost outline.
- **Do not kill the browser from a Bash call that names it.** `pgrep -f` matches
  the tool call's own command line, so `pkill -f claudebrowser` in a command that
  mentions `claudebrowser` anywhere — even in a `cd` path — kills the shell
  running it (exit 144). Put the kill in its own call, with the pattern broken up
  (`[c]laudebrows`) and nothing else on the line that spells the name out.
- **The agent's pacing pauses are only safe on the worker thread.** `agent.py`
  sleeps between steps so the cursor in `extract.HALO` can be seen travelling to
  its target and pressing on it. `Agent.run` only ever runs under the thread
  `Browser.run_agent` starts — the same reason `call_sync` is safe there. Move
  that loop onto the main loop and the pauses freeze the whole window.
- **Swap *occupancy* is not memory pressure; the swap-in *rate* is.** A laptop
  with a few days uptime sits at 70-80% swap used forever, because pages evicted
  last week still count. The first version of `resources.py` read that as
  pressure and discarded every background tab, repeatedly, on a healthy machine.
  `pswpin` from `/proc/vmstat`, differenced between readings, is the live
  signal — and `pswpin`, not `pgpgin`, which counts every ordinary file read.
- **`set_memory_pressure_settings` is a *static* function on
  WebsiteDataManager**, not a method — it configures every web process, not one
  manager. Fetching it off the class and calling it with the settings object
  passes the settings as `self`, which is what `perf.py` did inside a bare
  `except Exception: pass` for as long as the file existed. The handler had
  never once been installed. The boxed type also needs `.new()`; plain
  `MemoryPressureSettings()` does not construct.
- **`WebView.new_with_user_content_manager()` silently takes the default web
  context.** So does `new_with_related_view`'s relative, transitively — which
  means the *first* tab decides the whole window's cookie jar. Getting that one
  constructor wrong opted the entire browser out of persistent storage while
  every other line looked correct.
- **A `Gtk.Window` key handler sees keys before the focused widget does.** The
  find bar's own Escape binding never fires while `Browser._on_key` is
  connected, so Escape for the find bar is handled there, ahead of the panel's.
- **Screenshotting the chrome needs a cropped root grab.** `xwd -name` matches
  the legacy `WM_NAME`, which GTK does not set (it sets `_NET_WM_NAME`), and
  `xwd -id` on the toplevel misses popovers because a GTK popover is its own X
  window at a different depth. Locate the window with `xwininfo -id`, then crop
  the region out of a root-window pixbuf. Present from *inside* the process
  before grabbing, or the window manager leaves whatever was focused on top.

## Conventions

- Named exports, no `__all__` games; module-level constants in caps.
- Calendar-free: everything user-visible is a live query, nothing is cached that
  the user can change (see the uncached `envfile.values()` and its comment).
- Every `api_*` method takes a trailing `done` callback and calls it exactly
  once with a JSON-serializable dict. `@needs_tab` resolves the tab id first.
- Tests are colocated by concern in `tests/`, plain `unittest`. Anything that
  can be tested without a display should live in a GTK-free module so it can be.
