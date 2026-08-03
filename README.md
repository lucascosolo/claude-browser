# claude-browser

A small web browser for developers, with a control API for Claude agents built in
from the start rather than bolted on.

It renders with **WebKitGTK** — the same engine that backs Safari — using the copy
already installed on your system. There is no bundled Chromium, no Electron, and no
node_modules. The whole thing is a few thousand lines of Python against the standard
library, and it idles in roughly the memory one Chrome tab uses.

```
┌──────────────────────────────────────────────────────────┐
│ ← → ⟳ ⌂ │ https://example.com    │   ☆  ＋  ☰ │   40px of chrome
├──────────────────────────────────────────────────────────┤
│                                                          │
│                     the page                             │
│                                                          │
└──────────────────────────────────────────────────────────┘
        ▲                                        ▲
   agents drive this over HTTP            Ctrl+K asks Claude
```

Working on the code? Read **`CLAUDE.md`** instead — it is the short version,
plus the constraints that are not visible from the source.

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
./install.sh                  # menu entry + icon + ~/.local/bin symlinks, no sudo
./install.sh --set-default    # ...and make it the system web browser
./install.sh --uninstall
```

It appears under **Applications ▸ Internet** as *Claude Browser*, and symlinks
`claude-browser`, `cbctl` and `cb-mcp` into `~/.local/bin`. Everything installed
lives under `$HOME`, and the app keeps running from this checkout — `git pull`
updates the installed copy too.

`--set-default` writes all four registries that have an opinion about the
default browser: `mimeapps.list`, `xdg-settings`, XFCE's `helpers.rc` (which
needs a hand-written helper entry for a browser it does not ship), and `gio`.
They do not consult each other, so setting one leaves the system disagreeing
with itself — which is the state this box was in, reporting `claude-browser`
for `x-scheme-handler/http` and `google-chrome` for `xdg-settings get`.

**Opening a link when a window is already open** hands the URL to that window
and exits, rather than starting a second browser. That is what makes it usable
as the default: `xdg-open` has no idea one is running.

Right-clicking the menu entry offers **New Window** and **New Window (no agent API)**.

## Using it

| Key | |
|---|---|
| `Ctrl+L` | focus the address bar |
| `Ctrl+F` | **find on this page** — `Enter`/`Shift+Enter` step, `Aa` matches case, `Esc` closes |
| `Ctrl+Alt+R` | **reader mode** — strip the page to its article, and back |
| `Ctrl+K` | ask Claude about the current page |
| `Ctrl+Shift+K` | **the Claude console at full height, and back** |
| `Ctrl+T` / `Ctrl+W` | new tab / close tab |
| `Ctrl+Shift+P` | **new private tab** |
| `Ctrl+D` | **bookmark this page** |
| `Ctrl+H` / `Ctrl+Shift+O` | **history / bookmarks** |
| `Ctrl+Shift+A` | **the Deck — every tab as a card** |
| `Ctrl+R`, `Alt+←/→`, `Alt+Home` | reload, back, forward, start page |
| `Ctrl+±`, `Ctrl+0` | zoom |
| `F12` | web inspector |

Everything above also lives in the **hamburger menu** at the right of the
toolbar, grouped as *Claude* / *New* / *Library* / *This page* / *Machine*, with
each shortcut printed beside its row. Reader mode and Find live under *This
page*; saved logins, playbooks, cookies & cache and settings are reachable there
too, since they have no shortcut of their own. The four Claude actions used to be
four separate icons in the toolbar; unlabelled, they were four chances to misread
a wrench or a bulleted list. The toolbar now holds only what acts on the page in
front of you — the bookmark star and a new tab.

The address bar navigates when the input looks like an address and searches
otherwise, and suggests as you type from your history and bookmarks —
bookmarks first, then by visit count with a recency bonus, and a match on the
start of a hostname outranks one buried in a title. Below those it offers
**matches in the text of pages you have already read**, marked `¶` so a
full-text hit is never mistaken for a bookmark. Tabs appear only once there
is more than one.

### Reader mode

`Ctrl+Alt+R` — Firefox's binding for the same thing, and menu → This page →
Reader mode for anyone who has not learned it — finds the article on the
page and re-renders it for reading: one column at a comfortable measure, reading
typography, no sidebars or floating newsletter boxes. It reports the word count
and an estimated reading time as it opens. Pressing it again puts you back.

The page underneath is never touched. The article is painted into an overlay
stacked on top of the real document, so toggling off is instant and costs you
nothing — not your scroll position, not a half-filled form, not a load in
flight. Stripping the live DOM would be a one-way trip on any page that
re-renders itself.

The extraction is a small heuristic — score the blocks holding prose, credit
their parents, discount heavy link density — and it fails the way every reader
mode fails, on pages whose "article" is one `<div>` of `<br>`-separated text. It
falls back to `<article>`, `<main>`, then the body rather than showing nothing.

Agents get it too: `cbctl reader` toggles it, with `--font` and `--width` to set
the type size and line measure. It answers with what it found rather than the
prose — an agent that wants the text still calls `text` or `markdown`, which
read the overlay like any other DOM.

### Finding a page you already read

Every page that finishes loading has its text kept in
`~/.local/share/claude-browser/pagetext.db`, and `cbctl recall` searches it.

```bash
./cbctl recall 'rate limit'
./cbctl recall 'webkit process model' --limit 3
```

You get ranked matches with a snippet around the hit. The same index is in front
of you as you type: from four characters on, the **address bar** offers page-text
hits under the history and bookmark matches, marked `¶`, and `cb:history`'s
search box grows a **Found in page text** section under the titles it matched.
Neither runs on the keystroke — the omnibox waits for a pause in typing and then
searches on a worker thread, because this is a disk read through an index on a
machine picked for being slow, and a late answer is discarded rather than
appended to a box that has moved on.

This is not a web search:
it only ever sees pages this browser actually loaded, which is the whole point —
*"the page about X I had open yesterday"* is a question no search engine can
answer, and it is one of the more common things to ask an agent.

It is SQLite's own FTS5, with no embedding model anywhere. A semantic index
would mean a pip dependency and a few hundred megabytes of weights to rank the
few thousand pages one person has read; BM25 over full text is the honest answer
at that scale. If your `sqlite3` was built without FTS5 the cache still works and
`recall` says so rather than the browser refusing to start.

The text of an article is stored **once per content hash**, not once per URL, so
the canonical, AMP and tracking-parameter versions of the same page cost one copy
of the prose and come back as one result. The store is capped by size (24 MB of
body text) and evicts least-recently-*used* first — revisiting a page is the
strongest evidence its text is worth keeping. Private tabs are never recorded,
by the same check that keeps them out of history.

### Its own pages

Eight internal pages on the `cb:` scheme, laid out as cards and sharing the
Claude panel's visual language. A slim icon rail moves between them.

| | |
|---|---|
| `cb:home` | Start page: one input that both searches and navigates, four quick actions, then bookmarks and recent pages as tiles. This is the default new tab (`CB_HOME` overrides it). |
| `cb:deck` | **Every open tab as a card.** A tab strip stops being navigation somewhere around the eighth tab, however well the tabs are named. The deck is the same set laid out with room for full titles. |
| `cb:bookmarks` | Everything saved, filterable. |
| `cb:history` | Grouped by day, filterable, with per-entry delete and a two-step clear. A query also searches the *text* of the pages, listed separately as **Found in page text**. |
| `cb:passwords` | **Saved logins**, plus the sites you told it never to ask about. |
| `cb:playbooks` | **Recorded sequences**, what each one does, and a Run and a two-step Delete for every one. Starting and stopping a recording lives here too, so a capture left running is visible rather than invisible. |
| `cb:data` | **Memory, swap, CPU and disk** — what the resource guard is seeing, how many tabs it has freed, how much page text is cached for `recall`, an on/off switch for the lighter-page request (`CB_LIGHT`), and two-step buttons to clear the cache, the cookies, the cached page text, or everything. |
| `cb:settings` | **Every setting in the browser**, editable. See below. |

Tiles carry a **site mark** — a letter on a colour hashed from the hostname —
rather than a favicon. Favicons would mean a network request per tile on the
machine we are optimising for, and they leak your history to every site you
have ever visited the moment you open a new tab. The mark is stable, instant
and offline.

Filtering inside these pages is done in the page, not by re-rendering from
Python: a full page load per keystroke is not a search box.

**The buttons on these pages are authenticated.** Bookmarking, deleting,
clearing storage and writing a setting all travel back to Python over a WebKit
script-message handler — and that handler is registered on the *shared*
UserContentManager, so any page in the browser could post to it. Each `cb:`
document is therefore rendered with a per-session random token that it attaches
to every message (`msg.t`) and the handler checks. It is a **script-message auth
token, not a CSP nonce** — there is no Content-Security-Policy anywhere in this
project, and reading it as one leads straight to the wrong conclusions about
what it protects. Navigation does not use messages at all: those are ordinary
`<a href>` links, so middle-click and Back behave normally.

### Themes

Chosen by name in `CB_THEME`, never by a light/dark boolean. `--theme` takes
the same three names for one run (`system` is a settings-file value only):

| | |
|---|---|
| `phosphor` | **The default.** A near-black, blue-cast HUD: square corners, hairline rules, an omnibox held between two accent brackets that light up on focus, monospaced and letterspaced chrome labels, and one static scanline gradient. Nothing in it animates and nothing repaints on a timer — a decorative frame budget is not something a two-core laptop has. |
| `dark` / `light` | Claude's warm neutrals and coral accent, exactly as they were. |
| `system` | Follow the desktop's dark/light preference. |

An **unset** `CB_THEME` now means "nobody has chosen", and the answer to that is
phosphor. Following the desktop is the explicit value `system` rather than the
absence of a value. So the old look is one line — `CB_THEME=dark`,
`CB_THEME=light` or `CB_THEME=system` in the settings file, or the Theme
drop-down on `cb:settings`, which re-colours the window and the `cb:` pages
without a restart.

The theme covers the GTK chrome, the `cb:` pages and the Claude panel from one
palette, and colour is spent on two separate axes: **`accent`** is *chrome*
state (focus, the active tab, load progress) and **`agent`** is *Claude* state
(the AI buttons, "Claude is driving this tab"). They are the same coral in
dark and light, and deliberately different in phosphor — cyan for the chrome,
amber for Claude — because the cursor Claude moves across a page is drawn into
the page and cannot be re-themed, so the one signal that must never be missed
keeps its own ink.

### Settings

`cb:settings` (menu → Machine → Settings) lists all 19 `CB_*` settings, grouped
as *Appearance*, *Privacy*, *Performance*, *Claude* and *Control API*, each with
what it does and — the part a settings page usually gets wrong — a label saying
whether a change **applies now**, on the **next page load**, or only **after a
restart**. `CB_HOME` is read once at import; `CB_PERSONA` is re-read on every
use; saying "restart to apply" over all of them would be wrong in both
directions at once. Every value is validated in Python before it is written, by
the same table the HTTP route and the CLI use, because `CB_PORT` and
`CB_MAX_TABS` are `int()`-ed at startup and a settings page that can write
`CB_PORT=banana` is a settings page that can stop the next launch. Each row that
has a line in the file also gets a **Default** button, which removes the line
rather than writing the default back.

Your API key is *not* editable here — it lives in the same file, and a setter
that could write it would be a route from an API call to your credential.

The same thing from a shell:

```bash
./cbctl settings                        # every setting, its value and where it came from
./cbctl settings CB_THEME dark          # change one
./cbctl settings CB_THEME --reset       # drop the line, back to the built-in default
```

Every reply carries the full table back, so a write shows you the state it
landed in, along with a note about when it takes effect. Naming a key with no
value is a *write* of the empty value, not a read of that key — read them all
and pick the row out.

It is deliberately **not** an MCP tool, for a stronger version of the reason
`clear` and `persona` are not: these are your preferences about your own
browser, several of them decide what is sent to Anthropic and what is stripped
first, and one of them is the token guarding the control API. An agent driving
the browser has no business rewriting the rules it is being driven under. (A
read never returns the token's value either — only whether one is set.)

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
Answers are rendered markdown: headings, lists, tables, quotes and code blocks,
with links opening in a new tab rather than navigating the console away. Every
run says which credential it used and, on failure, why.

The console is docked, not fullscreen. **Drag the divider** above it to resize,
or `Ctrl+Shift+K` for full height and back. It remembers the height you left it
at, and shrinks itself rather than squeezing the page out of a short window.

| | | |
|---|---|---|
| `Ctrl+K` | **Ask** | Question about the current page. |
| `Ctrl+Shift+S` | **TL;DR** | Summarize this page. A button, never automatic — a request per page load would be slow and costs money on pages you never read. |
| `Ctrl+Shift+R` | **Research** | Reads *every open tab* and synthesizes across them. Leads with a table when they're comparable. |
| `Ctrl+Shift+K` | **Full height** | Grow the console to the whole window, and back. The divider above it resizes by hand. |
| `Ctrl+G` | **Command** | Give Claude a goal and it drives the browser — navigating, reading, clicking — in the window you're watching, on your own logged-in session. `Stop` cancels mid-run. |

**Personas.** A drop-down beside the mode pills switches how the panel answers:
*Developer* leads with the command or identifier and quotes code verbatim,
*Researcher* separates what the page states from what it implies and says what
evidence a claim rests on, *Critic* leads with the weakest part of the argument,
*Translator* answers in your language and gives the original wording alongside
its rendering. *No persona* is the default.

A persona is added to the panel's own instructions, never a replacement for
them — answers stay grounded in the page's text either way. The choice is
remembered in `~/.config/claude-browser/env` as `CB_PERSONA`, and
`cbctl persona` reports it (`cbctl persona critic` switches). It applies to Ask,
TL;DR and Research; Command is a tool-driving loop, not an answering style, so
it is left alone.

The command bar is the control API turned inward: the same navigate/read/click
primitives an external agent gets over HTTP, handed to a tool-use loop inside
the browser. It won't submit forms or change account state unless the goal
plainly asks for it.

### When Claude is driving

Claude moving a page you are watching is unnerving if nothing says why, so it is
always visible:

- the **tab** being driven carries an accent ring
- the **window** carries an accent frame while that tab is the one on screen
- a **cursor** travels across the page to whatever is about to be acted on,
  scrolls it into view, and dips as it presses
- every synthetic click or field write draws a **halo** at the point of action

The marker is set in the one place every tab-targeted agent call passes through,
so it cannot be forgotten by a new code path.

An unpaced step lands in a few milliseconds — the page jumps and the answer
appears, with nothing in between to watch — so the agent loop pauses briefly
between steps to let the cursor be seen arriving and pressing. It costs well
under a second per acting step. `CB_PACE=0` removes the pauses entirely for an
unattended run; a number above 1 slows it down for a demo (clamped at 5). The
pauses are slept on the agent's own worker thread, never on the UI's, so the
window stays responsive throughout.

## Saved logins

Sign in to a site and the browser offers to save the login; come back and it
fills it in. Everything lives at `cb:passwords`, where you can reveal, delete,
or lift a "never ask here".

**Where the passwords are.** In your system keyring, over the freedesktop Secret
Service — `gnome-keyring` on this desktop, already unlocked by PAM at login and
already inspectable with `seahorse`. Not in a file this project invents. A
password file of our own would need a master key, and the only place to put a
master key is another file next to it, which is not encryption; it is
obfuscation with extra steps. One keyring item per `(origin, username)`, where
the origin is `scheme://host[:port]` — never a path, because a password belongs
to a site rather than a page, and never a bare hostname, because `http://` and
`https://` are different security origins.

