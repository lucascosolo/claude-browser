"""CB_LIGHT: asking the server for a cheaper page.

Only the parts that do not need a screen are here, which is most of the
interesting ones: whether the knob is on, what headers that means, and that the
request handed to WebKit actually carries them. Constructing a
`WebKitURIRequest` needs no display -- it is a plain GObject with a
SoupMessageHeaders on it -- so the header assembly is checked against the real
type rather than a stand-in that could drift from it.

What is deliberately *not* here: the effect of any of this on a live page.
`perf.tune_gtk` sets a GTK setting whose page-side half only exists once a web
process is running, and a test that asserted otherwise would be asserting
nothing at all.
"""

import os
import tempfile
import unittest
from pathlib import Path

from claudebrowser import envfile, pages, perf, style


class Isolated(unittest.TestCase):
    """CB_LIGHT now comes from the settings file first, so every test here needs
    a settings file of its own. Without this, a developer who has used the
    switch on cb:data once has a real ~/.config/claude-browser/env that decides
    what these assertions see."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "claude-browser" / "env"

        for name in ("XDG_CONFIG_HOME", perf.LIGHT_ENV):
            saved = os.environ.get(name)
            self.addCleanup(self._restore, name, saved)
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        os.environ.pop(perf.LIGHT_ENV, None)

    @staticmethod
    def _restore(name, saved):
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


class TestKnob(Isolated):
    def test_it_defaults_on(self):
        self.assertTrue(perf.light_enabled())

    def test_the_off_spellings(self):
        for raw in ("0", "off", "false", "no", "OFF", " off ", "No"):
            self.assertFalse(perf.light_enabled(raw), raw)

    def test_the_on_spellings(self):
        for raw in ("", "1", "on", "yes", "true", None):
            self.assertTrue(perf.light_enabled("" if raw is None else raw), raw)

    def test_a_typo_leaves_it_on(self):
        """Matching CB_BLOCK: the knob turns a default *off*, so anything it
        cannot read must not be taken as permission to lose that default."""
        self.assertTrue(perf.light_enabled("offf"))
        self.assertTrue(perf.light_enabled("banana"))

    def test_the_environment_turns_it_off(self):
        os.environ[perf.LIGHT_ENV] = "0"
        self.assertFalse(perf.light_enabled())
        os.environ[perf.LIGHT_ENV] = "1"
        self.assertTrue(perf.light_enabled())


class TestHeaders(Isolated):
    def test_save_data_is_what_goes_out(self):
        self.assertEqual(perf.hint_headers(), {"Save-Data": "on"})

    def test_nothing_goes_out_when_it_is_off(self):
        os.environ[perf.LIGHT_ENV] = "off"
        self.assertEqual(perf.hint_headers(), {})

    def test_no_high_entropy_hints(self):
        """Device-Memory and Downlink were considered and refused -- one is a
        fingerprinting bit servers must ask for, the other a measurement this
        process does not have. A future addition should be a decision, not a
        drive-by, so the omission is asserted."""
        self.assertNotIn("Device-Memory", perf.hint_headers())
        self.assertNotIn("Downlink", perf.hint_headers())

    def test_the_user_agent_is_untouched(self):
        """Pretending to be a phone is the other way to get a light page, and
        it is a fingerprinting and correctness mess. Nothing here may do it."""
        for name in perf.hint_headers():
            self.assertNotEqual(name.lower(), "user-agent")

    def test_the_request_carries_the_hint(self):
        request = perf.make_request("https://example.com/a?b=c")
        self.assertIsNotNone(request)
        self.assertEqual(request.get_uri(), "https://example.com/a?b=c")
        self.assertEqual(request.get_http_headers().get_one("Save-Data"), "on")

    def test_headers_are_replaced_not_appended(self):
        """`replace`, not `append`: a second Save-Data would be a second header
        line, and a server reading only the first would see whichever won."""
        request = perf.make_request("https://example.com/")
        seen = []
        request.get_http_headers().foreach(lambda k, v: seen.append(k))
        self.assertEqual(seen.count("Save-Data"), 1)

    def test_no_request_is_built_when_it_is_off(self):
        """None means "fall back to load_uri", which is what keeps the off path
        identical to what the browser did before this existed."""
        os.environ[perf.LIGHT_ENV] = "0"
        self.assertIsNone(perf.make_request("https://example.com/"))


class TestLoadUrl(Isolated):
    """`load_url` picks a WebKit call; which one it picks is the whole logic."""

    class FakeView:
        def __init__(self):
            self.calls = []

        def load_uri(self, url):
            self.calls.append(("load_uri", url))

        def load_request(self, request):
            self.calls.append(("load_request", request))

    def test_on_it_loads_a_request(self):
        view = self.FakeView()
        perf.load_url(view, "https://example.com/")
        kind, request = view.calls[0]
        self.assertEqual(kind, "load_request")
        self.assertEqual(request.get_http_headers().get_one("Save-Data"), "on")

    def test_off_it_falls_back_to_load_uri(self):
        os.environ[perf.LIGHT_ENV] = "0"
        view = self.FakeView()
        perf.load_url(view, "https://example.com/")
        self.assertEqual(view.calls, [("load_uri", "https://example.com/")])


class TestTuneGtk(Isolated):
    """The GTK half. A real Gtk.Settings needs a display; what is testable is
    that the right property is asked for, and that it is left alone when the
    knob is off."""

    class FakeSettings:
        def __init__(self, fail=False):
            self.props = {}
            self.fail = fail

        def set_property(self, name, value):
            if self.fail:
                raise TypeError("no such property")
            self.props[name] = value

    def test_it_turns_animations_off(self):
        settings = self.FakeSettings()
        notes = perf.tune_gtk(settings)
        self.assertEqual(settings.props, {"gtk-enable-animations": False})
        self.assertTrue(notes)

    def test_it_leaves_them_alone_when_off(self):
        os.environ[perf.LIGHT_ENV] = "0"
        settings = self.FakeSettings()
        self.assertEqual(perf.tune_gtk(settings), [])
        self.assertEqual(settings.props, {})

    def test_a_missing_property_is_a_note_not_a_crash(self):
        """Same rule as the rest of perf.py: a browser that will not start
        because a tuning call vanished is worse than a slower browser."""
        notes = perf.tune_gtk(self.FakeSettings(fail=True))
        self.assertEqual(len(notes), 1)
        self.assertIn("unavailable", notes[0])

    def test_no_settings_object_is_survivable(self):
        self.assertEqual(perf.tune_gtk(None), [])


class TestDataPageSurface(unittest.TestCase):
    """cb:data is where a user who never reads a README finds out this is on."""

    def render(self, light):
        machine = {"level": "ok", "memory": "ok", "cpu": "ok",
                   "available_mb": 900, "total_mb": 3800, "swap_used_mb": 0,
                   "swap_total_mb": 1544, "load": 0.4, "cores": 2, "tabs": 1,
                   "discarded": 0, "tab_ceiling": 10, "loading": 0}
        info = {"policy": "nothird", "domains": 1, "cache_bytes": 0,
                "cookie_jar_bytes": 0, "data_dir": "/tmp/cb"}
        return pages.data_page(style.palette("dark"), "NONCE", machine, info,
                               lambda size: "0 B", light=light)

    def test_it_says_when_the_hint_is_being_sent(self):
        html = self.render({"enabled": True})
        self.assertIn("Ask sites for a lighter page", html)
        self.assertIn("Save-Data: on", html)
        self.assertIn("CB_LIGHT", html)

    def test_it_says_when_it_is_off(self):
        html = self.render({"enabled": False})
        self.assertIn("Ask sites for a lighter page", html)
        self.assertIn("not requested", html)

    def test_it_renders_without_the_argument_at_all(self):
        """cb:data must survive a caller that predates this section rather than
        raising a KeyError into the scheme handler, which renders as a blank
        page with no error anywhere."""
        html = self.render(None)
        self.assertIn("Ask sites for a lighter page", html)

    def test_the_switch_offers_the_other_state(self):
        self.assertIn("Turn it off", self.render({"enabled": True}))
        self.assertIn("Turn it on", self.render({"enabled": False}))

    def test_the_switch_posts_the_state_it_wants(self):
        """Not a flip: the button carries "on" or "off" so a cb:data tab that
        has been sitting open cannot invert a setting changed since it rendered."""
        self.assertIn('{action:\'set_light\', title:&quot;off&quot;}',
                      self.render({"enabled": True}))
        self.assertIn('{action:\'set_light\', title:&quot;on&quot;}',
                      self.render({"enabled": False}))

    def test_the_switch_goes_through_the_nonce_path(self):
        """cbui.send is what stamps the per-session token onto the message. A
        button that reached the browser any other way would be one a website
        could imitate."""
        self.assertIn("cbui.send({action:'set_light'", self.render({"enabled": True}))

    def test_motion_is_reported_separately_from_the_header(self):
        """After the switch is used the two genuinely disagree, and saying
        "reduced motion: requested" because the header is back on would be a
        lie for the rest of the session."""
        html = self.render({"enabled": True, "motion": False})
        self.assertIn("Save-Data: on", html)
        self.assertIn("not requested", html)
        html = self.render({"enabled": False, "motion": True})
        self.assertIn("requested at startup", html)

    def test_the_note_admits_the_restart(self):
        self.assertIn("restarts", self.render({"enabled": True}))


class TestRemembering(Isolated):
    """The switch writes the same settings file the persona selector does, which
    is what makes the choice survive the next launch."""

    def test_round_trip_through_the_settings_file(self):
        self.assertIs(perf.remember(False, path=self.config), False)
        self.assertEqual(envfile.values(self.config)[perf.LIGHT_ENV], "0")
        self.assertFalse(perf.light_enabled(path=self.config))

        self.assertIs(perf.remember(True, path=self.config), True)
        self.assertEqual(envfile.values(self.config)[perf.LIGHT_ENV], "1")
        self.assertTrue(perf.light_enabled(path=self.config))

    def test_switching_replaces_rather_than_appends(self):
        for wanted in (False, True, False):
            perf.remember(wanted, path=self.config)
        self.assertEqual(self.config.read_text().count("CB_LIGHT="), 1)

    def test_it_takes_effect_without_a_restart(self):
        """The whole point of reading the setting per call: the next navigation
        stops carrying the hint, rather than the one after the next launch."""
        perf.remember(False, path=self.config)
        self.assertEqual(perf.hint_headers(), {})
        self.assertIsNone(perf.make_request("https://example.com/"))
        perf.remember(True, path=self.config)
        self.assertEqual(perf.hint_headers(), {"Save-Data": "on"})

    def test_the_file_wins_over_the_environment(self):
        """envfile's rule, and the reason the switch works at all: a CB_LIGHT
        exported by whatever shell launched the browser cannot pin the setting
        against what the user just clicked."""
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("CB_LIGHT=0\n")
        os.environ[perf.LIGHT_ENV] = "1"
        self.assertFalse(perf.light_enabled(path=self.config))

    def test_a_hand_edit_is_picked_up(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("CB_LIGHT=off\n")
        self.assertFalse(perf.light_enabled(path=self.config))

    def test_the_default_path_is_the_settings_file(self):
        """Called with no path at all -- the way the browser calls it -- it must
        land in the same file every other setting lives in."""
        perf.remember(False)
        self.assertEqual(envfile.config_path(), self.config)
        self.assertFalse(perf.light_enabled())


if __name__ == "__main__":
    unittest.main()
