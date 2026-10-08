"""File uploads driven by the agent: which paths may be handed to a page, and
the short-lived hand-off between clicking a file input and WebKit asking which
files to choose. GTK-free so both are tested.

Paths are checked before the page is touched: a missing file found after the
click leaves a native chooser open that nobody is going to answer.
"""
import json
import os
import time

TTL_S = 10


def check_paths(raw):
    """One absolute path, or a JSON array string of them, as a list. Raises
    ValueError naming the first problem."""
    raw = (raw or "").strip()
    if raw.startswith("["):
        try:
            paths = json.loads(raw)
        except ValueError:
            raise ValueError("path is not a valid JSON array of paths")
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("path must be a JSON array of strings")
    else:
        paths = [raw] if raw else []
    if not paths:
        raise ValueError("no file given")
    for p in paths:
        if not os.path.isabs(p):
            raise ValueError("not an absolute path: %s" % p)
        if not os.path.exists(p):
            raise ValueError("no such file: %s" % p)
        if not os.path.isfile(p):
            raise ValueError("not a regular file: %s" % p)
        if not os.access(p, os.R_OK):
            raise ValueError("not readable: %s" % p)
    return paths


class Pending:
    """Files waiting for the next file chooser on a tab. Taken once, and they
    expire, so a list the page never asked for cannot answer a person's own
    click on some later file input."""

    def __init__(self, ttl_s=TTL_S):
        self.ttl_s = ttl_s
        self._by_tab = {}

    def put(self, tab_id, paths, now=None):
        self._by_tab[tab_id] = (list(paths), time.monotonic() if now is None else now)

    def take(self, tab_id, now=None):
        entry = self._by_tab.pop(tab_id, None)
        if entry is None:
            return None
        paths, at = entry
        now = time.monotonic() if now is None else now
        return paths if now - at <= self.ttl_s else None
