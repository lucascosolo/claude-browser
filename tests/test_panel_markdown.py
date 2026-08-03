"""The panel's markdown renderer, exercised in a real JS engine.

Claude answers in markdown. The panel used to print that source verbatim, so an
answer arrived as a wall of asterisks, hashes and fences. The renderer that
fixed it lives in the panel document, which makes it the one piece of this
repo's logic that Python cannot call -- so these tests run it under node.

Two properties are worth the trouble of a cross-language test:

  * the formatting is actually produced, for every construct a model reaches for
  * nothing a model writes can become an element. Model output is escaped before
    any markup exists, and the only tags in the result are ones the renderer
    wrote. A regression here is an injection into the browser's own chrome.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import panel_html, style  # noqa: E402

NODE = shutil.which("node")

# A DOM stub: the renderer is a pure function, but it is defined inside the
# panel's IIFE, which touches document and window on the way in.
HARNESS = """
const el = { querySelector: () => ({ set innerHTML(v) {}, set textContent(v) {} }) };
globalThis.window = { addEventListener() {}, scrollTo() {}, innerHeight: 0, scrollY: 0 };
globalThis.document = {
  getElementById: () => el, body: { scrollHeight: 0 }, createElement: () => el,
};
globalThis.requestAnimationFrame = (f) => f();
new Function(SCRIPT)();
const out = CASES.map((src) => window.__md(src));
console.log(JSON.stringify(out));
"""


def render(sources):
    """Run the panel's renderer over each source string, in node."""
    page = panel_html.page(style.palette("dark"))
    script = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    js = ("const SCRIPT = %s;\nconst CASES = %s;\n%s"
          % (json.dumps(script), json.dumps(sources), HARNESS))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
        fh.write(js)
        path = fh.name
    try:
        done = subprocess.run([NODE, path], capture_output=True, text=True, timeout=60)
    finally:
        Path(path).unlink(missing_ok=True)
    if done.returncode != 0:
        raise AssertionError("node failed: %s" % done.stderr.strip())
    return json.loads(done.stdout)


# Node costs about five seconds to start on this box, which is more than the
# rest of this suite put together. Every case is rendered in one run and looked
# up by name, so the cost is paid once for the file rather than once per test.
_RENDERED = {}


def one(source):
    if source not in _RENDERED:
        _RENDERED.update(zip(SOURCES, render(SOURCES)))
    if source not in _RENDERED:      # a case not declared in SOURCES
        _RENDERED[source] = render([source])[0]
    return _RENDERED[source]


SOURCES = [
    "Use **bold** and *italic* and `code` here.",
    "# Title",
    "## Sub",
    "- one\n- two",
    "1. one\n2. two",
    "- item\n  continued",
    "```py\nif a < b and c > d:\n    pass\n```",
    "Try:\n\n```py\nprint(1)",
    "| Tour | Rooms |\n|---|---|\n| 2763201 | 4 |",
    "> quoted",
    "one\n\n---\n\ntwo",
    "Just a sentence.",
    "<script>alert(1)</script> and <img src=x onerror=alert(1)>",
    "[docs](https://example.com)",
    "[click](javascript:alert(1))",
    '[x](https://e.com/")',
    "Use `<div>` and `**not bold**`.",
    "Tom & Jerry",
]


@unittest.skipUnless(NODE, "node is needed to run the panel's JS")
class Formatting(unittest.TestCase):
    def test_emphasis_and_inline_code(self):
        html = one("Use **bold** and *italic* and `code` here.")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)
        self.assertIn("<code>code</code>", html)
        self.assertNotIn("**", html)

    def test_headings_are_demoted_under_the_card_title(self):
        # The card already owns an h3; an answer's own "# Heading" must not
        # outrank it, or the hierarchy inverts.
        self.assertIn("<h3>Title</h3>", one("# Title"))
        self.assertIn("<h4>Sub</h4>", one("## Sub"))

    def test_lists(self):
        self.assertEqual(one("- one\n- two"), "<ul><li>one</li><li>two</li></ul>")
        self.assertEqual(one("1. one\n2. two"), "<ol><li>one</li><li>two</li></ol>")

    def test_list_item_continuation_line_joins_its_item(self):
        self.assertEqual(one("- item\n  continued"), "<ul><li>item continued</li></ul>")

    def test_fenced_code_keeps_its_contents_literal(self):
        html = one("```py\nif a < b and c > d:\n    pass\n```")
        self.assertIn("<pre><code>", html)
        self.assertIn("a &lt; b and c &gt; d", html)
        self.assertNotIn("```", html)

    def test_unterminated_fence_still_renders_while_streaming(self):
        # Mid-stream the closing fence has not arrived. Waiting for it would
        # dump the rest of the answer into a code block once it did.
        html = one("Try:\n\n```py\nprint(1)")
        self.assertIn("<pre><code>print(1)</code></pre>", html)

    def test_table(self):
        html = one("| Tour | Rooms |\n|---|---|\n| 2763201 | 4 |")
        self.assertIn("<th>Tour</th>", html)
        self.assertIn("<td>2763201</td>", html)

    def test_blockquote(self):
        # The marker is &gt; by the time the block parser sees it; matching a
        # bare > here silently rendered every quote as a paragraph.
        self.assertEqual(one("> quoted"), "<blockquote>quoted</blockquote>")

    def test_rule_and_paragraphs(self):
        self.assertEqual(one("one\n\n---\n\ntwo"), "<p>one</p><hr><p>two</p>")

    def test_plain_text_is_still_paragraphs(self):
        self.assertEqual(one("Just a sentence."), "<p>Just a sentence.</p>")


@unittest.skipUnless(NODE, "node is needed to run the panel's JS")
class Escaping(unittest.TestCase):
    def test_markup_in_model_output_cannot_become_an_element(self):
        html = one("<script>alert(1)</script> and <img src=x onerror=alert(1)>")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)

    def test_only_inert_link_schemes_become_anchors(self):
        self.assertIn('<a href="https://example.com">docs</a>',
                      one("[docs](https://example.com)"))
        html = one("[click](javascript:alert(1))")
        self.assertNotIn("<a ", html)
        self.assertIn("javascript:alert(1)", html)   # left as the source wrote it

    def test_a_quote_in_an_href_cannot_end_the_attribute(self):
        # The quote is stripped from the href, so nothing after it can be read
        # as another attribute.
        self.assertEqual(one('[x](https://e.com/")'),
                         '<p><a href="https://e.com/">x</a></p>')

    def test_markup_inside_inline_code_is_escaped_not_parsed(self):
        html = one("Use `<div>` and `**not bold**`.")
        self.assertIn("<code>&lt;div&gt;</code>", html)
        self.assertIn("<code>**not bold**</code>", html)
        self.assertNotIn("<strong>", html)

    def test_ampersand_is_escaped_once(self):
        self.assertEqual(one("Tom & Jerry"), "<p>Tom &amp; Jerry</p>")


if __name__ == "__main__":
    unittest.main()