**Why not Google Password Manager.** It is not a service other browsers can talk
to. The autofill half lives inside Chrome and Android; the sync half rides Chrome
Sync, whose API is gated behind client credentials Google issues to Chrome builds
and documents for nobody else. There is no endpoint, no extension point and no
file on disk to read, so "use Google's password manager here" is not a thing that
can be built — only a thing that can be faked badly. `passwords.google.com`
renders fine in a tab if you want to look one up by hand.

Signing *in* to a Google account is a different question and works: Google serves
this browser the real sign-in form, not the "this browser may not be secure"
interstitial.

**How autofill decides.** Filling is driven from the browser side against the
URL the WebView actually has; the script injected into the page only reports a
credential it captured and can never ask for one. A page cannot request a
password, and it is never filled into a field that already has something in it.
With two or more accounts saved for a site nothing is filled at all — picking one
for you and picking wrong signs you into the other account without saying so.

Nothing is written from a private tab.

## Driving it from an agent

The browser serves a JSON API on `127.0.0.1:8765` — loopback only, since it can read
any page you are signed into. Set `CB_TOKEN` to require a bearer token as well.

### As MCP tools (Claude Code)

```bash
claude mcp add -s user browser -- /path/to/claude-browser/cb-mcp
```

That registers `browser_open`, `browser_text`, `browser_markdown`, `browser_links`,
`browser_find`, `browser_click`, `browser_fill`, `browser_eval`, `browser_console`,
`browser_screenshot`, `browser_reader`, `browser_recall`, `browser_playbook-run`,
and the navigation tools — 26 in all, generated from the same table as the HTTP
routes, so the two cannot disagree.

