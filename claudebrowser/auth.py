"""Where the browser gets its credentials.

  1. **API key.** `ANTHROPIC_API_KEY` from the browser's own settings file,
     `~/.config/claude-browser/env`. This is the default and the supported
     path. The file wins over any variable exported in a shell, and the value
     is never copied into the process environment.
  2. **Claude subscription (Pro/Max).** Claude Code stores an OAuth credential
     in ~/.claude/.credentials.json when you log in. It is sent as
     `Authorization: Bearer <token>` with the `oauth-2025-04-20` beta header --
     OAuth tokens do not go in `x-api-key`.

Order is controlled by CB_AUTH: `auto` (default -- key first, subscription as a
fallback), `api`, or `subscription`.

**Why the subscription is not the default, and why there is no "Log in with
Claude" button here.** A Pro/Max subscription entitles you to Anthropic's own
apps -- claude.ai, Claude Code, the desktop and mobile clients. It is not a
credential for arbitrary third-party software, and Anthropic publishes no OAuth
client registration that would let this browser obtain its own subscription
token. The only way to get one would be to present Claude Code's client identity
as our own, which is impersonation; we do not do that. So the honest options are
an API key (metered, sanctioned, works) or reusing the token already on disk,
which is unsanctioned and may be declined or rate-limited at any time. The
second is kept as a fallback because it is the user's own credential on the
user's own machine, but it is not something to build a product on.

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
    """The browser's own key, from its settings file.

    Read here rather than from os.environ so that an inherited variable -- which
    the app cannot edit, delete, or even reliably trace back to a file -- can
    never decide which credential a request uses. The environment is consulted
    only when the settings file says nothing.
    """
    from . import envfile

    return envfile.setting("ANTHROPIC_API_KEY") or None


def key_source():
    """Where api_key() came from, for the status line and error panels."""
    from . import envfile

    if envfile.values().get("ANTHROPIC_API_KEY"):
        return str(envfile.config_path())
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "your environment"
    return None


def preference():
    from . import envfile

    value = (envfile.setting("CB_AUTH") or "auto").strip().lower()
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
        # API key first. It is the only credential Anthropic issues for
        # third-party programmatic use, so it is the one that reliably works;
        # the subscription token is a fallback for people who have no key.
        options = [api, subscription]

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
    lines.append("as  ANTHROPIC_API_KEY=sk-ant-...")
    lines.append("")
    lines.append("That file is the browser's own credential. It is read from every")
    lines.append("launcher -- terminal, desktop menu, dock -- and it wins over any")
    lines.append("key exported in a shell, so there is nothing else to keep in sync.")
    return "\n".join(lines)


def describe():
    """One line for the status bar: which credential will be used."""
    try:
        _headers, label = candidates()[0]
        return label
    except NoCredential:
        return "no credential"
