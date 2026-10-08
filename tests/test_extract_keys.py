"""Chunk 2 JS builders: parse_key, press, type_text, select, hover, submit.

Pure string construction -- nothing here runs a page. Every snippet is one JS
expression and every caller-supplied value goes through extract._js_str.
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
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write("(" + snippet + ")")
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


class TestParseKey(unittest.TestCase):
    def check(self, combo, key, code, **mods):
        got = extract.parse_key(combo)
        self.assertEqual(got["key"], key, combo)
        self.assertEqual(got["code"], code, combo)
        for m in ("ctrl", "shift", "alt", "meta"):
            self.assertIs(got[m], mods.get(m, False), "%s %s" % (combo, m))

    def test_named_keys(self):
        self.check("Enter", "Enter", "Enter")
        self.check("Escape", "Escape", "Escape")
        self.check("Tab", "Tab", "Tab")
        self.check("ArrowDown", "ArrowDown", "ArrowDown")

    def test_modifiers(self):
        self.check("Shift+Tab", "Tab", "Tab", shift=True)
        self.check("Ctrl+K", "k", "KeyK", ctrl=True)
        self.check("Meta+Enter", "Enter", "Enter", meta=True)

    def test_aliases(self):
        self.check("Control+Enter", "Enter", "Enter", ctrl=True)
        self.check("Cmd+Enter", "Enter", "Enter", meta=True)

    def test_printable_and_space(self):
        self.check("a", "a", "KeyA")
        self.check("Space", " ", "Space")
        self.check(" ", " ", "Space")

    def test_nonsense_is_refused(self):
        for bad in ("", "Ctrl+", "Foo+Bar", "Enter+Tab"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                extract.parse_key(bad)


class TestPress(_Compiles):
    def test_single_expression_with_events_and_defaults(self):
        for combo in ("Enter", "Escape", "Tab", "Shift+Tab", "Ctrl+K", "a"):
            js = extract.press(combo)
            self.assert_expression(js)
            for word in ("keydown", "keyup", "JSON.stringify"):
                self.assertIn(word, js, combo)

    def test_default_actions_present(self):
        js = extract.press("Enter")
        self.assertIn("requestSubmit", js)
        self.assertIn("blur", js)
        self.assertIn("tabIndex", js)

    def test_key_and_code_are_escaped_in(self):
        js = extract.press("Ctrl+K")
        self.assertIn(extract._js_str("k"), js)
        self.assertIn(extract._js_str("KeyK"), js)

    def test_selector_escaped_and_refs_resolve(self):
        js = extract.press("Enter", selector=NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)
        self.assert_expression(js)
        self.assertIn("__cbResolve", extract.press("Enter", selector="@e7"))

    def test_bad_combo_raises(self):
        with self.assertRaises(ValueError):
            extract.press("Enter+Tab")


class TestTypeText(_Compiles):
    def test_inserts_text(self):
        js = extract.type_text("hi")
        self.assert_expression(js)
        for word in ("insertText", "execCommand", "length", "JSON.stringify"):
            self.assertIn(word, js)

    def test_value_and_selector_are_escaped(self):
        js = extract.type_text(NASTY, selector=NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)
        self.assert_expression(js)

    def test_ref_selector(self):
        self.assertIn("__cbResolve", extract.type_text("x", selector="@e7"))


class TestSelect(_Compiles):
    def test_needs_value_or_checked(self):
        with self.assertRaises(ValueError):
            extract.select("#s")

    def test_variants_are_expressions(self):
        for kw in ({"value": "a"}, {"checked": True}, {"checked": False},
                   {"value": '["a","b"]'}):
            js = extract.select("#s", **kw)
            self.assert_expression(js)
            self.assertIn("JSON.stringify", js)

    def test_snippet_handles_the_documented_cases(self):
        js = extract.select("#s", value='["a","b"]')
        for word in ("multiple", "options", "50", "change", "input"):
            self.assertIn(word, js)
        self.assertIn("checked", extract.select("#c", checked=True))

    def test_values_escaped(self):
        js = extract.select(NASTY, value=NASTY)
        assert_escaped(self, js)
        self.assertIn(extract._js_str(NASTY), js)
        self.assert_expression(js)


class TestHover(_Compiles):
    def test_events_and_cursor(self):
        js = extract.hover("a.menu")
        self.assert_expression(js)
        for word in ("mouseover", "mouseenter", "mousemove", "pointerover",
                     "__cbCursorAt", "JSON.stringify"):
            self.assertIn(word, js)
        self.assertIn(extract._js_str("a.menu"), js)

    def test_escaped(self):
        js = extract.hover(NASTY)
        assert_escaped(self, js)
        self.assert_expression(js)


class TestSubmit(_Compiles):
    def test_default_first_form(self):
        js = extract.submit()
        self.assert_expression(js)
        for word in ("requestSubmit", "form", "url_changed", "JSON.stringify"):
            self.assertIn(word, js)

    def test_selector(self):
        js = extract.submit("#login")
        self.assert_expression(js)
        self.assertIn(extract._js_str("#login"), js)
        self.assertIn("requestSubmit", js)

    def test_escaped_and_ref(self):
        js = extract.submit(NASTY)
        assert_escaped(self, js)
        self.assert_expression(js)
        self.assertIn("__cbResolve", extract.submit("@e7"))


if __name__ == "__main__":
    unittest.main()
