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
