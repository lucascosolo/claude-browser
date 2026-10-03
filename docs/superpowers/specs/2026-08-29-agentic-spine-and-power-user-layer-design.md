# The agentic spine, and a power-user layer on top

Date: 2026-08-29

## Context

Claude Browser moved from a 2-core/3.8GB laptop to a 16-core/31GB machine
that is also the hub of the user's Openclaw AI toolsuite. Two things follow
from that, and they are not the same project:

1. The browser's actual reason to exist is to be **driven by AI agents doing
   complex, multi-step tasks** — the concrete, shipping example is Openclaw's
   "unbroker" skill filling out data-broker opt-out forms (see
   `docs/handoffs/2026-08-29-openclaw-unbroker-handoff.md`), but the goal is
   general: any complex web task an agent drives through `cb-mcp` or the
   in-browser Ctrl+G loop should cost as few turns and as few tokens as
   honestly possible.
2. On top of that, the user also wants Claude Browser as their own daily
   driver, with the resource caps that made sense on the old laptop lifted,
   and some ordinary browser conveniences (bookmarks/history/downloads
   reachable by an agent, session restore, ad-blocking) filled in.

**(1) is the spine. (2) is a layer on top of it.** This spec is organized
that way on purpose: Tier 1 is what actually changes how an agent drives this
browser; Tier 2 is what makes it a nicer daily driver. Tier 2 was scoped
first in this session's conversation and is unchanged from that scoping
except for two corrections an architecture review surfaced (noted inline).

An architecture question was raised and closed before this spec was written:
whether to swap the rendering engine (to CEF/Chromium) or rewrite the control
layer in a lower-level language. Decision: **no.** WebKitGTK 4.1 was
confirmed (via live GI introspection) to already expose
`WebKit2.UserContentFilterStore` / `UserContentManager.add_filter()` — real
Safari-style JSON content-blocking, not cosmetic CSS — which closed the
strongest argument for a swap. The actual ceiling was self-imposed cgroup
caps and conservative admission thresholds tuned for the old laptop, not the
engine or the language.

This spec's Tier 1 design was independently reviewed against the actual code
(`extract.py`, `control.py`, `agent.py`, `playbooks.py`) before being
finalized here; where the review changed the design, that is noted in place
rather than silently folded in, because the reasoning matters for whoever
implements it.

## Goals

- An agent driving a multi-step task (the opt-out flow is the concrete case)
  spends materially fewer tokens and turns per page than reading full page
  text and guessing CSS selectors costs today.
- A failure mode that today costs the agent a full page re-read to diagnose
  (a rejected form field, a CAPTCHA wall, a stale reference after a redirect)
  is reported inline, in the same call that caused it.
- The user's own PII, once in the keyring, never has to pass through a model
  context to be used in a form fill.
- A previously-handled site (the same data broker, checked again next month)
  costs zero LLM turns to re-handle, correctly, even if the user's profile
  data changed since the recording.
- The daily-driver gaps (resource ceiling, bookmark/history/download
  visibility to an agent, session restore, ad-blocking) are filled in without
  weakening any of the above, or any existing privacy invariant.

## Non-goals

- Solving or bypassing CAPTCHA/anti-bot challenges. Unchanged hard rule from
  the prior spec: detect and report, never act.
- A full accessible-name (accname) implementation, or full ARIA-tree
  fidelity. The snapshot's label heuristic will sometimes be wrong; every
  snapshot line always carries enough to fall back to a plain CSS selector.