**You do not need to start the browser first.** The MCP server launches it on
the first tool call and waits for it to come up (`CB_AUTOSTART=0` opts out).

### From the shell

```bash
./cbctl open https://docs.example.com
./cbctl text                                  # readable text, JSON
./cbctl markdown                              # headings, links and code preserved
./cbctl links | jq -r '.links[].href'
./cbctl find 'rate limit'
./cbctl fill '#search' 'webkit' && ./cbctl click 'button[type=submit]'
./cbctl reader                                # strip the page to its article
./cbctl recall 'rate limit'                   # search pages already read
./cbctl console --pattern 'MyApp'             # console output + uncaught errors
./cbctl shot /tmp/page.png
./cbctl settings                              # read them all; add KEY VALUE to change one
```

`cbctl` exits non-zero when the browser reports a failure, so `cbctl click .go &&
cbctl text` does the right thing.

### Playbooks: record a sequence, replay it later

A sequence worth repeating — open the dashboard, sign in, click through to the
report — can be recorded once and replayed by name.

```bash
./cbctl playbook-record start morning     # everything from here is captured
./cbctl open https://dash.example.com
./cbctl click '#reports'
./cbctl playbook-record stop              # saves it as "morning"

./cbctl playbook-list
./cbctl playbook-run morning
./cbctl playbook-delete morning
```

