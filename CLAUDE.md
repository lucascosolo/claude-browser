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
  ai.py        Anthropic Messages API over a pooled http.client connection;
               auth.py picks the credential
  resources.py THE RESOURCE POLICY -- when to wait, refuse, or drop a tab
  storage.py   the web context: persistent cookies, disk cache, clearing
  findbar.py   Ctrl+F, driving WebKit's per-view FindController
  tabnames.py  tab labelling (GTK-free so it is testable)
  urls.py      omnibox intent: navigate or search (GTK-free)
  reader.py    reader mode: article extraction + reading typography (GTK-free)
  pagetext.py  on-disk page-text cache + FTS5 `recall` search (GTK-free)
  scrub.py     outbound PII redaction over page text (GTK-free)
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
python3 -m unittest discover -s tests       # 388 tests, ~60s, no display
CB_AUTOSTART=0 python3 -m unittest ...      # in tests, so cb-mcp cannot launch a real window
```

Environment knobs the guard and storage read: `CB_MAX_TABS` (agent tab ceiling,
default 10), `CB_COOKIES` (`nothird`/`all`/`none`), `CB_ITP`, `CB_MEM_LIMIT`,
`CB_PACE` (agent pacing multiplier, default 1; `0`/`off` removes the pauses,
higher slows the cursor down, clamped to 5), `CB_SCRUB` (outbound PII redaction,
default on; `0`/`off` sends page text raw), `CB_LIGHT` (ask servers for a
cheaper page — `Save-Data: on` plus reduced motion, default on; `0`/`off`).

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
- **Reader mode overlays the page, it never rewrites it.** `reader.toggle()`
  paints the extracted article into a fixed overlay and toggling off removes
  that overlay and nothing else. Deleting half a single-page app's tree is a
  one-way trip — reloading to undo it loses scroll, form state, and any load an
  agent is waiting on. Scoring runs on the live tree, cleaning on a clone, so
  pages with no article never pay for the copy.
- **`store.recordable(url)` is the privacy boundary for everything written to
  disk.** History and the page-text cache are written from the same point in
  `Browser._record`, so a private tab or a `cb:` page is excluded from both by
  one check. A new on-disk sink hangs off that same point or it is a leak.
- **The page-text cache is keyed by content hash, not by URL.** `pagetext`
  stores one body per hash with a row per URL pointing at it, so the canonical,
  AMP and tracking-parameter spellings of an article cost one copy of the prose
  and one search hit. Eviction is LRU over a byte cap (`MAX_BYTES`), not a row
  count, and only drops a body once the last URL referencing it is gone.
  Clearing it goes through `Browser._clear_kind`, the single place both the
  `clear` op and the cb:data buttons pass through, so "clear everything" cannot
  come to mean two different sets depending on which surface was used.
- **Page text is scrubbed on the way to Anthropic, and the user is told.**
  Every path that puts page content in a request goes through `ai._redact`, so
  `CB_SCRUB` is honoured once and the per-category counts have one place to be
  collected. The counts are not decoration: a redaction the user cannot see is
  one they cannot correct, so `_run_stream` renders them as the card's meta line
  and the agent emits them as a step. `scrub.py` favours precision over recall
  on purpose — it validates cards with Luhn and IBANs with mod-97, and it does
  not try names or street addresses at all, because a scrubber that mangles
  prose makes the answers stop matching the page and the user stops trusting
  them.
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
- **FTS5 is compiled into most sqlite3 builds, not all of them.** `pagetext`
  creates the virtual table inside a `try` and degrades to a plain text cache
  when it cannot: `available` is False, `reason` says why, and `search()`
  returns nothing. A browser that refuses to start over a search index it never
  needed is a worse browser. The index is external-content (`content='bodies'`),
  so it reads its text back out of the table instead of keeping a second copy —
  which is also why the sync triggers live in SQL, where eviction cannot forget
  to maintain them, and why a first run that *gains* FTS5 must `'rebuild'` or it
  is silently blind to everything cached before the upgrade.
- **Raw text is never an FTS5 MATCH expression.** A stray `"`, a bare `NEAR` or
  an unbalanced paren raises instead of searching. `pagetext.match_query` quotes
  every token, ANDs them, and prefixes only the last one with `*` for
  search-as-you-type. Bypassing it turns user input into query syntax.