- Reaching into cross-origin iframes. Not solvable from the UI process in
  WebKitGTK 4.1 without a web-process extension this project has already
  ruled out building (see CLAUDE.md's `sent-request` note). Snapshot reports
  them as unreachable instead of silently failing to find their fields.
- Full EasyList-syntax ad-block rule compatibility. A small curated,
  maintained-like-`scrub.py` list, not a general-purpose filter-list parser.
- Auto-replaying a matched playbook without being asked. The browser
  surfaces "I recognize this site"; it never re-submits a form on its own.

## Design — Tier 1: the agentic spine

### 1. `snapshot`: a compact, indexed view of what's actionable

Today, the only way to see a page from any driving surface (`cb-mcp`,
`cbctl`, the in-browser Ctrl+G loop) is `text`/`read_page` — clean prose,
truncated at 15,000 characters — and the only way to act is `click`/`fill`
against a CSS selector the model has to invent from that prose. A 12-field
opt-out form costs 6-10K characters of prose to describe and is frequently
guessed wrong.

`snapshot` replaces "describe the page" with "list what's actionable," one
line per element:

```
e1  input#email       "Email address"        required
e2  radio             "Remove my data"       group=reason
e3  select            "State"                50 options
e4  textarea          "Reason for request"
e5  button            "Submit request"
e6  link              "Privacy policy" -> /privacy
f1  iframe            origin=cdn.onetrust.com  UNREACHABLE
```

Returned as `{"ok": true, "url": ..., "epoch": "...", "lines": [...],
"counts": {...}}`. **Lines, not nested objects** — same information,
meaningfully fewer tokens, and trivial for Openclaw to parse as a fixed
4-column format.

Visibility uses the same `getClientRects().length === 0` check `extract.TEXT`
already uses — this project's existing, honest answer to "is this actually
on screen." The accessible-name heuristic is a cheap cascade (`label[for]` →
`aria-label`/`aria-labelledby` → wrapping `<label>` → `placeholder` →
trimmed `innerText`) — explicitly not a full accname implementation, which is
why every line still carries what's needed to fall back to a CSS selector.
`<select>` reports an option **count**, never the option list inline (a
50-state dropdown listed in full is 400 wasted tokens re-sent every
subsequent turn); a separate expansion is used only when the agent actually
needs to pick a value.

**Same-origin iframes are walked** via `contentDocument`, with a frame index
folded into the element's ref. **Cross-origin iframes are reported, not
walked** — `f1 iframe origin=... UNREACHABLE` — which turns an unexplained
"no match" three turns later into an immediate, actionable signal: navigate
the tab directly to the iframe's `src`, which frequently works for embedded
opt-out widgets (OneTrust, DataGrail, Osano, and similar are common on broker
sites).

#### The ref registry: why an epoch, and why structural, not live-object

A naive design — assign refs into a `window`-scoped registry on `snapshot`,
resolve them on a later `click`/`fill` call — works mechanically: WebKit
evaluates each call in the page's own main-world `window`
(`evaluate_javascript(..., world_name=None, ...)`), and this project already
relies on exactly that cross-call persistence for `browser.py`'s
`CONSOLE_SHIM` (`window.__cb_console`, written once at document-start, read
back by a separate, later eval). So the mechanism is proven; three things
about it are not optional:

1. **The registry is page-writable.** Main-world JS means a hostile page can
   remap what a ref points at. The registry is therefore never trusted
   outright — resolving a ref re-checks the live element's role+label
   signature against what was recorded at snapshot time, and only acts if it
   still matches. A tampered registry is *detected*, not acted on.
2. **A fresh `window.__cbEpoch` is minted at document-start** (installed via
   the same `UserScript` mechanism as `CONSOLE_SHIM`, so it happens before
   any page script runs). `snapshot` returns the current epoch; every
   ref-based action takes it back and refuses with `{"ok": false, "error":
   "stale snapshot", "epoch": "..."}` if it doesn't match, rather than acting
   on an unrelated page after a navigation. This is the single most important
   piece of the ref design — without it, a stale ref clicking the wrong
   element after a redirect is a silent, wrong action instead of a cheap,
   explicit retry.
3. **An epoch alone is not enough** — `history.pushState` (any SPA route
   change) leaves the epoch untouched while replacing the DOM under it. Refs
   therefore resolve **structurally**: live object first (epoch matches, node
   still connected, signature still matches) → else re-resolve by a recorded
   structural path (frame index + nth-child chain) and re-check the signature
   → else re-resolve by signature alone if it's now unique → else fail with
   `{"ok": false, "error": "ref e7 no longer resolves", "hint": "call
   snapshot again"}`. This degrades correctly across both a full navigation
   and an SPA re-render, and the whole mechanism costs the transcript two
   characters (`e7`) — the descriptor lives page-side.

