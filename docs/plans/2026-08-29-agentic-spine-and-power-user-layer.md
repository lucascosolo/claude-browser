# Agentic Spine and Power-User Layer Implementation Plan

> **For agentic workers:** this plan has 9 tasks, so it is executed via the `scoped-delivery` skill in 1-3 task chunks via fresh subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude Browser cheap, in tokens and turns, for an AI agent to drive through a complex multi-step task (concretely: Openclaw's data-broker opt-out flow), via a compact snapshot-and-ref interaction model, self-verifying actions, and profile placeholders resolved outside the model's context — then, as a secondary layer, unchain the daily-driver resource caps and expose bookmarks/history/downloads/session-restore/ad-block.

**Architecture:** `extract.py` gains a `snapshot`/`delta`/ref-resolution JS layer alongside the existing `TEXT`/`click`/`fill` snippets; `api.py` gains the corresponding ops (`snapshot`, `fill-many`, plus the Tier-2 exposure ops); a new `fills.py` module resolves `{profile:key}` placeholders natively so PII never enters a model's context; `agent.py`'s in-browser tool loop gets the same new tools plus fixes to real bugs the redesign exposed (loop-detection signature, truncation, `MAX_STEPS`). Tier 2 (resource caps, bookmark/history/download exposure, session restore, ad-block) is independent of Tier 1's new features but touches the same files, so every task in this plan is sequential — no two tasks touch `browser.py` concurrently.

**Tech Stack:** Python 3 stdlib, GTK3, WebKit2GTK 4.1 (via PyGObject), `unittest`, no pip/node/build step.

## Global Constraints

- No new dependencies: standard library only, matching the project's existing posture.
- Every new `api.py` op follows the existing `Op(name, path, method, description, params=..., call=..., mcp=...)` shape; nothing is added to `control.py`'s routes, `cbctl`'s subcommands, or `cb-mcp`'s tools by hand.
- Every new JS snippet lives in `extract.py`, matches the existing `(function(){...})()` / `JSON.stringify` style, and every selector/value/ref that can be model- or page-supplied passes through `extract._js_str`.
- Every module that can be tested without a display stays GTK-free (`fills.py`, `blocklist.py`, the pure-JS-string parts of `extract.py`), per the project's existing `profile.py`/`playbooks.py`/`scrub.py` convention.
- `agent.py`'s `TOOLS` list stays hand-written (never generated from `api.py` — its wording is prompt engineering, per `CLAUDE.md`).
- Every new tab-touching tool added to `agent.py`'s `TOOLS` must also be added to `TAB_TOOLS` — this is the private-tab gate, and missing it is a data leak, not a cosmetic bug.
- A placeholder can only ever resolve against `profile.py`'s Vault (schema `net.claudebrowser.Profile`), never `passwords.py` — this must never be able to leak a saved credential.
- Substitution of a placeholder must never mutate the request's `args` dict in place — `control._handle` records from those same `args`, and an in-place mutation would write the literal PII value to a playbook file on disk.
- `is_secret_step` continues to match only on selector/script, never on value, including for the new `fill-many` op — it must parse that op's JSON `fields` argument and check keys only.
- The ad-block curated domain list must never include a CAPTCHA/anti-bot vendor domain (`recaptcha.net`, `hcaptcha.com`, Cloudflare Turnstile's `challenges.cloudflare.com`, and equivalents) — doing so would make `blocked` return false negatives and is functionally a bypass attempt, violating the standing hard rule that this browser detects and never bypasses a challenge.
- `python3 -m py_compile claudebrowser/*.py` must pass after every task, per the project's standing gate on the display-requiring modules.
- Full verification command for every task: `CB_AUTOSTART=0 python3 -m unittest discover -s tests` (680+ tests, ~13s, no display) plus the task's own targeted test file.

## File Structure

| File | Role in this plan |
|---|---|
| `claudebrowser/fills.py` | **New.** GTK-free `{profile:key}` placeholder grammar: `is_placeholder`, `resolve_value`. |
| `claudebrowser/extract.py` | New `SNAPSHOT_SHIM`, `snapshot()`, `delta()` JS generators; `click()`/`fill()` extended for `@ref` targets; new `fill_many()`. |
| `claudebrowser/browser.py` | Registers `SNAPSHOT_SHIM`; `api_snapshot`, `api_fill_many`; nav-result `playbooks` annotation; `MAX_AGENT_TABS` default; session-restore wiring; ad-block `UserContentFilterStore` wiring; new `api_*` methods for bookmarks/history/downloads. |
| `claudebrowser/api.py` | New `Op`s: `snapshot`, `fill-many`, `bookmarks`, `bookmark-add`, `bookmark-remove`, `history`, `history-clear`, `downloads`. |
| `claudebrowser/playbooks.py` | `match` field + `matching(url)`; `is_secret_step` extended for `fill-many`. |
| `claudebrowser/agent.py` | `TOOLS` += `snapshot`, `fill_form`, `blocked`, keys-only `profile`; `TAB_TOOLS` guard; structural truncation; URL-aware loop signature; `MAX_STEPS` 14→24; `SYSTEM` text fix. |
| `claudebrowser/store.py` | New `session_tabs` table + read/write methods. |
| `claudebrowser/settings.py` | `CB_RESTORE_SESSION`, `CB_ADBLOCK`; `CB_LIGHT` default flip; `CB_MAX_TABS` doc update. |
| `claudebrowser/blocklist.py` | **New.** GTK-free curated domain list → WebKit content-blocker JSON. |
| `cb` | `CB_CPU_QUOTA`, `CB_MEM_HIGH` defaults. |
| `CLAUDE.md` | New entries for `fills.py`, `blocklist.py`; ad-block/snapshot interaction note. |
| `tests/test_fills.py` | **New.** |
| `tests/test_extract_snapshot.py` | **New** (or added to an existing `test_extract.py` if one exists — check first). |
| `tests/test_blocklist.py` | **New.** |
| Existing `tests/test_api.py`, `tests/test_agent.py`, `tests/test_playbooks.py`, `tests/test_browser*.py`, `tests/test_settings.py`, `tests/test_store.py` | Extended in place. |

---

### Task 1: `fills.py` — profile placeholder resolution

**Files:**
- Create: `claudebrowser/fills.py`
- Test: `tests/test_fills.py`

**Interfaces:**
- Consumes: `profile.Vault` (already exists — `get(key)`/`get_all()`).
- Produces: `is_placeholder(text) -> bool`, `resolve_value(text, vault) -> str` (raises `FillError` if `text` is a placeholder naming a field the vault doesn't have, or if `vault` is `None`), used by Task 3 (`browser.py`'s fill path) and Task 4 (playbook replay).

- [ ] **Step 1: Write the failing tests**

```python
import unittest
from claudebrowser import fills, profile


class TestFills(unittest.TestCase):
    def setUp(self):
        self.vault = profile.Vault(profile.MemoryBackend())
        self.vault.set("email", "jane@example.com")

    def test_is_placeholder_true_for_profile_ref(self):
        self.assertTrue(fills.is_placeholder("{profile:email}"))

    def test_is_placeholder_false_for_plain_value(self):
        self.assertFalse(fills.is_placeholder("jane@example.com"))
        self.assertFalse(fills.is_placeholder(""))
        self.assertFalse(fills.is_placeholder("{not a placeholder}"))

    def test_resolve_value_looks_up_the_vault(self):
        self.assertEqual(fills.resolve_value("{profile:email}", self.vault),
                          "jane@example.com")

    def test_resolve_value_passes_through_non_placeholders(self):
        self.assertEqual(fills.resolve_value("literal", self.vault), "literal")

    def test_resolve_value_raises_on_missing_field(self):
        with self.assertRaises(fills.FillError):
            fills.resolve_value("{profile:phone}", self.vault)

    def test_resolve_value_raises_when_vault_unavailable(self):
        with self.assertRaises(fills.FillError):
            fills.resolve_value("{profile:email}", None)

    def test_resolve_value_never_mutates_the_vault(self):
        before = self.vault.get_all()
        fills.resolve_value("{profile:email}", self.vault)
        self.assertEqual(self.vault.get_all(), before)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_fills -v`
Expected: `ModuleNotFoundError: No module named 'claudebrowser.fills'` (or `AttributeError`).

- [ ] **Step 3: Implement `fills.py`**

```python
"""Resolving {profile:key} placeholders without ever handing the value to a
model.

**Why this exists.** A profile field (see profile.py) exists so an agent can
fill a form with it -- but if the agent has to hold the literal value to pass
it back in a `fill` call, that value sits in the model's context and gets
re-sent on every subsequent turn of the run, and passes through ai._redact on
the way there (which can mangle it). Resolving the placeholder natively, on
the way from the request to the page, means the model only ever sees the
field's name.

**Why this can never reach a saved login.** A placeholder resolves against
profile.py's Vault, whose keyring schema (net.claudebrowser.Profile) is
disjoint from passwords.py's -- there is no field name that could ever look
up a credential. Do not add a second placeholder form (e.g. {secret:...})
without re-reading this note.

**Recording.** control._handle records a request's raw args, and this module
is called on a *copy* -- never on the args dict itself -- so a playbook saves
the placeholder string, not the resolved value. That is what makes "record
once, replay against next month's profile data" work, and it is also the
only thing standing between this feature and writing PII to disk in a
playbook file. See the "never mutates" test in tests/test_fills.py.
"""

import re

_PATTERN = re.compile(r"^\{profile:([A-Za-z0-9_]+)\}$")


class FillError(Exception):
    pass


def is_placeholder(text):
    return bool(text) and _PATTERN.match(text) is not None


def resolve_value(text, vault):
    """`text` unchanged if it is not a placeholder; the vault's value if it
    is. Raises FillError if it is a placeholder this vault cannot satisfy.
    """
    match = _PATTERN.match(text or "")
    if not match:
        return text
    if vault is None:
        raise FillError("the system keyring is unavailable")
    key = match.group(1)
    value = vault.get(key)
    if value is None:
        raise FillError("no profile field %r" % key)
    return value
```

- [ ] **Step 4: Run and verify it passes**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_fills -v`
Expected: 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/fills.py tests/test_fills.py
git commit -m "Add fills.py: resolve {profile:key} placeholders outside model context"
```

---

### Task 2: `extract.py` — snapshot, delta, and ref-based targeting

**Files:**
- Modify: `claudebrowser/extract.py`
- Test: `tests/test_extract.py` (check whether this file exists first — `ls claudebrowser/../tests/test_extract*.py`; if it doesn't, create `tests/test_extract_snapshot.py`)

**Interfaces:**
- Consumes: `extract._js_str` (existing escaping helper), `extract.TEXT`'s visibility check pattern (`getClientRects().length`).
- Produces: `extract.SNAPSHOT_SHIM` (a JS string to register as a document-start `UserScript`, consumed by Task 3's `browser.py` wiring), `extract.snapshot(opts=None) -> str` (JS source, consumed by Task 3's `api_snapshot`), `extract.delta() -> str` (a JS expression fragment, consumed by Task 3's extended `click`/`fill`), `extract.click(selector) -> str` and `extract.fill(selector, value) -> str` (existing functions, extended to resolve a `@ref`-prefixed `selector` via the shim before acting, and to append `delta()`'s fragment to their returned JSON), `extract.fill_many(pairs) -> str` (JS source; `pairs` is a Python list of `(selector_or_ref, value)` tuples, consumed by Task 3's `api_fill_many`).

Since no test in this suite executes JS in a real WebView (no display in CI — see `CLAUDE.md`), every test here is a string-content assertion on the generated JS, matching the existing convention for `TEXT`/`MARKDOWN`/`BLOCKED`.

- [ ] **Step 1: Write the failing tests**

```python
import re
import unittest

from claudebrowser import extract


class TestSnapshotShim(unittest.TestCase):
    def test_mints_a_fresh_epoch_per_document(self):
        self.assertIn("__cbEpoch", extract.SNAPSHOT_SHIM)
        # Must be assigned unconditionally at document-start, not guarded by
        # `if (window.__cbEpoch) return` the way __cbHalo is -- a repeat guard
        # here would let a stale epoch survive a same-document re-run.
        self.assertNotIn('if (window.__cbEpoch)', extract.SNAPSHOT_SHIM)

    def test_defines_a_registry_and_resolver(self):
        self.assertIn("__cbRegister", extract.SNAPSHOT_SHIM)
        self.assertIn("__cbResolve", extract.SNAPSHOT_SHIM)


class TestSnapshot(unittest.TestCase):
    def test_returns_a_function_expression(self):
        js = extract.snapshot()
        self.assertTrue(js.strip().startswith("(function"))
        self.assertIn("JSON.stringify", js)

    def test_walks_same_origin_iframes_and_reports_cross_origin_ones(self):
        js = extract.snapshot()
        self.assertIn("contentDocument", js)
        self.assertIn("UNREACHABLE", js)

    def test_reports_option_count_not_option_list(self):
        js = extract.snapshot()
        self.assertIn("options", js)
        # A snapshot must never inline every <option> -- that is the exact
        # per-turn token cost the whole feature exists to avoid.
        self.assertNotIn("optionsList", js)

    def test_uses_the_same_visibility_check_as_text(self):
        js = extract.snapshot()
        self.assertIn("getClientRects", js)


class TestRefTargeting(unittest.TestCase):
    def test_click_resolves_an_at_sign_ref(self):
        js = extract.click("@e7")
        self.assertIn("__cbResolve", js)
        self.assertIn("e7", js)

    def test_click_on_a_plain_selector_does_not_touch_the_resolver(self):
        js = extract.click("#submit")
        self.assertIn("querySelector", js)

    def test_fill_resolves_an_at_sign_ref(self):
        js = extract.fill("@e3", "California")
        self.assertIn("__cbResolve", js)

    def test_click_and_fill_append_a_delta(self):
        self.assertIn("url_changed", extract.click("#submit"))
        self.assertIn("url_changed", extract.fill("#z", "1"))
        self.assertIn("blocked", extract.click("#submit"))
        self.assertIn("errors", extract.click("#submit"))


class TestFillMany(unittest.TestCase):
    def test_returns_a_function_expression(self):
        js = extract.fill_many([("#email", "a@b.com"), ("@e3", "CA")])
        self.assertTrue(js.strip().startswith("(function"))

    def test_escapes_every_selector_and_value(self):
        js = extract.fill_many([('"><script>alert(1)</script>', 'x')])
        self.assertNotIn("<script>", js)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_extract_snapshot -v`
Expected: `AttributeError: module 'claudebrowser.extract' has no attribute 'SNAPSHOT_SHIM'` (and similar for the rest).

- [ ] **Step 3: Implement in `extract.py`**

Add near `BLOCKED` (same module, same style). Read the existing `TEXT`,
`click()`, `fill()`, `point()`, `_js_str` and `_HALO_SRC` definitions first —
this task extends them, it does not replace them.

```python
# Installed once per document via a document-start UserScript (see
# browser.py's add_script loop, alongside CONSOLE_SHIM). __cbEpoch is
# unconditional -- unlike __cbHalo's `if (window.__cbHalo) return` guard, a
# stale epoch surviving a same-document re-run is exactly the silent-wrong-
# click failure mode this exists to prevent.
SNAPSHOT_SHIM = r"""
(function () {
  window.__cbEpoch = Math.random().toString(36).slice(2);
  window.__cbRegistry = window.__cbRegistry || {};
  window.__cbRegister = function (ref, node, sig, path) {
    window.__cbRegistry[ref] = { node: node, sig: sig, path: path };
  };
  window.__cbSig = function (node) {
    var role = node.getAttribute('role') || node.tagName.toLowerCase();
    var label = (node.getAttribute('aria-label')
                 || (node.labels && node.labels[0] && node.labels[0].innerText)
                 || node.getAttribute('placeholder')
                 || node.innerText || '').trim().slice(0, 80);
    return role + '|' + (node.type || '') + '|' + label;
  };
  window.__cbResolve = function (ref) {
    var entry = window.__cbRegistry[ref];
    if (!entry) return null;
    if (entry.node && entry.node.isConnected
        && window.__cbSig(entry.node) === entry.sig) return entry.node;
    // Structural fallback: re-walk the recorded frame/nth-child path.
    if (entry.path) {
      try {
        var node = document.querySelector(entry.path);
        if (node && window.__cbSig(node) === entry.sig) return node;
      } catch (e) {}
    }
    return null;
  };
})()
"""


def _visible(node_expr):
    return "%s.getClientRects().length > 0" % node_expr


def snapshot():
    """Interactive elements as a compact, ref-indexed line list, not prose.

    Same-origin iframes are walked (contentDocument is reachable from the
    top frame); cross-origin ones are reported UNREACHABLE rather than
    silently yielding no matches -- see the design spec's iframe section.
    """
    return r"""
(function () {
  var out = [];
  var n = 0;
  function label(el) {
    var l = el.getAttribute('aria-label')
      || (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('placeholder')
      || (el.innerText || '').trim();
    return (l || '').trim().slice(0, 80);
  }
  function sig(el) {
    return (el.getAttribute('role') || el.tagName.toLowerCase())
      + '|' + (el.type || '') + '|' + label(el);
  }
  function walk(doc, frameIdx, path) {
    if (!doc) return;
    var els = doc.querySelectorAll(
      'input,textarea,select,button,a[href],[role=button],[contenteditable]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!(%s)) continue;
      n++;
      var ref = 'e' + n;
      var elPath = path + ' ' + el.tagName.toLowerCase() + ':nth-child(' + (i + 1) + ')';
      if (typeof window.__cbRegister === 'function') {
        window.__cbRegister(ref, el, sig(el), elPath);
      }
      var row = { ref: ref, tag: el.tagName.toLowerCase(), label: label(el) };
      if (el.tagName === 'SELECT') row.options = el.options.length;
      if (el.tagName === 'A') row.href = el.href;
      if (el.required) row.required = true;
      out.push(row);
    }
    var frames = doc.querySelectorAll('iframe');
    for (var j = 0; j < frames.length; j++) {
      var f = frames[j];
      try {
        if (f.contentDocument) {
          walk(f.contentDocument, frameIdx + 1, path + ' f' + j);
        } else {
          out.push({ ref: 'f' + j, tag: 'iframe',
                     origin: (new URL(f.src, location.href)).origin,
                     unreachable: true });
        }
      } catch (e) {
        out.push({ ref: 'f' + j, tag: 'iframe', unreachable: true });
      }
    }
  }
  walk(document, 0, 'body');
  return JSON.stringify({
    ok: true, url: location.href,
    epoch: window.__cbEpoch || null,
    lines: out, counts: { total: out.length }
  });
})()
""" % _visible("el")


def delta():
    """A fragment appended to every acting op's return object: what actually
    changed, so the caller does not need a full re-read to find out."""
    return r"""
    url_changed: (location.href !== __cbUrlBefore),
    epoch: window.__cbEpoch || null,
    changed: document.querySelectorAll(
      '[aria-invalid="true"],[role="alert"],.error,:invalid').length,
    errors: (function () {
      var els = document.querySelectorAll('[aria-invalid="true"],[role="alert"],.error');
      var msgs = [];
      for (var i = 0; i < els.length && i < 5; i++) {
        var t = (els[i].innerText || '').trim();
        if (t) msgs.push(t);
      }
      return msgs;
    })()
"""


def _resolve_target(selector):
    """A ref target ("@e7") resolves via __cbResolve; anything else is a
    plain querySelector -- unchanged behavior for existing callers."""
    if selector.startswith("@"):
        ref = _js_str(selector[1:])
        return "(window.__cbResolve && window.__cbResolve(%s))" % ref
    return "document.querySelector(%s)" % _js_str(selector)


def click(selector):
    return r"""
(function () {
  var __cbUrlBefore = location.href;
  var el = %s;
  if (!el) return JSON.stringify({ ok: false, error: 'no match' });
  el.click();
  return JSON.stringify(Object.assign({ ok: true, tag: el.tagName.toLowerCase() }, {%s}));
})()
""" % (_resolve_target(selector), delta())


def fill(selector, value):
    return r"""
(function () {
  var __cbUrlBefore = location.href;
  var el = %s;
  if (!el) return JSON.stringify({ ok: false, error: 'no match' });
  el.value = %s;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return JSON.stringify(Object.assign({ ok: true }, {%s}));
})()
""" % (_resolve_target(selector), _js_str(value), delta())


def fill_many(pairs):
    """`pairs`: a list of (selector_or_ref, value) tuples, already resolved
    against the profile vault by the caller -- this function only ever sees
    literal strings to type, never a {profile:...} placeholder."""
    entries = ",".join(
        "[%s, %s]" % (
            ("'@' + " + _js_str(sel[1:])) if sel.startswith("@")
            else _js_str(sel),
            _js_str(val))
        for sel, val in pairs)
    return r"""
(function () {
  var __cbUrlBefore = location.href;
  var pairs = [%s];
  var results = [];
  for (var i = 0; i < pairs.length; i++) {
    var target = pairs[i][0];
    var el = target.charAt(0) === '@'
      ? (window.__cbResolve && window.__cbResolve(target.slice(1)))
      : document.querySelector(target);
    if (!el) { results.push({ ok: false, target: target, error: 'no match' }); continue; }
    el.value = pairs[i][1];
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    results.push({ ok: true, target: target });
  }
  return JSON.stringify(Object.assign({ ok: true, results: results }, {%s}));
})()
""" % (entries, delta())
```

- [ ] **Step 4: Run and verify it passes**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_extract_snapshot -v`
Expected: all tests pass. Then run `python3 -m py_compile claudebrowser/extract.py`.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/extract.py tests/test_extract_snapshot.py
git commit -m "Add snapshot/ref-targeting/delta JS to extract.py"
```

---

### Task 3: `browser.py` + `api.py` — wire up `snapshot` and `fill-many`

**Files:**
- Modify: `claudebrowser/browser.py` (near `CONSOLE_SHIM`'s registration ~line 478-487, and near `api_eval`/existing `click`/`fill` API methods ~line 3966-3996)
- Modify: `claudebrowser/api.py`
- Test: `tests/test_api.py` (or wherever `Op` registry / MCP-tool-list smoke tests live — check `test_offline.py` first, since it already covers every `api.OPS` entry generically)

**Interfaces:**
- Consumes: `extract.SNAPSHOT_SHIM`, `extract.snapshot()`, `extract.fill_many()` (Task 2); `fills.resolve_value`, `fills.FillError` (Task 1); `profile.Vault` (existing, via `self.profile` on `Browser`, already wired per the prior PII spec).
- Produces: `Browser.api_snapshot(tab, done)`, `Browser.api_fill_many(tab, fields_json, done)`; `api.OPS` entries `snapshot` and `fill-many`, consumed by Task 5 (`agent.py`'s `TOOLS`/`dispatch`).

- [ ] **Step 1: Register the shim**

In `browser.py`, find the loop that adds `CONSOLE_SHIM`/`PASSWORD_JS` as
`UserScript`s (search `add_script`). Add `extract.SNAPSHOT_SHIM` to that same
loop so every tab gets it at document-start, same injected-frames scope as
`CONSOLE_SHIM` (`TOP_FRAME` — the shim only needs to exist in the top frame;
`extract.snapshot()`'s own JS reaches into same-origin iframes via
`contentDocument`, it does not need the shim injected into them separately).

- [ ] **Step 2: Write the failing tests for `api_fill_many`'s placeholder handling**

```python
# In whatever test module covers Browser.api_* methods against a fake tab
# (check tests/test_browser_api.py or similar for the existing pattern used
# by api_profile/api_profile_set's tests, and mirror it).

def test_fill_many_resolves_profile_placeholders_before_calling_js(self):
    browser.profile.set("email", "jane@example.com")
    calls = []
    browser._eval_capture = lambda tab, js: calls.append(js)  # match existing eval-stubbing pattern in the test file
    browser.api_fill_many(tab_id, json.dumps({"#email": "{profile:email}"}), done)
    self.assertIn("jane@example.com", calls[-1])
    self.assertNotIn("{profile:email}", calls[-1])

def test_fill_many_reports_unresolvable_placeholder_without_touching_the_page(self):
    calls = []
    browser._eval_capture = lambda tab, js: calls.append(js)
    result = {}
    browser.api_fill_many(tab_id, json.dumps({"#phone": "{profile:phone}"}),
                           lambda r: result.update(r))
    self.assertFalse(result["ok"])
    self.assertEqual(calls, [])  # never reached the page
```

(Match this to whatever fake-tab/eval-stubbing harness the existing
`api_profile_set`/`api_click` tests already use in this codebase — read that
file first rather than inventing a new harness.)

- [ ] **Step 3: Run and verify it fails**

Run the targeted test module. Expected: `AttributeError: 'Browser' object has
no attribute 'api_fill_many'`.

- [ ] **Step 4: Implement in `browser.py`**

```python
def api_snapshot(self, tab, done):
    """The page's interactive elements as a compact, ref-indexed list --
    see extract.snapshot(). Read-only; never mutates the page."""
    self.api_eval(tab, extract.snapshot(), done)

def api_fill_many(self, tab, fields_json, done):
    """fields_json: {"selector_or_@ref": "value_or_{profile:key}", ...},
    passed as a JSON string (not a dict param) so playbooks.py's scalar-only
    parameter checking never has to trust a nested structure -- see the
    design spec's fill-many section.

    Placeholders are resolved here, natively, before any JS reaches the
    page -- the model that requested this call never receives the resolved
    value. An unresolvable placeholder fails the whole call before touching
    the page at all: a partially-filled form with a null in the middle is a
    worse failure than not starting.
    """
    try:
        fields = json.loads(fields_json)
    except ValueError:
        return done({"ok": False, "error": "fields must be a JSON object"})
    if not isinstance(fields, dict):
        return done({"ok": False, "error": "fields must be a JSON object"})
    try:
        resolved = [(k, fills.resolve_value(v, self.profile))
                    for k, v in fields.items()]
    except fills.FillError as e:
        return done({"ok": False, "error": str(e)})
    self.api_eval(tab, extract.fill_many(resolved), done)
```

Add `from . import fills` to `browser.py`'s imports.

- [ ] **Step 5: Add the `Op`s in `api.py`**

```python
Op("snapshot", "/snapshot", "GET",
   "List the page's interactive elements (inputs, buttons, links) as a "
   "compact, ref-indexed manifest -- use this instead of `text` before "
   "filling a form. Each ref (like @e7) can be passed to `click`/`fill` "
   "in place of a CSS selector.",
   call=lambda c, a: ("api_snapshot", (_tab(a),))),

Op("fill-many", "/fill/many", "POST",
   "Fill several fields in one call. `fields` is a JSON object mapping "
   "each selector (or @ref from `snapshot`) to a value -- use "
   "\"{profile:KEY}\" as a value to fill from the stored profile without "
   "ever seeing the value yourself.",
   params=[Param("fields", required=True,
                 help="JSON object: {selector_or_ref: value}.")],
   call=lambda c, a: ("api_fill_many", (_tab(a), a["fields"]))),
```

Update the descriptions of the existing `click`/`fill` `Op`s to mention that
their `selector` parameter also accepts a `@ref` from `snapshot`.

- [ ] **Step 6: Run and verify it passes**

Run the targeted test module, then `CB_AUTOSTART=0 python3 -m unittest
discover -s tests` for the full suite, then `python3 -m py_compile
claudebrowser/*.py`.

- [ ] **Step 7: Commit**

```bash
git add claudebrowser/browser.py claudebrowser/api.py tests/
git commit -m "Wire snapshot and fill-many into the control API"
```

---

### Task 4: playbook site-matching + `fill-many` secret-step handling

**Files:**
- Modify: `claudebrowser/playbooks.py`
- Modify: `claudebrowser/browser.py` (nav-result annotation, in `api_navigate`/`api_open`)
- Test: `tests/test_playbooks.py`

**Interfaces:**
- Consumes: `api.OPS` (existing), the `fill-many` op from Task 3.
- Produces: `Store.save(name, steps, skipped=0)` gains an inferred `match` field (host of the first `open`/`navigate` step); `matching(url) -> list[str]` (playbook names whose `match` is a suffix-or-equal match of `url`'s host), consumed by `browser.py`'s nav result and by a `url` filter on `playbook-list`.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_infers_match_from_first_navigate_step(self):
    steps = [{"op": "navigate", "params": {"url": "https://acxiom.com/optout"}},
             {"op": "click", "params": {"selector": "#submit"}}]
    self.store.save("acxiom-optout", steps)
    self.assertEqual(self.store.get("acxiom-optout")["match"], "acxiom.com")

def test_matching_finds_playbooks_by_host(self):
    self.store.save("acxiom-optout", [
        {"op": "open", "params": {"url": "https://acxiom.com/optout"}}])
    self.assertEqual(self.store.matching("https://acxiom.com/optout?ref=x"),
                      ["acxiom-optout"])
    self.assertEqual(self.store.matching("https://example.com"), [])

def test_is_secret_step_checks_fill_many_keys_only(self):
    # A fill-many step whose *selector* looks like a password field is
    # dropped; one whose *value* merely contains the word "password" is not.
    self.assertTrue(playbooks.is_secret_step(
        "fill-many", {"fields": json.dumps({"#password": "{profile:x}"})}))
    self.assertFalse(playbooks.is_secret_step(
        "fill-many", {"fields": json.dumps({"#note": "my password is old"})}))

def test_placeholder_round_trips_unresolved_through_save_and_replay(self):
    steps = [{"op": "fill-many",
              "params": {"fields": json.dumps({"#email": "{profile:email}"})}}]
    self.store.save("test", steps)
    saved = self.store.get("test")["steps"]
    self.assertIn("{profile:email}", saved[0]["params"]["fields"])
```

- [ ] **Step 2: Run and verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_playbooks -v`
Expected: failures on the new assertions (`KeyError: 'match'`, `AttributeError: matching`).

- [ ] **Step 3: Implement in `playbooks.py`**

```python
import json as _json  # if not already imported under a different alias — check the top of the file first

def _host_of(url):
    from urllib.parse import urlparse
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def _inferred_match(steps):
    for step in steps:
        if step.get("op") in ("navigate", "open"):
            host = _host_of(step.get("params", {}).get("url", ""))
            if host:
                return host
    return None
```

In `Store.save`, compute `match = _inferred_match(steps)` and store it
alongside the existing fields on the saved record.

Add:

```python
def matching(self, url):
    """Names of saved playbooks whose recorded host matches `url`'s host.
    Never auto-replayed -- purely advisory, surfaced on a nav result so the
    caller can decide."""
    host = _host_of(url)
    if not host:
        return []
    return [name for name, book in self._read().items()
            if book.get("match") and host == book["match"]]
```

Extend `is_secret_step`:

```python
def is_secret_step(op_name, params):
    if op_name == "fill-many":
        try:
            fields = json.loads(params.get("fields", "{}"))
        except ValueError:
            return False
        # Keys only -- matching a value here would violate the rule this
        # function exists to enforce (see the module docstring: only the
        # selector or script is ever inspected, never the value).
        return any(SECRET_HINT.search(k) for k in fields)
    # ... existing logic for fill/other ops, unchanged
```

(Read the existing `is_secret_step` body first and extend it rather than
replacing it — the `fill`/other-op branches stay as they are.)

- [ ] **Step 4: Add the nav-result annotation in `browser.py`**

In `api_navigate` and `api_open`, after a successful load, add:

```python
result["playbooks"] = self.playbooks.matching(result.get("url", url))
```

(`self.playbooks` — confirm the actual attribute name `Browser` uses for its
`playbooks.Store` instance by reading `browser.py`'s `__init__` and the
existing `api_playbook_list`/`api_playbook_run` methods first.)

- [ ] **Step 5: Run and verify it passes**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_playbooks -v`, then the
full suite.

- [ ] **Step 6: Commit**

```bash
git add claudebrowser/playbooks.py claudebrowser/browser.py tests/test_playbooks.py
git commit -m "Playbooks volunteer a site match; fill-many respects is_secret_step"
```

---

### Task 5: `agent.py` — new tools, `TAB_TOOLS` guard, and three latent-bug fixes

**Files:**
- Modify: `claudebrowser/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `snapshot`/`fill-many` ops (Task 3), `extract.snapshot()`/`extract.fill_many()` (Task 2), `browser_profile`/`browser_blocked` equivalents already on the external MCP surface (`api_profile`, `blocked` op — both already exist from the prior PII spec).
- Produces: nothing new consumed elsewhere — this task's output is the in-browser Ctrl+G loop itself.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_tab_touching_tool_is_in_tab_tools(self):
    # Guard test in the spirit of test_every_web_process_name_survives_truncation:
    # a tool that reaches the tab but is missing from TAB_TOOLS is a private-tab
    # data leak, not a cosmetic bug.
    TAB_TOUCHING = {"navigate", "read_page", "find_in_page", "page_links",
                     "click", "type_text", "snapshot", "fill_form", "blocked"}
    names = {t["name"] for t in agent.TOOLS}
    for name in TAB_TOUCHING & names:
        self.assertIn(name, agent.TAB_TOOLS, "%s touches the tab but is not gated" % name)

def test_tools_list_includes_the_new_primitives(self):
    names = {t["name"] for t in agent.TOOLS}
    self.assertIn("snapshot", names)
    self.assertIn("fill_form", names)
    self.assertIn("blocked", names)
    self.assertIn("profile", names)

def test_max_steps_raised(self):
    self.assertGreaterEqual(agent.MAX_STEPS, 24)

def test_loop_signature_includes_the_tab_url(self):
    # snapshot() takes no arguments, so the loop-detection key must not be
    # (tool_name, args) alone or four snapshots across four different pages
    # in one multi-page flow look like a repeat.
    a = agent.Agent(call=lambda *a, **k: {}, emit=lambda *a: None)
    sig1 = a._loop_signature("snapshot", {}, "https://site.example/page1")
    sig2 = a._loop_signature("snapshot", {}, "https://site.example/page2")
    self.assertNotEqual(sig1, sig2)

def test_structural_truncation_keeps_valid_json(self):
    huge = {"ok": True, "lines": [{"ref": "e%d" % i} for i in range(5000)]}
    truncated = agent.truncate_result("snapshot", huge, limit=200)
    json.loads(json.dumps(truncated))  # must not be a broken byte-slice
    self.assertLess(len(json.dumps(truncated)), len(json.dumps(huge)))
```

(Exact helper names — `_loop_signature`, `truncate_result` — are a proposed
shape; if `agent.py`'s current loop-detection and truncation logic is
inlined rather than in named helpers, factor it out into these functions as
part of this task rather than testing inline logic indirectly.)

- [ ] **Step 2: Run and verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_agent -v`
Expected: multiple failures (missing tools, `MAX_STEPS` too low, etc).

- [ ] **Step 3: Implement in `agent.py`**

1. Add to `TOOLS`:

```python
    {
        "name": "snapshot",
        "description": "List the page's interactive elements (inputs, "
                       "buttons, links) as a compact, ref-indexed list. Use "
                       "this instead of read_page before filling a form -- "
                       "refs (like @e7) can be passed to click/type_text/"
                       "fill_form in place of a CSS selector.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fill_form",
        "description": "Fill several fields in one call. `fields` maps "
                       "each selector or @ref to a value; use "
                       "\"{profile:KEY}\" to fill from the stored profile "
                       "without ever seeing the value yourself.",
        "input_schema": {
            "type": "object",
            "properties": {"fields": {"type": "object"}},
            "required": ["fields"],
        },
    },
    {
        "name": "blocked",
        "description": "Check whether the page is showing a CAPTCHA or "
                       "anti-bot challenge instead of its real content. "
                       "Never attempt to solve or bypass one -- stop and "
                       "report it.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "profile",
        "description": "List the names of the stored personal-info profile "
                       "fields (not their values) -- use a name with "
                       "fill_form's \"{profile:KEY}\" syntax.",
        "input_schema": {"type": "object", "properties": {}},
    },
```

2. `TAB_TOOLS = TAB_TOOLS | frozenset({"snapshot", "fill_form", "blocked"})`
   (`profile` does not touch the tab — it reads the keyring, not the page —
   so it is deliberately excluded here).

3. In `dispatch()`, add branches:

```python
if name == "snapshot":
    return self._eval(extract.snapshot())
if name == "fill_form":
    try:
        resolved = [(k, fills.resolve_value(v, self.vault))
                    for k, v in args["fields"].items()]
    except fills.FillError as e:
        return {"error": str(e)}
    out = self._eval(extract.fill_many(resolved))
    self._pause(ACT_S)
    return out
if name == "blocked":
    return self._eval(extract.BLOCKED)
if name == "profile":
    fields = self.call("api_profile")
    return {"available": fields.get("available"),
            "keys": sorted(fields.get("fields", {}).keys())}
```

(`self.vault` — `Agent` needs a reference to the `profile.Vault`; check how
`Agent` is constructed in `browser.py`'s `run_agent` and pass it through the
same way other browser-level dependencies reach `Agent` today, or route
through `self.call("api_profile")`/a new `api_profile_get_raw` if `Agent`
has no direct browser-object access — match whatever pattern the codebase
already uses for `Agent`'s access to browser state.)

4. Fix the loop-detection signature to include the tab's current URL:

```python
def _loop_signature(self, name, args, url):
    return (name, json.dumps(args, sort_keys=True), url)
```

Update wherever `self.seen` is read/written to pass the current tab's URL in
(fetch it via whatever the loop already uses to know the active tab, e.g. an
existing `list_tabs`/`current_url` call already made once per step, not a new
one).

5. Structural truncation for snapshot/fill_form results:

```python
def truncate_result(name, result, limit=RESULT_CHARS):
    if name in ("snapshot", "fill_form") and isinstance(result, dict) and "lines" in result:
        lines = result["lines"]
        out = dict(result)
        kept = []
        budget = limit
        for line in lines:
            cost = len(json.dumps(line))
            if budget - cost <= 0:
                out["truncated"] = True
                out["omitted"] = len(lines) - len(kept)
                break
            kept.append(line)
            budget -= cost
        out["lines"] = kept
        return out
    # existing byte-slice behavior for everything else, unchanged
    ...
```

Route the existing truncation call site through this function instead of the
raw `json.dumps(output)[:RESULT_CHARS]` slice.

6. `MAX_STEPS = 24`.

7. In `SYSTEM` (or wherever the prompt text lives), remove/replace the
   framing that assumes a slow machine — read the current text first and
   rewrite only that sentence, not the surrounding instructions.

- [ ] **Step 4: Run and verify it passes**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_agent -v`, then the full
suite, then `python3 -m py_compile claudebrowser/*.py`.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/agent.py tests/test_agent.py
git commit -m "agent.py: snapshot/fill_form/blocked/profile tools, TAB_TOOLS guard, loop/truncation/MAX_STEPS fixes"
```

---

### Task 6: resource unchaining

**Files:**
- Modify: `cb`
- Modify: `claudebrowser/browser.py` (`MAX_AGENT_TABS` default)
- Modify: `claudebrowser/perf.py` (`CB_LIGHT` default)
- Modify: `claudebrowser/settings.py` (doc text for `CB_MAX_TABS`, `CB_LIGHT`)
- Test: `tests/test_settings.py`, `tests/test_perf.py` (if present — check first)

**Interfaces:** none new — this task changes default constants only, no new
functions.

- [ ] **Step 1: Write/adjust the failing tests**

```python
def test_max_agent_tabs_default_is_24(self):
    # Reset any CB_MAX_TABS from the environment for this assertion.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CB_MAX_TABS", None)
        import importlib
        importlib.reload(browser)
        self.assertEqual(browser.MAX_AGENT_TABS, 24)

def test_cb_light_defaults_off(self):
    self.assertFalse(perf.light_enabled_by_default())  # or however the
    # existing default-reading function is named — check perf.py's actual
    # API for reading CB_LIGHT's default before writing this assertion
```

(If `perf.py` reads `CB_LIGHT` directly from `envfile.setting` rather than
through a named default-returning function, adjust this test to match the
actual shape rather than inventing a function that doesn't exist — this is a
one-line default-value change, keep the test proportional.)

- [ ] **Step 2: Run and verify it fails**

Run the targeted test files.

- [ ] **Step 3: Implement**

- `cb`: change `CPUQuota="${CB_CPU_QUOTA:-180%}"` → `CPUQuota="${CB_CPU_QUOTA:-800%}"`
  and `MemoryHigh="${CB_MEM_HIGH:-1200M}"` → `MemoryHigh="${CB_MEM_HIGH:-8192M}"`.
- `browser.py`: `MAX_AGENT_TABS = int(os.environ.get("CB_MAX_TABS", "24"))`.
- `perf.py`: find `LIGHT_ENV`'s default-reading call and flip its fallback
  from on to off (read the exact current code before editing — the default
  may be expressed as `"1"`/`"0"` or a boolean literal).
- `settings.py`: update the `CB_MAX_TABS`/`CB_LIGHT` description text to
  state the new defaults and, for `CB_LIGHT`, add a sentence noting the
  default changed because the CPU-conservation rationale no longer applies
  on this hardware class.
- Do **not** change `MAX_CONCURRENT_LOADS` — left at 2 deliberately; agent
  flows are strictly serial (playbook replay and `agent.py`'s loop both
  dispatch one action at a time) and `resources.admit` already drops the
  effective limit to 1 under real memory pressure regardless of this
  constant, so raising it serves neither tier.

- [ ] **Step 4: Run and verify it passes**

Run the targeted test files, then the full suite, then
`test_settings.EVERY_KEY` (this hand-kept list does not need a new entry
here since no new setting was added — only defaults changed — but confirm it
still passes).

- [ ] **Step 5: Commit**

```bash
git add cb claudebrowser/browser.py claudebrowser/perf.py claudebrowser/settings.py tests/
git commit -m "Unchain resource caps for a 16-core/31GB machine"
```

---

### Task 7: bookmarks/history/downloads become agent-visible

**Files:**
- Modify: `claudebrowser/api.py`
- Modify: `claudebrowser/browser.py`
- Test: `tests/test_offline.py` (extends existing generic op-list coverage automatically once ops exist), plus a targeted test file for the new `api_*` methods

**Interfaces:**
- Consumes: `store.py`'s existing `bookmark`/`unbookmark`/`bookmarks`/`history`/`clear_history` methods (already implemented — do not modify `store.py` in this task), `Browser.downloads`/`Browser.download_history` (already implemented).
- Produces: `Browser.api_bookmarks`, `Browser.api_bookmark_add`, `Browser.api_bookmark_remove`, `Browser.api_history`, `Browser.api_history_clear`, `Browser.api_downloads`.

- [ ] **Step 1: Write the failing tests**

```python
def test_api_bookmarks_lists_stored_bookmarks(self):
    self.store.bookmark("https://example.com", "Example")
    result = {}
    self.browser.api_bookmarks(None, lambda r: result.update(r))
    self.assertTrue(result["ok"])
    self.assertEqual(result["bookmarks"][0]["url"], "https://example.com")

def test_api_bookmark_add_adds_the_given_tab_url(self):
    result = {}
    self.browser.api_bookmark_add(self.tab_id, None, None, lambda r: result.update(r))
    self.assertTrue(result["ok"])
    self.assertTrue(self.store.is_bookmarked(self.current_tab_url))

def test_api_history_requires_a_query(self):
    # api.py's Op enforces required=True on `q` -- this test targets the
    # method's own behavior when called with an empty query, matching the
    # Op's contract rather than re-testing Param's own required-field logic.
    result = {}
    self.browser.api_history("", 10, lambda r: result.update(r))
    self.assertFalse(result["ok"])

def test_api_downloads_lists_session_downloads(self):
    result = {}
    self.browser.api_downloads(lambda r: result.update(r))
    self.assertTrue(result["ok"])
    self.assertIn("downloads", result)
```

(Match the fake-`Browser`/fake-`store` test harness already used by this
project's other `api_*` tests — read an existing one, e.g. whatever tests
`api_profile`, before writing these.)

- [ ] **Step 2: Run and verify it fails**

Run the targeted test file.

- [ ] **Step 3: Implement in `browser.py`**

```python
def api_bookmarks(self, q, done):
    rows = self.store.bookmarks(q or None) if self.store else []
    done({"ok": True, "bookmarks": rows})

def api_bookmark_add(self, tab, url, title, done):
    if not self.store:
        return done({"ok": False, "error": "history is unavailable"})
    target_url = url or self._tab_url(tab)  # reuse whatever helper already
    target_title = title or self._tab_title(tab)  # reads the active tab's
    self.store.bookmark(target_url, target_title or "")   # url/title elsewhere in browser.py
    done({"ok": True, "url": target_url})

def api_history(self, q, limit, done):
    if not q:
        return done({"ok": False, "error": "a search term is required"})
    rows = self.store.history(q, limit or 50) if self.store else []
    done({"ok": True, "history": rows})

def api_history_clear(self, done):
    if self.store:
        self.store.clear_history()
    done({"ok": True})

def api_downloads(self, done):
    done({"ok": True, "downloads": list(self.download_history)})
```

(`_tab_url`/`_tab_title` — use whatever existing helpers `browser.py`
already has for reading a tab's current URL/title, e.g. what
`toggle_bookmark` at line ~1927 already does; do not invent new ones if
equivalents exist.)

Add the matching `Op`s in `api.py`:

```python
Op("bookmarks", "/bookmarks", "GET",
   "List saved bookmarks, optionally filtered by a search term.",
   params=[Param("q", cli="opt", help="Filter text; omit to list all.")],
   call=lambda c, a: ("api_bookmarks", (a.get("q"),)), tab=False),

Op("bookmark-add", "/bookmark/add", "POST",
   "Bookmark a page -- the current tab by default, or a given URL.",
   params=[Param("url", cli="optarg"), Param("title", cli="optarg")],
   call=lambda c, a: ("api_bookmark_add", (_tab(a), a.get("url"), a.get("title")))),

Op("bookmark-remove", "/bookmark/remove", "POST", "Remove a bookmark.",
   params=[Param("url", required=True)],
   call=lambda c, a: ("api_bookmark_remove", (a["url"],)), tab=False, mcp=False),

Op("history", "/history", "GET",
   "Search browsing history. A search term is required -- this is a "
   "search, not a full listing, the same posture as `recall`.",
   params=[Param("q", required=True), Param("limit", "integer", cli="opt")],
   call=lambda c, a: ("api_history", (a["q"], a.get("limit"))), tab=False),

Op("history-clear", "/history/clear", "POST", "Delete all browsing history.",
   call=lambda c, a: ("api_history_clear", ()), tab=False, mcp=False),

Op("downloads", "/downloads", "GET",
   "List this session's downloads and their status.",
   call=lambda c, a: ("api_downloads", ()), tab=False),
```

Add `Browser.api_bookmark_remove(url, done)` similarly, calling
`self.store.unbookmark(url)`.

- [ ] **Step 4: Run and verify it passes**

Run the targeted test file, then the full suite (this also exercises
`test_offline.py`'s generic per-`Op` MCP/cbctl smoke coverage against the six
new ops automatically).

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/api.py claudebrowser/browser.py tests/
git commit -m "Expose bookmarks, history and downloads to cbctl and cb-mcp"
```

---

### Task 8: session restore

**Files:**
- Modify: `claudebrowser/store.py`
- Modify: `claudebrowser/browser.py`
- Modify: `claudebrowser/settings.py`
- Test: `tests/test_store.py`, `tests/test_settings.py`

**Interfaces:**
- Produces: `Store.save_session_tabs(urls)`, `Store.session_tabs() -> list[str]`, consumed by `browser.py`'s launch path.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_and_read_session_tabs(self):
    self.store.save_session_tabs(["https://a.example", "https://b.example"])
    self.assertEqual(self.store.session_tabs(),
                      ["https://a.example", "https://b.example"])

def test_save_session_tabs_overwrites_the_previous_list(self):
    self.store.save_session_tabs(["https://a.example"])
    self.store.save_session_tabs(["https://b.example"])
    self.assertEqual(self.store.session_tabs(), ["https://b.example"])

def test_restore_session_setting_defaults_on(self):
    self.assertEqual(settings.get("CB_RESTORE_SESSION"), "1")  # match
    # settings.py's actual on/off representation before asserting this
```

- [ ] **Step 2: Run and verify it fails**

Run the targeted test files.

- [ ] **Step 3: Implement**

In `store.py`, add a `CREATE TABLE IF NOT EXISTS session_tabs (url TEXT)`
next to the existing `history`/`bookmarks` table definitions, and:

```python
def save_session_tabs(self, urls):
    self._write("DELETE FROM session_tabs")
    for url in urls:
        self._write("INSERT INTO session_tabs (url) VALUES (?)", (url,))

def session_tabs(self):
    return [r["url"] for r in self._query("SELECT url FROM session_tabs")]
```

(Match the exact `_write`/`_query` call signatures already used by
`record`/`history` in this file — read them first.)

In `browser.py`: on tab open/close (wherever the tab list is already
mutated — reuse that hook, do not add a new timer/poll), call
`self.store.save_session_tabs([t.view.get_uri() for t in self.tabs.values()
if not t.private and t.view.get_uri()])` — **only non-private tabs**,
matching the existing `store.recordable()`-adjacent privacy posture (a
private tab is never written to `store.py`, and this table is no exception).

On launch, behind `CB_RESTORE_SESSION` (default on), read
`self.store.session_tabs()` and open each URL as a tab through the *existing*
tab-opening path (`api_open`/whatever `_admit`-routed method the launch
sequence already uses for other startup tabs) — never a bypass of admission
control, since a restored session of 20 tabs hitting `_admit` at once is
exactly the load `resources.py` exists to prevent.

Add `CB_RESTORE_SESSION` to `settings.py`'s registry, default on, boolean
validator (mirror `CB_LIGHT`'s validator).

- [ ] **Step 4: Run and verify it passes**

Run the targeted test files, then the full suite, then
`test_settings.EVERY_KEY` (add `CB_RESTORE_SESSION` to that hand-kept list —
this is deliberate, per the project's convention that a new setting is added
in both places on purpose).

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/store.py claudebrowser/browser.py claudebrowser/settings.py tests/
git commit -m "Add session restore (CB_RESTORE_SESSION, default on)"
```

---

### Task 9: ~~ad/tracker blocking~~ — VOID, already shipped

**Struck during execution (2026-08-29), before any chunk touched it.** While
updating `~/.agents/skills/web-browsing/SKILL.md` for the Tier 1 tools, a
`grep` for `CB_BLOCK` turned up that this browser already has a real,
production WebKit content-filter ad/tracker blocker: `perf.BLOCKED` (a
curated domain list, no anti-bot vendors in it — same safety property this
task's design called for), `perf.blocking_enabled()` (`CB_BLOCK`, default on),
and `perf.load_content_filter(...)` wired into `browser.py:576` via
`GLib.idle_add`. This plan's design spec did not account for it and proposed
building a second, parallel implementation (`blocklist.py`) — that would have
been genuine duplicate work. Do not build this task. If the existing list
ever needs domains added, edit `perf.BLOCKED` directly; there is no reason
for a second list or a second `UserContentFilterStore` registration.

<details>
<summary>Original task text (kept for the record, not to be built)</summary>

**Files:**
- Create: `claudebrowser/blocklist.py`
- Modify: `claudebrowser/browser.py`
- Modify: `claudebrowser/settings.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_blocklist.py`

**Files:**
- Create: `claudebrowser/blocklist.py`
- Modify: `claudebrowser/browser.py`
- Modify: `claudebrowser/settings.py`
- Modify: `CLAUDE.md`
- Test: `tests/test_blocklist.py`

**Interfaces:**
- Produces: `blocklist.RULES` (a Python list of domains), `blocklist.rules_json() -> str` (WebKit content-blocker JSON), consumed by `browser.py`'s `WebKit2.UserContentFilterStore` wiring.

**Precondition check, before writing any implementation code:** confirm
`WebKit2.UserContentFilterStore` and `UserContentManager.add_filter`/
`remove_filter` behave as expected against the actually-installed
`libwebkit2gtk-4.1` on this machine — write and run a tiny standalone script
that creates a store, saves a one-rule filter, and applies it to a real
`WebView` loading a test page, before building the rest of this task on top
of the API. If it does not behave as documented, halt and report rather than
guessing further — this is the one piece of this plan resting on an API
confirmed only via GI introspection, not a live WebView.

- [ ] **Step 1: Write the failing tests**

```python
import unittest
import json

from claudebrowser import blocklist

ANTI_BOT_VENDORS = ("recaptcha.net", "google.com/recaptcha", "hcaptcha.com",
                     "challenges.cloudflare.com", "gstatic.com/recaptcha")


class TestBlocklist(unittest.TestCase):
    def test_never_blocks_a_captcha_or_anti_bot_vendor(self):
        # The one test in this whole plan that exists specifically to
        # prevent a silent regression of the "detect, never bypass" rule --
        # see CLAUDE.md and the prior blocked-detection spec.
        for domain in blocklist.RULES:
            for vendor in ANTI_BOT_VENDORS:
                self.assertNotIn(vendor, domain,
                    "%s must never be in the ad-block list" % vendor)

    def test_rules_json_is_valid_webkit_content_blocker_format(self):
        rules = json.loads(blocklist.rules_json())
        self.assertIsInstance(rules, list)
        for rule in rules:
            self.assertIn("trigger", rule)
            self.assertIn("url-filter", rule["trigger"])
            self.assertEqual(rule["action"]["type"], "block")

    def test_rules_json_covers_at_least_common_ad_tracker_domains(self):
        rendered = blocklist.rules_json()
        self.assertIn("doubleclick.net", rendered)
        self.assertIn("google-analytics.com", rendered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_blocklist -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `blocklist.py`**

```python
"""A small, curated ad/tracker block list, converted to WebKit's
Safari-style content-blocker JSON.

**Why curated, not EasyList.** Full EasyList-syntax compatibility is a real
parser-writing project on its own -- this ships a short, maintained-like-
scrub.py's-patterns list of common ad/tracker/analytics domains instead. Not
comprehensive; honest about the ceiling, the same posture VPN Mode and
scrub.py already take in this project.

**Why anti-bot vendors are never in this list, and must never be added.**
This browser's other hard rule (see extract.BLOCKED and the blocked-state
detection spec) is that a CAPTCHA/anti-bot challenge is detected and
reported, never bypassed. Blocking a challenge vendor's script would make
the challenge never render -- which makes `blocked` wrongly report
`blocked: false` on a page that is, in fact, walled off, and is functionally
an attempted bypass. test_blocklist.py's
test_never_blocks_a_captcha_or_anti_bot_vendor exists specifically to catch
a future addition that violates this.
"""

RULES = [
    "doubleclick.net",
    "googlesyndication.com",
    "google-analytics.com",
    "googletagmanager.com",
    "googletagservices.com",
    "facebook.net",
    "connect.facebook.net",
    "adservice.google.com",
    "amazon-adsystem.com",
    "scorecardresearch.com",
    "quantserve.com",
    "outbrain.com",
    "taboola.com",
    "criteo.com",
    "adnxs.com",
]


def rules_json():
    import json
    return json.dumps([
        {"trigger": {"url-filter": ".*" + domain.replace(".", r"\.") + ".*"},
         "action": {"type": "block"}}
        for domain in RULES
    ])
```

- [ ] **Step 4: Run and verify it passes**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_blocklist -v`

- [ ] **Step 5: Wire into `browser.py`** (after the precondition check above
      has actually confirmed the API works against a real `WebView`)

```python
def _apply_adblock(self, content_manager):
    if not settings.get("CB_ADBLOCK", default=True):
        return
    store = WebKit2.UserContentFilterStore.new(
        os.path.join(storage.data_dir(), "adblock-filters"))
    store.save("claude-browser-adblock", GLib.Bytes.new(
        blocklist.rules_json().encode("utf-8")), None, self._on_filter_saved)

def _on_filter_saved(self, store, result, content_manager):
    try:
        filt = store.save_finish(result)
        content_manager.add_filter(filt)
    except Exception as e:
        print("adblock: disabled (%s)" % e, flush=True)
```

(Exact call shape for `UserContentFilterStore.save`/`save_finish` — this is
an async GIO-style pair; confirm the precise signature against the installed
library during the precondition check in Step 0, since the introspected
typelib alone does not pin the async callback's exact argument order.)

Call `_apply_adblock` wherever tabs' `UserContentManager` is otherwise
configured (near where `CONSOLE_SHIM`/`PASSWORD_JS`/`SNAPSHOT_SHIM` are
registered).

Add `CB_ADBLOCK` to `settings.py`, default on, boolean validator.

- [ ] **Step 6: Run and verify it passes**

Run the full suite, then `python3 -m py_compile claudebrowser/*.py`.

- [ ] **Step 7: Update `CLAUDE.md`**

Add `blocklist.py` and `fills.py` to the layout table (if not already added
by an earlier task's commit — check first). Add one sentence under "the
rules that matter" noting that ad-blocking changes what `text`/`snapshot`
see on a page (elements hidden by a filter no longer appear), and restate
the anti-bot-vendor exclusion rule there for visibility alongside the
existing blocked-state detection rule.

- [ ] **Step 8: Commit**

```bash
git add claudebrowser/blocklist.py claudebrowser/browser.py claudebrowser/settings.py CLAUDE.md tests/test_blocklist.py
git commit -m "Add ad/tracker blocking via WebKit's native content-filter API"
```

</details>

---

## Self-Review Notes (already applied above)

- Every task names exact files, exact function signatures, and real code —
  no "add appropriate tests" placeholders.
- Type/name consistency checked across tasks: `extract.snapshot()`/
  `extract.fill_many()` (Task 2) match what `browser.py`'s `api_snapshot`/
  `api_fill_many` (Task 3) call; `fills.resolve_value`/`fills.FillError`
  (Task 1) match their use in both Task 3 and Task 5; `Store.matching()`
  (Task 4) matches its use in Task 3's... no, Task 4's own `browser.py`
  nav-result change.
- Assumption flagged explicitly rather than guessed past: Task 9's WebKit
  content-filter async call shape needs live confirmation before the rest of
  that task is built — this is called out as a precondition check, not
  silently assumed.
- Every task that touches `browser.py` is sequenced (no concurrent chunks
  share that file), per this plan's own Global Constraints.

## Execution

Per the user's standing instruction: dispatch via the `scoped-delivery`
skill. Suggested chunk grouping (adjust live if a chunk's actual diff is
smaller or larger than expected):

- **Chunk 1:** Task 1 + Task 2 (no shared files, both independent of the rest)
- **Chunk 2:** Task 3 (the integration point; substantial enough alone)
- **Chunk 3:** Task 4 + Task 5 (both build on Chunk 2)
- **Chunk 4:** Task 6 + Task 7 (both small, independent Tier-2 items)
- **Chunk 5:** Task 8
- ~~Chunk 6: Task 9~~ — struck, see Task 9's note: ad/tracker blocking already
  exists (`perf.BLOCKED` / `perf.blocking_enabled()` / `CB_BLOCK`, wired at
  `browser.py:576`). Tier 2 ends at Chunk 5.

Per the user's instruction on subagent lifecycle: dispatch one implementer
subagent and continue it (via follow-up messages) across up to three chunks
before retiring it and starting a fresh one for the next group, to keep any
single subagent's context well clear of the 200-225K range. A ledger file
(`docs/plans/2026-08-29-agentic-spine-and-power-user-layer.ledger.md`,
git-ignored) is appended after each chunk is independently verified — not
after the subagent merely reports success.
