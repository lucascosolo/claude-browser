# PII Profile Store and Blocked-State Detection Implementation Plan

> **For agentic workers:** this plan has 3 tasks; the coordinator has chosen to execute it via the `scoped-delivery` skill anyway, in strictly sequential chunks (Task 2 depends on Task 1; Task 3 depends on Task 2; Tasks 2 and 3 both touch `browser.py` and must not run concurrently). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claude Browser a keyring-backed personal-info (PII) profile an agent can read to fill out forms, and a CAPTCHA/anti-bot "blocked-state" check an agent can call when a step seems to have had no effect — the two gaps identified in the design spec for swapping Claude Browser into Openclaw's `unbroker` data-broker-removal skill.

**Architecture:** A new `profile.py` module (keyring vault, mirrors `passwords.py`'s pattern but its own module/schema), two new `api.py` ops (`profile` read, MCP-exposed; `profile-set` write, not MCP-exposed) plus their `Browser.api_profile`/`api_profile_set` methods, a new `extract.py` JS snippet (`BLOCKED`) plus one new `api.py` op (`blocked`, MCP-exposed, no new Browser method needed — it goes through the existing `api_eval` path like `text`/`markdown`/`find`), and a `cb:profile` GUI page mirroring `cb:passwords`.

**Tech Stack:** Python 3 standard library + PyGObject (`gi.repository.Secret` for the keyring), no new dependencies.

## Global Constraints

- No SSN or financial fields in the profile — documented as convention, not enforced in code (spec explicitly scopes this out).
- Profile storage: system keyring only (freedesktop Secret Service via `gi.repository.Secret`), never a file this project invents.
- `profile` (read) is MCP-exposed; `profile-set` (write) is **not** MCP-exposed — mirrors `settings`/`persona`.
- `blocked` detects and reports only. It must never attempt to solve, click through, or otherwise bypass a CAPTCHA/challenge — this is a hard requirement, not a follow-up.
- No display needed for `profile.py`'s own tests (inject `MemoryBackend`), matching `passwords.py`'s testing pattern. `browser.py`, `pages.py`'s HTML generation is exercised by `python3 -m py_compile claudebrowser/*.py` only — there is no display in test runs, per existing project convention.
- Every new op added to `claudebrowser/api.py` must be added to `tests/test_offline.py`'s `TestApiRegistry.test_every_op_builds_a_call`'s `cases` dict, or that test fails (it asserts the case dict's keys equal every callable route in `api.OPS`).
- Run tests with `CB_AUTOSTART=0 python3 -m unittest discover -s tests`.

---

## Task 1: `profile.py` — the PII vault

**Files:**
- Create: `claudebrowser/profile.py`
- Test: `tests/test_profile.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (for Task 2 and Task 3):
  - `profile.open_vault() -> Vault | None`
  - `class Vault: __init__(self, backend=None)`
  - `Vault.get_all(self) -> dict` — every stored field, `{}` if none.
  - `Vault.get(self, key) -> str | None`
  - `Vault.set(self, key, value) -> bool` — `value` falsy (`None` or `""`) deletes the field; returns `False` only when `key` is falsy.
  - `class MemoryBackend` — test double, no persistence.
  - `class SecretBackend` — the real freedesktop Secret Service backend.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_profile.py`:

```python
"""The profile vault, against a fake keyring. No display, no Secret Service."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claudebrowser.profile import MemoryBackend, Vault


class Fields(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(MemoryBackend())

    def test_empty_vault_has_no_fields(self):
        self.assertEqual(self.vault.get_all(), {})

    def test_set_and_get_a_field(self):
        self.assertTrue(self.vault.set("first_name", "Ada"))
        self.assertEqual(self.vault.get("first_name"), "Ada")

    def test_unknown_key_is_none(self):
        self.assertIsNone(self.vault.get("nope"))

    def test_several_fields_coexist(self):
        self.vault.set("first_name", "Ada")
        self.vault.set("last_name", "Lovelace")
        self.assertEqual(self.vault.get_all(),
                         {"first_name": "Ada", "last_name": "Lovelace"})

    def test_setting_again_updates_rather_than_duplicates(self):
        self.vault.set("email", "old@example.com")
        self.vault.set("email", "new@example.com")
        self.assertEqual(self.vault.get("email"), "new@example.com")

    def test_setting_with_no_value_deletes_the_field(self):
        self.vault.set("phone", "555-0100")
        self.vault.set("phone", None)
        self.assertIsNone(self.vault.get("phone"))
        self.assertEqual(self.vault.get_all(), {})

    def test_setting_with_an_empty_string_also_deletes(self):
        self.vault.set("phone", "555-0100")
        self.vault.set("phone", "")
        self.assertIsNone(self.vault.get("phone"))

    def test_deleting_an_unset_field_does_not_raise(self):
        self.assertTrue(self.vault.set("nope", None))
        self.assertEqual(self.vault.get_all(), {})

    def test_missing_key_is_refused(self):
        self.assertFalse(self.vault.set("", "value"))
        self.assertFalse(self.vault.set(None, "value"))

    def test_values_needing_json_escaping_round_trip(self):
        tricky = 'Line1\nLine2 "quoted"  '
        self.vault.set("note", tricky)
        self.assertEqual(self.vault.get("note"), tricky)

    def test_other_fields_survive_a_delete(self):
        self.vault.set("first_name", "Ada")
        self.vault.set("last_name", "Lovelace")
        self.vault.set("last_name", None)
        self.assertEqual(self.vault.get_all(), {"first_name": "Ada"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_profile -v`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'claudebrowser.profile'`

- [ ] **Step 3: Write `claudebrowser/profile.py`**

```python
"""The personal-info profile, kept in the system keyring.

**Why this exists.** An agent filling out a data-broker opt-out form needs the
user's own name, address, email and phone -- not a secret, but personal data
that should not sit in a plaintext file either. The system keyring is already
the answer this project gives for saved logins (see passwords.py); a profile
field gets the same treatment for the same reason: we store, we do not invent
a file format of our own.

**Why this is not part of passwords.py.** The two differ on the axis that
matters most here: exposure. A saved login is never handed to an agent or
written to a playbook -- passwords.py is built entirely around that rule. A
profile field exists *specifically* so an agent can read it and put it in a
form; that is the whole point of storing it. Mixing the two into one module
would blur a boundary this codebase draws on purpose. They also differ in
shape: a login is keyed by (origin, username); a profile is one identity with
no origin at all, so there is exactly one item to store, not one per site.

**What is stored.** A single JSON object of arbitrary string fields, under one
keyring item. There is no fixed schema -- different opt-out forms want
different fields -- but by convention (never enforced in code) callers use
keys like `first_name`, `last_name`, `dob`, `email`, `phone`, `address_line1`,
`address_line2`, `city`, `state`, `zip`. Deliberately excluded from that
convention: SSN and financial fields. Opt-out forms don't need them, and
storing them here would raise the stakes of a compromise for no operational
benefit.

No GTK import here, deliberately: this is the layer that has to be testable
without a display, and the tests inject `MemoryBackend` in place of the
keyring.
"""

import json

APP = "Claude Browser"
KIND = "profile"


class MemoryBackend:
    """A dict pretending to be a keyring. Tests only; nothing persists."""

    def __init__(self):
        self.items = {}

    def store(self, attrs, _label, secret):
        self.items[attrs.get("kind", "")] = secret
        return True

    def lookup(self, attrs):
        return self.items.get(attrs.get("kind", ""))


class SecretBackend:
    """The real one: freedesktop Secret Service via libsecret."""

    def __init__(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self._secret = Secret
        # net.claudebrowser.Profile is its own schema, not passwords.py's --
        # see the module docstring on why the two are never merged.
        self._schema = Secret.Schema.new(
            "net.claudebrowser.Profile",
            Secret.SchemaFlags.NONE,
            {"kind": Secret.SchemaAttributeType.STRING},
        )

    def store(self, attrs, label, secret):
        return self._secret.password_store_sync(
            self._schema, attrs, self._secret.COLLECTION_DEFAULT,
            label, secret, None)

    def lookup(self, attrs):
        return self._secret.password_lookup_sync(self._schema, attrs, None)


def open_vault():
    """A Vault, or None if this machine has no keyring.

    Same posture as passwords.open_vault: a box without a Secret Service
    should cost you the profile feature, not your browser.
    """
    try:
        return Vault(SecretBackend())
    except Exception as e:
        print("profile: disabled (%s)" % e, flush=True)
        return None


class Vault:
    def __init__(self, backend=None):
        self.backend = backend or SecretBackend()

    def get_all(self):
        """Every stored field, or {} if nothing has been saved yet."""
        raw = self.backend.lookup({"kind": KIND})
        if not raw:
            return {}
        try:
            fields = json.loads(raw)
        except ValueError:
            return {}
        return fields if isinstance(fields, dict) else {}

    def get(self, key):
        return self.get_all().get(key)

    def set(self, key, value):
        """Set one field, or delete it when `value` is None or empty."""
        if not key:
            return False
        fields = self.get_all()
        if value:
            fields[key] = value
        else:
            fields.pop(key, None)
        return bool(self.backend.store(
            {"kind": KIND}, "%s - profile" % APP, json.dumps(fields)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_profile -v`
Expected: PASS, all 11 tests.

- [ ] **Step 5: Compile-check and commit**

Run: `python3 -m py_compile claudebrowser/profile.py`
Expected: no output (success).

```bash
git add claudebrowser/profile.py tests/test_profile.py
git commit -m "Add profile.py: a keyring-backed PII vault for agent form-filling"
```

---

## Task 2: `blocked` detection + `profile`/`profile-set` ops

**Files:**
- Modify: `claudebrowser/extract.py` (add `BLOCKED` constant after `TITLE`, ~line 100)
- Modify: `claudebrowser/api.py` (add `blocked` op after `console`, ~line 255; add `profile`/`profile-set` ops after `settings`, ~line 372)
- Modify: `claudebrowser/browser.py` (import `profile`; init `self.profile`; add `api_profile`/`api_profile_set` methods)
- Modify: `tests/test_offline.py` (add the three new routes to `test_every_op_builds_a_call`'s `cases` dict; extend `test_mcp_exposes_every_agent_facing_op`)

**Interfaces:**
- Consumes from Task 1: `profile.open_vault()`, `Vault.get_all()`, `Vault.set(key, value)`.
- Produces for Task 3:
  - `Browser.profile` — a `profile.Vault` instance, or `None`.
  - `Browser.api_profile(self, done)` — `done({"ok": True, "available": bool, "fields": dict})`.
  - `Browser.api_profile_set(self, key, value, done)` — `done({"ok": True, "fields": dict})` or `done({"ok": False, "error": str})`.

- [ ] **Step 1: Write the failing test for the op registry**

Open `tests/test_offline.py` and edit `test_every_op_builds_a_call`'s `cases` dict (around line 418-431). Change:

```python
            "/clear": {"kind": "pagetext"},
            "/playbook/record": {"action": "status"}, "/playbook/list": {},
            "/playbook/run": {"name": "login"}, "/playbook/delete": {"name": "login"},
            "/persona": {}, "/settings": {}, "/vpn": {},
        }
```

to:

```python
            "/clear": {"kind": "pagetext"},
            "/playbook/record": {"action": "status"}, "/playbook/list": {},
            "/playbook/run": {"name": "login"}, "/playbook/delete": {"name": "login"},
            "/persona": {}, "/settings": {}, "/vpn": {},
            "/blocked": {}, "/profile": {}, "/profile/set": {"key": "first_name"},
        }
```

Also extend `test_mcp_exposes_every_agent_facing_op` (around line 480-491) by adding, right before the final `assertNotIn` lines:

```python
        self.assertIn("browser_blocked", tools)
        self.assertIn("browser_profile", tools)
        # Writing the user's own profile is not something an agent should be
        # able to do as a side effect of some other goal -- same reasoning as
        # settings/persona.
        self.assertNotIn("browser_profile-set", tools)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_offline.TestApiRegistry -v`
Expected: FAIL — `test_every_op_builds_a_call` fails because `/blocked`, `/profile`, `/profile/set` are not yet in `api.OPS`; `test_mcp_exposes_every_agent_facing_op` fails because `browser_blocked`/`browser_profile` are not yet tools.

- [ ] **Step 3: Add the `BLOCKED` snippet to `extract.py`**

In `claudebrowser/extract.py`, after the existing `TITLE` constant (currently the line `TITLE = r"""JSON.stringify({url: location.href, title: document.title})"""`), add:

```python

BLOCKED = r"""
(function () {
  var html = document.documentElement.innerHTML;
  var text = document.body ? document.body.innerText : '';
  var title = (document.title || '').toLowerCase();
  var checks = [
    ['recaptcha', /recaptcha/i.test(html) ||
                  !!document.querySelector('iframe[src*="recaptcha"], .g-recaptcha')],
    ['hcaptcha', /hcaptcha/i.test(html) ||
                 !!document.querySelector('iframe[src*="hcaptcha"], .h-captcha')],
    ['turnstile', !!document.querySelector('.cf-turnstile, #cf-challenge-stage') ||
                  title.indexOf('just a moment') >= 0],
    ['generic', /verify you.{0,3}(are|.re).{0,3}(a )?human|i.?m not a robot|unusual traffic|access denied/i.test(text)],
  ];
  for (var i = 0; i < checks.length; i++) {
    if (checks[i][1]) return JSON.stringify({ blocked: true, kind: checks[i][0] });
  }
  return JSON.stringify({ blocked: false, kind: null });
})()
"""
```

(This detects and reports only. Nothing in this snippet, or anywhere else touched by this task, attempts to solve or click through a challenge.)

- [ ] **Step 4: Add the `blocked` op to `api.py`**

In `claudebrowser/api.py`, after the `console` op (ends with `call=lambda c, a: ("api_console", (_tab(a), a.get("pattern")))),`) and before the `# Cookies and caches.` comment, add:

```python

    Op("blocked", "/blocked", "GET", "Check whether the page is showing a "
       "CAPTCHA or anti-bot challenge instead of its real content. Call this "
       "when a click or fill had no visible effect. This never attempts to "
       "solve or bypass a challenge -- it only reports one.",
       call=_js("BLOCKED")),
```

- [ ] **Step 5: Add the `profile`/`profile-set` ops to `api.py`**

In `claudebrowser/api.py`, after the `settings` op (ends with `tab=False, mcp=False),`) and before the closing `]` of `OPS`, add:

```python

    # Not an MCP tool for `profile-set`, and for the same reason `settings`
    # and `persona` aren't: this is the user's own data, and an agent acting
    # toward some other goal has no business rewriting their name or address
    # as a side effect. `profile` itself IS exposed -- an agent needs to read
    # it to fill out forms, which is the entire point of storing it.
    Op("profile", "/profile", "GET",
       "Report the stored personal-info profile (name, address, contact "
       "fields) used to fill out forms.",
       call=lambda c, a: ("api_profile", ()), tab=False),

    Op("profile-set", "/profile/set", "POST",
       "Set or delete one field in the personal-info profile.",
       params=[Param("key", required=True),
               Param("value", cli="optarg",
                     help="New value; omit to delete the field.")],
       call=lambda c, a: ("api_profile_set", (a["key"], a.get("value"))),
       tab=False, mcp=False),
```

- [ ] **Step 6: Wire `profile.py` into `browser.py`**

In `claudebrowser/browser.py`, edit the import block (currently):

```python
from . import (agent, ai, auth, envfile, extract, findbar, pages, pagetext,  # noqa: E402
               panel_html, passwords, perf, personas, playbooks, progress,
               reader, resources, scrub, search, settings, siterules, storage,
               store, style, tabnames, urls, vpn, watchlater, youtube)
```

to:

```python
from . import (agent, ai, auth, envfile, extract, findbar, pages, pagetext,  # noqa: E402
               panel_html, passwords, perf, personas, playbooks, profile,
               progress, reader, resources, scrub, search, settings,
               siterules, storage, store, style, tabnames, urls, vpn,
               watchlater, youtube)
```

Then find the line `self.vault = passwords.open_vault()` (near where `self.content.register_script_message_handler("cbui")` is set up) and add right after it:

```python
        # Same "a box without a keyring should cost you the feature, not your
        # browser" posture as self.vault above. See profile.py.
        self.profile = profile.open_vault()
```

- [ ] **Step 7: Add `api_profile`/`api_profile_set` methods to `browser.py`**

Find the end of `api_persona` (the line `done({"ok": True, **personas.describe()})`, immediately followed by `def _change_setting(self, key, value):`). Insert the two new methods between them:

```python

    def api_profile(self, done):
        """Report every stored profile field, for an agent to fill forms with.

        Shaped like api_persona/api_settings: no arguments, since there is
        only ever one profile to read. Same "unavailable, not broken" posture
        as passwords.py when there is no keyring on this machine.
        """
        if self.profile is None:
            return done({"ok": True, "available": False, "fields": {}})
        done({"ok": True, "available": True, "fields": self.profile.get_all()})

    def api_profile_set(self, key, value, done):
        """Set one profile field, or delete it when `value` is empty.

        Not an MCP tool -- see the profile-set Op in api.py -- so the only
        callers are cbctl and cb:profile's own Save/Delete buttons. An agent
        driving the browser toward some other goal has no business rewriting
        the user's own name or address as a side effect.
        """
        if self.profile is None:
            return done({"ok": False, "error": "the system keyring is unavailable"})
        if not key:
            return done({"ok": False, "error": "a field name is required"})
        self.profile.set(key, value)
        done({"ok": True, "fields": self.profile.get_all()})
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_offline -v`
Expected: PASS, all tests including `TestApiRegistry` and `TestCbctlSurface`.

Run: `CB_AUTOSTART=0 python3 -m unittest discover -s tests`
Expected: PASS, full suite (previous count + the 11 new `test_profile` tests).

- [ ] **Step 9: Compile-check and commit**

Run: `python3 -m py_compile claudebrowser/*.py`
Expected: no output (success).

```bash
git add claudebrowser/extract.py claudebrowser/api.py claudebrowser/browser.py tests/test_offline.py
git commit -m "Add blocked-state detection and profile/profile-set ops"
```

---

## Task 3: `cb:profile` page + docs

**Files:**
- Modify: `claudebrowser/pages.py` (NAV entry, `profile_page`, `_profile_row`, `_profile_add_row`, client-side `cbui.pfsave`/`pfdrop`/`pfadd`)
- Modify: `claudebrowser/browser.py` (`_render_internal` dispatch for `name == "profile"`; `_on_ui_message` handling for `profile_set`)
- Modify: `CLAUDE.md` (layout table entry for `profile.py`)

**Interfaces:**
- Consumes from Task 2: `Browser.profile`, `Browser.api_profile_set(key, value, done)`.
- Produces: nothing consumed by a later task (final task in this plan).

- [ ] **Step 1: Add the NAV entry in `pages.py`**

Find the `NAV` tuple's `cb:passwords` entry:

```python
    ("cb:passwords", "Logins",
     "M6 10V7a6 6 0 1 1 12 0v3h1a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9a1 1"
     " 0 0 1 1-1zm2 0h8V7a4 4 0 0 0-8 0z"),
    ("cb:playbooks", "Playbooks",
```

Insert a new entry between them:

```python
    ("cb:passwords", "Logins",
     "M6 10V7a6 6 0 1 1 12 0v3h1a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9a1 1"
     " 0 0 1 1-1zm2 0h8V7a4 4 0 0 0-8 0z"),
    ("cb:profile", "Profile",
     "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 "
     "1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"),
    ("cb:playbooks", "Playbooks",
```

- [ ] **Step 2: Add `profile_page` and its row helpers to `pages.py`**

Immediately after the existing `_never_row` function (right before `def playbooks_page(...)`), add:

```python
def profile_page(palette, nonce, fields, available=True):
    """The stored personal-info profile used to fill out forms.

    Unlike passwords, values are rendered in the clear: a profile field exists
    specifically so it can be read -- by the user here, and by an agent over
    the control API -- so there is no secret to hide until asked for.
    """
    if not available:
        return shell("Profile", palette, nonce, "cb:profile", _empty(
            "&#128100;", "The system keyring is unavailable",
            "The profile is stored in the freedesktop Secret Service. On this "
            "desktop that is <code>gnome-keyring</code> — start it and reopen "
            "the browser."))

    if fields:
        body = '<div class="rows">%s</div>' % "".join(
            _profile_row(k, v) for k, v in sorted(fields.items()))
    else:
        body = _empty("&#128100;", "No profile fields saved",
                      "Add a field below — name, address and contact fields "
                      "an agent can use to fill out forms on your behalf.")
    return shell("Profile", palette, nonce, "cb:profile", body + _profile_add_row())


def _profile_row(key, value):
    return """
    <div class="row set">
      <span class="sl"><span class="rt">%(key)s</span></span>
      <span class="sc">
        <input class="sin" type="text" value="%(value)s" data-k="%(dkey)s"
               autocomplete="off" spellcheck="false">
        <button class="pbbtn" onclick="return cbui.pfsave(event, %(jkey)s)">Save</button>
        <button class="pbbtn" onclick="return cbui.pfdrop(event, %(jkey)s)">Delete</button>
      </span>
    </div>""" % {
        "key": _e(key), "value": _e(value or ""), "dkey": _e(key), "jkey": _js(key),
    }


def _profile_add_row():
    return """
    <div class="row set">
      <span class="sl"><span class="rt">Add a field</span>
        <span class="sx">e.g. first_name, address_line1, email</span></span>
      <span class="sc">
        <input class="sin" id="pfkey" type="text" placeholder="field name"
               autocomplete="off" spellcheck="false">
        <input class="sin" id="pfval" type="text" placeholder="value"
               autocomplete="off" spellcheck="false">
        <button class="pbbtn" onclick="return cbui.pfadd(event)">Add</button>
      </span>
    </div>"""
```

- [ ] **Step 3: Add the client-side actions**

In the `window.cbui = {` object (in the same file), find the `pwallow` method and its closing `},`:

```python
    pwallow: function (ev, origin) {
      ev.preventDefault(); ev.stopPropagation();
      var el = ev.currentTarget.closest('.row');
      if (el) { el.style.transition = 'opacity .12s'; el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 120); }
      return this.send({action: 'pw_allow', url: origin});
    },
```

Add, right after it:

```python
    // cb:profile. Same fixed {action, url, title} shape as set_setting: the
    // field name travels as url, the value as title.
    pfsave: function (ev, key) {
      ev.preventDefault();
      var el = ev.currentTarget;
      var box = el.parentNode.querySelector('input');
      if (!box) return false;
      return this.send({action: 'profile_set', url: key, title: box.value});
    },
    pfdrop: function (ev, key) {
      ev.preventDefault();
      var el = ev.currentTarget.closest('.row');
      if (el) { el.style.transition = 'opacity .12s'; el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 120); }
      return this.send({action: 'profile_set', url: key, title: ''});
    },
    pfadd: function (ev) {
      ev.preventDefault();
      var k = document.getElementById('pfkey'), v = document.getElementById('pfval');
      var key = k ? k.value.trim() : '';
      if (!key) { if (k) k.focus(); return false; }
      return this.send({action: 'profile_set', url: key, title: v ? v.value : ''});
    },
```

(Note: this is a `.py` file containing a JS template literal — write the JS exactly as shown inside the existing triple-quoted string the `pwallow` block already lives in; do not create a new Python string.)

- [ ] **Step 4: Wire the page and the action into `browser.py`**

In `_render_internal`, find the `passwords` dispatch block:

```python
        if name == "passwords":
            if self.vault is None:
                return pages.passwords_page(palette, self.nonce, [], available=False)
            return pages.passwords_page(palette, self.nonce, self.vault.entries(),
                                        never=self.vault.never_list())
```

Add, right after it:

```python

        if name == "profile":
            if self.profile is None:
                return pages.profile_page(palette, self.nonce, {}, available=False)
            return pages.profile_page(palette, self.nonce, self.profile.get_all())
```

In `_on_ui_message`, find the `pw_reveal` block:

```python
        elif action == "pw_reveal" and self.vault:
            # The page asked for one secret by name. It gets exactly that one,
            # written back into the row it came from -- cb:passwords is rendered
            # without any password in it, which is the point of the eye button.
            secret = self.vault.secret(url, title)
            tab = self.current()
            if secret is not None and tab is not None:
                self._pw_js(tab, "cbui.reveal(%s, %s)"
                            % (json.dumps(data.get("idx")), json.dumps(secret)))
```

Add, right after it (before `elif action == "clear_data":`):

```python
        elif action == "profile_set" and self.profile:
            # url carries the field name, title the new value (or "" to
            # delete it) -- same fixed message shape reasoning as set_setting.
            self.api_profile_set(url, title or None, lambda result: self._reload_internal())
```

- [ ] **Step 5: Update `CLAUDE.md`'s layout table**

Find the line:

```
  passwords.py saved logins in the system keyring + the injected form script
```

Add, right after it:

```
  profile.py   the PII profile agents fill forms from -- keyring-backed, one
               identity, no fixed schema (GTK-free)
```

- [ ] **Step 6: Manual verification (no display-based test exists for this page)**

Run: `python3 -m py_compile claudebrowser/pages.py claudebrowser/browser.py`
Expected: no output (success).

Run the full suite once more to confirm nothing else broke:
Run: `CB_AUTOSTART=0 python3 -m unittest discover -s tests`
Expected: PASS, full suite.

If a display is available, launch the browser (`./cb`), navigate to `cb:profile`, add a field, confirm it persists across a reload, edit it, delete it, and confirm the empty state renders when no fields remain. This step is best-effort — the project has no automated coverage for `browser.py`/`pages.py` rendering, per existing convention (`browser.py`, `control.py`, `findbar.py` and `__main__.py` need a display and are never imported by any test).

- [ ] **Step 7: Commit**

```bash
git add claudebrowser/pages.py claudebrowser/browser.py CLAUDE.md
git commit -m "Add cb:profile page for viewing/editing the PII profile"
```
