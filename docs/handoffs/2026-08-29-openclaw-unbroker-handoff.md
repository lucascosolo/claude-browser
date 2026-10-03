# Handoff: swap unbroker's browser-driving onto Claude Browser

Date: 2026-08-29
Audience: a session working in `~/.openclaw` (not this repo)
Status: describes real, implemented, tested behavior in `claude-browser` as of
commit `9677346` — nothing in this document is aspirational.

## What changed on the Claude Browser side

Two gaps unbroker had no answer for are now closed. Both live in the
`claude-browser` repo at `/home/lucas/Workspaces/claude-browser`; you do not
need to touch that repo to use them.

1. **A PII profile store** — the user's own name/address/email/phone, kept in
   the system keyring, readable by an agent to fill opt-out forms.
2. **Blocked-state detection** — a single call that answers "is this page
   showing a CAPTCHA/anti-bot challenge instead of its real content?"

Design rationale lives in
`claude-browser/docs/superpowers/specs/2026-08-29-pii-profile-and-blocked-detection-design.md`
if you want the "why." This document is the "how," from unbroker's side.

## What did NOT change

- unbroker's job orchestration (`scripts/pdd.py`, the `pdd next` / `pdd
  record` state machine) is untouched and needs no change. It already handles
  the multi-day submit → wait-for-email → re-check shape of a removal; it just
  needs a browser-driving tool underneath it that behaves honestly.
- No code in `~/.openclaw` has been modified as part of this work. This
  handoff describes what to change there; nothing has been done yet.

## Prerequisite: start the MCP server

Claude Browser exposes its control API to any MCP client via `cb-mcp`, a
stdio MCP server:

```bash
claude mcp add -s user browser -- /home/lucas/Workspaces/claude-browser/cb-mcp
```

`cb-mcp` autostarts the browser itself on first tool call if it isn't already
running (`client.autostart`, which execs the repo's `cb` launcher — so the
CPU/memory caps in that launcher apply, same as a manual launch). Every tool
is named `browser_<op>`, generated from the single registry at
`claude-browser/claudebrowser/api.py` — this is the actual source of truth;
if a tool you expect isn't listed, check that file's `mcp=False` ops (a
handful are deliberately excluded — see below).

Sanity check before wiring anything: `./cbctl health` in that repo should
report the browser reachable.

## Replacing Browserbase + CDP with `cb-mcp` tools

Today, per the design spec's research: Browserbase drives public opt-out
forms via natural-language instructions, and a separate Chrome-over-raw-CDP
process handles flows needing the user's real logins. Swapping in Claude
Browser removes the CDP tier entirely — Claude Browser is the user's
daily-driver browser, so any tab opened through it is already signed into
webmail and whatever else a removal flow needs. One tool surface replaces
both.

The relevant tools (all MCP-exposed, all verified present in `api.py`):

| Tool | Purpose |
|---|---|
| `browser_open` | Open a URL in a new tab (`url`, optional `background`) |
| `browser_navigate` | Send an existing tab to a URL (`tab`, `url`) |
| `browser_text` / `browser_markdown` | Read the page as clean text/markdown |
| `browser_click` | Click the first element matching a CSS selector |
| `browser_fill` | Set an input's value and dispatch input/change |
| `browser_links` | List every link as absolute URLs |
| `browser_console` | Read console output — useful when a form silently fails |
| `browser_eval` | Run arbitrary JS and get the value back |
| `browser_screenshot` | Save a PNG of the viewport to a path on disk |
| `browser_profile` | **New.** Read the stored PII fields (below) |
| `browser_blocked` | **New.** Check for a CAPTCHA/anti-bot challenge (below) |
| `browser_wait` | Block until the tab's current load finishes |
| `browser_tabs` | List open tabs with ids/URLs/titles |

**Honest failure, not exceptions.** `browser_click` and `browser_fill` return
`{"ok": false, "error": "no match"}` when the selector doesn't match, rather
than raising. Treat any `ok: false` as the signal to try `browser_blocked` or
`browser_console` next, not as a crash to catch.

**What's deliberately NOT an MCP tool**, because these are the user's own
settings/data and an agent pursuing an opt-out flow has no business touching
them as a side effect: `profile-set` (see below), `settings`, `persona`,
`vpn`, `clear`, `playbook-delete`. If unbroker ever needs one of these, it's a
`cbctl <op>` shell call, not an MCP tool call — that's a deliberate design
line in this repo, not an oversight.

## The PII profile

**Reading it — `browser_profile`.** No arguments. Returns:

```json
{"ok": true, "available": true, "fields": {"first_name": "...", "email": "...", ...}}
```

`available: false` (with `fields: {}`) means this machine has no system
keyring reachable — treat that as "no profile data," not an error to retry.
No fixed schema is enforced; by convention the keys are `first_name`,
`last_name`, `dob`, `email`, `phone`, `address_line1`, `address_line2`,
`city`, `state`, `zip`. **No SSN or financial fields are stored here, ever**
— opt-out forms don't need them, and unbroker's job code should never ask for
or expect them from this store.

**Populating it.** `profile-set` is intentionally not an MCP tool, so unbroker
can't (and shouldn't) write to it as part of an agent run. It's set once, by
a human or a one-time scripted setup step, via:

```bash
./cbctl profile-set first_name "Jane"
./cbctl profile-set email "jane@example.com"
./cbctl profile-set address_line1 "123 Main St"
# ...
./cbctl profile-set phone ""     # empty value deletes the field
```

or through the `cb:profile` page in the browser's own UI (Ctrl+L →
`cb:profile`), which lists, edits and deletes fields the same way
`cb:passwords` does for saved logins.

**Usage pattern for a removal job:**

1. Call `browser_profile` once at the start of a form-filling step; cache the
   `fields` dict for the rest of that step rather than re-reading per field.
2. Call `browser_navigate` (or `browser_open`) to the opt-out URL.
3. Call `browser_fill` once per form field, sourcing values from `fields`.
4. Only fill fields the broker's form actually asks for — don't assume every
   opt-out form wants every stored field.

## Blocked-state detection

**`browser_blocked`.** No arguments; runs against the active tab. Returns:

```json
{"blocked": true, "kind": "recaptcha"}
```

`kind` is one of `"recaptcha"`, `"hcaptcha"`, `"turnstile"`, `"generic"`, or
`null` when `blocked: false`. It always returns a verdict — it never raises,
so it's safe to call speculatively after any step that seems to have had no
effect.

**When to call it:** after a `browser_click` or `browser_fill` that returned
`ok: true` but the page state doesn't look like it advanced (URL unchanged,
expected confirmation text absent, etc.) — that's the actual signal broker
opt-out forms give when a challenge fired instead of the requested action.

**The hard line, and it governs how `pdd.py` should be wired to this:**
Claude Browser detects and reports a challenge. It does not solve, click
through, or otherwise attempt to bypass one — not now, and this is not a
planned future enhancement. When `browser_blocked` returns `blocked: true`,
the correct action in unbroker's job flow is:

- Stop attempting that opt-out step.
- Call `pdd record` with a "needs-human" outcome (or whatever equivalent
  status `pdd.py` already uses for "an agent could not complete this and a
  person needs to look at it") — **do not** record it as "submitted."
- Optionally call `browser_screenshot` first to save evidence of the
  challenge at whatever path the job already uses for audit screenshots, so
  the human reviewing it can see what stopped the agent.

This is the one piece of this handoff that isn't just plumbing: if `pdd.py`'s
outcome vocabulary doesn't currently have a "needs-human" / "blocked" state
distinct from "submitted" and "failed," that's a small addition worth making
there — collapsing a CAPTCHA wall into a generic "failed" loses the
information that a retry won't help and a human intervention will.

## What this handoff deliberately does not cover

- The daily-driver Chrome-replacement work on Claude Browser is a separate,
  later initiative — unrelated to this integration.
- The plaintext `BROWSERBASE_API_KEY` and a Gmail app password found in
  `~/.openclaw/jobs/hermes-home/.env` during the original research remain
  unaddressed. Worth fixing independently of this migration, not blocking it.
- Whether to remove the Browserbase/CDP code paths from unbroker entirely, or
  keep them as a fallback, is a decision for whoever does this integration —
  this document only establishes that the replacement tools exist, are
  tested, and behave as described above.
