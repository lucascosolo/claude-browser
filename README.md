# claude-browser

A small web browser for developers, with a control API for Claude agents built in
from the start rather than bolted on.

It renders with **WebKitGTK** — the same engine that backs Safari — using the copy
already installed on your system. There is no bundled Chromium, no Electron, and no
node_modules. The whole thing is about 2,000 lines of Python against the standard
library, and it idles in roughly the memory one Chrome tab uses.

```
┌──────────────────────────────────────────────────────────┐
│ ← → ⟳ ⌂ │ https://example.com    │ ☆  ✦ ⚟ ＋ │   40px of chrome
├──────────────────────────────────────────────────────────┤
│                                                          │
│                     the page                             │
│                                                          │
└──────────────────────────────────────────────────────────┘
        ▲                                        ▲
   agents drive this over HTTP            Ctrl+K asks Claude
```

## Why it exists

Agent browser automation usually means a headless browser that is *not* the browser
you are looking at: different profile, no session, different rendering. This one is
the browser you are looking at. An agent that opens a page sees what you would see —
your cookies, your logins, your dev server — and you can watch it happen in the
window.

## Install

The engine is probably already on your machine; what is usually missing is the
Python binding to it.

```bash
sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi python3-gi-cairo
```

Then just run it — there is nothing to build and nothing to `pip install`.

```bash
./cb                          # start browsing
./cb https://example.com      # ...at a URL
```

Requires Python 3.9+, GTK 3, and WebKitGTK 4.1. Fedora: `python3-gobject
webkit2gtk4.1`. Arch: `python-gobject webkit2gtk-4.1`.

### Add it to the desktop menu

```bash
./install.sh            # XFCE / GNOME / KDE menu entry + icon, no sudo
./install.sh --uninstall
```

It appears under **Applications ▸ Internet** as *Claude Browser*, registers as a
browser choice for `http`/`https` links, and symlinks `claude-browser`, `cbctl` and
`cb-mcp` into `~/.local/bin`. Everything installed lives under `$HOME`, and the app
keeps running from this checkout — `git pull` updates the installed copy too.

Right-clicking the menu entry offers **New Window** and **New Window (no agent API)**.

## Using it

| Key | |
|---|---|
| `Ctrl+L` | focus the address bar |
| `Ctrl+K` | ask Claude about the current page |
| `Ctrl+T` / `Ctrl+W` | new tab / close tab |
| `Ctrl+Shift+P` | **new private tab** |
| `Ctrl+D` | **bookmark this page** |
| `Ctrl+H` / `Ctrl+Shift+O` | **history / bookmarks** |
| `Ctrl+Shift+A` | **the Deck — every tab as a card** |
| `Ctrl+R`, `Alt+←/→`, `Alt+Home` | reload, back, forward, start page |
| `Ctrl+±`, `Ctrl+0` | zoom |
| `F12` | web inspector |

The address bar navigates when the input looks like an address and searches
otherwise, and suggests as you type from your history and bookmarks —
bookmarks first, then by visit count with a recency bonus, and a match on the
start of a hostname outranks one buried in a title. Tabs appear only once there
is more than one.

### Its own pages

Four internal pages on the `cb:` scheme, laid out as cards and sharing the
Claude panel's visual language. A slim icon rail moves between them.

| | |
|---|---|
| `cb:home` | Start page: one input that both searches and navigates, four quick actions, then bookmarks and recent pages as tiles. This is the default new tab (`CB_HOME` overrides it). |
| `cb:deck` | **Every open tab as a card.** A tab strip stops being navigation somewhere around the eighth tab — the labels ellipsize to nothing and you are clicking by position. The deck is the same set laid out where the titles are readable. |
| `cb:bookmarks` | Everything saved, filterable. |
| `cb:history` | Grouped by day, filterable, with per-entry delete and a two-step clear. |

Tiles carry a **site mark** — a letter on a colour hashed from the hostname —
rather than a favicon. Favicons would mean a network request per tile on the
machine we are optimising for, and they leak your history to every site you
have ever visited the moment you open a new tab. The mark is stable, instant
and offline.

Filtering inside these pages is done in the page, not by re-rendering from
Python: a full page load per keystroke is not a search box.

### Private tabs

`Ctrl+Shift+P` opens a tab with its own ephemeral `WebsiteDataManager` —
separate cookie jar, no disk cache, nothing kept when it closes — and nothing
it visits is written to history. The bar picks up a dashed accent underline and
the tab gets a badge, so a private tab is never something you have to remember.

The one real cost: a private view cannot also be a *related* view, and related
views are how ordinary tabs share a single web process. So a private tab does
spawn its own — deliberately, since inheriting the relative's storage is the
exact thing being avoided.

History and bookmarks live in one SQLite file at
`~/.local/share/claude-browser/browsing.db`. History is deduplicated by URL
with a visit count rather than appended per visit, which is what keeps the
history page scannable and gives autocomplete something to rank by. Writes go
through a background thread so a disk seek never lands on the frame the page is
painting.

### The Claude panel