**Plumbing:** a ref is accepted as the value of the existing `selector`
parameter, spelled `@e7`. `@` cannot begin a valid CSS selector, so there is
no ambiguity, and this needs zero changes to `Param`'s required/optional
model or to `playbooks.validate_step`'s scalar checking — `click`/`fill`
already take a string selector; `@e7` is just a string that means something
different once inside `extract.py`. (An isolated JS script world would also
close the page-writable-registry gap, but that's unverified without a
display and would separate the snapshot shim from `__cbHalo`/cursor code
every other acting snippet needs — treat as a follow-on probe, not something
this design depends on.)

### 2. Self-verifying actions: a delta instead of a re-read

Today `click`/`fill` return `{"ok": true, "tag": "button"}` — nothing about
whether anything actually happened, so the agent's next move is reflexively a
full `text`/`snapshot` re-read to check. That re-read is roughly half the
token cost of a typical multi-step flow, and it is pure waste when the action
is the *last* eval in a round trip that already touched the DOM.

Every acting op (`click`, `fill`, `fill-many`) appends one cheap additional
expression to the **same** eval and returns:

```json
{"ok": true, "ref": "e5", "url_changed": true, "epoch": "...",
 "blocked": {"blocked": false, "kind": null},
 "changed": 7,
 "errors": ["Email address is required", "Invalid ZIP"]}
```

`errors` is harvested from `[aria-invalid="true"]`, `[role="alert"]`,
`.error`, `:invalid` and adjacent text — exactly the signal a form gives when
a fill was rejected, today invisible without a full re-read. Folding the
already-cheap `BLOCKED` check into every action result serves the unbroker
hard rule directly: a challenge is reported the instant it appears, not
several confused turns of "click didn't seem to work" later.

### 3. `fill-many`: one call, not one call per field

```json
{"fields": {"@e1": "jane@example.com", "@e3": "CA", "@e4": "Please remove my data"}}
```

Each of N `fill` calls today costs a full assistant turn and a tool result,
both of which get re-sent every subsequent turn for the rest of the run —
collapsing 6 calls to 1 is a turn-count win, not just a latency one.

**Parameter shape, deliberately not a JSON object param:** `playbooks._coerce`
raises on dict/list values by design — `playbooks.py`'s replay path is
explicit that a replayed step's parameters are untrusted and scalar-checked,
and widening that trust boundary for one op's convenience is the wrong
trade. `fill-many` takes `fields` as a **JSON string**, parsed inside
`op.call`, keeping every parameter the registry validates a plain string.

### 4. Profile placeholders, resolved at fill time

Today, using the profile means: `browser_profile` returns the user's real
name/address/email to the model, the model echoes each value back inside a
`fill` call, and now that PII sits in the transcript being re-sent to
Anthropic on every subsequent turn of the run — and, since `ai._redact` runs
over everything sent, a profile value can round-trip through the model
**mangled** by `scrub.py`'s patterns, which is a correctness bug on top of
the exposure.

Instead, `fill`/`fill-many` accept a placeholder grammar — `value:
"{profile:email}"` — and the **native side** substitutes from the Vault
before building the JS that runs in the page. The model never receives the
value at all; the transcript carries only the field name.

New module: **`claudebrowser/fills.py`** (GTK-free, matching the project's
"testable without a display" convention — same reasoning as `profile.py`
itself):

```python
def is_placeholder(text) -> bool
def resolve_value(text, vault) -> str        # raises FillError if unresolvable
```

Shared by `browser.py`'s live-fill path and the playbook-replay path, so
there is exactly one definition of the grammar.

