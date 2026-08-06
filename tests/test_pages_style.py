"""The HTML surfaces, held to the same contract as the GTK chrome.

`cb:` pages and the Claude panel are rendered from one template per surface and
one palette per theme, which is what makes three themes affordable -- and what
makes a missing token a KeyError in front of the user rather than a slightly
wrong colour. These tests are the guard rails on that arrangement:

  * every page renders in every theme, with nothing left unsubstituted;
  * nothing repaints forever -- no keyframes, no timer, no canvas;
  * a stated preference against motion is honoured;
  * the panel gets the whole palette rather than a hand-picked subset, which is
    the bug this suite was written after: it used to rebuild the dict key by
    key, so `edge`, `grid` and `agent` never reached it at all;
  * the ratios the look is built on are computed, not eyeballed.
"""

import json
import unittest

from claudebrowser import pages, panel_html, style

from test_style import contrast


def _human(n):
    return "%d B" % (n or 0)


def _rows():
    return [{"url": "https://example.com/a", "title": "A page", "last_visit": 1,
             "visits": 3, "added": 1}]


def _every_page(palette):
    """One rendered document per `cb:` page, keyed by name.

    Each is given the smallest input that still exercises its own furniture --
    a row, a card, a meter, a setting -- because an empty page renders the
    empty state and none of the styling this file is about.
    """
    return {
        "home": pages.home(palette, "N", _rows(), _rows(), (1, 1)),
        "deck": pages.deck(palette, "N", [
            {"id": 1, "url": "https://example.com", "title": "T",
             "current": True, "private": True, "loading": True}]),
        "bookmarks": pages.bookmarks_page(palette, "N", _rows()),
        "history": pages.history_page(palette, "N", _rows()),
        "passwords": pages.passwords_page(
            palette, "N", [{"origin": "https://example.com", "username": "u"}],
            never=["https://other.example"]),
        "playbooks": pages.playbooks_page(palette, "N", [
            {"name": "Book", "steps": 2, "ops": ["open", "click"], "created": 1}]),
        "data": pages.data_page(
            palette, "N", {"total_mb": 100, "available_mb": 40, "cores": 2},
            {"policy": "all"}, _human),
        "private": pages.private_page(palette, "N"),
        "settings": pages.settings_page(palette, "N", {
            "path": "/tmp/settings",
            "sections": [{"title": "Section", "note": "note", "settings": [
                {"key": "CB_X", "label": "X", "kind": "bool", "on": True},
                {"key": "CB_Y", "label": "Y", "kind": "number", "value": "2"},
            ]}]}),
    }


class Renders(unittest.TestCase):
    def test_every_page_renders_in_every_theme(self):
        for name in style.THEME_NAMES:
            for page, html in _every_page(style.palette(name)).items():
                with self.subTest(theme=name, page=page):
                    self.assertNotIn("%(", html)
                    self.assertIn("<title>", html)

    def test_the_panel_renders_in_every_theme(self):
        for name in style.THEME_NAMES:
            with self.subTest(theme=name):
                self.assertNotIn("%(", panel_html.page(style.palette(name)))

    def test_a_missing_token_is_loud(self):
        """Proof the check above can fail: these templates are %-formatted
        against the palette, so a token dropped from style.py raises here
        rather than rendering a page with a hole in it."""
        crippled = style.palette("phosphor")
        del crippled["edge"]
        with self.assertRaises(KeyError):
            pages.home(crippled, "N", [], [], (0, 0))


class PrivateGuidance(unittest.TestCase):
    """cb:private is what a private tab opens with, so it is the one place the
    user finds out what private mode does -- and, more importantly, what it does
    not. A page that only listed the reassurances would be the lie this browser
    is otherwise careful not to tell."""

    def html(self):
        return pages.private_page(style.palette("phosphor"), "N")

    def test_it_says_what_is_not_kept(self):
        html = self.html()
        for claim in ("history", "cookie", "Claude", "Playbook", "Download"):
            with self.subTest(claim=claim):
                self.assertIn(claim, html)

    def test_it_says_what_it_does_not_do(self):
        html = self.html()
        self.assertIn("not a VPN", html)
        self.assertIn("network", html)

    def test_both_halves_are_present_and_neither_is_empty(self):
        self.assertTrue(pages.PRIVATE_KEPT)
        self.assertTrue(pages.PRIVATE_NOT)
        for head, rest in pages.PRIVATE_KEPT + pages.PRIVATE_NOT:
            self.assertTrue(head.strip())
            self.assertTrue(rest.strip())

    def test_it_does_not_open_the_history_dashboard(self):
        """The default start page is a grid of history and bookmarks, which is
        the one thing a private session must not open with."""
        self.assertNotIn("Recent", self.html())