One console at the bottom — mode pills, a status line, and results as **cards**
rendered by a WebView, because there is already a browser engine in the process.
Every run says which credential it used and, on failure, why.

| | | |
|---|---|---|
| `Ctrl+K` | **Ask** | Question about the current page. |
| `Ctrl+Shift+S` | **TL;DR** | Summarize this page. A button, never automatic — a request per page load would be slow and costs money on pages you never read. |
| `Ctrl+Shift+R` | **Research** | Reads *every open tab* and synthesizes across them. Leads with a table when they're comparable. |
| `Ctrl+G` | **Command** | Give Claude a goal and it drives the browser — navigating, reading, clicking — in the window you're watching, on your own logged-in session. `Stop` cancels mid-run. |

The command bar is the control API turned inward: the same navigate/read/click
primitives an external agent gets over HTTP, handed to a tool-use loop inside
the browser. It won't submit forms or change account state unless the goal
plainly asks for it.

## Driving it from an agent

The browser serves a JSON API on `127.0.0.1:8765` — loopback only, since it can read
any page you are signed into. Set `CB_TOKEN` to require a bearer token as well.

### As MCP tools (Claude Code)

```bash
claude mcp add browser -- /path/to/claude-browser/cb-mcp
```

That registers `browser_open`, `browser_text`, `browser_markdown`, `browser_links`,
`browser_find`, `browser_click`, `browser_fill`, `browser_eval`, `browser_console`,
`browser_screenshot`, and the navigation tools. Start the browser first; the MCP
server is a thin translation layer over the running window.

### From the shell

```bash
./cbctl open https://docs.example.com
./cbctl text                                  # readable text, JSON
./cbctl markdown                              # headings, links and code preserved
./cbctl links | jq -r '.links[].href'
./cbctl find 'rate limit'
./cbctl fill '#search' 'webkit' && ./cbctl click 'button[type=submit]'
./cbctl console --pattern 'MyApp'             # console output + uncaught errors
./cbctl shot /tmp/page.png
```

`cbctl` exits non-zero when the browser reports a failure, so `cbctl click .go &&
cbctl text` does the right thing.

### Over HTTP

```bash
curl -s 127.0.0.1:8765/text | jq -r .result.text
curl -s 127.0.0.1:8765/open -d '{"url":"https://example.com"}'
```

| Route | | |
|---|---|---|
| `/tabs` `/health` | GET | what is open |
| `/open` `/navigate` `/back` `/forward` `/reload` `/close` `/wait` | POST | move around |
| `/text` `/markdown` `/links` `/html` `/find` | GET | read the page |
| `/click` `/fill` `/eval` | POST | act on the page |
| `/console` `/screenshot` | GET | debug the page |

Navigation routes block until the load finishes, so an agent can `open` then `text`
without polling. Pass `wait=false` to return immediately. Every route takes an
optional `tab` id and defaults to the focused tab.

## Performance on slow hardware

Developed against a Celeron N3060 — two cores at 1.6GHz, 4GB of RAM, swap in
use. What's done about it:

- **Ad/tracker blocking is on by default** (`CB_BLOCK=0` disables it). 82 rules
  compiled into WebKit's native content-blocker bytecode and cached on disk.
- **Tabs share one web process.** WebKit's default is one process per view,
  which on a swapping box is what makes a fourth tab hurt. Measured: opening
  three more tabs added **zero** web processes. Note `set_process_model()` is a
  no-op in 2.52 despite being the obvious call — the mechanism that works is
  creating each view *related* to the first, which is what `new_tab` does.
- **Page reads walk the live DOM** instead of cloning it. The obvious version —
  clone, strip the chrome, read `.innerText` — is slower *and* wrong: a detached
  clone has no layout, so `innerText` silently degrades to `textContent`, losing
  block separation and leaking hidden text. Walking in place costs no copy and
  allows a real `getClientRects()` visibility test.
- **Console shim only in the top frame.** An ad-heavy page carries dozens of
  iframes and injecting into each was pure cost.
- **Blocklist compiles after first paint**, smooth scrolling and WebGL off,
  browser cache model, memory-pressure handler, progress repaints coalesced
  to 10/s.

Three APIs here exist, are documented, and do nothing in WebKitGTK 2.52:
`set_process_model`, `set_enable_hyperlink_auditing`, and `innerText` on a
detached node. `hasattr()` cannot tell you that — only running it can.

**No speedup figure is quoted, deliberately.** `tools/bench.py` exists and
measures load time with and without the blocker, but on this hardware it could
not produce a trustworthy result — the same URL took 2.9s in one run and timed
out at 90s in another. Swap pressure and network variance both exceed the effect.
The blocker demonstrably loads (82 rules, verified) and the process sharing is
measured above; everything else here is sound in principle and unquantified.

Python is not the bottleneck, which was worth checking: during a page load the
CPU is entirely `WebKitWebProcess` (15–21%) and `WebKitNetworkProcess` (8–20%) —
the Python chrome never rises above the noise floor. The interpreter costs 8.8MB
of RSS against WebKit's ~350MB, and the GTK/WebKit libraries that make up the
rest would be loaded by a C or Rust build too. The one real cost is startup:
~2.9s, nearly all of it loading GObject typelibs. A Rust rewrite via Tauri would
use this same WebKit engine and render no faster.