The same four things are on **`cb:playbooks`** (menu → Library → Playbooks):
what is saved and what each one does, a Run and a two-step Delete per playbook,
and the start/stop of a recording with the number of steps captured so far.

Recording watches the control API, so it captures whatever drives it — `cbctl`,
the MCP tools, or a raw `curl` — and *not* browsing by hand, which is why the
page says so where you start one. Failed operations are left out, and tab ids are
never recorded: every step replays against the focused tab, so a playbook still
works tomorrow.

**Credentials are never recorded.** A `fill` into a password, OTP or API-key
field is dropped at capture time rather than written to disk and hidden later,
and the reply says how many were skipped. On replay the browser's own autofill
supplies them, which is the only path here allowed to hold a secret.

Playbooks live in `~/.local/share/claude-browser/playbooks.json` — plain JSON, so
you can read, edit, diff and copy them. Replay validates every step against the
API registry before running any of them, and the page loads among them go into
the same one-at-a-time queue as every other API-initiated load, so a six-page
playbook is six queued loads rather than six simultaneous ones.

### Over HTTP

```bash
curl -s 127.0.0.1:8765/text | jq -r .result.text
curl -s 127.0.0.1:8765/open -d '{"url":"https://example.com"}'
```

| Route | | |
|---|---|---|
| `/tabs` `/health` | GET | what is open |
| `/present` | POST | raise the window |
| `/open` `/navigate` `/back` `/forward` `/reload` `/close` `/wait` | POST | move around |
| `/text` `/markdown` `/links` `/html` `/find` | GET | read the page |
| `/click` `/fill` `/eval` | POST | act on the page |
| `/reader` | POST | strip the page to its article, and back |
| `/recall` | GET | search the text of pages already read |
| `/console` `/screenshot` | GET | debug the page |
| `/playbook/record` `/playbook/run` `/playbook/delete` | POST | record and replay a sequence |
| `/playbook/list` | GET | what is saved |
| `/persona` `/settings` | POST | report or change the user's own preferences — neither is an MCP tool |