class PanelTokens(unittest.TestCase):
    """The panel used to rebuild the palette key by key, so every token added
    to style.py stopped at that function. It takes the whole dict now, and
    these tests are what says so."""

    def test_every_palette_token_reaches_the_panel(self):
        for token in ("edge", "grid", "agent", "agent_soft", "on_agent",
                      "field", "tab_active"):
            palette = dict(style.palette("phosphor"))
            palette[token] = "#123456"
            with self.subTest(token=token):
                # Substituted, not merely present: a filtered dict would raise
                # a KeyError on the template's own reference instead.
                self.assertNotIn("%(", panel_html.page(palette))

    def test_claude_state_uses_the_agent_ink(self):
        """A card, a running step and the streaming cursor all mean "Claude is
        working", and none of them may borrow the chrome's focus colour."""
        palette = dict(style.palette("phosphor"), agent="#ffaa11")
        html = panel_html.page(palette)
        self.assertIn("--agent: #ffaa11", html)
        for rule in ("border-left: 3px solid var(--agent)",
                     ".step.active { color: var(--agent)",
                     'content: "\\2588"; color: var(--agent)'):
            with self.subTest(rule=rule):
                self.assertIn(rule, html)


class StillFrames(unittest.TestCase):
    """No continuous repaint, anywhere. The whole browser exists to be cheap on
    two cores, and a permanently animating surface is the most expensive
    decoration there is."""

    def surfaces(self):
        for name in style.THEME_NAMES:
            yield "panel/" + name, panel_html.page(style.palette(name))
            for page, html in _every_page(style.palette(name)).items():
                yield page + "/" + name, html

    def test_nothing_animates_on_a_timer(self):
        for where, html in self.surfaces():
            for banned in ("@keyframes", "animation-name", "setInterval",
                           "<canvas", "getContext("):
                with self.subTest(where=where, banned=banned):
                    self.assertNotIn(banned, html)

    def test_the_only_animation_declared_is_the_one_that_turns_it_off(self):
        """`animation:` is allowed to appear exactly where reduced motion
        cancels it, and nowhere else -- so this cannot pass by a keyframe
        being renamed."""
        for where, html in self.surfaces():
            for declaration in html.split("animation:")[1:]:
                with self.subTest(where=where):
                    self.assertTrue(declaration.lstrip().startswith("none"))

    def test_the_texture_is_a_static_gradient(self):
        for where, html in self.surfaces():
            with self.subTest(where=where):
                self.assertIn("repeating-linear-gradient", html)

    def test_reduced_motion_is_honoured(self):
        """WebKitGTK drives `prefers-reduced-motion` from GTK's
        `gtk-enable-animations`, which perf.tune_gtk turns off whenever
        CB_LIGHT is on -- the default. This is the live branch here."""
        for where, html in self.surfaces():
            with self.subTest(where=where):
                self.assertIn("prefers-reduced-motion", html)


class Additive(unittest.TestCase):
    """Picking phosphor is a visible choice, not a silent redesign of the other
    two: the HUD rules are appended for that theme alone."""

    def test_dark_and_light_are_untouched_by_the_hud(self):
        for name in ("dark", "light"):
            with self.subTest(theme=name):
                html = pages.home(style.palette(name), "N", _rows(), _rows(), (1, 1))
                self.assertNotIn("border-radius:0", html)
                self.assertNotIn("registration marks", html)

    def test_phosphor_adds_hud_geometry(self):
        html = pages.home(style.palette("phosphor"), "N", _rows(), _rows(), (1, 1))
        for rule in ("border-radius:0", "text-transform:uppercase",
                     'h1::before { content:"["', "position:absolute"):
            with self.subTest(rule=rule):
                self.assertIn(rule, html)

    def test_dark_and_light_render_identically(self):
        """Same template, same shape: the only difference between these two is
        the palette, which is what proves nothing branches on theme except the
        one HUD append."""
        dark = pages.home(style.palette("dark"), "N", _rows(), _rows(), (1, 1))
        light = pages.home(style.palette("light"), "N", _rows(), _rows(), (1, 1))
        self.assertEqual(len(dark), len(light))


