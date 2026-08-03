"""History and bookmarks, in one small SQLite file.

    ~/.local/share/claude-browser/browsing.db

Two design choices worth stating, because both look like shortcuts and are not:

**History is deduplicated by URL, not appended per visit.** A visit log is what
most browsers keep, and it makes the history page a wall of the same three URLs
repeated forty times. One row per URL with a visit count is smaller, is what
autocomplete actually wants to rank by, and still supports "what did I look at
on Tuesday" through `last_visit`. The thing it cannot answer is "how many times
did I open this on Tuesday specifically" -- nobody has ever needed that here.

**Writes go to a background thread.** These calls happen on the GTK main loop,
once per page load, and this browser targets a machine that is already swapping.
A commit that blocks the UI thread for a disk seek is a stutter on exactly the
frame where the page is painting. The queue makes it free; `flush()` exists so
tests (and shutdown) can wait for it.

No GTK import here on purpose: this is the layer that has to be testable without
a display.
"""

import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

# What never belongs in history: our own internal pages, blank tabs, and inline
# documents (a data: URL can be megabytes, and re-visiting one is meaningless).
SKIP_SCHEMES = ("about:", "cb:", "data:", "blob:", "javascript:")

# Above this many rows, the oldest are dropped on startup. Ten thousand URLs is
# far more than the autocomplete can usefully rank and keeps the file small
# enough that the whole index stays in page cache.
MAX_HISTORY = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    url        TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    visits     INTEGER NOT NULL DEFAULT 1,
    last_visit INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS history_recent ON history(last_visit DESC);