**Recording falls out for free.** `control._handle` records from the raw
request `args`, which already contain the placeholder string, not the
resolved value — so a playbook recorded once contains `{"selector": "#email",
"value": "{profile:email}"}`, and replaying it next month resolves against
whatever the profile holds *then*. This is the "record once, replay forever,
stay correct" goal from a smaller mechanism than a separate
playbook-parameterization step.

**Walking the `is_secret_step` invariant, because it must still hold:**

- `is_secret_step` is unchanged — it still matches only on selector/script,
  never on value. A placeholder doesn't change what it's matching against.
- A placeholder can only ever resolve against `profile.py`'s Vault, which is
  a structurally different keyring schema (`net.claudebrowser.Profile`) from
  `passwords.py` and therefore can never hold a credential. This is an
  invariant enforced by which module the grammar is allowed to name, not a
  check that has to be remembered — document it directly in `fills.py`'s
  docstring so nobody later adds a `{secret:...}` form.
- **Substitution must produce a new string and never write back into the
  request's `args` dict.** `control._handle` records from those same `args`
  after `op.call` runs; if substitution mutated them in place, the literal
  PII value would be written to the playbook file on disk. This gets its own
  test (`test_profile_placeholder_is_recorded_unresolved`), not just a code
  comment.
- An unresolvable placeholder fails loudly — `{"ok": false, "error": "no
  profile field 'email'"}` — never types the literal string `{profile:email}`
  into the user's opt-out form.
- `browser_profile` gains a keys-only mode, so the model can learn what
  fields exist (`["first_name", "email", "zip"]`) without ever receiving
  values, which is what it needs to construct a `fill-many` call using
  placeholders.

### 5. Playbooks volunteer a site match

A saved playbook gains an optional `match` field — the host(s) derived from
its first `open`/`navigate` step at save time. When a later `open`/`navigate`
call lands on a matching host, **the navigation result itself** carries
`"playbooks": ["acxiom-optout"]` — about 20 tokens on a response the agent
already receives, zero extra round trips, and Openclaw needs no side channel
to know a recorded flow exists for this site. `playbook-list` separately
gains an optional `url` filter for an explicit query.

