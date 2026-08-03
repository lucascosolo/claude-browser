"""The parts of cookie/cache handling that do not need a browser running.

The interesting half of storage.py is WebKit calls, which are covered by the
browser actually starting. What is testable without a display is the policy
resolution, the size accounting, and the render of cb:data -- and the last one
matters more than it looks, because that page is the only place a user finds out
that a tab was discarded or that the cache has grown to a gigabyte.
"""

import os
import tempfile
import unittest

from claudebrowser import pages, style


class TestPolicy(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("CB_COOKIES")

    def tearDown(self):
        os.environ.pop("CB_COOKIES", None)
        if self.saved is not None:
            os.environ["CB_COOKIES"] = self.saved

    def policy_name(self):
        from claudebrowser import storage

        return storage.policy_name()

    def test_the_default_rejects_third_party_cookies(self):
        os.environ.pop("CB_COOKIES", None)
        self.assertEqual(self.policy_name(), "nothird")

    def test_the_environment_can_override_it(self):
        os.environ["CB_COOKIES"] = "all"
        self.assertEqual(self.policy_name(), "all")
        os.environ["CB_COOKIES"] = "NONE"
        self.assertEqual(self.policy_name(), "none")

    def test_nonsense_falls_back_rather_than_failing(self):
        """A typo in an env var should cost you the setting, not the browser."""
        os.environ["CB_COOKIES"] = "yes-please"
        self.assertEqual(self.policy_name(), "nothird")


class TestSizes(unittest.TestCase):
    def sizes(self):
        from claudebrowser import storage

        return storage

    def test_directory_size_sums_the_tree(self):
        storage = self.sizes()
        root = tempfile.mkdtemp()
        os.mkdir(os.path.join(root, "sub"))
        with open(os.path.join(root, "a"), "wb") as handle:
            handle.write(b"x" * 100)
        with open(os.path.join(root, "sub", "b"), "wb") as handle:
            handle.write(b"y" * 250)
        self.assertEqual(storage.dir_size(root), 350)

    def test_a_missing_path_is_zero_not_an_error(self):
        storage = self.sizes()
        self.assertEqual(storage.dir_size("/nonexistent/cache"), 0)
        self.assertEqual(storage.file_size("/nonexistent/cookies.sqlite"), 0)

    def test_human_readable_sizes(self):
        storage = self.sizes()
        self.assertEqual(storage.human(0), "0 B")
        self.assertEqual(storage.human(512), "512 B")
        self.assertEqual(storage.human(1536), "1.5 KB")
        self.assertEqual(storage.human(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(storage.human(2 * 1024 ** 3), "2.0 GB")

    def test_unknown_renders_as_a_dash_rather_than_zero(self):
        """None means "we could not find out", which is a different fact from
        "there is none", and the page must not claim the second."""
        self.assertEqual(self.sizes().human(None), "—")


class TestDataPage(unittest.TestCase):
    """cb:data renders from plain dicts, so it can be built without a browser."""

    def render(self, machine=None, info=None, pagetext_info=None):
        def human(size):
            return "—" if size is None else "%d B" % size

        machine = machine or {"level": "ok", "memory": "ok", "cpu": "ok",
                              "available_mb": 900, "total_mb": 3800,
                              "swap_used_mb": 200, "swap_total_mb": 1544,
                              "load": 0.4, "cores": 2, "tabs": 3, "discarded": 1,
                              "tab_ceiling": 10, "loading": 0}
        info = info or {"policy": "nothird", "domains": 12, "cache_bytes": 4096,
                        "cookie_jar_bytes": 512, "data_dir": "/tmp/cb"}
        return pages.data_page(style.palette(True), "NONCE", machine, info, human,
                               pagetext_info=pagetext_info)

    def test_it_renders_the_numbers_that_matter(self):
        html = self.render()
        self.assertIn("Cookies", html)
        self.assertIn("reject third-party", html)
        self.assertIn("Freed to save memory", html)
        self.assertIn("Limit for agent-opened tabs", html)

    def test_memory_and_cpu_levels_are_reported_separately(self):
        """A busy CPU on a machine with gigabytes free must not print
        "critical" over the Memory heading. They decide different things: only
        memory can refuse a page load."""
        html = self.render(machine={"level": "critical", "memory": "ok",
                                    "cpu": "critical", "available_mb": 2000,
                                    "total_mb": 3800, "swap_used_mb": 0,
                                    "swap_total_mb": 1544, "load": 12.0,
                                    "cores": 2, "tabs": 2, "discarded": 0,
                                    "tab_ceiling": 10, "loading": 0})
        self.assertIn('<em class="lvl ok">ok</em>', html)
        self.assertIn("cpu critical", html)

    def test_the_nonce_is_in_the_document(self):
        """Every cb: page authenticates its messages; a page that forgot would
        have buttons that silently do nothing."""
        self.assertIn("NONCE", self.render())

    def test_clearing_is_armed_rather_than_immediate(self):
        html = self.render()
        self.assertIn("confirmData", html)
        for kind in ("cache", "cookies", "all", "pagetext"):
            self.assertIn('data-kind="%s"' % kind, html)

    def test_the_page_text_cache_is_reported_and_clearable(self):
        """It holds the prose of everything read, which makes it the most
        personal thing on disk -- so it belongs on the page that clears data,
        not only in a file nobody knows the path of."""
        html = self.render(pagetext_info={"pages": 42, "bodies": 30,
                                          "bytes": 8192, "search": True})
        self.assertIn("Page text", html)
        self.assertIn("Pages cached", html)
        self.assertIn(">42<", html)
        self.assertIn("Clear page text", html)

    def test_a_disabled_page_text_cache_renders_without_numbers(self):
        html = self.render(pagetext_info=None)
        self.assertIn("Page text", html)
        self.assertNotIn(">None<", html)

    def test_meters_are_clamped(self):
        """A load average can exceed the core count, and a bar wider than its
        track reads as a rendering bug rather than the alarming number it is."""
        html = self.render(machine={"level": "critical", "available_mb": 40,
                                    "total_mb": 3800, "swap_used_mb": 1500,
                                    "swap_total_mb": 1544, "load": 9.0, "cores": 2,
                                    "tabs": 9, "discarded": 4, "tab_ceiling": 4,
                                    "loading": 1})
        # Only the inline meter widths, not every "width:" in the stylesheet.
        widths = [float(chunk.split("%")[0])
                  for chunk in html.split('style="width:')[1:]]
        self.assertTrue(widths, "the page rendered no meters at all")
        for width in widths:
            self.assertLessEqual(width, 100.0)

    def test_a_machine_with_no_swap_says_so(self):
        html = self.render(machine={"level": "ok", "available_mb": 900,
                                    "total_mb": 3800, "swap_used_mb": 0,
                                    "swap_total_mb": 0, "load": 0.1, "cores": 4,
                                    "tabs": 1, "discarded": 0, "tab_ceiling": 10,
                                    "loading": 0})
        self.assertIn("no swap configured", html)

    def test_unknown_cookie_count_does_not_render_as_none(self):
        html = self.render(info={"policy": "all", "domains": None,
                                 "cache_bytes": 0, "cookie_jar_bytes": 0,
                                 "data_dir": "/tmp/cb"})
        self.assertNotIn(">None<", html)

    def test_the_rail_links_to_it(self):
        self.assertIn("cb:data", [url for url, _label, _d in pages.NAV])


if __name__ == "__main__":
    unittest.main()
