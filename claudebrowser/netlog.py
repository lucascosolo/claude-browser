"""The requests one tab has made since its last navigation.

GTK-free so the ring, the filter and the quiet-period rule are tested; the
WebKit signals that feed it live in browser.py. Memory only, which is why a
private tab keeps one too -- the same footing as the console and dialog logs.

Keys are `id()` of a WebKitWebResource. A resource that started before a
navigation reset is forgotten, and its late events are ignored rather than
resurrecting it -- including for `quiet_for`, which is why a reset also has
to be the moment those resources stop counting as in flight.
"""
import re
import time
from collections import OrderedDict


class Log:
    def __init__(self, limit=300):
        self.limit = limit
        self.last_activity = None
        self._live = OrderedDict()   # key -> [entry, start monotonic seconds]

    def start(self, key, url, main=False, now=None):
        now = time.monotonic() if now is None else now
        entry = {"url": url, "status": None, "mime": None, "bytes": 0,
                 "started_ms": int(time.time() * 1000), "duration_ms": None,
                 "error": None, "main": bool(main)}
        self._live.pop(key, None)
        self._live[key] = [entry, now]
        while len(self._live) > self.limit:
            self._live.popitem(last=False)
        self.last_activity = now
        return entry

    def _touch(self, key, now):
        slot = self._live.get(key)
        if slot is not None:
            self.last_activity = time.monotonic() if now is None else now
        return slot

    def response(self, key, status, mime, now=None):
        slot = self._touch(key, now)
        if slot:
            slot[0]["status"], slot[0]["mime"] = status, mime

    def data(self, key, n, now=None):
        slot = self._touch(key, now)
        if slot:
            slot[0]["bytes"] += int(n)

    def _end(self, key, now, error=None):
        slot = self._live.get(key)
        if slot is None or slot[0]["duration_ms"] is not None:
            return
        now = time.monotonic() if now is None else now
        self.last_activity = now
        slot[0]["duration_ms"] = int(round((now - slot[1]) * 1000))
        if error is not None:
            slot[0]["error"] = error

    def finish(self, key, now=None):
        self._end(key, now)

    def fail(self, key, message, now=None):
        self._end(key, now, message)

    def entries(self, pattern=None, limit=100):
        try:
            rx = re.compile(pattern) if pattern else None
        except re.error as e:
            raise ValueError("bad pattern: %s" % e) from None
        found = [dict(e) for e, _ in self._live.values()
                 if rx is None or rx.search(e["url"] or "")]
        limit = 100 if limit is None else max(0, int(limit))
        return found[-limit:] if limit else []

    def clear(self):
        self._live.clear()

    def reset_for_navigation(self, keep=None):
        kept = [(k, s) for k, s in self._live.items() if keep is not None and s[0] is keep]
        self._live = OrderedDict(kept)

    def quiet_for(self, quiet_ms, now=None):
        if any(s[0]["duration_ms"] is None for s in self._live.values()):
            return False
        if self.last_activity is None:
            return True
        now = time.monotonic() if now is None else now
        return now - self.last_activity >= quiet_ms / 1000