Navigation routes block until the load finishes, so an agent can `open` then `text`
without polling. Pass `wait=false` to return immediately. Every route takes an
optional `tab` id and defaults to the focused tab.

## Performance on slow hardware

Developed against a Celeron N3060 — two cores at 1.6GHz, 4GB of RAM, swap in
use. What's done about it:

- **Ad/tracker blocking is on by default** (`CB_BLOCK=0` disables it). 82 rules
  compiled into WebKit's native content-blocker bytecode and cached on disk.
- **Sites are asked for a lighter page** (`CB_LIGHT=0` disables it). Every page
  the browser loads for you carries `Save-Data: on`, the standard client hint
  that Cloudflare, Akamai, Google's transcoders and a good few CMSes read as
  "send smaller images, fewer fonts, a lighter template". It rides on the pages
  this browser navigates to, not on the files a page fetches for itself:
  WebKitGTK's UI process only gets told a subresource request *was* sent, so
  there is nowhere to add a header to it. The same switch asks for reduced
  motion, which is the one media-feature preference this WebKit lets an app
  assert — pages that respect it skip their animations, and animation is
  per-frame work on the same two cores laying the page out.
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

### The resource guard

The above is tuning. This is a brake, and it exists because of one incident: an
agent was asked to research something, opened five tabs as fast as the API would
take them, and the laptop thrashed for twenty minutes until it was
power-cycled. Nothing in it was a bug — every step was reasonable, and there was
simply nothing anywhere that said *no*.

