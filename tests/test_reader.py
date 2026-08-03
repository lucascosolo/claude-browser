"""Reader mode's GTK-free half: the knobs, the estimate, and the snippet.

The extraction heuristic itself lives in JavaScript and needs a real DOM, so
what is asserted here is everything around it -- that a hostile option or a
hostile stylesheet cannot break out of the string it is injected into, and that
the snippet still looks like something evaluate_javascript can run.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import reader  # noqa: E402


class TestOptions(unittest.TestCase):
    def test_defaults_when_nothing_is_asked_for(self):
        self.assertEqual(reader.options(),
                         {"font_px": reader.DEFAULT_FONT_PX,
                          "width_px": reader.DEFAULT_WIDTH_PX})

    def test_values_pass_through(self):
        self.assertEqual(reader.options(18, 640), {"font_px": 18, "width_px": 640})

    def test_out_of_range_is_clamped_not_refused(self):
        self.assertEqual(reader.options(4000, 4000),
                         {"font_px": reader.FONT_RANGE[1],
                          "width_px": reader.WIDTH_RANGE[1]})
        self.assertEqual(reader.options(-3, 0),
                         {"font_px": reader.FONT_RANGE[0],
                          "width_px": reader.WIDTH_RANGE[0]})

    def test_strings_from_the_cli_are_accepted(self):
        # cbctl hands over whatever the shell typed; MCP hands over an int.
        self.assertEqual(reader.options("22", "800"), {"font_px": 22, "width_px": 800})

    def test_junk_falls_back_to_the_default(self):
        for bad in ("", "wide", None, object()):
            self.assertEqual(reader.options(bad)["font_px"], reader.DEFAULT_FONT_PX)


class TestReadingTime(unittest.TestCase):
    def test_short_article_still_reads_as_a_minute(self):
        self.assertEqual(reader.minutes(12), 1)

    def test_scales_with_the_word_count(self):
        self.assertEqual(reader.minutes(reader.WPM * 5), 5)

    def test_nothing_to_read_is_no_minutes(self):
        self.assertEqual(reader.minutes(0), 0)
        self.assertEqual(reader.minutes(None), 0)
        self.assertEqual(reader.minutes("lots"), 0)


class TestStylesheet(unittest.TestCase):
    def test_options_reach_the_css(self):
        css = reader.stylesheet(reader.options(26, 900))
        self.assertIn("font-size:26px", css)
        self.assertIn("max-width:900px", css)

    def test_no_placeholder_survives(self):
        css = reader.stylesheet()
        self.assertNotIn("$FONT", css)
        self.assertNotIn("$WIDTH", css)

    def test_rules_are_scoped_to_the_overlay(self):
        """A bare `p{...}` rule would restyle the page under the overlay, which
        is visible the moment reader mode is switched off."""
        for line in reader.stylesheet().splitlines():
            rule = line.strip()
            if not rule or rule.startswith(("@", "}", "#cb-reader-root")):
                continue
            self.assertTrue(rule.startswith(("#cb-reader-root", "background", "color",
                                             "border", "font", "margin", "padding",
                                             "overflow", "line-height", "letter-spacing",
                                             "text-", "-webkit-", "float", "max-width",
                                             "box-sizing", "z-index", "position", "top",
                                             "right", "bottom", "left", "height",
                                             "display", "border-radius", "opacity",
                                             "width")),
                            "unscoped rule: %s" % rule)


class TestSnippet(unittest.TestCase):
    def test_is_a_single_expression_returning_json(self):
        js = reader.toggle()
        self.assertTrue(js.startswith("(function(){"))
        self.assertTrue(js.rstrip().endswith("})()"))
        self.assertIn("JSON.stringify", js)

    def test_css_is_injected_as_an_escaped_literal(self):
        """The stylesheet goes through extract._js_str like every other string
        put into a snippet -- a raw newline in it would end the statement."""
        js = reader.toggle()
        self.assertNotIn("var CSS=\n", js)
        literal = js[js.index("var CSS=") + len("var CSS="):js.index(";\n")]
        self.assertEqual(json.loads(literal.replace("\\u003c", "<")
                                           .replace("\\u003e", ">")),
                         reader.stylesheet())

    def test_angle_brackets_cannot_close_a_script_block(self):
        self.assertNotIn("</", reader.toggle())

    def test_both_states_are_reported(self):
        js = reader.toggle()
        self.assertIn("reader:false", js)
        self.assertIn("reader:true", js)

    def test_the_page_dom_is_never_rewritten(self):
        """Reader mode is an overlay. Nothing in the snippet may remove a node
        that belongs to the page -- only ones it made, and ones on its clone."""
        js = reader.toggle()
        self.assertIn("cloneNode(true)", js)
        self.assertNotIn("document.body.innerHTML", js)
        self.assertNotIn("document.body.remove", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
