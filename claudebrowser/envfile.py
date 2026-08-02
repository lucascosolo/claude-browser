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

The real environment always wins, so `CB_BLOCK=0 ./cb` still overrides the file
for one run.
"""

import os
import stat
from pathlib import Path


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


def load(path=None, environ=None, warn=None):
    """Merge the config file into `environ` without clobbering what is set.

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
        if key in environ:
            continue  # an explicit environment variable outranks the file
        environ[key] = value
        applied.append(key)
    return applied


TEMPLATE = """\
# Settings for Claude Browser. Read at startup from every launcher -- terminal,
# desktop menu, and the MCP server -- because a menu-launched app never sees
# your shell environment.
#
# The real environment wins, so `CB_BLOCK=0 claude-browser` still overrides.
# Keep this file private: chmod 600.

# Enables Ask, TL;DR, Research and the Command bar.
#ANTHROPIC_API_KEY=sk-ant-...

# Ad/tracker blocking. On by default; set to 0 if a site misbehaves.
#CB_BLOCK=1

# Start page and search engine (%s is the query).
#CB_HOME=about:blank
#CB_SEARCH=https://duckduckgo.com/?q=%s

# Agent control API.
#CB_PORT=8765
#CB_TOKEN=

# dark or light, overriding the system preference.
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
