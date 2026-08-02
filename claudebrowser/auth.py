"""Where the browser gets its credentials.

Two ways to authenticate, and the subscription is preferred because most people
who have one would rather spend it than metered API credit:

  1. **Claude subscription (Pro/Max).** Claude Code stores an OAuth credential
     in ~/.claude/.credentials.json when you log in. It is sent as
     `Authorization: Bearer <token>` together with the `oauth-2025-04-20` beta
     header -- OAuth tokens do not go in `x-api-key`, and converting between the
     two is a header change, not a key swap.
  2. **API key.** `ANTHROPIC_API_KEY`, from the environment or the settings file.

Order is controlled by CB_AUTH: `auto` (default -- subscription first, key as
fallback), `subscription`, or `api`.

An important caveat, stated plainly because it is the user's to weigh: the
subscription token is issued for Claude Code. Whether Anthropic authorizes it
for another client's API calls is their decision, not ours, and it may come back
401/403. That is why `auto` silently falls back to the API key on an auth
failure and reports which credential actually served the request -- the failure
mode should be a working browser and an honest label, not a dead panel.

Refreshing is deliberately not implemented: it needs Claude Code's OAuth client
identity, which is not ours to reuse. An expired token says so and points at
`/login` in Claude Code, which is the supported way to renew it.
"""

import json
import os
import time
from pathlib import Path

CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
OAUTH_BETA = "oauth-2025-04-20"


class NoCredential(Exception):
    """Nothing usable was found. Message is written for the panel."""


def _read_subscription():
    """Return (token, subscription_type, expires_at_ms) or None."""
    try:
        data = json.loads(CLAUDE_CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = data.get("claudeAiOauth") or {}
    token = block.get("accessToken")
    if not token:
        return None
    return token, block.get("subscriptionType") or "subscription", block.get("expiresAt")


def subscription_status():
    """Human-readable state of the subscription credential, for the UI."""
    found = _read_subscription()
    if not found:
        return None, "no Claude subscription login found"
    _token, kind, expires = found
    if expires and time.time() * 1000 > expires:
        return None, "%s login expired -- run /login in Claude Code" % kind
    return kind, "%s subscription" % kind


def api_key():
    return os.environ.get("ANTHROPIC_API_KEY") or None


def preference():
    value = (os.environ.get("CB_AUTH") or "auto").strip().lower()
    return value if value in ("auto", "subscription", "api") else "auto"


def _subscription_headers():
    found = _read_subscription()
    if not found:
        return None
    token, kind, expires = found
    if expires and time.time() * 1000 > expires:
        return None
    return (
        {"authorization": "Bearer " + token, "anthropic-beta": OAUTH_BETA},
        "%s subscription" % kind,
    )


def _api_headers():
    key = api_key()
    if not key:
        return None
    return {"x-api-key": key}, "API key"


def candidates():
    """Credentials to try, in order. Each is (headers, label)."""
    pref = preference()
    subscription, api = _subscription_headers(), _api_headers()

    if pref == "api":
        options = [api]
    elif pref == "subscription":
        options = [subscription]
    else:
        options = [subscription, api]

    options = [o for o in options if o]
    if options:
        return options

    raise NoCredential(_explain(pref))


def _explain(pref):
    _kind, subscription_note = subscription_status()
    from . import envfile

    lines = ["No usable credential."]
    if pref != "api":
        lines.append("Subscription: %s." % subscription_note)
    if pref != "subscription":
        lines.append("API key: ANTHROPIC_API_KEY is not set.")
    lines.append("")
    lines.append("Either log in with Claude Code (/login), or put a key in")
    lines.append("    %s" % envfile.config_path())
    lines.append("as  ANTHROPIC_API_KEY=sk-ant-...  and restart the browser.")
    lines.append("")
    lines.append("A window launched from the desktop menu does not inherit your")
    lines.append("shell environment, so an export in ~/.bashrc will not reach it.")
    return "\n".join(lines)


def describe():
    """One line for the status bar: which credential will be used."""
    try:
        _headers, label = candidates()[0]
        return label
    except NoCredential:
        return "no credential"
