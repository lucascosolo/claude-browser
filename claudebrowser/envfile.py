"""Settings that survive being launched from a desktop menu.

A GUI app started from the XFCE/GNOME menu does not get your shell environment:
`~/.bashrc` is never read, so an `export ANTHROPIC_API_KEY=...` there works for
`./cb` in a terminal and silently does nothing for the menu entry. The usual
workarounds are all unpleasant -- `~/.profile` needs a full re-login, and baking
the value into the .desktop file puts an API key in a world-readable text file.

So the browser reads its own file instead. One place, same behaviour from every
launcher.

    ~/.config/claude-browser/env

Format is the familiar KEY=VALUE, one per line, `#` for comments, optional
surrounding quotes, and a tolerated `export ` prefix so a snippet pasted out of
a shell profile works unchanged.

**This file wins over the environment.** It used to be the other way around,
which is the intuitive rule for a command-line tool and the wrong one for this.
A browser is launched from a menu, a terminal, and a dock, and it must behave
identically from all three; deferring to whatever happens to be exported means
the answer to "which key am I using?" depends on how the window was opened. Worse,
an environment variable cannot be un-set from inside the app: a shell that
exported a stale key before you fixed your `~/.bashrc` keeps handing that dead
value to every process it spawns, forever, and no amount of editing files can
reach it. Making this file authoritative gives the browser one credential that
you can actually change.

Secrets go further and never touch `os.environ` at all -- see SECRET_KEYS.
"""

import os
import stat
from pathlib import Path

#: Read straight out of the file by the code that needs them, and deliberately
#: never exported into the process environment. Two reasons. It cannot be
#: shadowed by an inherited variable, which is the whole point above; and it is
#: not handed to child processes -- the control API server, a spawned helper, a
#: crash reporter -- none of which have any business holding the user's API key.
#: CB_VPN_PROXY belongs here for the same two reasons, and it is easy to miss
#: why: it is a URL, but the URL embeds the proxy's password (WebKit's
#: NetworkProxySettings takes credentials no other way -- see vpn.py). A proxy
#: URL in os.environ is a password in os.environ, inherited by every child
#: process, and a proxy URL a settings control could write is a password an
#: API call could rewrite.
SECRET_KEYS = frozenset({"ANTHROPIC_API_KEY", "CB_VPN_PROXY", "CB_SEARCH_KEY"})


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "claude-browser"


def config_path():
    return config_dir() / "env"


