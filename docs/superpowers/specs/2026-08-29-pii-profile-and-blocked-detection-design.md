# PII profile store and blocked-state detection

Date: 2026-08-29

## Context

The user is pivoting Claude Browser's role: alongside becoming a daily-driver
Chrome replacement (a separate, later initiative), it should be a reliable
substrate for autonomous agent work — specifically, driving the "unbroker"
data-broker-removal skill that already runs inside Openclaw (the user's AI
toolsuite, at `~/.openclaw`, imported from a retired "Hermes" setup).

Research into `~/.openclaw` found that unbroker already owns job orchestration:
`scripts/pdd.py` is a deterministic state machine (`pdd next` → next action,
`pdd record` → outcome) that already handles the multi-day nature of a removal
(submit → wait for a confirmation email → re-check weeks later). That layer
needs no change. What it currently lacks is a good local browser to drive:
today it uses Browserbase (a cloud browser) for public opt-out forms, driven by
natural-language instructions an LLM agent executes via Browserbase's own tool
integration, and a separate Chrome-over-raw-CDP process for flows needing the
user's real logins (webmail, guided opt-out gates).

Swapping Claude Browser in for that browser-driving tier removes the CDP tier
entirely — Claude Browser already persists cookies as the user's daily-driver
browser, so any tab is already logged into webmail and other accounts a
removal flow needs. Auditing the existing `api.py`/`extract.py` surface
(`navigate`, `click`, `fill`, `text`, `links`, `screenshot`, `console`, `eval`,
playbooks) found it already covers generic form-driving well, including
honest failure reporting (`click`/`fill` already return `{ok:false,
error:'no match'}` rather than failing silently) and native handling of
checkboxes and `<select>` dropdowns.

Two real gaps remain, and are the whole scope of this spec:

1. Nothing stores the user's own personal-info fields (name, address, email,
   phone, DOB) for an agent to fill opt-out forms with.
2. Nothing detects a CAPTCHA/anti-bot challenge page, which broker opt-out
   forms hit constantly — without it, an unattended agent can only infer
   "stuck" from repeated failed clicks, with no clear signal to act on.

**Explicitly out of scope:**

- Job orchestration of any kind — Openclaw's `pdd.py` already owns this.
- The Openclaw-side wiring (`autopilot.py`, `cdp.py` in `~/.openclaw`) needed
  to actually swap Claude Browser in. That's a different repo. A separate
  handoff document, written once this spec is implemented and verified, will
  brief a new session on how to use these additions from that side.
- The daily-driver Chrome-replacement initiative (bookmarks, history UI,
  session restore, profiles, etc.) — a separate initiative, to be spec'd
  independently.
- The plaintext `BROWSERBASE_API_KEY` and Gmail app password found in
  `~/.openclaw/jobs/hermes-home/.env` during research. Flagged to the user;
  unrelated to this repo and not addressed here.

## Goals

- An agent driving Claude Browser can read the user's own PII fields to fill
  opt-out forms, without that data ever needing to be re-sent by the caller
  on every call.
- The user's PII is stored at the same trust level as saved logins: system
  keyring, never a file this project invents.
- An agent can ask "is this page showing a challenge instead of its real
  content?" and get an honest, structured answer.
- Claude Browser never attempts to solve or bypass a CAPTCHA/anti-bot
  challenge. It reports; it does not act.

## Non-goals

- A fixed/validated PII schema. Different opt-out forms want different
  fields; the store is freeform key→value, not a hardcoded set of columns.
- Multi-identity support. There is one user and one profile.
- Reliable detection of every anti-bot product that will ever exist. This is
  a heuristic, maintained like any other piece of scraping logic — it will
  need updates as vendors change markup, the same way `scrub.py`'s patterns
  are maintained.

## Design

### 1. `claudebrowser/profile.py` (new module)

Structurally mirrors `passwords.py`'s keyring pattern, but is its own module
rather than an extension of it, because the two differ on the axis that
matters most in this codebase: **exposure**. A saved login is never handed to
an agent or written to a playbook. A PII field exists *specifically* so an
agent can read it and put it in a form — that is the whole point of storing
it. Mixing the two into one module would blur a boundary the codebase already
draws deliberately (see `passwords.py`'s docstring on what is/isn't ever
exposed). They also differ structurally: passwords are keyed per
`(origin, username)`; a profile is one identity with no origin at all.

```
class Vault:
    def get_all(self) -> dict           # {} if no keyring or nothing stored
    def get(self, key) -> str | None
    def set(self, key, value)           # value=None deletes the field
    def available(self) -> bool

def open_vault() -> Vault | None        # None if this machine has no keyring
```

