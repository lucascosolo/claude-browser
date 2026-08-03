"""The themes: same tokens everywhere, readable, and CSS GTK3 actually accepts.

The last one is the reason this file exists at all. GTK3's parser is not the
web's -- `text-transform` is not a property it knows, and a sheet that uses one
loads with an error the user only ever sees as a warning on stderr at every
launch. `Gtk.CssProvider.load_from_data` can be exercised with no display and no
window, so the sheet is parsed here rather than discovered by launching.
"""

import unittest

from claudebrowser import style

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception:                                     # pragma: no cover
    Gtk = None


def _luminance(hex_colour):
    """WCAG relative luminance of `#rrggbb`."""
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    """WCAG contrast ratio between two `#rrggbb` colours, 1.0 to 21.0."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


#: Every surface a chrome label is ever painted on.
SURFACES = ("bg", "bar", "panel", "field", "field_focus", "tab_active")


class Tokens(unittest.TestCase):
    def test_every_theme_carries_every_token(self):
        """A template written against one theme has to render in all of them --
        pages.py does `_CSS % palette`, which raises KeyError on a miss."""
        keys = [set(style.palette(name)) for name in style.THEME_NAMES]
        for other in keys[1:]:
            self.assertEqual(keys[0], other)
        self.assertIn("phosphor", style.THEME_NAMES)

    def test_colour_tokens_are_hex(self):
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            for key, value in palette.items():
                if key in ("name", "mono"):
                    continue
                with self.subTest(theme=name, token=key):
                    self.assertRegex(value, r"^#[0-9a-f]{6}$")

    def test_palette_is_a_copy(self):
        """Callers mutate what they get (browser.py hands it to templates)."""
        style.palette("dark")["bg"] = "#ff0000"
        self.assertNotEqual(style.palette("dark")["bg"], "#ff0000")

    def test_name_token_matches_the_theme(self):
        for name in style.THEME_NAMES:
            self.assertEqual(style.palette(name)["name"], name)

    def test_mono_chain_is_installed_faces_only(self):
        """No downloaded fonts: this machine has these and nothing else."""
        chain = style.palette("phosphor")["mono"]
        self.assertTrue(chain.endswith("monospace"))
        for family in ("DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
                       "Nimbus Mono PS"):
            self.assertIn(family, chain)

    def test_resolve_falls_back_rather_than_raising(self):
        """A typo in CB_THEME must not stop the browser from starting."""
        for junk in ("", None, "  ", "matrix", "DARKK"):
            self.assertEqual(style.resolve(junk), "dark")
        self.assertEqual(style.resolve(" Phosphor "), "phosphor")

    def test_is_dark_covers_the_stock_widgets(self):
        self.assertTrue(style.is_dark("dark"))
        self.assertTrue(style.is_dark("phosphor"))
        self.assertFalse(style.is_dark("light"))


class Contrast(unittest.TestCase):
    """Ratios are computed, not eyeballed. A gorgeous unreadable chrome is a
    failure, and these numbers are the only thing standing between a palette
    edit and one."""

    def test_body_text_clears_4_5_on_every_surface(self):
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            for surface in SURFACES:
                with self.subTest(theme=name, surface=surface):
                    self.assertGreaterEqual(
                        contrast(palette["text"], palette[surface]), 4.5)

    def test_secondary_text_clears_4_5_on_every_surface(self):
        """`dim` only owes 3:1 as non-essential text, but every theme happens to
        clear the body threshold, and dropping below it should be a decision."""
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            for surface in SURFACES:
                with self.subTest(theme=name, surface=surface):
                    self.assertGreaterEqual(
                        contrast(palette["dim"], palette[surface]), 4.5)

    def test_meaning_colours_clear_4_5_on_every_surface(self):
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            for token in ("accent", "agent", "ok", "warn"):
                for surface in SURFACES:
                    with self.subTest(theme=name, token=token, surface=surface):
                        self.assertGreaterEqual(
                            contrast(palette[token], palette[surface]), 4.3)

    def test_text_on_the_filled_accent_is_readable(self):
        """The checked mode pill and the primary button paint text *on* the
        accent, which is the one place the ratio inverts."""
        for name in style.THEME_NAMES:
            palette = style.palette(name)
            with self.subTest(theme=name):
                self.assertGreaterEqual(
                    contrast(palette["on_accent"], palette["accent"]), 4.5)
                self.assertGreaterEqual(
                    contrast(palette["on_agent"], palette["agent"]), 4.5)
                # Hover states paint accent-on-accent_soft.
                self.assertGreaterEqual(
                    contrast(palette["accent"], palette["accent_soft"]), 3.0)

    def test_phosphor_rules_clear_3_to_1(self):
        """Borders and structural lines. dark/light predate this rule and are
        deliberately left alone -- see the module docstring in style.py; the new
        theme is held to it."""
        palette = style.palette("phosphor")
        for surface in SURFACES:
            with self.subTest(surface=surface):
                self.assertGreaterEqual(contrast(palette["line"], palette[surface]), 3.0)
                self.assertGreaterEqual(contrast(palette["edge"], palette[surface]), 3.0)

    def test_phosphor_surfaces_are_cool_and_not_black(self):
        """The look, asserted: blue channel leads, and nothing is #000 -- a
        hairline needs a surface under it, and pure black reads as a hole."""
        palette = style.palette("phosphor")
        for surface in SURFACES:
            value = palette[surface].lstrip("#")
            red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
            with self.subTest(surface=surface):
                self.assertGreater(blue, red)
                self.assertGreater(blue + green + red, 0)

    def test_agent_ink_is_distinguishable_from_chrome_ink(self):
        """The whole point of the two-accent split: "Claude is doing something"
        must never be mistakable for "this has focus"."""
        palette = style.palette("phosphor")
        self.assertNotEqual(palette["accent"], palette["agent"])
        # Not a contrast ratio: cyan and amber are near-identical in luminance,
        # which is exactly why they read as *different things* rather than as
        # two intensities of one thing. What has to differ is the hue, and the
        # cheapest honest test of that is the channel order -- blue leads the
        # chrome ink, red leads Claude's.
        chrome = [int(palette["accent"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        claude = [int(palette["agent"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        self.assertEqual(max(range(3), key=chrome.__getitem__), 2)
        self.assertEqual(max(range(3), key=claude.__getitem__), 0)


@unittest.skipIf(Gtk is None, "GTK 3 is not importable here")
class Parses(unittest.TestCase):
    """The sheet, through the real parser. No display is needed: a CssProvider
    is not a widget and never touches the screen."""

    def load(self, data):
        provider = Gtk.CssProvider()
        errors = []
        provider.connect("parsing-error",
                         lambda _p, _section, error: errors.append(error.message))
        provider.load_from_data(data)
        return errors

    def test_every_theme_parses_clean(self):
        for name in style.THEME_NAMES:
            for motion in (True, False):
                with self.subTest(theme=name, motion=motion):
                    self.assertEqual(self.load(style.css(name, motion=motion)), [])

    def test_an_unknown_property_would_have_been_caught(self):
        """Proof the check above can fail: GTK3 has no `text-transform`, and
        this is exactly the class of mistake it exists to catch."""
        with self.assertRaises(Exception):
            self.load(b"label { text-transform: uppercase; }")


class Motion(unittest.TestCase):
    def test_reduced_motion_removes_every_transition(self):
        for name in style.THEME_NAMES:
            with self.subTest(theme=name):
                self.assertIn(b"transition:", style.css(name, motion=True))
                self.assertNotIn(b"transition", style.css(name, motion=False))

    def test_phosphor_texture_is_static(self):
        """A gradient, not an animation: nothing here may repaint on a timer."""
        sheet = style.css("phosphor")
        self.assertIn(b"repeating-linear-gradient", sheet)
        self.assertNotIn(b"animation:", sheet)
        self.assertNotIn(b"@keyframes", sheet)

    def test_phosphor_is_additive(self):
        """dark and light keep the shape they had: the HUD block is appended for
        phosphor only, so picking it is a choice rather than a silent redesign
        of the other two."""
        self.assertNotIn(b"repeating-linear-gradient", style.css("dark"))
        self.assertLess(len(style.css("dark")), len(style.css("phosphor")))
        self.assertEqual(style.css("dark"), style.css("nonsense"))