Four things now say no. All of them are in
[`claudebrowser/resources.py`](claudebrowser/resources.py), which is GTK-free so
the policy can be tested without a display.

- **Page loads are queued, never parallel.** One or two run at a time and the
  rest wait their turn in order. This is the one that fixes it: it was the
  *simultaneity* that killed the machine, not the tab count — five pages loading
  together peak their memory in the same second, where five in a row peak one at
  a time and each releases before the next begins.
- **Idle background tabs are dropped when memory gets tight**, least recently
  used first, and reload when you come back to them. Never the tab you are
  looking at, never one mid-load, and never a private tab — a private tab is not
  written down anywhere, so discarding it is not a discard, it is a close.
- **An agent gets a tab ceiling** (`CB_MAX_TABS`, default 10, lowered
  automatically on a struggling machine) and a refusal that says what to do
  about it. You never hit it: `Ctrl+T` always works, because a person opening a
  tab has looked at the screen and an agent has not.
- **WebKit's content processes are reniced below the UI.** This is the one that
  turns "unusable" into "slow": thrashing is survivable if you can still move
  the mouse and close a tab, and what forced the power cycle was the desktop
  losing every scheduling contest to the page renderers.

Two things it deliberately does **not** do, both learned by running it on the
machine it was written for:

- **CPU load never refuses anything.** A laptop with a couple of agents and a
  Chrome open sits at a load average of ten all day. The first version refused
  on CPU pressure and would have meant a browser that never opened a tab again.
  Load average is also not a clean CPU signal on Linux — it counts uninterruptible
  sleep, which is most of what a thrashing machine is doing, so half of what
  looks like CPU pressure during a freeze is the memory pressure counted twice.
- **Swap *occupancy* is not treated as pressure.** A machine with a few days
  uptime sits at 70–80% swap used permanently, because pages evicted last week
  and never touched again still count. Reading that as pressure made the browser
  discard every background tab it had, repeatedly, on a healthy machine. The
  live signal is the swap-in *rate* from `/proc/vmstat`.

`./cbctl machine` prints what it is seeing; `cb:data` shows the same thing with
bars. An agent that gets refused can call `browser_machine` and find out whether
to wait, discard a tab, or do something else.

Three APIs here exist, are documented, and do nothing in WebKitGTK 2.52:
`set_process_model`, `set_enable_hyperlink_auditing`, and `innerText` on a
detached node. `hasattr()` cannot tell you that — only running it can.

