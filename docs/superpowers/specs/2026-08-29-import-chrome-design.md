# Import Chrome — Design

**Goal:** a one-shot, user-triggered import of bookmarks, history and saved
passwords from the local Google Chrome profile into Claude Browser's existing
storage (`store.py`'s bookmarks/history tables, `passwords.py`'s keyring-backed
Vault). Never agent-invocable.

**Why this exists:** the user is moving to Claude Browser as their daily
driver and does not want to manually re-save every bookmark, re-type every
password, or lose their browsing history in the move. A raw ad hoc read of
Chrome's Secret Service key was blocked by Claude Code's own permission
classifier during scoping, which is the right call for an exploratory shell
command — this spec routes the same underlying access through reviewed,
checked-in code instead, with an explicit design constraint that no plaintext
credential is ever collected, logged, returned across a function boundary, or
written to a file.

**Scope:** Chrome's single `Default` profile only (the user confirmed they use
one Chrome profile). No profile picker, no ongoing sync — this is an import,
run once (idempotent if re-run).

## Global constraints

- No decrypted password value is ever written to disk, logged, printed, put
  in an HTTP response, or returned from more than one function frame — see
  "Secret handling" below. This is the one constraint every other decision in
  this spec exists to serve.
- `mcp=False` on the new API op — this is a user-triggered migration action,
  never something an agent should be able to invoke as a step toward some
  other goal.
- Existing bookmarks/history/passwords are never overwritten by an import —
  an import only fills gaps.
- No new third-party dependency beyond what `passwords.py`/`profile.py`
  already use for Secret Service access (this project is stdlib-only plus
  that one existing exception).

## Components

### `claudebrowser/chrome_import.py` (new, GTK-free — testable without a display)

Mirrors the existing GTK-free-module convention (`playbooks.py`, `profile.py`,
`fills.py`). Three read functions, each scoped to one Chrome file:

- `read_bookmarks(profile_dir)` — Chrome's `Bookmarks` file is plain JSON
  (never encrypted). Walks its `roots` tree and yields `(url, title,
  added_epoch)` for every `type: "url"` node. `added_epoch` is converted from
  Chrome's own epoch (microseconds since 1601-01-01) to the Unix epoch
  `store.py`'s `bookmarks.added` column expects.
- `read_history(profile_dir)` — Chrome's `History` file is a plain,
  unencrypted SQLite database, but Chrome holds it open, so it is copied into
  a `tempfile.TemporaryDirectory()` first (deleted in a `finally`, so a
  crash mid-import never leaves a copy of the user's history sitting in
  `/tmp`). Reads the `urls` table, yields `(url, title, visit_count,
  last_visit_epoch)`, with the same Chrome-epoch conversion as bookmarks.
- `read_passwords(profile_dir, secret)` — the one function that touches
  encrypted data. `Login Data` is copied the same way `History` is. `secret`
  is the already-decrypted Chrome Safe Storage key (see "Secret handling").
  Reads the `logins` table's `origin_url`, `username_value`,
  `password_value` columns and **yields** `(origin, username, password)` one
  row at a time — a generator, never a list — decrypting each
  `password_value` with the standard Chrome-on-Linux scheme:
  - Strip the 3-byte `v10`/`v11` prefix from `password_value`.
  - Derive a 16-byte AES key: `PBKDF2-HMAC-SHA1(secret, salt=b"saltysalt",
    iterations=1, dklen=16)`.
  - Decrypt with AES-128-CBC, a fixed IV of 16 space bytes (`b" " * 16`), and
    strip PKCS7 padding.
  - A row that fails to decrypt (corrupt, or a scheme this function does not
    recognise) is skipped and counted, never raised past the caller in a way
    that aborts the whole import.

None of these three functions is reachable from any CLI, MCP tool, or API
route on its own — only `Browser.api_import_chrome` (below) calls them, and
only from inside the same process, so a decrypted password value never
crosses a process boundary, a subprocess's captured stdout, or a log line.

### Secret handling (the part this spec exists to get right)

Chrome's per-installation encryption key ("Chrome Safe Storage") is itself
protected by the OS Secret Service — the same D-Bus service
`passwords.Vault`'s `SecretBackend` already talks to, via the same
`gi.repository.Secret` (libsecret) library already imported in this codebase
(no new dependency). It is not, however, the same *lookup*: `SecretBackend`
declares its own schema (`net.claudebrowser.Login`, attributes `kind`/
`origin`/`username`) which only ever matches Claude Browser's own items —
Chrome registered its key under its own, different schema and attributes.
`chrome_import` needs its own lookup against whatever schema Chrome actually
used, matched by inspecting the item's *attributes* (`Secret.Collection.
get_items()` / `item.get_attributes()`), never its secret value, until the
one moment the real key is fetched.

**Precondition check, before writing any decryption code:** confirm, on this
machine, what schema name and attributes the "Chrome Safe Storage" item was
actually registered under — by listing keyring items and their attributes
only (never loading/printing the secret value itself, the same
`SearchFlags.ALL` without `LOAD_SECRETS` pattern `SecretBackend.search`
already uses for its own items) — before assuming a schema and writing code
against a guess. If this differs across Chrome versions, note which version
this machine has (`google-chrome --version`) so the plan records what was
actually confirmed, not assumed.

If the key cannot be retrieved (Secret Service unreachable, entry not found,
or the user declines a keyring unlock prompt), `read_passwords` raises before
copying `Login Data` at all, and `api_import_chrome` reports
`{"ok": false, "error": "..."}"` for the passwords portion — bookmarks and
history import proceed independently since they need no secret at all.