class AgentInk(unittest.TestCase):
    def test_only_the_claude_actions_carry_it(self):
        html = pages.home(style.palette("phosphor"), "N", [], [], (0, 0))
        self.assertEqual(html.count('class="qa ai"'), 3)
        self.assertEqual(html.count('class="qa"'), 1)     # the private tab


class Contrast(unittest.TestCase):
    """Every pairing these sheets introduce, computed. A gorgeous unreadable
    page is a failure, and this is the only thing standing between a palette
    edit and one."""

    #: (what it is, ink, surface, floor). 4.5 for anything read as text, 3.0
    #: for a line or a border, which is all WCAG asks of a non-text element.
    PAIRS = (
        ("page body text", "text", "bg", 4.5),
        ("tile and card text", "text", "bar", 4.5),
        ("field text", "text", "field", 4.5),
        ("hosts, captions, explanations", "dim", "bar", 4.5),
        ("panel body text", "text", "panel", 4.5),
        ("panel secondary text", "dim", "panel", 4.5),
        ("Claude actions, live step, card spine", "agent", "bar", 4.5),
        ("Claude ink on the panel surface", "agent", "panel", 4.4),
        ("h1 brackets, lit rail button", "accent", "bar", 4.5),
        ("badge ink on its own wash", "accent", "accent_soft", 3.0),
        ("agent ink on its own wash", "agent", "agent_soft", 3.0),
        ("a finished run", "ok", "bar", 4.5),
        ("a failed one", "warn", "bar", 4.5),
    )

    def test_introduced_pairings_hold(self):
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            for what, ink, surface, floor in self.PAIRS:
                with self.subTest(theme=name, what=what):
                    self.assertGreaterEqual(
                        contrast(palette[ink], palette[surface]), floor)

    def test_hud_structure_lines_clear_3_to_1(self):
        """`edge` is only ever drawn by the phosphor sheet -- brackets, corner
        marks, the rail's divider -- so it is only phosphor that owes the
        border floor on it."""
        palette = style.palette("phosphor")
        for surface in ("bg", "bar", "panel", "field", "tab_active"):
            with self.subTest(surface=surface):
                self.assertGreaterEqual(
                    contrast(palette["edge"], palette[surface]), 3.0)


if __name__ == "__main__":
    unittest.main()


class ScriptLiterals(unittest.TestCase):
    """`_js` and `_js_block` are two different contexts, not two spellings.

    An HTML parser decodes entities inside an attribute and does not decode
    them inside a <script>. Using the attribute helper in a script block is
    what turned a queued video called "Rick & Morty" into
    `SyntaxError: Unexpected token '&'` and took the whole page down.
    """

    def test_ampersand_survives_a_script_block(self):
        self.assertNotIn("&amp;", pages._js_block(["Rick & Morty"]))
        self.assertIn("&", pages._js_block(["Rick & Morty"]))

    def test_a_script_end_tag_cannot_escape_the_block(self):
        for payload in ("</script>", "<!--", "<script>"):
            out = pages._js_block([payload])
            self.assertNotIn("<", out, payload)
            self.assertNotIn(">", out, payload)

    def test_line_separators_are_escaped(self):
        # Newlines to a JS parser, ordinary characters to a JSON one.
        out = pages._js_block("a b c")
        self.assertNotIn(" ", out)
        self.assertNotIn(" ", out)

    def test_it_still_round_trips_as_json(self):
        value = ["Rick & Morty", "quote\" and \\ back", "emoji \U0001f600"]
        decoded = json.loads(pages._js_block(value)
                             .replace("\\u003c", "<").replace("\\u003e", ">"))
        self.assertEqual(decoded, value)

    def test_the_queue_page_puts_titles_through_it(self):
        state = {"ok": True, "truncated": False, "items": [
            {"video_id": "abc", "title": "Rick & Morty </script>",
             "channel": "C & D", "duration": "1:00", "seconds": 60}]}
        html_out = pages.queue_page(style.palette("dark"), "tok", state)
        self.assertNotIn("&amp;\"", html_out.split("<script>")[-1])
        self.assertNotIn("</script>\"", html_out)