**No speedup figure is quoted, deliberately.** `tools/bench.py` measures load
time with and without the blocker, but on this hardware it cannot produce a
trustworthy number — the same URL took 2.9s in one run and timed out at 90s in
another, and swap pressure alone exceeds the effect. The blocker demonstrably
loads and the process sharing is measured above; the rest is unquantified.

Python is not the bottleneck, which was worth checking: during a load the CPU is
entirely `WebKitWebProcess` and `WebKitNetworkProcess`, and the interpreter costs
8.8MB of RSS against WebKit's ~350MB. Startup (~2.9s) is nearly all GObject
typelib loading. A Rust rewrite via Tauri would use this same engine.

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

**Every browser touch is marshalled onto the GTK main loop.** GTK and WebKit are
not thread-safe. `control.on_main_loop` is the single bridge.

**Selectors and values are escaped for any context** — they can come from a page
the agent is reading. See `extract._js_str`.

**One table describes the API.** `claudebrowser/api.py` generates the HTTP
routes, `cbctl`'s subcommands and `cb-mcp`'s tools, so the three cannot drift.

## Configuration

Settings live in **`~/.config/claude-browser/env`**, created with a commented
example on first run:

```ini
ANTHROPIC_API_KEY=sk-ant-...
CB_BLOCK=1
```

Every one of them is editable from **`cb:settings`** or `cbctl settings` — the
table below is what they mean; that page is where you change them.

| Setting | |
|---|---|
| `CB_BLOCK` | `0` turns the ad/tracker blocker off for a session. |
| `CB_COOKIES` | `nothird` (default), `all`, or `none`. Third-party cookies are rejected by default; the ones this drops are mostly attached to loads the blocker already refuses. |
| `CB_ITP` | `0` turns off tracking prevention. |
| `CB_LIGHT` | On by default: asks sites for a cheaper page. Sends `Save-Data: on` with every page the browser loads for you, and asks for reduced motion so pages skip animations. `0`/`off` if a site serves you a stripped-down version you did not want; there is a switch for it on `cb:data`. The header follows on the next page load, but reduced motion cannot — WebKit reads that once, before the first tab exists, so that half waits for a restart. |
| `CB_MAX_TABS` | The ceiling on tabs an *agent* may open, default 10. Never applies to you. |
| `CB_MEM_LIMIT` | MB per web process before WebKit starts shedding caches, default 512. |
| `CB_PERSONA` | How the Claude panel answers: `off` (default), `developer`, `researcher`, `critic`, `translator`. The panel's own selector writes this line. |
| `CB_PACE` | How slowly the agent moves so you can follow it. `1` (default), `0`/`off` for no pauses, up to `5` to slow it down. |
| `CB_SCRUB` | `0`/`off` sends page text to Claude exactly as it appears. On by default: email addresses, phone numbers, card and IBAN numbers, SSNs and account numbers are replaced with `[email]`, `[card]` and friends before anything leaves the machine, and the panel says how many of each it removed. |
| `CB_THEME` | `phosphor` (the default when nothing is set), `dark`, `light`, or `system` to follow the desktop. |
| `CB_HOME`, `CB_SEARCH`, `CB_PORT`, `CB_TOKEN`, `CB_GPU`, `CB_WEBGL`, `CB_URL`, `CB_AUTOSTART` | as before. |

Cookies, the disk cache and per-site storage live in
`~/.local/share/claude-browser` and `~/.cache/claude-browser`, and **persist
across restarts** — you stay signed in. `cb:data` shows how much is there and
clears any of it; `cbctl clear cookies|cache|storage|pagetext|all` does the same from a
shell. Clearing is deliberately *not* an MCP tool: signing you out of every site
you use is not a step an agent should be able to take in pursuit of some other
goal.

This file exists because a window launched from the desktop menu **does not get
your shell environment** — `~/.bashrc` is never read, so an `export` there works
for `./cb` in a terminal and silently does nothing for the menu entry. All three
entry points (`claude-browser`, `cbctl`, `cb-mcp`) read it, so `CB_PORT` and
`CB_TOKEN` stay consistent between them.

Format is `KEY=VALUE`, one per line; `#` comments, optional quotes, and a
tolerated `export ` prefix so a line pasted out of a shell profile works as-is.
Keep it `chmod 600` — it holds an API key, and the browser warns on startup if
it is readable by anyone else.

