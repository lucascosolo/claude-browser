"""The text of pages you have actually read, kept on disk and searchable.

    ~/.local/share/claude-browser/pagetext.db

This is the honest, dependency-free version of "semantic index over visited
pages": SQLite's own FTS5 over the extracted text of every page that finished
loading. There is no embedding model here on purpose -- one would mean a pip
dependency and a few hundred megabytes of weights for a browser whose whole
premise is the standard library.

Three design choices worth stating:

**The body is stored once per content hash, not once per URL.** The same article
is routinely reachable at a canonical URL, a tracking-parameter URL and an AMP
URL. Keying the text by hash means those three cost one copy of the prose and
three small rows, and it is also what keeps the FTS index from returning the
same paragraph three times.

**Eviction is LRU over a byte cap, not a row count.** Page text varies by three
orders of magnitude -- a docs page is 2 KB, a rendered spec is 400 KB -- so a
row count is not a size bound in any useful sense, and a size bound is what the
disk actually cares about. Least-recently-*used*, not least-recently-stored,
because revisiting a page is the strongest evidence that its text is worth
keeping.

**FTS5 is optional.** It is compiled into every mainstream sqlite3 build, but
not all of them, and a browser that refuses to start because of a search index
it never needed is a worse browser. If the virtual table cannot be created the
store still caches and serves text; `search()` returns nothing and `available`
says why.

Writes go to a background thread for the same reason store.py's do: these calls
land on the GTK main loop once per page load, on a machine that is already
swapping, and a commit that waits on a disk seek is a stutter on exactly the
frame the page is painting. `flush()` exists so tests and shutdown can wait.

No GTK import here on purpose: this is the layer that has to be testable without
a display.
"""

import hashlib
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path

from .store import data_dir, recordable

#: Total body bytes to keep. Roughly a thousand ordinary articles, and small
#: enough that the file plus its index stays cheap to page in on a slow disk.
MAX_BYTES = 24 * 1024 * 1024

#: Longest single document kept. A generated API reference can be megabytes;
#: past this point the tail is boilerplate and would evict real reading.
MAX_DOC_CHARS = 200_000

#: Eviction hysteresis: once over the cap, drop down to this fraction of it
#: rather than to exactly the cap. Trimming one page per load forever is a write
#: amplification problem; trimming in batches is not.
TRIM_TO = 0.85

SCHEMA = """
CREATE TABLE IF NOT EXISTS bodies (
    id    INTEGER PRIMARY KEY,
    hash  TEXT NOT NULL UNIQUE,
    text  TEXT NOT NULL,
    bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    url       TEXT PRIMARY KEY,
    title     TEXT NOT NULL DEFAULT '',
    hash      TEXT NOT NULL,
    stored    INTEGER NOT NULL,
    last_used INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_lru  ON pages(last_used ASC);
CREATE INDEX IF NOT EXISTS pages_hash ON pages(hash);
"""

