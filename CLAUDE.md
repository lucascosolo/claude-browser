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
  personas.py  named answering styles composed onto ai.py's prompts (GTK-free)
  playbooks.py recorded op sequences: capture, validation, JSON store (GTK-free)
  tabnames.py  tab labelling (GTK-free so it is testable)
  urls.py      omnibox intent: navigate or search (GTK-free)
  reader.py    reader mode: article extraction + reading typography (GTK-free)
  pagetext.py  on-disk page-text cache + FTS5 `recall` search (GTK-free)
  scrub.py     outbound PII redaction over page text (GTK-free)
  vpn.py       VPN Mode: proxy-URI policy, redaction, the state machine and the
               external exit-IP probe (GTK-free)
backend/       the VPS half of VPN Mode: tinyproxy in a container, tailnet-bound,
               deployed as the cb-vpn systemd unit. See backend/README.md
AUDIT.md       the private-tab leak inventory the hardening was written against
  passwords.py saved logins in the system keyring + the injected form script
  settings.py  EVERY SETTING DESCRIBED ONCE -- values, validation, when each
               one lands. Behind cb:settings and `cbctl settings` (GTK-free)
  style.py     THE PALETTE + the GTK3 sheet. Three themes by name, never by a
               boolean; `phosphor` is the default (GTK-free)
  pages.py     the cb: pages; its own copy of the HUD sheet, same tokens
  panel_html.py the Claude panel document; ditto
  store.py perf.py envfile.py
