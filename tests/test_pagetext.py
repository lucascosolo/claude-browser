"""The on-disk page-text cache and the search built on it.

pagetext.py is GTK-free for the same reason store.py is: everything worth
asserting about dedupe, eviction and ranking can be asserted without a display.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import pagetext  # noqa: E402


def store(**kw):
    # background=False: an in-memory database cannot be shared across threads,
    # so a writer thread would get its own empty copy.
    return pagetext.PageText(":memory:", background=False, **kw)


class DedupeTest(unittest.TestCase):
    def setUp(self):
        self.p = store()

    def test_same_text_at_two_urls_is_stored_once(self):
        text = "The quick brown fox jumps over the lazy dog. " * 20
        self.p.record("https://a.example/article", "A", text)
        self.p.record("https://a.example/article?utm_source=x", "A", text)
        self.p.record("https://amp.a.example/article", "A", text)
        stats = self.p.stats()
        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["bodies"], 1, "one body per content hash")

    def test_different_text_gets_its_own_body(self):
        self.p.record("https://a.example/1", "One", "alpha beta")
        self.p.record("https://a.example/2", "Two", "gamma delta")
        self.assertEqual(self.p.stats()["bodies"], 2)

    def test_a_shared_body_survives_until_the_last_url_goes(self):
        text = "shared prose about wombats"
        self.p.record("https://a.example/x", "X", text)
        self.p.record("https://b.example/x", "X", text)
        self.p.forget("https://a.example/x")
        self.assertEqual(self.p.text_for("https://b.example/x"), text)
        self.p.forget("https://b.example/x")
        self.assertEqual(self.p.stats()["bodies"], 0)

    def test_clear_empties_the_cache_and_its_search_index(self):
        """`clear()` is what cb:data and `cbctl clear pagetext` call. Leaving
        the FTS rows behind would mean recall still returning hits for prose
        the user just erased."""
        self.p.record("https://a.example/x", "X", "wombats and their burrows")
        self.p.record("https://b.example/y", "Y", "quokkas of Rottnest")
        self.p.clear()
        self.assertEqual(self.p.stats()["pages"], 0)
        self.assertEqual(self.p.stats()["bodies"], 0)
        self.assertIsNone(self.p.text_for("https://a.example/x"))
        if self.p.available:
            self.assertEqual(self.p.search("wombats"), [])

    def test_internal_and_empty_pages_are_never_cached(self):
        for url in ("cb:home", "about:blank", "data:text/html,x", ""):
            self.assertFalse(self.p.record(url, "nope", "text"), url)
        self.assertFalse(self.p.record("https://a.example/x", "empty", "   "))
        self.assertEqual(self.p.stats()["pages"], 0)

    def test_a_giant_document_is_truncated_rather_than_refused(self):
        self.p.record("https://a.example/spec", "Spec", "x" * (pagetext.MAX_DOC_CHARS * 2))
        self.assertEqual(len(self.p.text_for("https://a.example/spec")),
                         pagetext.MAX_DOC_CHARS)


class EvictionTest(unittest.TestCase):
    def setUp(self):
        self.p = store()
        self._cap = pagetext.MAX_BYTES

    def tearDown(self):
        pagetext.MAX_BYTES = self._cap

    def fill(self, count, stamp=1000):
        """Cache `count` pages of ~900 bytes each, under a cap high enough that
        nothing is evicted on the way in -- these tests want to control when."""
        pagetext.MAX_BYTES = self._cap
        for i in range(count):
            self.p.record("https://a.example/%d" % i, "Page %d" % i,
                          "body %d %s" % (i, "z" * 900))
            # Explicit clock: real timestamps have one-second resolution, and a
            # test that writes ten pages in one second has no LRU order at all.
            self.p._connect().execute(
                "UPDATE pages SET last_used = ? WHERE url = ?",
                (stamp + i, "https://a.example/%d" % i))

    def test_the_cache_stays_under_its_byte_cap(self):
        pagetext.MAX_BYTES = 4000  # a handful of pages overflows this
        for i in range(20):
            self.p.record("https://a.example/%d" % i, "Page %d" % i,
                          "body %d %s" % (i, "z" * 900))
            self.assertLessEqual(self.p.stats()["bytes"], 4000)
        self.assertLess(self.p.stats()["pages"], 20)
        self.assertGreater(self.p.stats()["pages"], 0, "the cap is not a wipe")

    def test_eviction_takes_the_least_recently_used_first(self):
        self.fill(10)
        # Page 0 is the oldest by the clock above; using it again should save it
        # and leave page 1 as the next in line.
        self.p.touch("https://a.example/0")
        with self.p._connect() as db:
            self.p._evict(db, cap=4000)
        self.assertIsNotNone(self.p.text_for("https://a.example/0"))
        self.assertIsNone(self.p.text_for("https://a.example/1"))
        self.assertIsNotNone(self.p.text_for("https://a.example/9"))

    def test_eviction_stops_once_it_is_under_the_cap(self):
        self.fill(10)
        before = self.p.stats()["bytes"]
        with self.p._connect() as db:
            self.p._evict(db, cap=int(before * 0.9))
        after = self.p.stats()
        self.assertLessEqual(after["bytes"], before * 0.9)
        self.assertGreater(after["pages"], 5, "a small overflow is not a purge")


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.p = store()
        if not self.p.available:
            self.skipTest("this sqlite3 has no FTS5")
        self.p.record("https://a.example/otters", "Otters",
                      "Otters are semiaquatic mammals. Otters eat fish. "
                      "The otter is playful.")
        self.p.record("https://a.example/badgers", "Badgers",
                      "Badgers dig setts. A badger once met an otter.")
        self.p.record("https://a.example/rocks", "Rocks", "Granite and basalt.")

    def test_finds_the_page_and_says_where(self):
        hits = self.p.search("otters")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["url"], "https://a.example/otters")
        self.assertEqual(hits[0]["title"], "Otters")
        self.assertIn("tter", hits[0]["snippet"].lower())

    def test_density_ranks_the_better_page_first(self):
        urls = [h["url"] for h in self.p.search("otter")]
        self.assertEqual(urls[0], "https://a.example/otters")
        self.assertIn("https://a.example/badgers", urls)

    def test_unrelated_pages_do_not_match(self):
        self.assertEqual([h["url"] for h in self.p.search("granite")],
                         ["https://a.example/rocks"])
        self.assertEqual(self.p.search("marsupial"), [])

    def test_two_words_are_anded(self):
        self.assertEqual(self.p.search("badger otter")[0]["url"],
                         "https://a.example/badgers")
        self.assertEqual(self.p.search("badger granite"), [])

    def test_a_deduped_article_is_one_result(self):
        text = "Capybaras are large rodents."
        self.p.record("https://c.example/capy", "Capy", text)
        self.p.record("https://c.example/capy?ref=twitter", "Capy", text)
        self.assertEqual(len(self.p.search("capybaras")), 1)

    def test_hostile_input_returns_nothing_instead_of_raising(self):
        """A query is untrusted text. Anything that looks like FTS5 syntax has
        to come back as no results, never as an exception."""
        for query in ('"', 'otter"', 'NEAR(', '(a OR', 'a*"b', '""', 'otter AND',
                      "'; DROP TABLE pages; --", "^", "*", ""):
            self.assertIsInstance(self.p.search(query), list, query)
        # The store is intact afterwards.
        self.assertTrue(self.p.search("otters"))

    def test_snippets_are_plain_text_unless_highlighting_is_asked_for(self):
        """The CLI and MCP answers are read as text; markers in them would be
        noise on every line."""
        plain = self.p.search("otters")[0]["snippet"]
        self.assertNotIn(pagetext.HL_OPEN, plain)
        self.assertNotIn(pagetext.HL_CLOSE, plain)

    def test_highlighting_wraps_the_matched_term(self):
        snippet = self.p.search("otters", highlight=True)[0]["snippet"]
        self.assertIn(pagetext.HL_OPEN, snippet)
        self.assertIn(pagetext.HL_CLOSE, snippet)
        marked = snippet.split(pagetext.HL_OPEN)[1].split(pagetext.HL_CLOSE)[0]
        self.assertIn("tter", marked.lower())

    def test_a_page_cannot_forge_a_highlight_marker(self):
        """The delimiters are what tells pages.py which run of characters is a
        real match. A body that contains them would be rendering its own."""
        self.p.record("https://a.example/forge", "Forge",
                      "wombats %spwned%s wombats" % (pagetext.HL_OPEN,
                                                     pagetext.HL_CLOSE))
        hit = self.p.search("wombats", highlight=True)[0]
        self.assertNotIn(pagetext.HL_OPEN + "pwned", hit["snippet"])
        self.assertIn("pwned", hit["snippet"], "the words themselves stay")

    def test_a_forgotten_page_stops_matching(self):
        self.p.forget("https://a.example/rocks")
        self.assertEqual(self.p.search("granite"), [])


class MatchQueryTest(unittest.TestCase):
    def test_every_token_is_quoted_and_the_last_is_a_prefix(self):
        self.assertEqual(pagetext.match_query("foo bar"), '"foo" AND "bar"*')

    def test_punctuation_and_syntax_are_stripped_not_escaped(self):
        self.assertEqual(pagetext.match_query('"foo" OR (bar'), '"foo" AND "OR" AND "bar"*')
        self.assertEqual(pagetext.match_query("!!! ??"), "")


class NoFts5Test(unittest.TestCase):
    """The browser must still start, and still cache text, on an sqlite3 built
    without FTS5. Simulated by pointing the schema at a module that cannot
    exist, which produces the same 'no such module' failure."""

    def setUp(self):
        self._schema = pagetext.FTS_SCHEMA
        pagetext.FTS_SCHEMA = (
            "CREATE VIRTUAL TABLE IF NOT EXISTS body_fts "
            "USING fts_that_does_not_exist(text);")
        self.p = store()

    def tearDown(self):
        pagetext.FTS_SCHEMA = self._schema

    def test_the_store_still_opens_and_says_why(self):
        self.assertFalse(self.p.available)
        self.assertIn("FTS5", self.p.reason)

    def test_caching_and_reading_text_still_work(self):
        self.p.record("https://a.example/x", "X", "otters and badgers")
        self.assertEqual(self.p.text_for("https://a.example/x"), "otters and badgers")

    def test_search_is_empty_rather_than_broken(self):
        self.p.record("https://a.example/x", "X", "otters and badgers")
        self.assertEqual(self.p.search("otters"), [])

    def test_eviction_still_runs(self):
        cap = pagetext.MAX_BYTES
        pagetext.MAX_BYTES = 2000
        try:
            for i in range(12):
                self.p.record("https://a.example/%d" % i, "P", "%d %s" % (i, "z" * 500))
            self.assertLessEqual(self.p.stats()["bytes"], 2000)
        finally:
            pagetext.MAX_BYTES = cap


class BackgroundWriterTest(unittest.TestCase):
    """The on-disk path, where writes really do go to another thread."""

    def test_flush_makes_a_queued_write_visible(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = pagetext.PageText(Path(tmp) / "pagetext.db")
            try:
                p.record("https://a.example/x", "X", "wombats are stout")
                p.flush()
                self.assertEqual(p.text_for("https://a.example/x"), "wombats are stout")
                if p.available:
                    self.assertEqual(p.search("wombats")[0]["url"], "https://a.example/x")
            finally:
                p.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
