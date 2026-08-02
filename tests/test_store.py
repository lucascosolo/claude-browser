"""History, bookmarks, and the pages built on them.

No display and no GTK bindings needed: store.py and pages.py are deliberately
free of both so the rules they encode can be tested directly.
"""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import pages, store, style  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        # background=False: an in-memory database cannot be shared across
        # threads, so each connection would otherwise get its own empty copy.
        self.s = store.Store(":memory:", background=False)

    def test_visits_accumulate_on_one_row(self):
        for _ in range(3):
            self.s.record("https://a.example/x", "A")
        rows = self.s.history()
        self.assertEqual(len(rows), 1, "history dedupes by URL")
        self.assertEqual(rows[0]["visits"], 3)

    def test_internal_and_blank_urls_are_never_recorded(self):
        for url in ("cb:home", "about:blank", "data:text/html,x", "", "javascript:1"):
            self.assertFalse(store.recordable(url), url)
            self.s.record(url, "nope")
        self.assertEqual(self.s.history(), [])

    def test_a_later_empty_title_does_not_erase_a_good_one(self):
        """WebKit reports the title after the load, and sometimes reports an
        empty one on the way. Losing the title would break search."""
        self.s.record("https://a.example/x", "Real Title")
        self.s.record("https://a.example/x", "")
        self.assertEqual(self.s.history()[0]["title"], "Real Title")

    def test_retitle_does_not_count_a_visit(self):
        """notify::title fires several times per load; counting each would make
        one page look like five visits and poison every ranking."""
        self.s.record("https://a.example/x", "")
        for _ in range(4):
            self.s.retitle("https://a.example/x", "Settled Title")
        row = self.s.history()[0]
        self.assertEqual(row["visits"], 1)
        self.assertEqual(row["title"], "Settled Title")

    def test_bookmarks_outrank_history_in_suggestions(self):
        for _ in range(50):
            self.s.record("https://example.com/popular", "Popular")
        self.s.bookmark("https://example.com/saved", "Saved")
        top = self.s.suggest("example")[0]
        self.assertEqual(top["url"], "https://example.com/saved")
        self.assertTrue(top["bookmark"])

    def test_host_prefix_match_wins_over_a_buried_one(self):
        """Typing 'git' should offer github.com before a page with 'git' in the
        middle of its title."""
        self.s.record("https://github.com/", "GitHub")
        for _ in range(9):
            self.s.record("https://example.com/legit-page", "A legit page")
        self.assertEqual(self.s.suggest("git")[0]["url"], "https://github.com/")

    def test_suggest_is_empty_for_empty_input(self):
        self.s.record("https://a.example/x", "A")
        self.assertEqual(self.s.suggest(""), [])
        self.assertEqual(self.s.suggest("   "), [])

    def test_toggle_bookmark_reports_the_new_state(self):
        url = "https://a.example/x"
        self.assertTrue(self.s.toggle_bookmark(url, "A"))
        self.assertTrue(self.s.is_bookmarked(url))
        self.assertFalse(self.s.toggle_bookmark(url, "A"))
        self.assertFalse(self.s.is_bookmarked(url))

    def test_bookmarking_an_internal_page_is_refused(self):
        self.assertFalse(self.s.toggle_bookmark("cb:home", "Home"))
        self.assertEqual(self.s.bookmarks(), [])

    def test_forget_removes_history_but_keeps_the_bookmark(self):
        url = "https://a.example/x"
        self.s.record(url, "A")
        self.s.bookmark(url, "A")
        self.s.forget(url)
        self.assertEqual(self.s.history(), [])
        self.assertTrue(self.s.is_bookmarked(url))

    def test_clear_history_leaves_bookmarks_alone(self):
        self.s.record("https://a.example/x", "A")
        self.s.bookmark("https://b.example/y", "B")
        self.s.clear_history()
        self.assertEqual(self.s.history(), [])
        self.assertEqual(len(self.s.bookmarks()), 1)

    def test_prune_keeps_the_most_recent(self):
        for i in range(12):
            self.s.record("https://a.example/%d" % i, "A%d" % i)
        self.s.prune(keep=5)
        rows = self.s.history()
        self.assertEqual(len(rows), 5)
        self.assertIn("/11", rows[0]["url"])

    def test_search_matches_title_and_url(self):
        self.s.record("https://a.example/deep/path", "Nothing relevant")
        self.s.record("https://b.example/", "Findable Title")
        self.assertEqual(len(self.s.history("deep")), 1)
        self.assertEqual(len(self.s.history("Findable")), 1)
        self.assertEqual(len(self.s.history("nomatch")), 0)

    def test_top_sites_shows_each_host_once(self):
        for i in range(5):
            self.s.record("https://same.example/page%d" % i, "P")
        self.s.record("https://other.example/", "O")
        hosts = [store.host_of(r["url"]) for r in self.s.top_sites()]
        self.assertEqual(len(hosts), len(set(hosts)))