- **A pooled connection may only be recycled once its body is gone.** `ai.py`
  keeps the connection to api.anthropic.com alive across requests, which removes
  a TLS handshake from every panel question and every agent step. The price is
  that a response abandoned half-read leaves its remaining bytes in the socket,
  and the *next* request on that socket parses them as its own reply — a
  corruption that shows up one call later, in an unrelated feature. `_Response`
  hands the connection back only on proof of a clean finish: `read()` with no
  argument, or an iteration that actually reached EOF. Breaking out of an SSE
  loop drops the connection, on purpose.
- **`CLOCK_MONOTONIC` does not tick while the laptop is suspended**, so an idle
  timeout cannot be the only staleness check on a kept-alive socket: a
  connection that has been dead since yesterday still looks seconds old after a
  resume. The real defence is `ai._request`'s single retry on a *fresh*
  connection when a *reused* one dies before answering. `reused` is the whole
  condition — the same exception on a new socket is a genuine network failure,
  and retrying it only delays the message. The retry is safe exclusively because
  it sits between sending the request and returning the response, so a stream
  that has already yielded text can never be silently restarted.
- **That retry is scoped to the write phase, and collapsing it back into a
  blanket retry costs real money.** `_send` flips a `sent` flag once
  `conn.request()` has written the whole body, and `_is_stale` refuses once it
  is set. The two phases raise the *same* exceptions, so the type tells you
  nothing: a `RemoteDisconnected` from `conn.request()` is a keep-alive socket
  the server closed while idle and nothing was processed, while the identical
  one from `getresponse()` means the full body already reached Anthropic, which
  may have received, run and **billed** the inference before the connection
  died. Re-sending that duplicates a paid inference call — the same reason
  urllib3 keeps POST out of its default retry set. The phase is the thing that
  determines safety; do not "simplify" this into inspecting exception types.
- **There are three layers that would retry, and the phase has to be visible to
  all of them.** Scoping only the inner retry was cosmetic: `_open_with`'s
  backoff loop catches `OSError`/`HTTPException` and would have re-sent the same
  delivered request up to `MAX_RETRIES` times, and `_open` falls through to the
  *next credential* on any `ApiError`, which is another full send. So a
  post-send failure is re-raised from `_request` as `_Delivered` (subclassing
  both `OSError` and `HTTPException`, so any older `except` still catches it
  rather than letting it escape); `_open_with` catches that *before* its two
  retry clauses and converts it to an `ApiError` with `delivered = True`; and
  `_open` stops on that flag instead of trying the next credential. Adding a
  fourth retry layer means teaching it the same thing.
- **The omnibox "changed" signal is one keystroke, not one intent.** DNS
  preconnect debounces (`PREFETCH_DELAY_MS`) *and* dedupes by host
  (`urls.HostWarmer`), because neither alone is enough: without the pause,
  typing `example.com` resolves `example.c` and `example.co` first, and those
  are different hosts as far as a set is concerned. The prefetch decision is
  `urls.prefetch_host`, built on `looks_like_url` so it can never disagree with
  what Enter would do — warming a name for a search query is a lookup nobody
  visits and a leak of what was typed.
- **A redaction placeholder is evidence to the next pattern.** `scrub.py`'s
  account-number rule fires on wording like `card` or `iban` next to a digit
  run — and `[card]` is a string it wrote itself one pass earlier, so without
  the `(?<!\[)` guard a redacted card turned the phone number after it into an
  "account number". Related, and found the same way: a card regex anchored only
  at its start happily matches the last four digits of a phone number plus the
  first twelve of the card behind it, fails Luhn, and *hides the real card* by
  consuming the region — hence the trailing `(?![ -]\d)`.
- **`WebKitWebResource` has `sent-request`, not `send-request`** — past tense,
  no return value, fired after the bytes are gone. There is no way to modify an
  outgoing request from the UI process in WebKitGTK 4.1. `send-request` is on
  `WebKitWebPage`, which lives in the *web process extension* API and needs a C
  shared library this project cannot build. So `perf.load_url` attaches
  `Save-Data: on` to a `WebKitURIRequest` for loads the browser itself starts,
  and subresources a page fetches for itself cannot carry it. Do not "fix" this
  by intercepting `decide-policy` and re-issuing with `load_request`: that drops
  the `Referer` WebKit would have set on a link click and turns a form POST into
  a GET (a `WebKitURIRequest` has no body accessor), which breaks logins.