def parse(text):
    """KEY=VALUE lines to a dict. Anything unparseable is skipped, not fatal --
    a typo in a settings file should not stop the browser from starting."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip one matching pair of quotes; leave inner quotes alone.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def values(path=None):
    """The settings file as a dict, read fresh every time.

    Deliberately uncached. This is called once per API request, against a
    sub-kilobyte file, on the way to a network round trip -- the read does not
    show up. A cache keyed on size and mtime *looks* free and is not: an edit
    that keeps the length the same (swapping one key for another of the same
    shape -- the exact thing someone does here) within the filesystem's mtime
    granularity is invisible to it, so the browser keeps using the key the user
    just replaced. That is the failure this whole change exists to end.
    """
    path = Path(path) if path else config_path()
    try:
        return parse(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def setting(name, default=None, path=None, environ=None):
    """One setting, file first. The environment is a fallback, not an override.

    Non-secret CB_* values are also pushed into os.environ by load() so that
    module-level constants elsewhere see them; this is the direct route for
    anything read after startup.
    """
    environ = os.environ if environ is None else environ
    found = values(path).get(name)
    if found:
        return found
    return environ.get(name) or default


def put(key, value, path=None, environ=None):
    """Write one setting into the file, leaving everything else as it was.

    The file is the user's -- comments, ordering and the commented-out examples
    from the template are all things they may have edited -- so this is a
    surgical rewrite rather than a dump of a parsed dict: the first uncommented
    assignment of `key` is replaced in place, and if there is none, one line is
    appended. A commented `#CB_PERSONA=` example is deliberately left alone, so
    the documentation of what the key means survives being set.

    Secrets are refused. Nothing in this browser should ever be able to write
    the API key from a UI control or an API call: the key goes into this file by
    the user's own hand, and a setter here would be a way for a page-driven
    operation to overwrite it.
    """
    if key in SECRET_KEYS:
        raise ValueError("%s is not settable from inside the browser" % key)
    if "\n" in str(value) or "\n" in key:
        raise ValueError("a setting is one line")

    path = Path(path) if path else config_path()
    environ = os.environ if environ is None else environ
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing = []

    line = "%s=%s" % (key, value)
    out, written = [], False
    for raw in existing:
        stripped = raw.strip()
        candidate = (stripped[len("export "):].lstrip()
                     if stripped.startswith("export ") else stripped)
        name, sep, _rest = candidate.partition("=")
        if sep and not stripped.startswith("#") and name.strip() == key:
            if not written:
                out.append(line)
                written = True
            continue           # a duplicate assignment collapses into the one
        out.append(raw)
    if not written:
        out.append(line)

    # Replaced atomically: a half-written settings file is one that parses to
    # fewer settings than it had, which reads as the browser forgetting them.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        tmp.chmod(0o600)       # it may come to hold the API key
    except OSError:
        pass
    os.replace(tmp, path)

    # Keep the process in step, for the module-level constants that read
    # os.environ once at import. values() is uncached, so this is belt only.
    environ[key] = str(value)
    return path


def remove(key, path=None, environ=None):
    """Delete a setting's assignment so the built-in default applies again.

    The mirror of `put`, and cb:settings is why it exists: "back to default"
    has to *mean* the default. Writing the current default value back would pin
    it instead -- a later release that ships a better default would silently not
    reach anyone who had ever pressed that button -- so the line goes away.

    Surgical for the same reason `put` is: comments, ordering and the template's
    commented examples are the user's, and a `#CB_GPU=` line documenting what the
    key means must survive the value being cleared. Secrets are refused on the
    same grounds as well -- deleting the user's API key from inside the browser
    is not a smaller thing than writing it.

    Returns True when a line was actually removed.
    """
    if key in SECRET_KEYS:
        raise ValueError("%s is not settable from inside the browser" % key)

    path = Path(path) if path else config_path()
    environ = os.environ if environ is None else environ
    try:
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        existing = []

    out, dropped = [], False
    for raw in existing:
        stripped = raw.strip()
        candidate = (stripped[len("export "):].lstrip()
                     if stripped.startswith("export ") else stripped)
        name, sep, _rest = candidate.partition("=")
        if sep and not stripped.startswith("#") and name.strip() == key:
            dropped = True
            continue
        out.append(raw)

    if dropped:
        # Same atomic replace as put(): a half-written file reads as the browser
        # having forgotten settings the user never touched.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(("\n".join(out) + "\n") if out else "", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    # Popped, not left behind. put() pushes what it writes into this process's
    # environment, and `setting` falls back to the environment -- so a value
    # that outlived the line it came from would keep answering for a setting the
    # user has just reset.
    environ.pop(key, None)
    return dropped


def load(path=None, environ=None, warn=None):
    """Apply the config file to `environ`, overriding what is already set.

    Returns the list of keys it actually applied.
    """
    path = Path(path) if path else config_path()
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []

    # The file holds an API key. If it is readable by anyone else, say so once --
    # silently loading a world-readable secret is worse than being noisy.
    if warn:
        try:
            mode = path.stat().st_mode
            if mode & (stat.S_IRGRP | stat.S_IROTH):
                warn("%s is readable by other users; chmod 600 it" % path)
        except OSError:
            pass

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        if warn:
            warn("could not read %s: %s" % (path, e))
        return []

    applied = []
    for key, value in parse(text).items():
        if key in SECRET_KEYS:
            # Never exported. auth.api_key() reads it from the file directly.
            # Clear any inherited copy so nothing downstream can pick up a stale
            # one by reaching for os.environ out of habit.
            if environ.pop(key, None) not in (None, value) and warn:
                warn("ignoring the %s in your environment; using the one in %s"
                     % (key, path))
            applied.append(key)
            continue
        if environ.get(key) not in (None, value) and warn:
            warn("%s=%s in %s overrides the %s in your environment"
                 % (key, value, path, key))
        environ[key] = value
        applied.append(key)
    return applied


TEMPLATE = """\
# Settings for Claude Browser. Read at startup from every launcher -- terminal,
# desktop menu, and the MCP server -- because a menu-launched app never sees
# your shell environment.
#
# Whatever you set here wins over your shell environment, so the browser behaves
# the same from a terminal, the desktop menu and the dock. Keep it private:
# chmod 600.
#
# Every CB_* line below is also editable from cb:settings inside the browser,
# which writes this same file. The API key is not, and deliberately so.

# Enables Ask, TL;DR, Research and the Command bar. This is the browser's own
# key: it is read from this file only, is never put into the environment, and an
# ANTHROPIC_API_KEY exported in a shell is ignored while this line is uncommented.
# Edits take effect on the next request -- no restart needed.
#ANTHROPIC_API_KEY=sk-ant-...

# auto (default: API key, then a Claude Code subscription token), api, subscription.
#CB_AUTH=auto

# Ad/tracker blocking. On by default; set to 0 if a site misbehaves.
#CB_BLOCK=1

# Ask sites for a lighter page: sends Save-Data: on with each page this browser
# loads, and asks for reduced motion. On by default. The switch on cb:data
# writes this line; setting it here works too. The header follows on the next
# page load, the reduced motion only from the next launch.
#CB_LIGHT=1

# Start page and search engine (%s is the query).
#CB_HOME=about:blank
#CB_SEARCH=https://duckduckgo.com/?q=%s

# Agent control API.
#CB_PORT=8765
#CB_TOKEN=

# How the Claude panel answers: off, developer, researcher, critic, translator.
# The panel's own selector writes this line; setting it here works too.
#CB_PERSONA=off

# dark, light, or phosphor -- the near-black HUD you get when this is unset.
# "system" is the one that follows the desktop's own dark/light preference.
#CB_THEME=

# Force software rendering (off) or GPU compositing (on).
#CB_GPU=
"""


def ensure_template():
    """Write a commented example on first run so the file is discoverable.
    Never overwrites an existing one."""
    path = config_path()
    if path.exists():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
        return path
    except OSError:
        return None