**Never auto-replay.** Silently re-submitting a recorded form on a page that
may have changed since it was recorded is exactly the class of side effect
`agent.SYSTEM` and the existing `mcp=False` exclusions on `clear`/`profile-set`
guard against elsewhere in this codebase. The browser suggests; the caller
(a human, or Openclaw's job logic) decides whether to replay.

Deliberately not built: shape-based similarity matching across snapshots
("this looks like a broker form I've seen before, even on a different
domain"). A heuristic with a false-positive mode of "submitted the wrong
form" is exactly what this codebase consistently refuses elsewhere
(`resources.py`'s swap-occupancy note, `scrub.py`'s precision-over-recall
stance) — host matching is exact, free, and covers the real workload: the
same broker, checked again next month.

### 6. Fixing what fights this design in `agent.py`

Three latent issues, found by walking the redesign against the actual loop
code rather than assumed:

- **`RESULT_CHARS` truncation slices the JSON-encoded string** (`json.dumps
  (output)[:RESULT_CHARS]`). Fine for prose; it hands the model a
  syntactically broken fragment for structured output. Truncation for
  `snapshot`/`fill-many` results is structural — drop trailing lines, append
  `"[N more elements]"` — not a byte slice.
- **Loop detection keys on `(tool_name, json.dumps(args))`.** `snapshot`
  takes no arguments, so calling it on four different pages of a multi-page
  flow is four identical signatures, and the fourth is refused as a repeat.
  The signature needs the tab's current URL folded in, or no-arg reads need
  to be exempted when their result differs from the last one.
- **`MAX_STEPS=14` is the actual binding constraint**, not the character
  budgets — a two-page form plus a confirmation plus a verification read
  already approaches it. Raised to 24.

Additionally: `agent.py`'s `TOOLS` list gets `snapshot`, `fill_form` (the
`fill-many` equivalent — named to match this file's existing convention of a
distinct in-browser tool name per op, e.g. `type_text` for `fill`), `blocked`,
and a keys-only `profile`, hand-written (per CLAUDE.md: `TOOLS`
stays hand-authored prompt engineering, not generated from `api.py`, even
though `api.py` remains the single source for everything else). **Every one
of them that touches the tab must be added to `TAB_TOOLS`** — that's the
private-tab gate, and missing it is exactly the leak class CLAUDE.md
documents at length (`list_tabs` dropping private tabs, page text never
reaching Anthropic from a private tab). A guard test asserts every
tab-touching tool in `TOOLS` is present in `TAB_TOOLS`, in the spirit of
`test_every_web_process_name_survives_truncation`.

One more, small but real: `agent.SYSTEM`'s existing framing that this is
running on a slow machine is now false and actively counterproductive — it
discourages exactly the exploratory navigation a complex task needs. Update
it alongside the above.

## Design — Tier 2: the power-user / daily-driver layer

Scoped earlier in this session; two corrections came out of the review and
are reflected below.

### 7. Resource unchaining

- `cb` launcher: `CB_CPU_QUOTA` default 180% → 800%; `CB_MEM_HIGH` default
  1200M → 8192M.
- `browser.py`: `MAX_AGENT_TABS` (backing `CB_MAX_TABS`) default 10 → 24.
- `perf.py`: `CB_LIGHT`'s default flips to off — its entire rationale
  (`Save-Data`/reduced-motion for a 1.6GHz CPU) no longer applies. **Sequencing
  note:** flip this *before* recording any new playbooks, not after — it can
  change which markup a site serves, which changes selectors and snapshot
  output for anything recorded under the old default.
- **`MAX_CONCURRENT_LOADS` (currently 2) is explicitly left unchanged.** The
  review's correction: agent-driven flows are strictly serial by
  construction (playbook replay is one step at a time; `agent.py` dispatches
  one tool at a time), so raising this serves neither tier and the admission
  gate already adapts its effective limit down to 1 under real memory
  pressure regardless of the constant. Raising the tab ceiling and the cgroup
  caps is what actually matters here.

### 8. Bookmarks, history, and downloads become agent-visible

`store.py` already has full backends for all three
(`bookmark`/`unbookmark`/`toggle_bookmark`/`bookmarks`, `history`/
`clear_history`, and `browser.py` already runs real `WebKitDownload`
handling with a session download list) — none of it is exposed through
`api.py`, so `cbctl`/`cb-mcp` cannot read a bookmark, search history, or
check a download's status.

New ops, following existing precedent in `api.py` for what is and isn't an
MCP tool:

- `bookmarks` (GET, MCP-exposed — read is benign, same posture as `recall`)
- `bookmark-add` (POST, MCP-exposed — an agent bookmarking a page it found
  useful is reversible and visible in the UI)
- `bookmark-remove` (POST, **not** MCP — same reasoning as `playbook-delete`:
  an agent shouldn't delete user data as a side effect of an unrelated goal)
- `history` (GET, MCP-exposed, but **`q` is required**, not optional —
  correction from the review: an unfiltered dump of up to 300 URLs/titles is
  a materially bigger blast radius than `recall`'s already-exposed ranked
  answer over page text. Requiring a query brings its exposure in line with
  `recall`'s existing precedent instead of introducing a new, wider one. The
  `cb:history` page itself is unaffected — it renders server-side straight
  from `store.py`, not through `api.py`.)
- `history-clear` (not MCP, matches `clear`)
- `downloads` (GET, MCP-exposed, read-only status check against the existing
  session download list — not new agent-initiated downloads, which is new
  capability beyond exposing what already exists)

### 9. Session restore

New: `store.py` gains a `session_tabs` table, updated on tab open/close;
restored on launch behind a new `CB_RESTORE_SESSION` setting (default on).
Private tabs are excluded by construction — nothing about a private tab is
ever written to `store.py` (`store.recordable()` already guarantees this for
every existing sink; the new table hangs off the same point). This also
directly serves Tier 1's goal: a multi-day broker flow surviving a restart
without losing its open tabs.

### 10. Ad/tracker blocking

New module **`claudebrowser/blocklist.py`** (GTK-free conversion logic,
testable without a display): a small curated list of ad/tracker/analytics
domains, converted to WebKit's Safari-style content-blocker JSON
(`{"trigger": {"url-filter": "..."}, "action": {"type": "block"}}`),
compiled once via `WebKit2.UserContentFilterStore.save()` and applied to
every tab's (shared) `UserContentManager` via `add_filter()`. Toggled by a
new `CB_ADBLOCK` setting (default on), validated in `settings.py` like any
other consumer-checked boolean.

**Hard rule, found by the review and load-bearing for the whole prior spec's
promise:** the curated list must **never** include a CAPTCHA/anti-bot
vendor's domains (`recaptcha.net`, `google.com/recaptcha`, `hcaptcha.com`,
Cloudflare Turnstile's `challenges.cloudflare.com`, and equivalents). Blocking
their assets would make the page's challenge never render at all — so
`blocked` would report `{"blocked": false}` on a page that is, in fact,
walled off, and the agent would walk straight into a form that can never
submit. Blocking a challenge's assets is also, functionally, an attempted
bypass — the exact thing the prior spec's hard rule forbids. This exclusion
is documented in the list's source comment and pinned by a test asserting no
known anti-bot vendor domain appears in the shipped list.

Secondary note: the filter runs on the same shared `UserContentManager` as
`CONSOLE_SHIM`, `PASSWORD_JS`, and the new snapshot shim — they coexist
without conflict, but a `display:none` blocking action changes
`getClientRects()`, which changes what both `text` and `snapshot` see on a
page with ads. Desirable in general; worth one sentence in `CLAUDE.md` that
"the page as an agent sees it" now depends on the filter list being active.

Explicitly out of scope, restated from the earlier scoping pass: full
EasyList-syntax rule compatibility. That is a real parser-writing project of
its own, not a small addition.

## File-level plan

| File | Change |
|---|---|
| `claudebrowser/extract.py` | `SNAPSHOT_SHIM` (document-start registry + epoch), `snapshot()`, `delta()`, `click`/`fill` extended for `@ref` targets via `__cbResolve`, `fill_many()`. Every ref and value still passes through `_js_str` — a ref is model-supplied and page-adjacent exactly like a selector is. |
| `claudebrowser/fills.py` | **New.** `is_placeholder`/`resolve_value` — the `{profile:key}` grammar, GTK-free, shared by the live-fill path and playbook replay. |
| `claudebrowser/browser.py` | Register `SNAPSHOT_SHIM` alongside `CONSOLE_SHIM`/`PASSWORD_JS` in the existing `add_script` loop; `api_snapshot`, `api_fill_many`; nav results annotated with `playbooks.matching(url)`; `session_tabs` read/write around tab open/close and launch; `MAX_AGENT_TABS` default; `WebKit2.UserContentFilterStore` wiring for ad-block. |
| `claudebrowser/api.py` | `snapshot`, `fill-many` (JSON-string `fields` param), `bookmarks`, `bookmark-add`, `bookmark-remove` (mcp=False), `history` (required `q`), `history-clear` (mcp=False), `downloads`. `click`/`fill` summaries documented to mention `@ref`. |
| `claudebrowser/playbooks.py` | `match` field on save, `matching(url)`; `is_secret_step` extended for `fill-many` — parses the JSON and matches **keys (selectors) only**, never values, preserving the existing value-blindness rule. |
| `claudebrowser/agent.py` | `TOOLS` += `snapshot`, `fill_form`, `blocked`, keys-only `profile` (hand-written descriptions); every tab-touching addition also added to `TAB_TOOLS`; structural (not byte-slice) truncation for structured results; tab URL folded into the loop-detection signature; `MAX_STEPS` 14 → 24; stale "slow machine" framing removed from `SYSTEM`. |
| `claudebrowser/settings.py` | `CB_RESTORE_SESSION`, `CB_ADBLOCK`; `CB_LIGHT` default flip; doc updates for `CB_MAX_TABS`. |
| `claudebrowser/blocklist.py` | **New.** Curated domain list → WebKit content-blocker JSON, GTK-free conversion, anti-bot-vendor exclusion enforced by test. |
| `cb` | `CB_CPU_QUOTA`, `CB_MEM_HIGH` defaults. |
| `CLAUDE.md` | New entries for `fills.py`, `blocklist.py`; a line noting ad-block affects what `text`/`snapshot` see. |

`claudebrowser/control.py` needs no change — the substitution and recording
paths already meet at `_handle` correctly as long as substitution never
writes back into the request's `args`, which is a rule, not a code change.

## Error handling

- `snapshot`/action ops always return a verdict; a stale or unresolvable ref
  is `{"ok": false, ...}` with a hint to re-snapshot, never a wrong action.
- An unresolvable profile placeholder fails loudly and never types the
  literal placeholder text into a form.
- Cross-origin iframes are reported (`UNREACHABLE`), never silently skipped.
- `blocked` is unaffected by ad-blocking by construction (the exclusion
  rule above) — it remains a true report of what the page is actually
  showing.

## Testing

- `tests/test_fills.py`: placeholder grammar round-trip via
  `profile.MemoryBackend`; unresolvable-field failure; **substitution never
  mutates the caller's `args`** (`test_profile_placeholder_is_recorded_unresolved`).
- `extract.py` snippets get the same string-content assertions already used
  for `BLOCKED`/`TEXT` (no display in this suite): `SNAPSHOT_SHIM` mints an
  epoch, ref resolution falls through its stated priority order, iframe
  handling emits `UNREACHABLE` for a stubbed cross-origin case.
- `tests/test_agent.py` (or wherever `agent.py` is covered today): a guard
  test asserting every tab-touching entry in `TOOLS` is present in
  `TAB_TOOLS`; loop-detection signature includes URL; structural truncation
  on a stubbed oversized snapshot result.
- `tests/test_playbooks.py`: `is_secret_step` on a `fill-many` step matches
  only selector keys, never field values; a recorded placeholder round-trips
  unresolved through save/replay.
- `tests/test_blocklist.py`: **the shipped list contains no known
  CAPTCHA/anti-bot vendor domain** — this is the one test in this spec that
  exists specifically to prevent a silent regression of the prior spec's
  hard rule.
- `test_offline.py`'s existing generic `cbctl`/MCP-tool-list smoke coverage
  extends to every new `api.OPS` entry, as it already does for all others.
- `python3 -m py_compile claudebrowser/*.py` still gates the
  display-requiring modules, per the project's standing rule.

## Explicitly out of scope, stated plainly

- Cross-origin iframe access. Unsolvable from the UI process in this WebKitGTK
  version without a web-process extension this project has already ruled
  out building.
- An isolated JS script world for the ref registry. Would remove the
  page-writable-registry caveat, but is unverified without a display and
  would split the snapshot shim away from the cursor/halo code every acting
  snippet shares. A future probe, not a dependency of this design.
- A full accessible-name implementation. The heuristic will be wrong
  sometimes; the fallback (a plain CSS selector, always present on every
  snapshot line) is the actual safety net, not heuristic accuracy.
- Full EasyList-syntax ad-block rules.
- Auto-replaying a matched playbook without being asked.
- Shape-based ("this looks like a broker form") snapshot similarity matching
  across different domains, as opposed to exact host matching.

## Documentation follow-up

- `CLAUDE.md` gains entries for `fills.py` and `blocklist.py` in the layout
  table, and a line under "the rules that matter" for the ad-block/snapshot
  interaction noted above.
- Once implemented and verified, `docs/handoffs/2026-08-29-openclaw-unbroker-handoff.md`
  should be revised (not replaced) to describe `snapshot`/`fill-many`/
  placeholder usage as the recommended flow for unbroker, superseding the
  plain `browser_fill`-per-field pattern it currently documents.