- **WebKit 2.52 has no media-feature override, and `prefers-reduced-data` does
  not exist in it.** `WebKitSettings` has no property for either; the 489-entry
  `webkit_settings_get_all_features()` list has nothing matching "reduced" or
  "motion"; and of the `prefers-*` strings in libwebkit2gtk-4.1 only
  `color-scheme`, `contrast`, `dark-interface` and `reduced-motion` are present.
  The single input to `prefers-reduced-motion` is the GTK setting
  `gtk-enable-animations`, which the library watches (`notify::` on it sits in
  the same forwarded settings block as `gtk-theme-name`), so `perf.tune_gtk`
  sets it on the UI process's `Gtk.Settings` before the first WebView. The
  page-side half of that is inferred from what the library contains, not
  observed — confirming it needs a display. Never assert reduced motion with an
  injected `animation: none !important` sheet instead: it fights the page's own
  styles and breaks anything waiting on `animationend`.
- **Screenshotting the chrome needs a cropped root grab.** `xwd -name` matches
  the legacy `WM_NAME`, which GTK does not set (it sets `_NET_WM_NAME`), and
  `xwd -id` on the toplevel misses popovers because a GTK popover is its own X
  window at a different depth. Locate the window with `xwininfo -id`, then crop
  the region out of a root-window pixbuf. Present from *inside* the process
  before grabbing, or the window manager leaves whatever was focused on top.

## Architectures already rejected

Each of these was proposed for this project, examined, and refused on evidence.
They are recorded so the next session does not spend a day re-deriving the same
answer. Reopening one needs a new fact, not a new preference.

- **Tauri + React/Next.js for the UI.** On Linux, Tauri's webview *is*
  WebKitGTK — the same engine already embedded here. It would add a Rust
  toolchain and a node build step and buy zero memory, because Tauri's RAM win
  is measured against Electron, which this is not.
- **A Python service framework (FastAPI + uvicorn + uvloop) for the control
  API.** The traffic is a handful of loopback requests per session. Async
  machinery and three pip dependencies buy nothing measurable against
  `http.server` on a thread, and the no-pip rule is load-bearing: the install
  instruction is one `apt` line.
- **Embeddings, a vector store, and HNSW in WASM or Rust for search.** Replaced
  by SQLite FTS5 (`pagetext.py`), which ships inside the sqlite3 already in the
  standard library. An embedding model means a pip dependency and hundreds of
  megabytes of weights in a browser whose premise is the standard library, to
  rank a few thousand pages one person has actually read. BM25 over full text is
  the honest answer at this scale.
- **An opt-in VPS backend** — gateway, Redis, Postgres+pgvector or Qdrant, a
  Playwright container, device pairing and JWTs. Remote infrastructure is a
  separate project, and it contradicts the local-only posture the rest of this
  browser is built on: the point is that the agent uses *your* session on *your*
  machine.
- **A model-written summary on the discard path.** A discarded tab keeps a
  standing summary (`tabnames.lead_extract` over `pagetext.text_for`, captured
  from an idle in `Browser._capture_summary`), and it is a lead extract on
  purpose. Discards fire *because* memory is short, so an API call there is a
  paid network round trip on the machine least able to afford it, for a tab the
  user may never return to. A summary keyed by content hash and generated
  lazily when a card renders was the alternative; the lead extract is free,
  needs no new table, and is honest about being the opening of the page.
- **Canvas-grade UI toys** — an animated knowledge graph, a session time-lapse,
  physics-y tab folders, a tray widget cycling summaries. They need a frontend
  this GTK chrome does not have and would not gain cheaply.

## Conventions

- Named exports, no `__all__` games; module-level constants in caps.
- Calendar-free: everything user-visible is a live query, nothing is cached that
  the user can change (see the uncached `envfile.values()` and its comment).
- Every `api_*` method takes a trailing `done` callback and calls it exactly
  once with a JSON-serializable dict. `@needs_tab` resolves the tab id first.
- Tests are colocated by concern in `tests/`, plain `unittest`. Anything that
  can be tested without a display should live in a GTK-free module so it can be.
