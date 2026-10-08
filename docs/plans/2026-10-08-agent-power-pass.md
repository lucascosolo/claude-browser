# Agent power pass — plan

Written 2026-10-08. Goal: make Claude Browser a stronger tool for Claude
sessions driving it over MCP and `cbctl`. Every new operation is added in
`claudebrowser/api.py` once, so `cbctl`, `cb-mcp` and the HTTP routes pick it
up; nothing is added to `agent.py`'s in-browser tool list (out of scope).

Approved scope (user, 2026-10-08): all six chunks below plus self-update.

## Conventions every chunk follows

- Read `CLAUDE.md` first. The rules that bite here: ops live in `api.py`;
  never touch GTK/WebKit off the main loop; selectors and values are
  attacker-adjacent and go through `extract._js_str`; a private tab never
  writes a server-named or page-derived file to disk; privacy only travels
  downhill.
- Every `api_*` method takes a trailing `done` and calls it exactly once with
  a JSON-serializable dict carrying `ok`. Tab-targeted ones use `@needs_tab`.
- `tests/test_offline.py::test_every_op_builds_a_call` enumerates every
  route; a new op must be added to its `cases` dict or the suite fails.
- `browser.py`, `control.py`, `findbar.py`, `__main__.py` need a display and
  are never imported by tests; `python3 -m py_compile claudebrowser/*.py` is
  their only gate. Put every piece of logic that can be GTK-free in a GTK-free
  module (`extract.py` for JS builders, a new module for anything else) so it
  is tested.
- Verification for every chunk:
  `python3 -m py_compile claudebrowser/*.py && CB_AUTOSTART=0 python3 -m unittest discover -s tests`
  (must end in `OK`; 917 tests at the start of this plan).
- A new setting is added in `settings.py` **and** `tests/test_settings.EVERY_KEY`.
- Commit per chunk with a message that explains why, not what.

## Chunk 0 — self-update and `restart` (coordinator, done inline)

The installed launcher is a symlink into this checkout, so code is live on
the next launch. What is missing is restarting the running window from inside
the project, because an agent session cannot reach the port or the process.

- New GTK-free module `claudebrowser/update.py`: `head_revision(repo)` reads
  `.git/HEAD` and resolves the ref (loose ref file or `packed-refs`) with no
  subprocess; `Watcher` remembers the revision it started on and answers
  `changed()` with the new short hash or `None`; `idle(state)` decides whether
  a restart is safe from a plain dict (`loading`, `agent_running`,
  `recording`, `downloads_active`, `seconds_since_request`,
  `seconds_since_input`).
- `browser.py`: a `GLib.timeout_add_seconds(UPDATE_POLL_S)` poll; when the
  revision changed and the browser is idle, flash "Updated to <hash> —
  restarting", save the session and restart. Restart = `Gtk.main_quit()` with
  `browser.restart_requested = True`; `__main__` stops the control server and
  `os.execv`s `<repo>/cb` with `CB_IN_SCOPE=1` kept so the process stays in
  its existing systemd scope. Tabs come back through `CB_RESTORE_SESSION`.
- New op `restart` (POST `/restart`, `mcp=False`): restart now regardless of
  idleness, for a person at `cbctl`. Answers `{"ok": true, "restarting": true}`
  before quitting (from an idle).
- Setting `CB_AUTOUPDATE` (bool, default `1`, section Agent): "Restart on a
  new commit". Effect text: read on every poll.
- `control.py` records `last_request_at` (monotonic) so idleness can be read.

## Chunk 1 — `wait-for`, `scroll`, scoped reads, `tables`

- `wait-for` (POST `/wait/for`): params `selector` (optional; element
  present and visible), `text` (optional; regex over `document.body.innerText`),
  `url` (optional; regex over `location.href`), `gone` (bool: invert
  `selector`, wait until it is absent), `timeout` (seconds, default 20, max
  120). At least one condition required. Polls every 250 ms by evaluating one
  JS predicate built in `extract.wait_predicate(...)`; answers
  `{"ok": true, "matched": "selector"|"text"|"url", "elapsed_ms": n}` or
  `{"ok": false, "error": "timed out after Ns waiting for ..."}`. A load in
  progress is not an error: the predicate simply keeps polling. Op timeout
  must exceed the max wait (set 130).
- `scroll` (POST `/scroll`): params `to` (optional: CSS selector, `@ref`,
  `top`, `bottom`), `by` (optional integer px, signed), `tab`. Exactly one of
  `to`/`by`. Scrolls the window (or, when `to` is an element inside a
  scrollable container, `scrollIntoView`). Answers `{"ok": true, "x", "y",
  "height", "viewport", "at_bottom": bool}`. Moves the halo cursor to the
  element when `to` names one.
