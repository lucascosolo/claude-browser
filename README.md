# claude-browser

A small web browser for developers, with a control API for Claude agents built in
from the start rather than bolted on.

It renders with **WebKitGTK** — the same engine that backs Safari — using the copy
already installed on your system. There is no bundled Chromium, no Electron, and no
node_modules. The whole thing is about 1,200 lines of Python against the standard
library, and it idles in roughly the memory one Chrome tab uses.

```
┌──────────────────────────────────────────────────────────┐
│  ←  →  ⟳   │ https://example.com          │   ✦    ＋    │   40px of chrome
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

## Using it

| Key | |
|---|---|
| `Ctrl+L` | focus the address bar |
| `Ctrl+K` | ask Claude about the current page |
| `Ctrl+T` / `Ctrl+W` | new tab / close tab |
| `Ctrl+R`, `Alt+←/→` | reload, back, forward |
| `Ctrl+±`, `Ctrl+0` | zoom |
| `F12` | web inspector |

The address bar navigates when the input looks like an address and searches
otherwise. Tabs appear only once there is more than one.

**Ask Claude** (`Ctrl+K`) extracts the readable text of the page, sends it with your
question, and streams the answer into a panel at the bottom. It needs
`ANTHROPIC_API_KEY` in the environment; everything else in the browser works
without one.

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

| Variable | |
|---|---|
| `ANTHROPIC_API_KEY` | enables Ask Claude |
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

28 tests covering URL intent, JS escaping, SSE parsing, control routing, the CLI, and
the MCP server. They run without a display or the GTK bindings — the stub in
`tests/test_offline.py` speaks the control protocol so the agent-facing layers are
exercised end to end. The GTK layer itself is not covered by them; it needs a display.