class PageRenderTest(unittest.TestCase):
    def setUp(self):
        self.palette = style.palette(True)
        self.nonce = "test-nonce-value"

    def rows(self, url="https://a.example/x", title="A"):
        now = int(time.time())
        return [{"url": url, "title": title, "visits": 2,
                 "last_visit": now, "added": now}]

    def test_every_page_carries_the_nonce(self):
        """Without it the page's buttons silently do nothing."""
        for html in (
            pages.home(self.palette, self.nonce, self.rows(), self.rows(), (1, 1)),
            pages.history_page(self.palette, self.nonce, self.rows()),
            pages.bookmarks_page(self.palette, self.nonce, self.rows()),
            pages.deck(self.palette, self.nonce, [{"id": 1, "url": "https://a.example",
                                                   "title": "A"}]),
        ):
            self.assertIn(self.nonce, html)

    def test_titles_are_escaped(self):
        """A page title is attacker-controlled text going into our own document,
        which is the one place an injection would run with the nonce in scope."""
        hostile = '<img src=x onerror="alert(1)">'
        html = pages.history_page(self.palette, self.nonce,
                                  self.rows(title=hostile))
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_hostile_text_cannot_break_out_of_an_html_attribute(self):
        """These literals live inside onclick="...". json.dumps alone emits a
        bare double quote, which closes the attribute early and turns the rest
        of the URL into markup."""
        for hostile in ('https://a.example/"+alert(1)+"',
                        "https://a.example/' onmouseover='alert(1)",
                        '</a><script>alert(1)</script>'):
            literal = pages._js(hostile)
            self.assertNotIn('"', literal, "a bare quote would end the attribute")
            self.assertNotIn("'", literal)
            self.assertNotIn("<", literal)

    def test_hostile_url_stays_inside_its_attribute_when_rendered(self):
        url = 'https://a.example/"+alert(1)+"'
        html = pages.bookmarks_page(self.palette, self.nonce, self.rows(url=url))
        onclick = html.split('onclick="', 1)[1].split('"', 1)[0]
        self.assertIn("cbui.star", onclick, "the handler must survive intact")
        self.assertIn("&quot;", onclick, "the quotes must be entity-escaped")

    def test_empty_states_explain_what_to_do(self):
        html = pages.bookmarks_page(self.palette, self.nonce, [])
        self.assertIn("No bookmarks", html)
        self.assertIn("Ctrl", html, "should say how to make one")

    def test_site_marks_are_stable_and_host_derived(self):
        self.assertEqual(pages._mark("https://github.com/a"),
                         pages._mark("https://github.com/b/c?d=e"))
        self.assertNotEqual(pages._mark("https://github.com/"),
                            pages._mark("https://gitlab.com/"))
        self.assertEqual(pages._mark("https://www.example.com/")[0], "E")

    def test_yesterday_is_correct_across_a_year_boundary(self):
        """The regression this replaced: comparing tm_yday means December 31st
        is "not yesterday" when today is January 1st."""
        import datetime
        newyear = datetime.date(2026, 1, 1)
        dec31 = datetime.datetime(2025, 12, 31, 15, 0).timestamp()
        self.assertEqual(pages._day(dec31, today=newyear), "Yesterday")
        self.assertEqual(
            pages._day(datetime.datetime(2026, 1, 1, 9, 0).timestamp(), today=newyear),
            "Today")
        self.assertEqual(
            pages._day(datetime.datetime(2025, 12, 20, 9, 0).timestamp(), today=newyear),
            "Saturday, December 20")

    def test_relative_times_read_naturally(self):
        now = int(time.time())
        self.assertEqual(pages._ago(now), "just now")
        self.assertEqual(pages._ago(now - 60), "1 min ago")
        self.assertEqual(pages._ago(now - 7200), "2 hrs ago")
        self.assertEqual(pages._ago(now - 86400 * 2), "2 days ago")

    def test_deck_marks_the_current_tab(self):
        html = pages.deck(self.palette, self.nonce, [
            {"id": 1, "url": "https://a.example", "title": "A", "current": True},
            {"id": 2, "url": "https://b.example", "title": "B"},
        ])
        self.assertIn("card cur", html)
        self.assertEqual(html.count('cbui.send({action:\'switch\''), 2)


if __name__ == "__main__":
    unittest.main()