CREATE TABLE IF NOT EXISTS bookmarks (
    url   TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    added INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bookmarks_added ON bookmarks(added DESC);
"""


def data_dir():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "claude-browser"


def default_path():
    return data_dir() / "browsing.db"


def recordable(url):
    """Is this a URL worth remembering?"""
    if not url:
        return False
    url = url.strip()
    return bool(url) and not url.lower().startswith(SKIP_SCHEMES)


def host_of(url):
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


class Store:
    def __init__(self, path=None, background=True):
        self.path = Path(path) if path else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # :memory: cannot be shared across threads -- each connection would get
        # its own empty database -- so in-memory stores (i.e. tests) run inline.
        self._background = background and str(self.path) != ":memory:"
        self._queue = queue.Queue()
        self._writer = None
        with self._connect() as db:
            db.executescript(SCHEMA)
        if self._background:
            self._writer = threading.Thread(target=self._drain, daemon=True)
            self._writer.start()

    # -- plumbing -----------------------------------------------------------

    def _connect(self):
        """One connection per thread. WAL plus synchronous=NORMAL means a commit
        does not wait on fsync, which is the difference between 'instant' and
        'noticeable' on a slow disk."""
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(str(self.path), timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass  # a read-only or exotic filesystem is not worth failing over
            self._local.db = db
        return db

    def _drain(self):
        while True:
            job = self._queue.get()
            if job is None:
                self._close_local()
                self._queue.task_done()
                return
            sql, args = job
            try:
                with self._connect() as db:
                    db.execute(sql, args)
            except sqlite3.DatabaseError:
                pass  # losing a history row must never take the browser with it
            finally:
                self._queue.task_done()

    def _close_local(self):
        """Close this thread's connection, if it has one.

        sqlite3 refuses a close() from a thread other than the one that opened
        the connection, so each thread has to hand its own back -- the writer
        does it on its way out, `close()` does it for the caller."""
        db = getattr(self._local, "db", None)
        if db is not None:
            self._local.db = None
            try:
                db.close()
            except sqlite3.Error:
                pass

    def _write(self, sql, args=()):
        if self._background:
            self._queue.put((sql, args))
            return
        try:
            with self._connect() as db:
                db.execute(sql, args)
        except sqlite3.DatabaseError:
            pass

    def flush(self):
        """Block until queued writes have landed. For tests and shutdown."""
        if self._background:
            self._queue.join()

    def close(self):
        if self._background and self._writer:
            self._queue.put(None)
            self._writer.join(timeout=2)
            self._background = False
        self._close_local()

    def _query(self, sql, args=()):
        try:
            return self._connect().execute(sql, args).fetchall()
        except sqlite3.DatabaseError:
            return []

    # -- history ------------------------------------------------------------

    def record(self, url, title=""):
        """Note a visit. Called once per completed load, and again when the
        title arrives -- which is normally *after* the load finishes, so the
        upsert deliberately keeps the longer of the two titles rather than
        letting a later empty one erase a good one."""
        if not recordable(url):
            return False
        self._write(
            """INSERT INTO history (url, title, visits, last_visit)
                    VALUES (?, ?, 1, ?)
               ON CONFLICT(url) DO UPDATE SET
                    visits = visits + 1,
                    last_visit = excluded.last_visit,
                    title = CASE WHEN length(excluded.title) > 0
                                 THEN excluded.title ELSE history.title END""",
            (url, title or "", int(time.time())),
        )
        return True

    def retitle(self, url, title):
        """Update a title without counting another visit. `notify::title` fires
        several times on a single load; counting each would make one page look
        like five visits and poison the ranking."""
        if not recordable(url) or not title:
            return False
        self._write("UPDATE history SET title = ? WHERE url = ?", (title, url))
        return True

    def history(self, query=None, limit=300):
        if query:
            like = "%%%s%%" % query.strip()
            return self._query(
                """SELECT url, title, visits, last_visit FROM history
                   WHERE url LIKE ? OR title LIKE ?
                   ORDER BY last_visit DESC, rowid DESC LIMIT ?""", (like, like, limit))
        return self._query(
            """SELECT url, title, visits, last_visit FROM history
               ORDER BY last_visit DESC, rowid DESC LIMIT ?""", (limit,))

    def forget(self, url):
        self._write("DELETE FROM history WHERE url = ?", (url,))

    def forget_host(self, host):
        self._write("DELETE FROM history WHERE url LIKE ?", ("%%//%s%%" % host,))

    def clear_history(self):
        self._write("DELETE FROM history")

    def prune(self, keep=MAX_HISTORY):
        self._write(
            """DELETE FROM history WHERE url IN (
                   SELECT url FROM history
                   ORDER BY last_visit DESC, rowid DESC LIMIT -1 OFFSET ?)""",
            (keep,))

    # -- bookmarks ----------------------------------------------------------

    def bookmark(self, url, title=""):
        if not recordable(url):
            return False
        self._write(
            """INSERT INTO bookmarks (url, title, added) VALUES (?, ?, ?)
               ON CONFLICT(url) DO UPDATE SET
                    title = CASE WHEN length(excluded.title) > 0
                                 THEN excluded.title ELSE bookmarks.title END""",
            (url, title or "", int(time.time())))
        return True

    def unbookmark(self, url):
        self._write("DELETE FROM bookmarks WHERE url = ?", (url,))

    def is_bookmarked(self, url):
        if not url:
            return False
        return bool(self._query("SELECT 1 FROM bookmarks WHERE url = ?", (url,)))

    def toggle_bookmark(self, url, title=""):
        """Returns the new state, so the caller can restyle the star without a
        second round trip."""
        if not recordable(url):
            return False
        if self.is_bookmarked(url):
            self.unbookmark(url)
            self.flush()
            return False
        self.bookmark(url, title)
        self.flush()
        return True

    def bookmarks(self, query=None, limit=500):
        if query:
            like = "%%%s%%" % query.strip()
            return self._query(
                """SELECT url, title, added FROM bookmarks
                   WHERE url LIKE ? OR title LIKE ?
                   ORDER BY added DESC LIMIT ?""", (like, like, limit))
        return self._query(
            "SELECT url, title, added FROM bookmarks ORDER BY added DESC LIMIT ?", (limit,))

    # -- the omnibox --------------------------------------------------------

    def suggest(self, text, limit=8):
        """Rank matches for what has been typed so far.

        Bookmarks outrank history: something deliberately saved is a better
        guess than something merely seen. Within history the score is visits
        with a recency bonus -- a page opened forty times last month should
        still beat one opened once this morning, but not by so much that a
        fresh interest can never surface.
        """
        text = (text or "").strip()
        if not text:
            return []
        like = "%%%s%%" % text
        now = int(time.time())
        rows = []
        seen = set()

        for row in self._query(
                """SELECT url, title FROM bookmarks
                   WHERE url LIKE ? OR title LIKE ? ORDER BY added DESC LIMIT ?""",
                (like, like, limit)):
            rows.append({"url": row["url"], "title": row["title"], "bookmark": True})
            seen.add(row["url"])

        for row in self._query(
                """SELECT url, title, visits, last_visit FROM history
                   WHERE url LIKE ? OR title LIKE ?
                   ORDER BY visits DESC, last_visit DESC LIMIT ?""",
                (like, like, limit * 3)):
            if row["url"] in seen:
                continue
            days = max(0, (now - row["last_visit"]) / 86400.0)
            score = row["visits"] + (6.0 if days < 1 else 3.0 if days < 7 else 0.0)
            # A match at the start of the host is what the user is most likely
            # reaching for; "git" should offer github.com before a random page
            # with "git" buried in its title.
            if host_of(row["url"]).lower().lstrip("www.").startswith(text.lower()):
                score += 12.0
            rows.append({"url": row["url"], "title": row["title"],
                         "bookmark": False, "_score": score})

        head = [r for r in rows if r.get("bookmark")]
        tail = sorted((r for r in rows if not r.get("bookmark")),
                      key=lambda r: -r["_score"])
        return (head + tail)[:limit]

    def top_sites(self, limit=8):
        """Most-visited, one row per host, for the start page."""
        rows = self._query(
            """SELECT url, title, visits, last_visit FROM history
               ORDER BY visits DESC, last_visit DESC LIMIT ?""", (limit * 6,))
        out, hosts = [], set()
        for row in rows:
            host = host_of(row["url"])
            if host in hosts:
                continue
            hosts.add(host)
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def counts(self):
        history = self._query("SELECT COUNT(*) AS n FROM history")
        marks = self._query("SELECT COUNT(*) AS n FROM bookmarks")
        return ((history[0]["n"] if history else 0),
                (marks[0]["n"] if marks else 0))
