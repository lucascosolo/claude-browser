import re
import unittest

from claudebrowser import extract


class TestSnapshotShim(unittest.TestCase):
    def test_mints_a_fresh_epoch_per_document(self):
        self.assertIn("__cbEpoch", extract.SNAPSHOT_SHIM)
        # Must be assigned unconditionally at document-start, not guarded by
        # `if (window.__cbEpoch) return` the way __cbHalo is -- a repeat guard
        # here would let a stale epoch survive a same-document re-run.
        self.assertNotIn('if (window.__cbEpoch)', extract.SNAPSHOT_SHIM)

    def test_defines_a_registry_and_resolver(self):
        self.assertIn("__cbRegister", extract.SNAPSHOT_SHIM)
        self.assertIn("__cbResolve", extract.SNAPSHOT_SHIM)


class TestSnapshot(unittest.TestCase):
    def test_returns_a_function_expression(self):
        js = extract.snapshot()
        self.assertTrue(js.strip().startswith("(function"))
        self.assertIn("JSON.stringify", js)

    def test_walks_same_origin_iframes_and_reports_cross_origin_ones(self):
        js = extract.snapshot()
        self.assertIn("contentDocument", js)
        self.assertIn("UNREACHABLE", js)

    def test_reports_option_count_not_option_list(self):
        js = extract.snapshot()
        self.assertIn("options", js)
        # A snapshot must never inline every <option> -- that is the exact
        # per-turn token cost the whole feature exists to avoid.
        self.assertNotIn("optionsList", js)

    def test_uses_the_same_visibility_check_as_text(self):
        js = extract.snapshot()
        self.assertIn("getClientRects", js)


class TestRefTargeting(unittest.TestCase):
    def test_click_resolves_an_at_sign_ref(self):
        js = extract.click("@e7")
        self.assertIn("__cbResolve", js)
        self.assertIn("e7", js)

    def test_click_on_a_plain_selector_does_not_touch_the_resolver(self):
        js = extract.click("#submit")
        self.assertIn("querySelector", js)

    def test_fill_resolves_an_at_sign_ref(self):
        js = extract.fill("@e3", "California")
        self.assertIn("__cbResolve", js)

    def test_click_and_fill_append_a_delta(self):
        self.assertIn("url_changed", extract.click("#submit"))
        self.assertIn("url_changed", extract.fill("#z", "1"))
        self.assertIn("blocked", extract.click("#submit"))
        self.assertIn("errors", extract.click("#submit"))


class TestFillMany(unittest.TestCase):
    def test_returns_a_function_expression(self):
        js = extract.fill_many([("#email", "a@b.com"), ("@e3", "CA")])
        self.assertTrue(js.strip().startswith("(function"))

    def test_escapes_every_selector_and_value(self):
        js = extract.fill_many([('"><script>alert(1)</script>', 'x')])
        self.assertNotIn("<script>", js)


if __name__ == "__main__":
    unittest.main()