### `store.py` additions

Two new methods, deliberately distinct from the existing `record()` and
`bookmark()` (which always stamp "now" — wrong for data with a real historical
timestamp from Chrome):

```
def import_bookmarks(self, entries):   # entries: iterable of (url, title, added_epoch)
def import_history(self, entries):     # entries: iterable of (url, title, visits, last_visit_epoch)
```

Both use `INSERT OR IGNORE` against the existing `bookmarks`/`history` tables
(both already keyed by `url TEXT PRIMARY KEY`), so an entry whose URL is
already present — imported earlier, or saved natively — is left completely
untouched. Each returns the count actually inserted, which is how
`api_import_chrome` reports `"skipped"`.

### `Browser.api_import_chrome(kinds, done)`

`kinds` is a subset of `{"passwords", "bookmarks", "history"}` (default: all
three). For each requested kind, calls the matching `chrome_import` reader and
the matching `store`/`vault` write, catching and counting per-kind failures
independently (a failed password decrypt should not abort the bookmarks
import). Response shape:

```json
{"ok": true,
 "bookmarks": {"imported": 312, "skipped": 9},
 "history": {"imported": 1204, "skipped": 0},
 "passwords": {"imported": 47, "skipped": 3, "failed": 1}}
```

Never a value, never a URL list beyond what the user could already see in
Chrome or Claude Browser's own bookmark/history pages — counts only.

### `api.py`

One new `Op`: `import-chrome`, POST, `mcp=False` (matching `settings`,
`persona`, `clear`, `playbook-delete` — the existing precedent for
"user-only, never agent-invocable"). Reachable via `cbctl import-chrome
[--passwords] [--bookmarks] [--history]` (all three if no flag given).

## Error handling

- Chrome not installed / profile directory missing → `{"ok": false, "error":
  "Chrome profile not found at ~/.config/google-chrome/Default"}` for the
  whole call, before touching anything.
- `Login Data`/`History` present but unreadable (permissions, unexpected
  schema from a future Chrome version) → that kind's own error, other kinds
  still attempted.
- Chrome running concurrently: reading a live-open SQLite file via a plain
  filesystem copy can, rarely, catch it mid-write. This is a known, accepted
  limitation — the import can simply be re-run (it is idempotent) and a
  message in the CLI output notes that closing Chrome first gives the most
  consistent read.

## Testing

`tests/test_chrome_import.py`, following the fixture-file convention already
used elsewhere in this suite:

- A small fixture `Bookmarks` JSON (hand-written, a few nodes including a
  nested folder) — asserts `read_bookmarks` walks folders and converts the
  epoch correctly.
- A small fixture `History` SQLite (built in `setUp` with `sqlite3`, matching
  Chrome's real `urls` table shape) — asserts `read_history` reads it via a
  temp copy and cleans the temp copy up afterward.
- A small fixture `Login Data` SQLite plus a **test-only, hand-generated**
  encryption key (never a real keyring call, never real Chrome data) —
  encrypts a few known plaintext passwords with the same v10 scheme in
  `setUp`, then asserts `read_passwords(profile_dir, secret=test_key)`
  recovers the exact plaintexts. This is what lets the suite exercise the
  real decryption logic with zero display and zero Secret Service dependency,
  matching this project's "tests must not start a real browser" /
  no-live-dependency convention.
- `store.py` tests for `import_bookmarks`/`import_history`: insert, then
  import an overlapping set, assert the pre-existing rows are untouched and
  only the new ones were added.
- `test_offline.py`'s `TestApiRegistry` gets the same wiring-correctness
  coverage as `snapshot`/`fill-many` did: the `import-chrome` op dispatches
  correctly, is `mcp=False`, and does not appear in the generated MCP tool
  list.

No test ever calls the real `keyring`/Secret Service, so the suite keeps
running headless with no dependency on a real login session — matching how
`passwords.py`'s and `profile.py`'s own tests already avoid a live keyring.

## Out of scope (explicitly)

- Other browsers (Firefox, Edge) — not asked for.
- Multiple Chrome profiles — the user uses one.
- An ongoing sync — this is an import, not a link between the two browsers.
- A GUI page for this (a `cb:import` page) — `cbctl import-chrome` is enough
  for a one-time migration; can be added later if it turns out to be used
  more than once.
