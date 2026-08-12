"""The resource-mode ladder: which mode a URL gets, and what that mode costs.

GTK-free like `modes.py` itself. The assertions worth reading are the ones about
what a mode may *not* turn off: the ladder's whole risk is that it grows a switch
which saves a little CPU and quietly breaks a site into looking like a bot, and
those are the tests that would catch it.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import modes  # noqa: E402


class NormalizeTest(unittest.TestCase):
    def test_every_mode_survives_a_round_trip(self):
        for mode in modes.MODES:
            with self.subTest(mode=mode):
                self.assertEqual(mode, modes.normalize(mode))

    def test_case_and_whitespace_are_not_a_typo(self):
        self.assertEqual(modes.POTATO, modes.normalize("  Potato\n"))

    def test_nonsense_falls_back_rather_than_raising(self):
        """A typo in the settings file must not stop the browser starting."""
        for bad in ("banana", "", None, "lite"):
            with self.subTest(bad=bad):
                self.assertEqual(modes.DEFAULT_MODE, modes.normalize(bad))

    def test_the_fallback_is_the_compatible_end_of_the_ladder(self):
        """Guessing too light breaks pages in ways nobody connects to a setting
        they never touched. Guessing normal only costs cycles."""
        self.assertEqual(modes.NORMAL, modes.DEFAULT_MODE)


class LadderTest(unittest.TestCase):
    def test_the_slider_does_not_offer_scraper(self):
        """"This page, but broken" is not a browsing mode."""
        self.assertNotIn(modes.SCRAPER, modes.SLIDER)
        self.assertFalse(modes.is_selectable(modes.SCRAPER))
        for mode in modes.SLIDER:
            self.assertTrue(modes.is_selectable(mode))

    def test_settings_compose_down_the_ladder(self):
        """A switch added to a lighter mode cannot be forgotten in a heavier
        one, which is the entire reason the table is written as deltas."""
        normal = modes.settings_for(modes.NORMAL)
        light = modes.settings_for(modes.LIGHT)
        potato = modes.settings_for(modes.POTATO)
        for key, value in normal.items():
            self.assertEqual(value, light.get(key), key)
            self.assertEqual(value, potato.get(key), key)
        for key, value in light.items():
            self.assertEqual(value, potato.get(key), key)

    def test_each_rung_actually_turns_something_more_off(self):
        sizes = [len(modes.settings_for(m)) for m in modes.SLIDER]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[-1])

    def test_scraper_has_no_webkit_settings_at_all(self):
        """There is no WebView in that tier for any of them to be set on."""
        self.assertEqual({}, modes.settings_for(modes.SCRAPER))
        self.assertFalse(modes.uses_webkit(modes.SCRAPER))
        for mode in modes.SLIDER:
            self.assertTrue(modes.uses_webkit(mode))

    def test_the_caller_gets_a_fresh_dict(self):
        """It is handed to WebKit; nobody should have to be careful with it."""
        first = modes.settings_for(modes.POTATO)
        first["auto-load-images"] = "tampered"
        self.assertIs(False, modes.settings_for(modes.POTATO)["auto-load-images"])


class CompatibilityFloorTest(unittest.TestCase):
    """What no mode is allowed to break.

    These are the tests that exist because the failure they describe is silent:
    a site that decides the browser is automated does not report a missing API,
    it shows a challenge that never passes.
    """

    def test_javascript_is_never_switched_off_by_a_slider_mode(self):
        """Potato is 'HTML and basic JavaScript'. Turning script off is the
        scraper tier, which is a different implementation entirely -- and a
        slider position that silently broke every client-rendered site would be
        the worst thing on the control."""
        for mode in modes.SLIDER:
            with self.subTest(mode=mode):
                self.assertNotIn("enable-javascript", modes.settings_for(mode))

    def test_no_mode_disables_the_app_platform(self):
        """Service workers, IndexedDB, WebAssembly and storage are what current
        web apps are built on, and they are idle until used -- so switching them
        off pays in compatibility for a saving that does not exist."""
        forbidden = ("enable-offline-web-application-cache",
                     "enable-html5-database", "enable-html5-local-storage")
        for mode in modes.MODES:
            got = modes.settings_for(mode)
            for key in forbidden:
                with self.subTest(mode=mode, key=key):
                    self.assertNotIn(key, got)

    def test_normal_keeps_everything_a_page_can_feature_detect(self):
        """Normal is the mode a site is put in when it has to work, so nothing
        in it may be observable to a bot check -- only waste."""
        for key in modes.settings_for(modes.NORMAL):
            with self.subTest(key=key):
                self.assertNotIn(key, ("auto-load-images", "enable-webgl",
                                       "enable-webaudio", "enable-javascript"))

    def test_every_mode_has_a_label_and_a_summary(self):
        for mode in modes.MODES:
            with self.subTest(mode=mode):
                self.assertTrue(modes.LABELS.get(mode))
                self.assertTrue(modes.SUMMARIES.get(mode))

    def test_potato_admits_that_it_hides_pictures(self):
        """A page with no images and no explanation reads as broken."""
        self.assertTrue(modes.blocks_images(modes.POTATO))
        self.assertFalse(modes.blocks_images(modes.NORMAL))


class HostTest(unittest.TestCase):
    def test_www_is_not_a_different_site(self):
        self.assertEqual("example.com", modes.host_of("https://www.example.com/x"))
        self.assertEqual("example.com", modes.host_of("https://example.com/"))

    def test_case_is_not_a_different_site(self):
        self.assertEqual("example.com", modes.host_of("https://EXAMPLE.com"))

    def test_internal_pages_have_no_site(self):
        """cb: pages are rendered by the browser and have nothing to economise
        on, so they must never pick up a stored choice."""
        for url in ("cb:home", "about:blank", "", None, "not a url"):
            with self.subTest(url=url):
                self.assertEqual("", modes.host_of(url))


class SitesTest(unittest.TestCase):
    def test_pairs_round_trip(self):
        sites = {"example.com": modes.POTATO, "news.example.org": modes.LIGHT}
        self.assertEqual(sites, modes.parse_sites(modes.format_sites(sites)))

    def test_formatting_is_stable(self):
        """An unstable order would make every rewrite look like a change."""
        sites = {"b.com": modes.LIGHT, "a.com": modes.POTATO}
        self.assertEqual(modes.format_sites(sites), modes.format_sites(sites))
        self.assertTrue(modes.format_sites(sites).startswith("a.com="))

    def test_commas_and_whitespace_both_separate(self):
        got = modes.parse_sites("a.com=light, b.com=potato\nc.com=normal")
        self.assertEqual({"a.com": modes.LIGHT, "b.com": modes.POTATO,
                          "c.com": modes.NORMAL}, got)

    def test_a_broken_entry_costs_that_entry_and_not_the_map(self):
        got = modes.parse_sites("a.com=light garbage b.com=potato")
        self.assertEqual({"a.com": modes.LIGHT, "b.com": modes.POTATO}, got)

    def test_an_unknown_mode_name_lands_on_the_safe_side(self):
        """Guessing normal means a page that works."""
        self.assertEqual({"a.com": modes.NORMAL}, modes.parse_sites("a.com=ptato"))


class ResolutionTest(unittest.TestCase):
    SITES = {"example.com": modes.POTATO, "docs.example.com": modes.NORMAL}

    def resolve(self, url, default=modes.NORMAL):
        return modes.for_url(url, sites=self.SITES, default=default)

    def test_a_site_override_wins_over_the_default(self):
        self.assertEqual(modes.POTATO,
                         self.resolve("https://example.com/a", modes.LIGHT))

    def test_a_choice_covers_subdomains(self):
        self.assertEqual(modes.POTATO, self.resolve("https://cdn.example.com/a"))

    def test_the_most_specific_choice_wins(self):
        """docs.example.com=normal must survive example.com=potato, or pinning
        one section of a site to working is impossible."""
        self.assertEqual(modes.NORMAL, self.resolve("https://docs.example.com/a"))

    def test_an_unlisted_site_gets_the_default(self):
        self.assertEqual(modes.LIGHT,
                         self.resolve("https://other.org/", modes.LIGHT))

    def test_a_page_with_no_host_gets_the_default_not_a_stored_choice(self):
        self.assertEqual(modes.LIGHT, self.resolve("cb:home", modes.LIGHT))

    def test_a_nonsense_default_still_resolves(self):
        self.assertEqual(modes.NORMAL, self.resolve("https://other.org/", "wat"))


class WriteTest(unittest.TestCase):
    """`default` is passed explicitly throughout: without it these read the
    developer's own settings file, and a test whose result depends on the
    machine it runs on is not pinning anything."""

    def test_setting_a_site_records_the_registrable_host(self):
        sites, host = modes.with_site("https://www.example.com/deep/path",
                                      modes.POTATO, sites={},
                                      default=modes.NORMAL)
        self.assertEqual("example.com", host)
        self.assertEqual({"example.com": modes.POTATO}, sites)

    def test_a_page_with_no_site_records_nothing(self):
        sites, host = modes.with_site("cb:home", modes.POTATO, sites={},
                                      default=modes.NORMAL)
        self.assertEqual("", host)
        self.assertEqual({}, sites)

    def test_choosing_the_default_removes_the_entry_rather_than_pinning_it(self):
        """An entry the user did not choose would pin that site against any
        future change of default -- the same reasoning as `envfile.remove`."""
        sites, _ = modes.with_site("https://a.com/", modes.NORMAL,
                                   sites={"a.com": modes.POTATO},
                                   default=modes.NORMAL)
        self.assertEqual({}, sites)

    def test_a_non_default_choice_is_written(self):
        sites, _ = modes.with_site("https://a.com/", modes.POTATO, sites={},
                                   default=modes.LIGHT)
        self.assertEqual({"a.com": modes.POTATO}, sites)

    def test_the_caller_s_map_is_not_mutated(self):
        original = {"a.com": modes.LIGHT}
        modes.with_site("https://b.com/", modes.POTATO, sites=original,
                        default=modes.NORMAL)
        self.assertEqual({"a.com": modes.LIGHT}, original)


if __name__ == "__main__":
    unittest.main()
