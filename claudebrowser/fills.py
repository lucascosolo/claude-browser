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
