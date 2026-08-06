"""Per-site declutter rules.

GTK-free like the module it tests. What is worth asserting here is not the
appearance of a page -- that needs eyes -- but the three things that decide
whether the right sheet reaches the right document: which URL matches which
rule, that the injected snippet is well-formed and idempotent, and that the
rules never hide something that is also a control.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import siterules  # noqa: E402


class Hosts(unittest.TestCase):
    def test_www_is_stripped_so_one_entry_covers_the_subdomains(self):
        for url, host in (
                ("https://www.youtube.com/", "youtube.com"),
                ("https://m.youtube.com/feed", "m.youtube.com"),
                ("https://music.youtube.com/", "music.youtube.com"),
                ("https://youtube.com:443/watch?v=x", "youtube.com")):
            self.assertEqual(siterules.host_of(url), host, url)

    def test_things_with_no_host_match_nothing(self):
        for url in ("cb:home", "about:blank", "/just/a/path", "", None):
            self.assertEqual(siterules.host_of(url), "")
            self.assertIsNone(siterules.for_url(url))

    def test_a_malformed_url_is_not_an_exception(self):
        # urlsplit raises on a bad IPv6 literal rather than returning nothing.
        self.assertEqual(siterules.host_of("http://[oops/"), "")

    def test_a_suffix_match_is_a_label_boundary(self):
        # The bug a naive endswith invites: notyoutube.com is not youtube.com.
        self.assertIsNone(siterules.for_url("https://notyoutube.com/"))
        self.assertIsNone(siterules.for_url("https://youtube.com.evil.test/"))

    def test_youtube_and_the_short_domain_both_match(self):
        for url in ("https://www.youtube.com/feed/subscriptions",
                    "https://m.youtube.com/", "https://youtu.be/abc123"):
            rule = siterules.for_url(url)
            self.assertIsNotNone(rule, url)
            self.assertEqual(rule.name, "youtube")

    def test_a_site_with_no_rule_gets_none(self):
        for url in ("https://example.com/", "https://news.ycombinator.com/"):
            self.assertIsNone(siterules.for_url(url))
            self.assertIsNone(siterules.apply_css(url))
            self.assertIsNone(siterules.toggle(url))


class Switch(unittest.TestCase):
    def test_on_unless_a_word_that_means_off(self):
        for raw in ("0", "off", "false", "no", " OFF ", "No"):
            self.assertFalse(siterules.enabled(raw), raw)

    def test_a_typo_keeps_the_default(self):
        # Same rule as CB_LIGHT and CB_BLOCK: a typo must not silently remove
        # a default nobody asked to lose.
        for raw in ("", "1", "on", "yes", "banana", "offf"):
            self.assertTrue(siterules.enabled(raw), raw)


class Snippet(unittest.TestCase):
    """The injected JavaScript, checked without a browser."""

    def scripts(self):
        url = "https://www.youtube.com/"
        return siterules.apply_css(url), siterules.toggle(url)

    def test_both_snippets_are_produced_for_a_covered_site(self):
        for script in self.scripts():
            self.assertTrue(script.startswith("(function(){"))
            self.assertTrue(script.endswith("})()"))

    def test_the_css_reaches_the_snippet_as_a_string_literal(self):
        apply_css, _toggle = self.scripts()
        # The whole sheet is one JSON string, so a stray quote or newline in a
        # rule cannot end the literal and turn the rest into code.
        self.assertIn(json.dumps(siterules.RULES[0].css)[:60], apply_css)

    def test_the_style_id_is_carried_in_both(self):
        for script in self.scripts():
            self.assertIn(json.dumps(siterules.STYLE_ID), script)

    def test_apply_is_idempotent_and_toggle_is_not(self):
        apply_css, toggle = self.scripts()
        # apply returns early when the sheet is already there; toggle removes
        # it. Confusing the two is what would make an auto-applied sheet turn
        # itself off on a page's second load.
        self.assertIn("if(document.getElementById(ID))", apply_css)
        self.assertNotIn("live.remove()", apply_css)
        self.assertIn("live.remove()", toggle)

    def test_every_snippet_reports_a_json_state(self):
        for script in self.scripts():
            self.assertIn("JSON.stringify", script)
            self.assertIn("simplified", script)
            self.assertIn("rule", script)

    def test_the_sheet_is_appended_to_head_or_documentElement(self):
        # A sheet injected at COMMITTED can arrive before <head> exists.
        for script in self.scripts():
            self.assertIn("document.head||document.documentElement", script)


class Sheets(unittest.TestCase):
    """What the rules may and may not hide."""

    def test_every_rule_has_a_name_hosts_a_summary_and_css(self):
        self.assertTrue(siterules.RULES)
        for rule in siterules.RULES:
            self.assertTrue(rule.name)
            self.assertTrue(rule.hosts)
            self.assertTrue(rule.summary.strip())
            self.assertTrue(rule.css.strip())

    def test_rule_names_are_unique(self):
        names = [r.name for r in siterules.RULES]
        self.assertEqual(len(names), len(set(names)))

    def test_no_rule_hides_a_container_that_also_holds_controls(self):
        """The stated policy of the module, pinned.

        `ytd-popup-container` is where the promos live *and* where "Save to
        Watch Later" lives -- hiding it would quietly break the one action this
        browser's user opens YouTube to perform.
        """
        forbidden = ("ytd-popup-container", "tp-yt-iron-dropdown",
                     "ytd-menu-popup-renderer", "#masthead-container",
                     "ytd-searchbox", "#search-form", "tp-yt-paper-dialog")
        for rule in siterules.RULES:
            for selector in forbidden:
                self.assertNotIn(selector, rule.css,
                                 "%s hides %s" % (rule.name, selector))

    def test_the_masthead_itself_survives(self):
        # It carries search, which is how you leave the feed.
        css = siterules.RULES[0].css
        self.assertNotRegex(css, r"(^|[\s,]) ?#masthead\s*[,{]")

    def test_selectors_are_hidden_never_removed(self):
        # display:none only. A rule that used `content-visibility` or a width
        # of zero would change layout in ways a site's own script notices.
        for rule in siterules.RULES:
            self.assertIn("display:none", rule.css)

    def test_the_sheet_has_balanced_braces(self):
        for rule in siterules.RULES:
            self.assertEqual(rule.css.count("{"), rule.css.count("}"), rule.name)

    def test_every_declaration_block_ends_with_a_semicolon_or_brace(self):
        # A dropped semicolon silently kills the *next* declaration too.
        for rule in siterules.RULES:
            for block in re.findall(r"\{([^}]*)\}", rule.css):
                body = block.strip()
                if body:
                    self.assertTrue(body.endswith(";"),
                                    "%s: %r" % (rule.name, body))


if __name__ == "__main__":
    unittest.main()