# External-content FTS5: the index reads its text back out of `bodies` instead
# of keeping a second copy, which is what makes snippet() work without doubling
# the file. The triggers are the documented way to keep the two in step, and
# living in SQL means eviction cannot forget to maintain the index.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS body_fts
    USING fts5(text, content='bodies', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS bodies_ai AFTER INSERT ON bodies BEGIN
    INSERT INTO body_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS bodies_ad AFTER DELETE ON bodies BEGIN
    INSERT INTO body_fts(body_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def default_path():
    return data_dir() / "pagetext.db"


def content_hash(text):
    """Short, fast, and not a security boundary -- this only has to make two
    identical articles collide and two different ones not."""
    return hashlib.blake2b((text or "").encode("utf-8", "replace"),
                           digest_size=16).hexdigest()


def match_query(text):
    """Turn whatever the user typed into an FTS5 MATCH expression.

    Every token is quoted, so the raw input can never be read as FTS syntax --
    a stray `"`, a bare `NEAR` or an unbalanced paren would otherwise raise
    rather than search. Tokens are ANDed, and the last one gets a prefix `*`
    so search-as-you-type finds `authentication` while you are still typing
    `authen`.
    """
    words = _WORD.findall(text or "")
    if not words:
        return ""
    terms = ['"%s"' % w for w in words[:-1]]
    terms.append('"%s"*' % words[-1])
    return " AND ".join(terms)


class PageText:
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
        self.available = False
        self.reason = ""
        with self._connect() as db:
            db.executescript(SCHEMA)
        self._setup_fts()
        if self._background:
            self._writer = threading.Thread(target=self._drain, daemon=True)
            self._writer.start()

    # -- plumbing -----------------------------------------------------------

    def _connect(self):
        """One connection per thread, WAL, synchronous=NORMAL -- see store.py."""
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

    def _setup_fts(self):
        """Degrade to a plain cache if this sqlite3 was built without FTS5."""
        try:
            with self._connect() as db:
                db.executescript(FTS_SCHEMA)
        except sqlite3.DatabaseError as e:
            self.available = False
            self.reason = "sqlite3 has no FTS5 (%s)" % e
            return
        self.available = True
        self.reason = ""
        # Bodies cached while FTS5 was missing carry no index entries, and the
        # triggers only fire on new writes. Rebuilding once on the first run
        # that *has* FTS5 is the difference between a working search and one
        # that is silently blind to everything read before the upgrade.
        try:
            with self._connect() as db:
                indexed = db.execute("SELECT COUNT(*) FROM body_fts").fetchone()[0]
                stored = db.execute("SELECT COUNT(*) FROM bodies").fetchone()[0]
                if stored and not indexed:
                    db.execute("INSERT INTO body_fts(body_fts) VALUES ('rebuild')")
        except sqlite3.DatabaseError:
            pass

    def _drain(self):
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                with self._connect() as db:
                    job(db)
            except sqlite3.DatabaseError:
                pass  # losing a page's text must never take the browser with it
            finally:
                self._queue.task_done()

    def _write(self, job):
        """`job` is a callable taking a connection, not a single statement:
        storing a page touches three tables and has to be one transaction."""
        if self._background:
            self._queue.put(job)
            return
        try:
            with self._connect() as db:
                job(db)
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

    def _query(self, sql, args=()):
        try:
            return self._connect().execute(sql, args).fetchall()
        except sqlite3.DatabaseError:
            return []

    # -- writing ------------------------------------------------------------

    def record(self, url, title="", text=""):
        """Cache one page's extracted text. Returns whether it was accepted."""
        if not recordable(url) or not (text or "").strip():
            return False
        body = text[:MAX_DOC_CHARS]
        digest = content_hash(body)
        title = title or ""
        now = int(time.time())

        def job(db):
            # The body first: a second URL for the same article finds the row
            # already there and adds nothing but its own small page row.
            db.execute(
                "INSERT INTO bodies (hash, text, bytes) VALUES (?, ?, ?) "
                "ON CONFLICT(hash) DO NOTHING",
                (digest, body, len(body.encode("utf-8", "replace"))))
            db.execute(
                """INSERT INTO pages (url, title, hash, stored, last_used)
                        VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                        hash = excluded.hash,
                        stored = excluded.stored,
                        last_used = excluded.last_used,
                        title = CASE WHEN length(excluded.title) > 0
                                     THEN excluded.title ELSE pages.title END""",
                (url, title, digest, now, now))
            self._evict(db)

        self._write(job)
        return True

    def touch(self, url):
        """Mark a cached page as used, so revisiting it defers its eviction."""
        self._write(lambda db: db.execute(
            "UPDATE pages SET last_used = ? WHERE url = ?", (int(time.time()), url)))

    def forget(self, url):
        self._write(lambda db: (db.execute("DELETE FROM pages WHERE url = ?", (url,)),
                                self._drop_orphans(db)))

    def clear(self):
        self._write(lambda db: (db.execute("DELETE FROM pages"),
                                db.execute("DELETE FROM bodies")))

    # -- eviction -----------------------------------------------------------

    def _evict(self, db, cap=None):
        """Drop least-recently-used pages until the cached bytes fit the cap.

        Only bodies no page still points at are deleted: two URLs sharing an
        article keep it alive until the last of them is evicted."""
        cap = MAX_BYTES if cap is None else cap
        total = self._total(db)
        if total <= cap:
            return
        target = int(cap * TRIM_TO)
        # Refcounts first, so the loop can tell how much a deletion actually
        # frees: dropping one of three URLs for an article frees nothing, and a
        # loop that assumed otherwise would stop while still over the cap.
        refs = {row["hash"]: row["n"] for row in db.execute(
            "SELECT hash, COUNT(*) AS n FROM pages GROUP BY hash")}
        freed = 0
        for row in db.execute(
                """SELECT p.url AS url, p.hash AS hash, b.bytes AS bytes
                   FROM pages p LEFT JOIN bodies b ON b.hash = p.hash
                   ORDER BY p.last_used ASC, p.rowid ASC""").fetchall():
            if total - freed <= target:
                break
            db.execute("DELETE FROM pages WHERE url = ?", (row["url"],))
            refs[row["hash"]] = refs.get(row["hash"], 1) - 1
            if refs[row["hash"]] <= 0:
                freed += row["bytes"] or 0
        # One scan at the end rather than one per page: the orphans are only
        # unreachable once the whole batch is gone.
        self._drop_orphans(db)

    def _drop_orphans(self, db):
        db.execute("DELETE FROM bodies WHERE hash NOT IN (SELECT hash FROM pages)")

    def _total(self, db):
        row = db.execute("SELECT COALESCE(SUM(bytes), 0) AS n FROM bodies").fetchone()
        return row["n"] if row else 0

    # -- reading ------------------------------------------------------------

    def text_for(self, url):
        rows = self._query(
            "SELECT b.text AS text FROM pages p JOIN bodies b ON b.hash = p.hash "
            "WHERE p.url = ?", (url,))
        return rows[0]["text"] if rows else None

    def search(self, query, limit=10):
        """Rank cached pages against a query. Never raises on bad input.

        One hit per body, not per URL: three URLs for one article are one
        result, attributed to the most recently used of them.
        """
        if not self.available:
            return []
        expr = match_query(query)
        if not expr:
            return []
        try:
            rows = self._connect().execute(
                """SELECT p.url AS url, p.title AS title, p.last_used AS last_used,
                          b.id AS body,
                          snippet(body_fts, 0, '', '', '…', 14) AS snippet,
                          bm25(body_fts) AS score
                   FROM body_fts
                   JOIN bodies b ON b.id = body_fts.rowid
                   JOIN pages  p ON p.hash = b.hash
                   WHERE body_fts MATCH ?
                   ORDER BY score, p.last_used DESC
                   LIMIT ?""",
                (expr, max(1, int(limit)) * 8)).fetchall()
        except sqlite3.DatabaseError:
            # A malformed MATCH expression is user input, not a server fault.
            return []
        out, seen = [], set()
        for row in rows:
            # Deduped by body, not by URL: the ordering above already put the
            # most recently used URL for an article first, and the other two
            # spellings of it are the same result.
            if row["body"] in seen:
                continue
            seen.add(row["body"])
            out.append({"url": row["url"], "title": row["title"],
                        "snippet": " ".join((row["snippet"] or "").split()),
                        "score": round(-row["score"], 4)})
            if len(out) >= limit:
                break
        return out

    def stats(self):
        pages = self._query("SELECT COUNT(*) AS n FROM pages")
        bodies = self._query("SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b "
                             "FROM bodies")
        return {"pages": pages[0]["n"] if pages else 0,
                "bodies": bodies[0]["n"] if bodies else 0,
                "bytes": bodies[0]["b"] if bodies else 0,
                "search": self.available}
