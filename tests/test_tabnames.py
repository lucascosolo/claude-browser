"""Tab naming: the rule is about collisions, so the tests are too.

The bug this locks down had two halves. Every tab rendered as a bare "..."
because an ellipsizing GtkLabel with no minimum width lets GtkNotebook shrink it
to the ellipsis -- fixed in browser.py. And even at a readable width the names
were not useful, which is what this module fixes.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser.tabnames import (SUMMARY_CHARS, base_name, clamp,  # noqa: E402
                                    host_of, label_tabs, lead_extract,
                                    tab_tooltip)


class BaseName(unittest.TestCase):
    def test_a_real_title_wins(self):
        self.assertEqual(base_name("https://example.com/", "Example Domain"),
                         "Example Domain")

    def test_no_title_falls_back_to_the_host(self):
        # The RFC editor serves a text/plain-ish page with no <title>; before
        # this it showed the full URL and ellipsized to "https://...".
        self.assertEqual(base_name("https://www.rfc-editor.org/rfc/rfc2606.html"),
                         "rfc-editor.org")

    def test_a_title_that_is_just_the_url_is_not_a_title(self):
        self.assertEqual(
            base_name("https://example.com/a", "https://example.com/a"),
            "example.com")

    def test_www_is_dropped(self):
        self.assertEqual(host_of("https://www.iana.org/x"), "iana.org")

    def test_internal_pages_get_their_own_names(self):
        self.assertEqual(base_name("cb:home"), "Start")
        self.assertEqual(base_name("cb:history"), "History")
        self.assertEqual(base_name("cb:bookmarks"), "Bookmarks")

    def test_a_blank_tab_says_so(self):
        self.assertEqual(base_name("about:blank"), "New tab")
        self.assertEqual(base_name("", "", True), "Loading…")

    def test_a_malformed_url_does_not_raise(self):
        self.assertIsInstance(base_name("http://[", ""), str)


class Collisions(unittest.TestCase):
    def test_distinct_titles_are_left_alone(self):
        self.assertEqual(
            label_tabs([("https://example.com/", "Example Domain", False),
                        ("https://www.iana.org/x", "IANA", False)]),
            ["Example Domain", "IANA"])

    def test_same_title_different_hosts_is_split_by_host(self):
        # Three "GitHub" tabs are the motivating case.
        self.assertEqual(
            label_tabs([("https://github.com/a", "GitHub", False),
                        ("https://gitlab.com/a", "GitHub", False)]),
            ["GitHub · github.com", "GitHub · gitlab.com"])

    def test_same_title_same_host_is_split_by_path(self):
        self.assertEqual(
            label_tabs([("https://github.com/one/pulls", "GitHub", False),
                        ("https://github.com/two/issues", "GitHub", False)]),
            ["GitHub · pulls", "GitHub · issues"])

    def test_a_suffix_that_repeats_the_name_is_not_added(self):
        # "example.com · example.com" would be noise, not information.
        labels = label_tabs([("https://example.com/", "", False),
                             ("https://example.com/", "", False)])
        self.assertEqual(labels, ["example.com", "example.com"])

    def test_only_the_colliding_group_gets_suffixes(self):
        labels = label_tabs([("https://github.com/a", "GitHub", False),
                             ("https://gitlab.com/b", "GitHub", False),
                             ("https://example.com/", "Example Domain", False)])
        self.assertEqual(labels[2], "Example Domain")
        self.assertTrue(all("·" in l for l in labels[:2]))

    def test_one_label_per_tab_in_order(self):
        tabs = [("https://a.test/", "A", False), ("https://b.test/", "B", False),
                ("https://c.test/", "C", False)]
        self.assertEqual(label_tabs(tabs), ["A", "B", "C"])

    def test_empty_strip(self):
        self.assertEqual(label_tabs([]), [])


class LeadExtract(unittest.TestCase):
    """The standing summary a discarded tab leaves behind."""

    PROSE = ("Skip to content\nMenu\n"
             "Rate limiting protects a service from a client that would "
             "otherwise overwhelm it, and the token bucket is the usual shape.\n"
             "More paragraphs follow.")

    def test_the_first_paragraph_sized_line_wins(self):
        self.assertTrue(lead_extract(self.PROSE).startswith("Rate limiting"))

    def test_navigation_furniture_is_skipped(self):
        self.assertNotIn("Skip to content", lead_extract(self.PROSE))

    def test_an_empty_cache_yields_no_summary(self):
        # The ordinary case: a page that never finished loading, or one that
        # was never recordable, has nothing cached to summarise.
        for empty in (None, "", "   \n\n  "):
            self.assertEqual(lead_extract(empty), "")

    def test_a_page_of_only_short_lines_still_says_something(self):
        # A link list has no paragraph anywhere; its words beat an empty tooltip.
        summary = lead_extract("Docs\nAPI\nPricing\nBlog")
        self.assertEqual(summary, "Docs API Pricing Blog")

    def test_the_summary_is_clamped_to_the_limit(self):
        long = "word " * 400
        summary = lead_extract(long)
        self.assertLessEqual(len(summary), SUMMARY_CHARS)
        self.assertTrue(summary.endswith("…"))

    def test_the_limit_is_honoured_when_asked_for_explicitly(self):
        self.assertLessEqual(len(lead_extract("a b c d e f g h", limit=6)), 6)

    def test_a_short_summary_is_left_alone(self):
        self.assertEqual(clamp("A short line.", 80), "A short line.")

    def test_the_ellipsis_counts_inside_the_limit(self):
        # A caller sizing a tooltip means the whole rendered string.
        self.assertEqual(clamp("abcdefghij klmnop", 10), "abcdefghi…")

    def test_clamping_cuts_at_a_word_boundary(self):
        self.assertEqual(clamp("alpha beta gamma delta", 16), "alpha beta…")

    def test_one_very_long_token_is_cut_mid_word_rather_than_to_nothing(self):
        out = clamp("x" * 50, 10)
        self.assertEqual(out, "x" * 9 + "…")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(clamp("a\n\t b   c", 80), "a b c")


class Tooltip(unittest.TestCase):
    def test_a_live_tab_shows_only_its_url(self):
        self.assertEqual(tab_tooltip("https://example.com/"),
                         "https://example.com/")

    def test_a_discarded_tab_explains_itself(self):
        tip = tab_tooltip("https://example.com/", True, "About rate limits.")
        self.assertEqual(tip.splitlines()[0], "https://example.com/")
        self.assertIn("About rate limits.", tip)
        self.assertIn("click to reload", tip)

    def test_a_discarded_tab_with_no_summary_omits_the_blank_line(self):
        tip = tab_tooltip("https://example.com/", True, "")
        self.assertEqual(len(tip.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