- Scoped reads: `text`, `markdown`, `html`, `find`, `links` gain an optional
  `selector` param (CSS or `@ref`). `extract.TEXT`/`MARKDOWN`/`LINKS`/`HTML`
  stay as module constants (agent.py reads them); add `extract.text(selector)`
  etc. that wrap the same walkers rooted at the match, answering
  `{"ok": false, "error": "no match"}` when the selector finds nothing. `api.py`
  dispatches through a new `_js_scoped(name)` builder that uses the constant
  when no selector is given, so existing callers are byte-for-byte unchanged.
- `tables` (GET `/tables`): param `selector` (optional, default every
  `<table>`), `limit` rows per table (default 200). Returns
  `{"ok": true, "tables": [{"caption", "headers": [...], "rows": [[...]]}]}`;
  header cells from `<thead>` or the first row of `<th>`; cell text is
  `innerText` collapsed; `rowspan`/`colspan` are not expanded (document it).
- Tests: `tests/test_extract_reads.py` (new) for the JS builders: escaping,
  the predicate for each condition, scoped reads fall back to the constants,
  `tables` snippet shape; `test_offline` cases for the four routes.

## Chunk 2 — `press`, `type`, `select`, `hover`, `submit`

All JS builders in `extract.py`, each returning the `delta()` block, each
driving the halo cursor like `click` does.

- `press` (POST `/press`): params `key` (required: a combo like `Enter`,
  `Escape`, `Tab`, `Shift+Tab`, `Ctrl+K`, `ArrowDown`, `a`), `selector`
  (optional target to focus first, else `document.activeElement`). Dispatches
  `keydown`, `keypress` (printable only), `keyup` with correct `key`, `code`
  and modifier flags, then applies the default action browsers would have
  applied if no handler called `preventDefault()`: `Enter` on a field inside a
  form → `form.requestSubmit()` (or `.submit()` fallback), `Enter` on a
  button/link → `.click()`, `Tab`/`Shift+Tab` → focus the next/previous
  tabbable element, `Escape` → blur. `extract.parse_key(combo)` is a
  GTK-free Python function returning `{"key", "code", "ctrl", "shift", "alt",
  "meta"}` and raising `ValueError` on nonsense.
- `type` (POST `/type`): params `text` (required), `selector` (optional).
  Inserts via `document.execCommand("insertText")` when supported (works in
  `contenteditable` and inputs, fires real `input` events), falling back to
  appending to `.value` plus `input`/`change`. Per-character key events are
  not simulated (document why: cost, and `insertText` already fires the
  events frameworks listen to). Answers `{"ok": true, "length": n, ...delta}`.
- `select` (POST `/select`): params `selector` (required), `value` (optional:
  option value or visible label; for a `<select multiple>` a JSON array
  string), `checked` (optional bool for checkbox/radio). Fires `input` and
  `change`. Answers the resulting `value`/`checked`, and for a `<select>`
  with no match the list of its option labels (capped at 50) so the next
  call can succeed.
- `hover` (POST `/hover`): param `selector`. Dispatches `pointerover`,
  `pointerenter`, `mouseover`, `mouseenter`, `mousemove` at the element's
  centre and moves the halo cursor there without pressing.
- `submit` (POST `/submit`): param `selector` (optional: a form or a field
  inside one; default the first form). `requestSubmit()` so validation and
  `submit` handlers run; then, from Python, wait up to `LOAD_TIMEOUT` for a
  load that *started within 1 s* of the submit (reuse `_await_load`), else
  answer immediately with the delta. Answers `{"ok", "navigated": bool,
  ...tab.info()}`.
- Tests: `tests/test_extract_keys.py` (new): `parse_key` table, every
  builder is a single expression (reuse the check in
  `test_offline.test_snippets_are_single_expressions`), escaping of values,
  default-action branches present for Enter/Tab/Escape; `test_offline` cases.

## Chunk 3 — dialogs, `upload`, `download`

- Dialogs. Connect `script-dialog` on every tab view. Policy setting
  `CB_DIALOGS` (choice `auto`|`ask`, default `auto`; section Agent). `auto`:
  alert → close; confirm → accept; prompt → accept its default text;
  before-unload → accept (leave). `ask`: return False so WebKit shows its
  own dialog. Every dialog, under either policy, is appended to a per-tab
  ring buffer (last 50) as `{"kind", "message", "default", "answered",
  "at"}` (ISO-8601 UTC). New op `dialogs` (GET `/dialogs`): params `clear`
  (bool). Private tabs are included (the log lives in memory only and is
  per tab). The ring buffer and its entry shape live in a GTK-free module
  `claudebrowser/dialogs.py` so they are tested.
- `upload` (POST `/upload`): params `selector` (required: a `file` input or
  `@ref`), `path` (required; absolute, or a JSON array string of absolute
  paths for `multiple`). Checks each path is a readable regular file
  *before* touching the page. Registers the file list as pending on the tab,
  then clicks the input from JS; the `run-file-chooser` handler, when a
  pending list exists for that view, calls `request.select_files(paths)` and
  clears it; with nothing pending it returns False so the GTK chooser still
  appears for a human click. The pending entry expires after 10 s. Answers
  `{"ok": true, "files": [names], ...delta}` once the input's `files.length`
  matches, else an error.