Storage: **one** keyring item (not one per field — there is only one
identity, so there's nothing to partition on), attrs `{"app": "profile"}`
under a dedicated schema `net.claudebrowser.Profile`, secret = a JSON object
of all fields. `SecretBackend`/`MemoryBackend` follow the exact shape already
in `passwords.py` (same reasoning: store, don't invent crypto; tests inject
`MemoryBackend`, no display needed).

No enforced schema. The module's docstring documents suggested keys —
`first_name`, `last_name`, `dob`, `email`, `phone`, `address_line1`,
`address_line2`, `city`, `state`, `zip` — as a convention for whoever
populates the vault (by hand via `cbctl`, or scripted from Openclaw), not a
requirement the code checks. Per the user's explicit scope choice: **no
SSN or financial fields** — opt-out forms don't need them, and storing them
here would raise the stakes of a compromise for no operational benefit.

Degradation: same posture as `passwords.open_vault()` — no Secret Service
available means `open_vault()` returns `None` and the feature is quietly
unavailable, not a browser that fails to start.

### 2. `api.py` additions

```python
Op("profile", "/profile", "GET",
   "Report the stored personal-info profile (name, address, contact fields) "
   "used to fill out forms.",
   call=lambda c, a: ("api_profile", ()), tab=False)

Op("profile-set", "/profile/set", "POST",
   "Set or delete one field in the personal-info profile.",
   params=[Param("key", required=True),
           Param("value", cli="optarg",
                 help="New value; omit to delete the field.")],
   call=lambda c, a: ("api_profile_set", (a["key"], a.get("value"))),
   tab=False, mcp=False)
```

`profile` is MCP-exposed (`browser_profile`) — an agent needs to read it to do
its job. `profile-set` is **not** MCP-exposed, for the same reason `settings`
and `persona` aren't: this is the user's own data, and an agent acting toward
some other goal has no business silently rewriting it. `cbctl profile` /
`cbctl profile-set KEY VALUE` are one command away for a person (or a scripted
setup step in the Openclaw handoff) who means it.

`Browser.api_profile(done)` / `Browser.api_profile_set(key, value, done)` on
the browser side follow the `available`/`reason`-style shape already used by
`storage.py` and the passwords page when the underlying store is missing,
rather than raising.

### 3. Blocked-state detection

`extract.py` gets one new snippet, `BLOCKED`, matching the existing style of
`TEXT`/`MARKDOWN`/`find` (a `(function(){...})()` returning `JSON.stringify`):

```js
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
```

(Exact regex/selectors get refined during implementation; this fixes the
shape and the four categories, not the final byte-for-byte pattern.)

```python
Op("blocked", "/blocked", "GET",
   "Check whether the page is showing a CAPTCHA or anti-bot challenge "
   "instead of its real content. Call this when a click or fill had no "
   "visible effect.",
   call=_js("BLOCKED"))
```

MCP-exposed (`browser_blocked`), same shape as `find`/`console`. Always
returns a verdict; never raises.

**Hard line:** this op detects and reports. Claude Browser never attempts to
solve, click through, or otherwise bypass a CAPTCHA or anti-bot challenge —
not now, not as a later enhancement to this feature. When `blocked: true`,
the correct agent behavior is to stop and surface it for the user (in
Openclaw's case, `pdd.py` records a "needs-human" outcome instead of
"submitted").

### 4. `cb:profile` page

Mirrors `cb:passwords`: lists the stored fields (key, value, edit, delete),
rendered server-side from `Vault.get_all()`, edits authenticated through the
existing per-session nonce and the shared `cbui` message handler — no new
trust mechanism. Added to the `NAV` tuple in `pages.py` alongside the other
`cb:` pages.

## Data flow (how an agent is expected to use this)

1. Agent calls `browser_profile` once to read the fields it needs.
2. Agent calls `browser_navigate` to the opt-out URL.
3. Agent calls `browser_fill` per field, using values from the profile.
4. When a step seems to have had no visible effect, agent calls
   `browser_blocked`.
5. If `blocked: true`, the agent stops; Openclaw's `pdd.py` records a
   "needs-human" outcome rather than "submitted".
6. Agent calls `browser_screenshot` to save evidence at whatever path
   Openclaw's job expects.
7. Openclaw's existing `pdd next`/`record` state machine continues as before
   — nothing about it changes.

## Error handling

Consistent with existing conventions in this codebase:

- `click`/`fill`/`navigate` already report `{ok:false, error:...}` instead of
  throwing; no change needed there.
- `profile`/`profile-set` report unavailability the same way `passwords.py`
  and `storage.py` do when there's no keyring — a missing Secret Service
  costs the feature, not the browser.
- `blocked` always returns a verdict (`blocked: false` when nothing matches);
  it never raises.

## Testing

- `tests/test_profile.py`, mirroring `tests/test_passwords.py`: get/set/delete
  roundtrip via `MemoryBackend`, no-keyring degradation, values containing
  characters that need JSON escaping, `get_all()` shape when empty.
- New ops (`profile`, `profile-set`, `blocked`) get the same generic
  `cbctl`/MCP-tool-list smoke coverage `test_offline.py` already applies to
  every entry in `api.OPS`.
- `extract.BLOCKED` is exercised the way other `extract.py` snippets are
  today: string-content assertions on the generated JS, since no test in this
  suite executes JS in a real WebView (no display in CI).
- `python3 -m py_compile claudebrowser/*.py` still needs to pass, per the
  project's standing gate on the display-requiring modules.

## Documentation follow-up (not part of this spec's implementation, but owed)

- `CLAUDE.md`'s layout table and "the rules that matter" section should gain
  an entry for `profile.py`, mirroring the existing passwords entry, once
  this lands.
- Once implemented and tested, a separate handoff document — for a new
  session working in `~/.openclaw` — explains how to replace unbroker's
  Browserbase/CDP browser-driving calls with `cb-mcp`'s tools, including
  `browser_profile` and `browser_blocked`. Not written yet: it should
  describe real, verified behavior, not a design.
