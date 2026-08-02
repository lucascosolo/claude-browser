"""Saved logins, kept in the system keyring.

**Why this exists instead of Google Password Manager.** Google Password Manager
is not a service third-party browsers can talk to. The autofill half lives
inside Chrome and Android; the sync half rides Chrome Sync, whose API is gated
behind client credentials Google issues to Chrome builds and does not document
for anyone else. There is no endpoint, no extension point, and no file on disk
to read -- so "use Google's password manager here" is not a thing that can be
built, only a thing that can be faked badly. `passwords.google.com` renders fine
in this browser if you want to look one up by hand; that is the whole of the
integration that actually exists.

Signing *in* to a Google account works, which is a separate question and is why
that one is worth answering separately.

**Where the secrets go.** The system keyring, over the freedesktop Secret
Service (gnome-keyring here), never a file this project invents. That is not
deference to a standard for its own sake: a password file of our own would need
a master key, and the only place to put a master key is another file next to it,
which is not encryption -- it is obfuscation with extra steps. The keyring is
already unlocked by PAM at login, already backed by the user's login password,
and already what `seahorse` can audit. We store; we do not invent crypto.

**What is stored.** One item per (origin, username). The origin is a normalized
scheme://host[:port] -- never a path, because a password belongs to a site and
not to a page, and never a bare hostname, because `http://` and `https://` are
different security origins and merging them would let a downgraded page claim a
secret typed into a secure one.

No GTK import here, deliberately: this is the layer that has to be testable
without a display, and the tests inject `MemoryBackend` in place of the keyring.
"""

from urllib.parse import urlsplit

# The kinds of item we put in the keyring. `never` is a tombstone with no real
# secret: it records "stop asking about this site", which has to survive a
# restart or the prompt becomes nagware.
LOGIN = "login"
NEVER = "never"

APP = "Claude Browser"


def origin_of(url):
    """`https://example.com/a/b?c` -> `https://example.com`, or None.

    Returns None for anything that is not a real web origin -- `cb:`, `about:`,
    `data:`, `file:`. Those have no meaningful site identity to file a password
    under, and `file:` in particular would file every local page on earth under
    one shared key.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:          # a malformed port raises rather than returning None
        return None
    default = 443 if parts.scheme == "https" else 80
    if port and port != default:
        return "%s://%s:%d" % (parts.scheme, host, port)
    return "%s://%s" % (parts.scheme, host)


def label_for(origin, username):
    """What `seahorse` and every other keyring UI will show for this item."""
    who = username or "(no username)"
    return "%s - %s (%s)" % (APP, origin, who)


class MemoryBackend:
    """A dict pretending to be a keyring. Tests only; nothing persists."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(attrs):
        return (attrs.get("kind", ""), attrs.get("origin", ""),
                attrs.get("username", ""))

    def store(self, attrs, _label, secret):
        self.items[self._key(attrs)] = (dict(attrs), secret)
        return True

    def lookup(self, attrs):
        found = self.items.get(self._key(attrs))
        return found[1] if found else None

    def search(self, kind):
        return [dict(a) for a, _ in self.items.values() if a.get("kind") == kind]

    def clear(self, attrs):
        return self.items.pop(self._key(attrs), None) is not None