- `download` (POST `/download`): params `url` (required), `path` (required:
  absolute destination file), `overwrite` (bool, default false). Refused for
  a private tab unless `CB_PRIVATE_DOWNLOADS` allows, same as
  `_on_download`. Uses `view.download_uri(url)` so the tab's cookies apply;
  the download record is pre-registered by URL so `_dl_decide_destination`
  sets the destination directly instead of showing the offer row. Waits for
  `finished`/`failed` (op timeout 300) and answers `{"ok": true, "path",
  "bytes", "mime"}`; a failure answers with WebKit's message. Also appends to
  `download_history` so `downloads` lists it.
- Tests: `tests/test_dialogs.py` for the ring buffer and policy table;
  `tests/test_uploads.py` for the path checks (use `tmp_path`-style temp
  dirs only); `test_offline` cases for the three routes.

## Chunk 4 — `network`, screenshot modes, `pdf`, `wait-for idle`

- Network log. Connect `resource-load-started` per view; on each
  `WebResource` connect `notify::response`, `finished`, `failed`, and
  `received-data` (bytes). Entries `{"url", "status", "mime", "bytes",
  "started_ms", "duration_ms", "error", "main": bool}` in a per-tab ring
  buffer (last 300) that resets on a main-frame `COMMITTED`. GTK-free
  bookkeeping in `claudebrowser/netlog.py` (the ring, the entry shape, the
  regex filter, the quiet-period computation). Op `network` (GET
  `/network`): params `pattern` (regex over URL), `limit` (default 100),
  `clear` (bool). Private tabs: logged in memory like console, never on disk.
- `wait-for` gains `idle` (bool): true when the tab is not loading and no
  resource started or finished in the last `quiet_ms` (param, default 500).
  Implemented against `netlog`'s last-activity stamp.
- `screenshot` gains `full` (bool → `SnapshotRegion.FULL_DOCUMENT`) and
  `selector` (full-document snapshot cropped to the element's rect, read via
  JS `getBoundingClientRect` plus scroll offsets, using cairo). The private-tab
  path rule is unchanged.
- `pdf` (POST `/pdf`): param `path` (required, absolute, `.pdf`). Uses
  `WebKit2.PrintOperation` with `Gtk.PrintSettings` `output-file-format=pdf`
  and `output-uri=file://...`, `print_()`, answer on `finished`/`failed`.
  Refused for a private tab (same reason as a screenshot path).
- Tests: `tests/test_netlog.py`; `test_offline` cases for `/network`,
  `/pdf`, and the new screenshot params.

## Chunk 5 — playbook parameters, scroll restore on discard

- Parameters. A step's string parameter may contain `{param:NAME}`
  (`NAME` matches `[A-Za-z_][A-Za-z0-9_]*`). `playbooks.params_of(steps)`
  lists the names a playbook needs; `playbooks.substitute(steps, values)`
  returns new steps with every placeholder replaced and raises
  `PlaybookError` naming the first missing parameter. `playbook-run` gains
  `params` (JSON object string, optional); substitution happens *after*
  `validate` and *before* any step runs, and a missing parameter refuses the
  whole run. `playbook-list` reports `"params": [...]` per playbook. The
  recorder is unchanged (a person edits the JSON file, or records with a
  literal and replaces it); say so in the docstring. A `{profile:...}`
  placeholder is not a param and passes through untouched.
- Scroll restore. `discard_tab` first evaluates `window.scrollY` on the view
  and stores it in `tab.scroll` (the dead field), then continues the discard
  from the callback; it still returns `True` synchronously meaning "discard
  begun". `restore_tab` registers a one-shot waiter that, on the load's
  `FINISHED`, evaluates `window.scrollTo(0, y)` when `y > 0`. The value is
  never written to disk.
- Tests: `tests/test_playbooks.py` additions for `params_of`, `substitute`,
  missing-param refusal, profile placeholders untouched, and validation still
  rejecting unknown ops after substitution.

## Chunk 6 — docs (coordinator)

README: the MCP tool list and count; a short section per new capability.
CLAUDE.md: layout lines for the new modules, rules worth keeping (dialog
policy, the pending-upload handshake, the restart path and why `CB_IN_SCOPE`
is kept). `suggestions.md`: remove the two built items. The `web-browsing`
skill under `~/.agents/skills/` cannot be written from a sandboxed session;
hand the user the diff to apply.

## Order and dispatch

0 (inline) → 1 → 2 → 3 → 4 → 5 → 6. Sequential, because every chunk edits
`api.py`, `browser.py` and `test_offline.py`. Chunks 1, 2, 5: `test-author`
then `implementer`. Chunks 3, 4: `deep-implementer` (WebKit signal work with
no test coverage possible on the GTK half). Ledger:
`docs/plans/2026-10-08-agent-power-pass.ledger.md` (git-ignored).
