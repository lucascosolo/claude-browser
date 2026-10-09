"""Chunk 1 JS builders: wait_predicate, scroll_js, scoped reads, tables.

Pure string construction -- nothing here runs a page. A snippet must be one
JS expression (it is handed to evaluate_javascript), and every caller-supplied
value must pass through extract._js_str.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudebrowser import extract  # noqa: E402

NASTY = "</script> x"


def assert_escaped(tc, js):
    tc.assertNotIn("</script>", js)
    tc.assertNotIn(" ", js)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class _Compiles(unittest.TestCase):
    def assert_expression(self, snippet):
        """"(" + snippet + ")" must parse: the snippet is one expression."""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write("(" + snippet + ")")
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class TestWaitPredicate(_Compiles):
    def test_changed_since_matches_a_moved_token(self):
        js = extract.wait_predicate(changed_since="abc:4")
        self.assert_expression(js)
        self.assertIn("m='change'", js)
        self.assertIn(extract._js_str("abc:4"), js)

    def test_changed_since_alone_is_enough(self):
        extract.wait_predicate(changed_since="abc:4")
        with self.assertRaises(ValueError):
            extract.wait_predicate()

    def test_state_is_an_expression(self):
        self.assert_expression(extract.state())
        self.assert_expression(extract.STATE_SHIM.strip().rstrip(";"))

    def test_each_condition_is_a_single_expression(self):
        for kw in ({"selector": "#a"}, {"text": "done"}, {"url": "/cart"},
                   {"selector": "#a", "gone": True},
                   {"selector": "#a", "text": "x", "url": "y"}):
            self.assert_expression(extract.wait_predicate(**kw))

    def test_no_condition_is_refused(self):
        with self.assertRaises(ValueError):
            extract.wait_predicate()
        with self.assertRaises(ValueError):
            extract.wait_predicate(gone=True)

    def test_values_are_escaped(self):
        for kw in ("selector", "text", "url"):
            js = extract.wait_predicate(**{kw: NASTY})
            assert_escaped(self, js)
            self.assertIn(extract._js_str(NASTY), js, kw)
        self.assert_expression(extract.wait_predicate(selector=NASTY, text=NASTY, url=NASTY))

    def test_gone_inverts_the_selector_branch(self):
        js = extract.wait_predicate(selector="#spin", gone=True)
        self.assertIn(extract._js_str("#spin"), js)
        self.assertIn("gone", js)

    def test_ref_selector_resolves_through_cbresolve(self):
        self.assertIn("__cbResolve", extract.wait_predicate(selector="@e7"))


class TestScrollJs(_Compiles):
    def test_targets_are_single_expressions(self):
        for kw in ({"to": "top"}, {"to": "bottom"}, {"to": "#footer"},
                   {"to": "@e7"}, {"by": 500}, {"by": -300}):
            self.assert_expression(extract.scroll_js(**kw))

    def test_exactly_one_of_to_and_by(self):
        with self.assertRaises(ValueError):
            extract.scroll_js()
        with self.assertRaises(ValueError):
            extract.scroll_js(to="top", by=10)

    def test_answer_shape(self):
        js = extract.scroll_js(to="bottom")
        for key in ("x", "y", "height", "viewport", "at_bottom"):
            self.assertIn(key, js)

    def test_ref_and_selector_targets(self):
        self.assertIn("__cbResolve", extract.scroll_js(to="@e7"))
        js = extract.scroll_js(to=NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)

    def test_by_is_an_integer_in_the_snippet(self):
        self.assertIn("-300", extract.scroll_js(by=-300))


class TestScopedReads(_Compiles):
    def test_no_selector_returns_the_constants_unchanged(self):
        self.assertEqual(extract.text(None), extract.TEXT)
        self.assertEqual(extract.markdown(None), extract.MARKDOWN)
        self.assertEqual(extract.links(None), extract.LINKS)
        self.assertEqual(extract.html(None), extract.HTML)

    def test_selector_is_escaped_and_reports_no_match(self):
        for fn in (extract.text, extract.markdown, extract.links, extract.html):
            js = fn(NASTY)
            assert_escaped(self, js)
            self.assertIn(extract._js_str(NASTY), js, fn.__name__)
            self.assertIn("no match", js, fn.__name__)
            self.assert_expression(fn("#main"))

    def test_ref_selector_resolves(self):
        self.assertIn("__cbResolve", extract.text("@e7"))

    def test_find_without_selector_searches_the_body(self):
        js = extract.find("x")
        self.assertIn("document.body", js)
        self.assertNotIn("no match", js)

    def test_find_with_selector(self):
        js = extract.find("x", "#main")
        self.assertIn(extract._js_str("#main"), js)
        self.assertIn("no match", js)
        self.assert_expression(js)
        assert_escaped(self, extract.find("x", NASTY))


class TestTables(_Compiles):
    def test_shape(self):
        js = extract.tables()
        for word in ("caption", "headers", "rows"):
            self.assertIn(word, js)
        self.assert_expression(js)

    def test_limit_and_selector(self):
        js = extract.tables("#grid", limit=37)
        self.assertIn("37", js)
        self.assertIn(extract._js_str("#grid"), js)
        self.assert_expression(js)

    def test_default_limit_is_200(self):
        self.assertIn("200", extract.tables())

    def test_selector_escaped(self):
        js = extract.tables(NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)

    def test_limit_must_be_an_int(self):
        with self.assertRaises((TypeError, ValueError)):
            extract.tables(limit="1); alert(1")


class TestRect(_Compiles):
    def test_is_one_expression(self):
        self.assert_expression(extract.rect("#a"))
        self.assert_expression(extract.rect("@e3"))

    def test_reports_document_coordinates(self):
        js = extract.rect("#a")
        for needle in ("getBoundingClientRect", "scrollX", "scrollY", "no match"):
            self.assertIn(needle, js)
        self.assertIn(extract._js_str("#a"), js)

    def test_ref_uses_the_resolver(self):
        self.assertIn("__cbResolve", extract.rect("@e3"))

    def test_selector_escaped(self):
        js = extract.rect(NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)


if __name__ == "__main__":
    unittest.main()