**This file wins over the environment.** Anything you set here beats a variable
exported in a shell, so the browser behaves identically from a terminal, the
menu and the dock. `--env-file` points at a different one.

It used to be the other way around, and that was a bug factory: a stale
`export ANTHROPIC_API_KEY=` in `~/.bashrc` silently overrode the key you had
just edited in here, and an exported variable cannot be fixed from a file — the
shell keeps handing the dead value to everything it spawns until you close it.
The browser now ignores it and says so.

**The API key never enters the process environment.** It is read from the file
at the moment a request is made, so it cannot be shadowed, and it is not handed
to the control-API server or any other child process. Editing it takes effect on
the next request with no restart, and a rejected-key card names the file it came
from. (The full reasoning is in `envfile.py`.)

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
| `CB_THEME` | `phosphor` (default), `dark`, `light`, or `system` to follow the desktop |
| `CB_GPU=off` | software rendering — often faster on old integrated GPUs |

`./cb --no-control` runs it as a plain browser with no API at all.

## Tests

```bash
CB_AUTOSTART=0 python3 -m unittest discover -s tests
```

576 tests, about 9 seconds, no display needed. `CB_AUTOSTART=0` matters:
`test_offline.py` runs `cbctl` and `cb-mcp` as real subprocesses, and those
launch the browser on demand unless told not to.

Nothing here needs a screen, a network, or your real `~/.config/claude-browser`.
Two files use the real GTK/WebKit bindings anyway, because a stand-in could
drift from the type the library actually gets handed: `test_light.py` builds a
real `WebKitURIRequest` to check the client hints ride on it, and
`test_style.py` runs every theme's sheet through a real `Gtk.CssProvider`. Both
work headless — neither is a widget and neither touches the screen.

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

`test_pagetext.py` covers the page-text cache: that two URLs for the same
article share one body and come back as one search hit, that eviction is LRU
over a byte cap and only drops a body once nothing points at it, that whatever
the user types is quoted into a valid FTS5 query, and that everything still
works when sqlite3 has no FTS5 at all.

`test_scrub.py` covers the outbound redaction: one test per pattern, that a
card-shaped order number is rejected by the Luhn check and a mistyped IBAN by
mod-97, that the placeholders and counts are stable, that `CB_SCRUB=0` turns it
off — and a list of ordinary prose (version strings, commit hashes, coordinates,
numeric tables) that must come back untouched, because a scrubber with false
positives rewrites the page out from under the answer.

`test_light.py` covers `CB_LIGHT`: the spellings that turn it off, that a typo
leaves it on, that the request handed to WebKit really carries `Save-Data: on`
and nothing higher-entropy than that, and that `cb:data` says which way it is
set. The signal wiring is not covered and cannot be — it needs a display.

`test_reader.py` covers reader-mode option clamping, the reading-time estimate,
and that the overlay never rewrites the page's own DOM; `test_pacing.py` covers
`CB_PACE` parsing and clamping, the per-step time budget, and that the cursor
the agent moves is inert and unreadable as page content.

`test_tabnames.py` covers tab labelling, which is mostly a question about
collisions: same title on different hosts, same title on the same host, and a
suffix that would only repeat the name.

`test_style.py` and `test_pages_style.py` cover the themes: that every token
exists in all three palettes, that text, rules and each of accent/agent/ok/warn
clear their contrast floor on every surface they are painted on (computed, not
eyeballed), that phosphor's texture is a static gradient with no animation and
no keyframes, that the phosphor sheet is purely additive so dark and light come
out exactly as they did before it existed, and — the reason the file exists — that the
GTK sheet parses without a single error, since GTK3 drops what it cannot
understand and says so only as a startup warning on stderr.

`test_settings.py` covers the settings table: that every value it lets through
is one the code reading that variable understands, that the values it refuses
are refused with a sentence a person can act on, that "Default" removes the
line rather than writing the default back, and that a setting's "when does this
land" label matches what the code actually does with it.

The suite is clean under `-W error::ResourceWarning`, which is worth keeping:
that is what caught `store` and `pagetext` shutting their writer threads down
without closing a single sqlite connection.

The GTK layer itself is not covered; it needs a display.