class SecretBackend:
    """The real one: freedesktop Secret Service via libsecret."""

    def __init__(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret

        self._secret = Secret
        # Every attribute we ever query on has to be declared here; libsecret
        # matches items by schema name *and* attribute set.
        self._schema = Secret.Schema.new(
            "net.claudebrowser.Login",
            Secret.SchemaFlags.NONE,
            {
                "kind": Secret.SchemaAttributeType.STRING,
                "origin": Secret.SchemaAttributeType.STRING,
                "username": Secret.SchemaAttributeType.STRING,
            },
        )

    def store(self, attrs, label, secret):
        return self._secret.password_store_sync(
            self._schema, attrs, self._secret.COLLECTION_DEFAULT,
            label, secret, None)

    def lookup(self, attrs):
        return self._secret.password_lookup_sync(self._schema, attrs, None)

    def search(self, kind):
        # SearchFlags.ALL without LOAD_SECRETS: the management page lists which
        # logins exist, and has no business pulling every password into memory
        # to do it. Secrets are fetched one at a time, on an explicit reveal.
        items = self._secret.password_search_sync(
            self._schema, {"kind": kind}, self._secret.SearchFlags.ALL, None)
        return [dict(i.get_attributes()) for i in items]

    def clear(self, attrs):
        return self._secret.password_clear_sync(self._schema, attrs, None)


def open_vault():
    """A Vault, or None if this machine has no keyring.

    Same posture as the history store: a box without a Secret Service should
    cost you password saving, not your browser.
    """
    try:
        return Vault(SecretBackend())
    except Exception as e:
        print("passwords: disabled (%s)" % e, flush=True)
        return None


class Vault:
    def __init__(self, backend=None):
        self.backend = backend or SecretBackend()

    # -- logins -------------------------------------------------------------

    def save(self, origin, username, password):
        if not origin or not password:
            return False
        attrs = {"kind": LOGIN, "origin": origin, "username": username or ""}
        return bool(self.backend.store(attrs, label_for(origin, username), password))

    def secret(self, origin, username):
        if not origin:
            return None
        return self.backend.lookup(
            {"kind": LOGIN, "origin": origin, "username": username or ""})

    def usernames(self, origin):
        """Every username saved for a site, oldest-sorted for a stable list."""
        if not origin:
            return []
        return sorted(a.get("username", "") for a in self.backend.search(LOGIN)
                      if a.get("origin") == origin)

    def credentials(self, origin):
        """`[{"username": ..., "password": ...}]` for one origin.

        The only method that hands out secrets in bulk, and the only caller is
        autofill -- which must have already established the origin from the
        WebView's own URL, not from anything the page said.
        """
        out = []
        for username in self.usernames(origin):
            password = self.secret(origin, username)
            if password is not None:
                out.append({"username": username, "password": password})
        return out

    def entries(self):
        """Everything saved, without secrets, for the management page."""
        rows = [{"origin": a.get("origin", ""), "username": a.get("username", "")}
                for a in self.backend.search(LOGIN)]
        return sorted(rows, key=lambda r: (r["origin"], r["username"]))

    def delete(self, origin, username):
        return bool(self.backend.clear(
            {"kind": LOGIN, "origin": origin, "username": username or ""}))

    # -- "never for this site" ----------------------------------------------

    def set_never(self, origin):
        if not origin:
            return False
        attrs = {"kind": NEVER, "origin": origin, "username": ""}
        return bool(self.backend.store(
            attrs, "%s - never save for %s" % (APP, origin), "1"))

    def is_never(self, origin):
        if not origin:
            return False
        return self.backend.lookup(
            {"kind": NEVER, "origin": origin, "username": ""}) is not None

    def clear_never(self, origin):
        return bool(self.backend.clear(
            {"kind": NEVER, "origin": origin, "username": ""}))

    def never_list(self):
        return sorted(a.get("origin", "") for a in self.backend.search(NEVER))

    # -- what the save bar needs to decide whether to appear ----------------

    def should_offer(self, origin, username, password):
        """False when saving this would be a no-op or was declined forever."""
        if not origin or not password:
            return False
        if self.is_never(origin):
            return False
        # Re-offering a credential we already hold verbatim is the single most
        # annoying thing a password manager does. An unchanged login should be
        # silent; a *changed* password for a known username still prompts,
        # because that is the password-rotation case and it matters.
        return self.secret(origin, username) != password


# The page-side half. Injected at document-start into the top frame only.
#
# Two rules shape all of this:
#
#   1. **The page is never asked what origin it is.** It reports a captured
#      credential and nothing else; the native side pairs it with the URL the
#      WebView actually has. A page that lies can only lie about its own data.
#   2. **Nothing is pushed into a page that did not just load.** Autofill is
#      driven from the native side after a load finishes, against an origin
#      derived from the view. The page cannot ask for a password.
#
# `postMessage` here is a doorbell, not a delivery: it carries no credential.
# The native side answers it by reading `__cbPwTake()` out of the *focused*
# view, so a background tab ringing the bell gets someone else's empty pocket.
PASSWORD_JS = r"""
(function () {
  if (window.__cbPw) { return; }
  window.__cbPw = 1;
  var pending = null;

  function visible(el) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }

  function passwords() {
    return Array.prototype.filter.call(
      document.querySelectorAll('input[type=password]'), visible);
  }

  // The username field is the one a human would have typed in just before the
  // password: an explicit autocomplete hint if the site bothered, otherwise the
  // nearest preceding text-ish input inside the same form.
  function userField(pw) {
    var scope = pw.form || document;
    var candidates = Array.prototype.filter.call(
      scope.querySelectorAll('input'), function (i) {
        var t = (i.getAttribute('type') || 'text').toLowerCase();
        return visible(i) && (t === 'text' || t === 'email' || t === 'tel');
      });
    var best = null;
    for (var i = 0; i < candidates.length; i++) {
      var el = candidates[i];
      var hint = (el.getAttribute('autocomplete') || '').toLowerCase();
      if (hint.indexOf('username') >= 0 || hint.indexOf('email') >= 0) { return el; }
      if (pw.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING) {
        best = el;
      }
    }
    return best || candidates[0] || null;
  }

  // React and friends install their own `value` setter on the element and track
  // state separately; assigning `el.value` updates the DOM but leaves the
  // framework believing the field is still empty, so the form submits blank.
  // Going through the prototype descriptor sets it the way a keystroke would.
  function setValue(el, value) {
    var d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    if (d && d.set) { d.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function capture() {
    var fields = passwords();
    for (var i = 0; i < fields.length; i++) {
      if (!fields[i].value) { continue; }
      var u = userField(fields[i]);
      pending = { username: u ? u.value : '', password: fields[i].value };
      try { webkit.messageHandlers.cbpw.postMessage(1); } catch (e) {}
      return;
    }
  }

  window.__cbPwTake = function () {
    var p = pending;
    pending = null;
    return p ? JSON.stringify(p) : '';
  };

  window.__cbPwFill = function (username, password) {
    var fields = passwords();
    if (!fields.length) { return 0; }
    var pw = fields[0];
    // Never overwrite something already in the box -- that is either the user
    // mid-type or a value the site put there on purpose.
    if (pw.value) { return 0; }
    var u = userField(pw);
    if (u && !u.value && username) { setValue(u, username); }
    setValue(pw, password);
    return 1;
  };

  // Three ways a login leaves: a real form submit, a click on whatever the site
  // uses instead of one, and the page going away. An SPA login often fires none
  // of the first two in a way we would recognise, which is what pagehide is for.
  document.addEventListener('submit', capture, true);
  window.addEventListener('pagehide', capture, true);
  document.addEventListener('click', function (e) {
    var el = e.target;
    for (var hops = 0; el && hops < 4; hops++, el = el.parentElement) {
      var tag = (el.tagName || '').toLowerCase();
      var type = (el.getAttribute && (el.getAttribute('type') || '')).toLowerCase();
      if (tag === 'button' || type === 'submit' || type === 'button') {
        return capture();
      }
    }
  }, true);
})();
"""