tests/         unittest, no display needed
```

## Commands

```bash
./cb                                        # run it
./cbctl health                              # is it up
./cbctl machine                             # what the resource guard thinks
./cbctl --help                              # every subcommand, generated
./cbctl settings                            # every setting; add KEY VALUE to change one
CB_AUTOSTART=0 python3 -m unittest discover -s tests   # 680 tests, ~13s, no display
```

Environment knobs the guard and storage read: `CB_MAX_TABS` (agent tab ceiling,
default 10), `CB_COOKIES` (`nothird`/`all`/`none`), `CB_ITP`, `CB_MEM_LIMIT`,
`CB_PACE` (agent pacing multiplier, default 1; `0`/`off` removes the pauses,
higher slows the cursor down, clamped to 5), `CB_SCRUB` (outbound PII redaction,
default on; `0`/`off` sends page text raw), `CB_LIGHT` (ask servers for a
cheaper page — `Save-Data: on` plus reduced motion, default on; `0`/`off`),
`CB_PERSONA` (the Claude panel's answering style; `off` by default), `CB_THEME`
(`phosphor` by default, or `dark`/`light`/`system`), `CB_PRIVATE_AI` (off — a
private tab refuses Ask/TL;DR/Research/the agent rather than sending its page to
Anthropic), `CB_PRIVATE_DOWNLOADS` (off — a download from a private tab is
cancelled rather than writing a server-named file to disk), `CB_VPN` (off) and
`CB_VPN_PROXY` (the VPS proxy URL; a `SECRET_KEY`, so never exported to the
environment and never writable from inside the browser).

`settings.py` describes all 23 of them, with the validator each one's *consumer*
actually needs. `test_settings.EVERY_KEY` is a hand-kept copy of that key list —
adding a knob means adding it in both places, on purpose, so a new setting is a
deliberate act in a test as well as in the table.

`CB_AUTOSTART=0` is not optional: `test_offline.py` runs `cbctl` and `cb-mcp` as
real subprocesses, and `client.py` autostarts the browser by default — without
it a test run opens a window.

There is no linter or type checker configured. `python3 -m py_compile
claudebrowser/*.py` is the syntax gate, and it is *not* redundant with the
suite: `browser.py`, `control.py`, `findbar.py` and `__main__.py` need a display
and so are never imported by any test. On everything else the suite is the
stronger gate; on those four, py_compile is the only one there is.

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
- **The `nonce` in `pages.shell()` is a script-message auth token, not a CSP
  nonce.** There is no Content-Security-Policy anywhere in this project —
  `grep -rni "security.policy"` finds nothing — and reading it as one leads
  straight to the wrong conclusions about what it defends. The `cbui` script
  handler is registered on the *shared* UserContentManager, so any page loaded
  in this browser can call `window.webkit.messageHandlers.cbui.postMessage(...)`
  and ask for history to be cleared. Every `cb:` document is rendered with a
  per-session random token, attaches it as `msg.t`, and the native handler drops
  anything without it. It authenticates the *sender of a message*; it says
  nothing about what script a document may execute.
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
- **`store.recordable()` was never the whole privacy boundary, and a new sink is
  not the only way to leak.** It gates the two *disk* sinks. Everything else that
  learns about a private tab needs its own guard, because for a long time nothing
  had one: `grep -rn "private"` used to return zero hits in `ai.py`, `agent.py`,
  `playbooks.py`, `control.py`, `urls.py` and `storage.py`. What leaked was not
  storage — an `is_ephemeral` view really does keep cookies, localStorage,
  IndexedDB, service workers, HSTS and ITP off disk (probed) — but the *policy*
  applied to that view and the whole Python layer above it. Three rules came out
  of it, and they are the ones to keep:
  **privacy only ever travels downhill** (`storage.child_is_private` can add it to
  a child tab and can never remove it, so the next thing that opens a tab cannot
  reintroduce the popup bug); **an unresolvable identity answers "private"** (the
  playbook recorder cannot always tell which tab an op targeted, so an id it
  cannot resolve is treated as private and the step is dropped); and **the agent
  is not told private tabs exist** — `api_tabs` omits them rather than labelling
  them, because `list_tabs` used to ship every private URL and title to Anthropic
  annotated with which ones were private.
- **A private tab is its own network session, so VPN Mode has to be applied
  twice.** `WebContext.set_network_proxy_settings` does not reach a view created
  `is_ephemeral=True`; that view's own `WebsiteDataManager` needs the same call,
  in `Tab.__init__`, before its first navigation. Miss it and the two modes do the
  opposite of composing — the private tab is the one that goes out direct.
  For the same reason `ai._Pool` stamps every pooled socket with the route it was
  opened on: a direct connection serving a tunnelled request is exactly the leak
  the mode exists to prevent, and it would look like a cache hit.
- **"VPN on" means an external echo answered *through* the proxy.** A proxy that
  accepts a connection proves only that the tailnet path works — the VPS's default
  route, Docker's NAT and the provider's firewall each fail independently. And
  there is no fallback anywhere in that path: a failed mode refuses navigation and
  says why, because silently reverting to `DEFAULT` is a user browsing from home
  believing otherwise. Be honest about the ceiling: this is a browser proxy with a
  VPS exit, not a VPN, and without an OS-level egress kill switch it is
  best-effort — a compromised page or WebKit subprocess can still open a socket.
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
- **A playbook is replayed input, and is validated against `api.py` before any
  of it runs.** `playbooks.validate` resolves each step's op in the registry,
  refuses the ones with no browser method (`/health`) and the playbook ops
  themselves (no recursion), and checks every parameter against that op's
  declared ones — so a file on disk can only ever reach an `api_*` method that
  already exists with arguments it already accepts. Nothing is evaluated.
  Validation is all-or-nothing: a bad fourth step is refused before the first
  three have moved the browser somewhere nobody asked for. Replay then dispatches
  through the *same* `op.call` builder the HTTP route uses, which is what keeps
  its navigations inside `_admit`'s queue, and it runs strictly one step at a
  time — each step starts from the previous step's `done` callback.
- **Recording happens in `control._handle`, not per-op.** That is the one funnel
  every API-initiated operation already passes through, so a new entry in
  `api.OPS` is recordable the moment it exists. Failed operations are dropped
  (a click on a selector that was not there is not part of the sequence), and
  `tab` is never recorded — a tab id is valid for one session, so a hard-coded
  one acts on whatever holds that id tomorrow.
- **A credential is never written to a playbook, not even redacted.**
  `playbooks.is_secret_step` matches the *selector* or the *script* — never the
  value, because a pattern that has to read the value has already loaded the
  secret into a variable someone then has to be careful with. The step is
  dropped at capture time so no copy exists to leak, the count is reported, and
  replay leans on the native autofill, which is the only path here allowed to
  hold a secret.
- **A persona is composed onto the base system prompt, never in place of it.**
  `personas.compose` always starts from the base it is given and can only append
  a paragraph, so no persona -- valid, mistyped or absent -- produces a prompt
  without the instructions the browser depends on (answer from the page's text,
  say so when the answer is not there). Composition happens inside `ai._stream`,
  the one point Ask, TL;DR and Research all pass through, so a new text feature
  cannot forget it. `agent.SYSTEM` deliberately stays outside: it is
  instructions for driving a browser with tools, and an answering style layered
  over it would change what the agent *does* rather than how it writes.
- **A theme is a *name*, never a boolean, and the palette is one fixed set of
  tokens.** `style.palette(name)` / `style.css(name)` take `dark`, `light` or
  `phosphor`; `style.resolve()` sends anything else to `style.DEFAULT_THEME`
  rather than raising, because a typo in `CB_THEME` must not stop the browser
  starting. Every theme carries every one of `bg bar panel field field_focus
  tab_active line edge text dim accent accent_soft on_accent agent agent_soft
  on_agent ok warn grid mono name` — a template written against one theme has
  to render in all three, which `test_style.test_every_theme_carries_every_token`
  holds. Add a token and you add it three times.
- **`accent` and `agent` are two different inks on purpose.** `accent` is
  *chrome* state (focus, active tab, load progress, private-window frame);
  `agent` is *Claude* state (the AI buttons, "Claude is driving this tab", the
  busy status). They are the same coral in dark and light, and cyan vs amber in
  phosphor. The reason they cannot be merged is off-screen from `style.py`: the
  cursor `extract.HALO` draws is painted *into the page*, where the chrome theme
  cannot reach, and it is amber. Collapse the two and either the cursor stops
  matching its own chrome, or "Claude is doing something" becomes the same ink
  as "this field has focus" — the one signal that must never be missed made the
  most common colour on screen.
- **Phosphor is the default; the *absence* of `CB_THEME` is not deference to the
  desktop.** An unset value means nobody has chosen, and the answer to that is
  `DEFAULT_THEME`. "Follow the desktop" is now the explicit string `system`,
  handled in `Browser._theme_for` and nowhere else — `style.resolve()` has never
  heard of it and must not learn, since `style.py` is GTK-free and the desktop
  preference is a GTK reading.
- **Every settings write goes through `settings.apply`, and the validator is
  written from what the *consumer* does with the value.** `CB_PORT` and
  `CB_MAX_TABS` are `int()`-ed before the window exists, so they do not degrade
  to a default — a settings surface that can write them is one that can stop the
  next launch. The page, the `/settings` route and `cbctl settings` all call the
  same function, so they cannot disagree. `settings` is deliberately not an MCP
  tool: several of these decide what is sent to Anthropic, and one is the token
  guarding the API the agent is calling through.
- **`envfile.put` and `envfile.remove` are the only writers of the settings
  file, and both refuse `SECRET_KEYS`.** `put` rewrites the first uncommented
  assignment in place and leaves comments, ordering and the commented-out
  template examples alone -- the file is the user's. `remove` deletes the line
  instead of writing the default back, which is what "reset to default" has to
  mean: a line the user never chose would pin the value against any future
  change of default. A setter that could write `ANTHROPIC_API_KEY` would be a
  route from an API call to the user's credential, which is why it raises
  instead.
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
- **Shipping is not finished until `./install.sh --set-default` has been run.**
  This is the user's daily browser, so a merge that never reaches the installed
  entry points is a release that did not happen. `~/.local/bin/claude-browser` is
  a *symlink into this working tree*, so the code itself follows whatever is
  checked out — but the desktop entry, the icons and the four registries above do
  not, and a stale one fails as `Failed to execute child process
  "claude-browser"`, which looks like a broken browser rather than a stale
  registration. Run the installer after every ship or deploy, then confirm with
  `xdg-settings get default-web-browser` and one real launch.
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
- **GTK3's CSS parser silently drops what it does not understand.** An unknown
  property is not an error you can see: the sheet loads, the rule vanishes, and
  the only trace is a warning on stderr at every launch, which nobody reads
  after the first week. This is why `test_style.Parses` exists — it hangs a
  `parsing-error` handler on a real `Gtk.CssProvider` and asserts the list comes
  back empty for every theme. A `CssProvider` is not a widget and never touches
  the screen, so this runs headless with the rest of the suite. Note the trap it
  was built for: GTK3 has **no `text-transform`**, so the phosphor chrome gets
  its engraved look from the mono face and letter-spacing instead. The `cb:`
  pages *do* use `text-transform` — those are rendered by WebKit, not GTK, and
  the two sheets are not the same sheet.
- **The phosphor sheet is additive, in all three files.** `style._HUD`,
  `pages._HUD_CSS` and `panel_html._HUD` are appended to the base template for
  that theme only, and every rule in them overrides one already above. Written
  this way so dark and light come out exactly as they did before phosphor
  existed — picking it is meant to be a visible choice, not a silent redesign of
  the other two, which `test_phosphor_is_additive` pins. In `panel_html` the HUD
  text is spliced in at `_HUD_SLOT` *before* the single `%`-format pass rather
  than substituted as a value: a `%`-substituted value is not rescanned, so its
  own `%(accent)s` would reach the document raw.
- **`grid == bg` is a deliberate no-op, not a copy-paste slip.** In dark and
  light the scanline ink equals the background, so the token exists in every
  palette and no template has to branch on whether the theme has a texture. Do
  not "clean it up" by making `grid` optional — that puts an `if` in every place
  that reads it.
- **`panel_html.page()` takes the whole palette.** It used to rebuild the dict
  key by key, which meant every token added to `style.py` — `edge`, `grid`,
  `agent`, `mono` — silently stopped at that function and the panel drifted out
  of the theme it was supposed to be part of, with no error anywhere. The only
  two names remapped are the ones the panel's template calls something else
  (`bg` = the chrome's `panel`, `card` = the chrome's `bar`).
  `test_pages_style.test_every_palette_token_reaches_the_panel` is the guard.
- **A mutable/bound default is fixed at `def` time, and that is a hole a test
  can fall through.** `ai._open(payload, timeout, sleep=None)` takes `None` and
  resolves to `time.sleep` inside the body, precisely because `sleep=time.sleep`
  in the signature binds the real function once at import and no caller can ever
  replace it. The test that tried to worked around it by patching
  `ai.time.sleep` — which disabled sleeping *process-wide* for everything that
  ran after it in the same suite. If an injection point looks unused, check
  whether it is actually reachable before deleting it.
- **`close()` on a background-writer store has to close a connection too.**
  `store` and `pagetext` each keep one sqlite3 connection *per thread*, and
  sqlite3 refuses a `close()` from a thread other than the one that opened it —
  so shutting the writer thread down is not shutting the store down. Each side
  hands its own connection back (`_close_local`): the writer on its way out,
  `close()` for the caller. Run the suite under `-W error::ResourceWarning`
  occasionally; that is what surfaced the leak.
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
- **`comm` in `/proc/PID/stat` is truncated to 15 bytes, and all three WebKit
  process names are longer than that.** The kernel reports `WebKitWebProces`,
  `WebKitNetworkPr` and `WebKitGPUProces` — never the full spelling. So
  `renice_children` testing those reads against the full `WEB_PROCESSES` names
  matched nothing and was a **silent no-op for as long as the file existed**,
  which is why a runaway page could still make the desktop unusable: measured on
  a live browser, every content process sat at nice 0. Its test passed the whole
  time, because the fixture supplied `WebKitWebProcess` as a `comm` — a string
  the kernel cannot produce. `WEB_PROCESS_COMMS` holds the truncated forms and is
  what matching uses; `WEB_PROCESSES` stays as the documentation of what WebKit
  spawns. The general lesson is the one worth keeping: **a fixture more permissive
  than the kernel hides the bug it was written to catch.** Any new name added
  needs to survive the cut without colliding, which
  `test_every_web_process_name_survives_truncation` holds.
- **The CPU/memory cap lives in `cb`, not in `settings.py`.** A `systemd-run
  --user --scope` wrapper gives the browser a `CPUWeight`, a `CPUQuota` backstop
  and a `MemoryHigh` ceiling, because two cores shared with Claude Code means "the
  browser is busy" must not mean "the desktop stops responding". It is in the
  launcher because the limits have to exist *before* the process does — there is
  no browser yet to ask, which is the same reason `CB_PORT` is read early. Every
  launch path already funnels through `cb` (the `claude-browser` symlink, the
  desktop entry, xdg-open, and `client.autostart`, which resolves to `<repo>/cb`
  rather than spawning python itself), so this is the one place it can go; adding
  a second launcher bypasses it. `CB_CAP=0` disables it, and `CB_CPU_WEIGHT` /
  `CB_CPU_QUOTA` / `CB_MEM_HIGH` tune it. Two details are load-bearing:
  **`MemoryHigh`, never `MemoryMax`** (High throttles and reclaims, Max kills a
  browser mid-page on a machine that has swap to spend), and the **user-bus socket
  check before `exec`** — without it `systemd-run` fails *after* `exec` has
  replaced the shell and the browser simply never starts, which is a far worse
  bug than running uncapped.
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
  visits and a leak of what was typed. The recall suggestions hanging off the
  same signal need the same treatment twice over: an FTS5 query is a disk read
  through an index, so it is debounced (`RECALL_DELAY_MS`), run on a worker
  thread (`pagetext` keeps one connection per thread, so this does not disturb
  the writer), and stamped with a serial that every keystroke bumps. Without the
  serial, a query that returns after another character was typed appends rows
  that do not match the box — the answer to a question nobody is asking any
  more.
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
- **Three of this engine's privacy-shaped settings are deprecated no-ops, and
  they read as protection.** `set_enable_hyperlink_auditing` logs "deprecated and
  does nothing" once per view while the getter still returns `True`;
  `enable-dns-prefetching` always reads `False` and pages get no prefetch at all
  (the real local lookup is the browser's *own* `context.prefetch_dns` off the
  omnibox, which is where the guard belongs); and `set_process_model` is the
  original of the species. `hasattr` cannot tell you — all three exist. Only
  `enable-webrtc` is real, and it is the one that matters, since ICE candidates
  carry the host address straight past an HTTP proxy. Do not add the other calls
  back to look thorough.
- **`media-playback-requires-user-gesture` defaults to `False`.** On two 1.6GHz
  cores an autoplaying video is not a nuisance, it is the page. Worth knowing
  alongside the rest of `perf.py`'s defaults, which are tuned for a Celeron N3060
  with 3.8GB and **no swap at all** — check `swapon --show` before assuming
  `resources.py`'s swap-in signal has anything to read.
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
- **An opt-in VPS backend *as a second brain*** — gateway, Redis,
  Postgres+pgvector or Qdrant, a Playwright container, device pairing and JWTs.
  Still refused, and for the original reason: that is a separate project, and it
  contradicts the local-only posture the rest of this browser is built on — the
  point is that the agent uses *your* session on *your* machine.
  **Narrowed, not reversed.** VPN Mode (`vpn.py`, `backend/`) reopened exactly one
  slice of this on an explicit instruction: a *transport* proxy, and nothing else.
  No database, no remote browser, no pairing, and no browsing data at rest on the
  server — the VPS sees packets it is forwarding and keeps no log of them. The
  distinction is the whole reason the entry above still stands: routing bytes
  through a machine you own is not the same as moving the browser's knowledge onto
  it. Anything that proposes storing history, page text or embeddings server-side
  is the rejected architecture again and needs its own new fact.
- **A model-written summary on the discard path.** A discarded tab keeps a
  standing summary (`tabnames.lead_extract` over `pagetext.text_for`, captured
  from an idle in `Browser._capture_summary`), and it is a lead extract on
  purpose. Discards fire *because* memory is short, so an API call there is a
  paid network round trip on the machine least able to afford it, for a tab the
  user may never return to. A summary keyed by content hash and generated
  lazily when a card renders was the alternative; the lead extract is free,
  needs no new table, and is honest about being the opening of the page.
- **An animated CRT/HUD chrome, and a second full stylesheet to carry it.**
  Phosphor is a `repeating-linear-gradient` at 3% alpha painted once into each
  chrome strip and cached by GTK like any other background — no flicker, no
  sweep, no timer. An animated one is a per-frame repaint of decoration on the
  two cores that are already laying out the page, on a machine where
  `CB_LIGHT` is spending real effort asking *sites* to stop animating. The
  overrides are appended to the existing sheet rather than written as a second
  template, so dark and light cannot be changed by editing phosphor.
- **A settings page that writes any key it is given.** `CB_PORT=banana` is not
  "falls back to the default", it is a browser that does not start next launch.
  Validation lives in `settings.py`, written from what each consumer does, and
  `ANTHROPIC_API_KEY` is not editable from the browser at all.
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