## Design notes

**Navigation waits are edge-triggered, not timed.** `/open` and `/navigate` attach a
callback to the tab's load and return when WebKit says the load ended — no `sleep 2`
and hope. If the tab is already idle they return immediately rather than blocking
until the *next* navigation.

**Console output is captured by a shim, not a signal.** WebKitGTK does not expose
console messages to the embedder, so a user script installed at document-start wraps
`console.*` and the `error` / `unhandledrejection` events into a 500-entry ring
buffer that `/console` reads back. It runs before page scripts, so it catches
early errors.

**Every browser touch is marshalled onto the GTK main loop.** GTK and WebKit are not
thread-safe; calling in from the HTTP thread crashes in ways that look like unrelated
rendering bugs. `control.py` hands work to `GLib.idle_add` and blocks on a queue.

**Selectors and values are escaped for any context.** `_js_str` escapes `<`, `>`, and
the U+2028/U+2029 line separators on top of `json.dumps`, because a selector can come
from a page the agent is reading.

## Configuration

Settings live in **`~/.config/claude-browser/env`**, created with a commented
example on first run:

```ini
ANTHROPIC_API_KEY=sk-ant-...
CB_BLOCK=1
```

This file exists because a window launched from the desktop menu **does not get
your shell environment** — `~/.bashrc` is never read, so an `export` there works
for `./cb` in a terminal and silently does nothing for the menu entry. All three
entry points (`claude-browser`, `cbctl`, `cb-mcp`) read it, so `CB_PORT` and
`CB_TOKEN` stay consistent between them.

Format is `KEY=VALUE`, one per line; `#` comments, optional quotes, and a
tolerated `export ` prefix so a line pasted out of a shell profile works as-is.
Keep it `chmod 600` — it holds an API key, and the browser warns on startup if
it is readable by anyone else.

**The real environment always wins**, so `CB_BLOCK=0 ./cb` still overrides the
file for one run, and `--env-file` points at a different one.

**One environment variable beats this file, and that is the usual bug.** The
rule is "the real environment wins", which is right for `CB_BLOCK=0 ./cb` and a
trap for secrets: a stale `export ANTHROPIC_API_KEY=` left in `~/.bashrc`
silently overrides the key you just edited into the settings file. The symptom
is a browser that authenticates fine from the desktop menu — which has no shell
environment — and 401s from a terminal. The browser now prints
`config: ANTHROPIC_API_KEY from your environment overrides the one in …` at
startup when the two differ, and says so again in the 401 card.

### Credentials

Use an **API key**. That is the credential Anthropic issues for programmatic
use, and it is what `auto` tries first.

A Pro/Max subscription is *not* a general-purpose API credential — it entitles
you to Anthropic's own apps (claude.ai, Claude Code, desktop, mobile). There is
no public OAuth registration that would let this browser obtain its own
subscription token, so there is deliberately **no "Log in with Claude" button**:
building one would mean presenting Claude Code's client identity as ours, which
is impersonation. What `CB_AUTH=subscription` does instead is reuse the token
Claude Code already wrote to `~/.claude/.credentials.json`. It is your own
credential on your own machine, it often works, and it may be declined or
rate-limited at any time — that quota is shared with Claude Code, so a busy
session eats it. It is a fallback, not a foundation.

| Variable | |
|---|---|
| `ANTHROPIC_API_KEY` | enables Ask, TL;DR, Research and Command |
| `CB_AUTH` | `auto` (default: key, then subscription), `api`, `subscription` |
| `CB_PORT` | control port (default 8765) |
| `CB_TOKEN` | require this bearer token on control requests |
| `CB_HOME` | start page |
| `CB_SEARCH` | search URL template, `%s` for the query |
| `CB_THEME` | `dark` or `light`, overriding the system preference |
| `CB_GPU=off` | software rendering — often faster on old integrated GPUs |

`./cb --no-control` runs it as a plain browser with no API at all.

## Tests

```bash
python3 -m unittest discover -s tests
```

102 tests, no display or GTK bindings needed.

`test_offline.py` covers URL intent, JS escaping, SSE parsing, control routing,
the CLI and the MCP server — a stub speaks the control protocol so the
agent-facing layers run end to end.

`test_ai.py` covers the failure paths that are expensive to discover in front of
a user: retry and backoff (including `Retry-After`, and *not* retrying a 4xx),
refusals, truncated turns, missing API key, and an agent loop that stops making
progress — repeat detection, step budget, output budget, cancellation, and
malformed tool blocks.

`test_envfile.py` covers settings parsing and precedence, including the
world-readable warning and that the shipped template sets nothing.

`test_store.py` covers history, bookmarks and the pages built on them: that a
later empty title cannot erase a good one, that `retitle` does not count a
visit (`notify::title` fires several times per load, and counting each would
make one page look like five), that internal pages are never recorded, and that
hostile page titles cannot break out of the `onclick="..."` attribute their URL
is written into.

The GTK layer itself is not covered; it needs a display.
