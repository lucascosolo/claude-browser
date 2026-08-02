"""The vault, against a fake keyring. No display, no Secret Service, no GTK."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claudebrowser import passwords
from claudebrowser.passwords import MemoryBackend, Vault, origin_of


class Origins(unittest.TestCase):
    def test_strips_path_query_and_fragment(self):
        self.assertEqual(origin_of("https://example.com/login?next=/a#z"),
                         "https://example.com")

    def test_lowercases_host(self):
        self.assertEqual(origin_of("https://Example.COM/x"), "https://example.com")

    def test_default_ports_are_implicit(self):
        self.assertEqual(origin_of("https://example.com:443/"), "https://example.com")
        self.assertEqual(origin_of("http://example.com:80/"), "http://example.com")

    def test_nonstandard_port_is_part_of_the_origin(self):
        self.assertEqual(origin_of("http://localhost:5173/app"),
                         "http://localhost:5173")

    def test_scheme_is_part_of_the_origin(self):
        # http and https are different security origins; a password typed into
        # the secure one must not be handed to the plain one.
        self.assertNotEqual(origin_of("http://example.com"),
                            origin_of("https://example.com"))

    def test_non_web_schemes_have_no_origin(self):
        for url in ("cb:home", "about:blank", "file:///etc/passwd",
                    "data:text/html,hi", "", None):
            self.assertIsNone(origin_of(url), url)

    def test_malformed_port_does_not_raise(self):
        self.assertIsNone(origin_of("https://example.com:notaport/"))


class Saving(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(MemoryBackend())

    def test_round_trip(self):
        self.assertTrue(self.vault.save("https://a.test", "ada", "hunter2"))
        self.assertEqual(self.vault.secret("https://a.test", "ada"), "hunter2")

    def test_unknown_lookup_is_none(self):
        self.assertIsNone(self.vault.secret("https://a.test", "nobody"))

    def test_origins_do_not_leak_into_each_other(self):
        self.vault.save("https://a.test", "ada", "one")
        self.vault.save("https://b.test", "ada", "two")
        self.assertEqual(self.vault.secret("https://a.test", "ada"), "one")
        self.assertEqual(self.vault.credentials("https://b.test"),
                         [{"username": "ada", "password": "two"}])

    def test_saving_again_updates_rather_than_duplicates(self):
        self.vault.save("https://a.test", "ada", "old")
        self.vault.save("https://a.test", "ada", "new")
        self.assertEqual(self.vault.usernames("https://a.test"), ["ada"])
        self.assertEqual(self.vault.secret("https://a.test", "ada"), "new")

    def test_several_accounts_on_one_site(self):
        self.vault.save("https://a.test", "ada", "one")
        self.vault.save("https://a.test", "grace", "two")
        self.assertEqual(self.vault.usernames("https://a.test"), ["ada", "grace"])
        self.assertEqual(len(self.vault.credentials("https://a.test")), 2)

    def test_empty_password_is_refused(self):
        self.assertFalse(self.vault.save("https://a.test", "ada", ""))

    def test_missing_origin_is_refused(self):
        self.assertFalse(self.vault.save(None, "ada", "hunter2"))

    def test_delete(self):
        self.vault.save("https://a.test", "ada", "hunter2")
        self.assertTrue(self.vault.delete("https://a.test", "ada"))
        self.assertIsNone(self.vault.secret("https://a.test", "ada"))
        self.assertEqual(self.vault.entries(), [])

    def test_entries_carry_no_secrets(self):
        self.vault.save("https://a.test", "ada", "hunter2")
        rows = self.vault.entries()
        self.assertEqual(rows, [{"origin": "https://a.test", "username": "ada"}])
        self.assertNotIn("hunter2", repr(rows))


class Offering(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(MemoryBackend())

    def test_offers_a_new_login(self):
        self.assertTrue(self.vault.should_offer("https://a.test", "ada", "hunter2"))

    def test_stays_quiet_for_an_unchanged_login(self):
        self.vault.save("https://a.test", "ada", "hunter2")
        self.assertFalse(self.vault.should_offer("https://a.test", "ada", "hunter2"))

    def test_still_offers_when_the_password_changed(self):
        # Password rotation is the case a "already know this site" check breaks.
        self.vault.save("https://a.test", "ada", "old")
        self.assertTrue(self.vault.should_offer("https://a.test", "ada", "new"))

    def test_offers_a_second_account_on_a_known_site(self):
        self.vault.save("https://a.test", "ada", "one")
        self.assertTrue(self.vault.should_offer("https://a.test", "grace", "two"))

    def test_never_silences_a_site(self):
        self.vault.set_never("https://a.test")
        self.assertFalse(self.vault.should_offer("https://a.test", "ada", "hunter2"))
        self.assertTrue(self.vault.should_offer("https://b.test", "ada", "hunter2"))

    def test_never_is_reversible(self):
        self.vault.set_never("https://a.test")
        self.assertEqual(self.vault.never_list(), ["https://a.test"])
        self.vault.clear_never("https://a.test")
        self.assertFalse(self.vault.is_never("https://a.test"))
        self.assertTrue(self.vault.should_offer("https://a.test", "ada", "hunter2"))

    def test_never_does_not_show_up_as_a_saved_login(self):
        self.vault.set_never("https://a.test")
        self.assertEqual(self.vault.entries(), [])

    def test_blank_password_is_never_offered(self):
        self.assertFalse(self.vault.should_offer("https://a.test", "ada", ""))


class InjectedScript(unittest.TestCase):
    """The page-side contract, asserted as text because there is no DOM here."""

    def test_defines_the_two_entry_points_the_native_side_calls(self):
        self.assertIn("window.__cbPwTake", passwords.PASSWORD_JS)
        self.assertIn("window.__cbPwFill", passwords.PASSWORD_JS)

    def test_the_doorbell_carries_no_credential(self):
        # postMessage must never be handed the pending object -- the native side
        # reads it back out of the focused view instead. If this ever becomes
        # postMessage(pending), a background tab can forge a save prompt.
        self.assertIn("postMessage(1)", passwords.PASSWORD_JS)
        self.assertNotIn("postMessage(pending)", passwords.PASSWORD_JS)

    def test_fill_refuses_to_clobber_a_nonempty_field(self):
        self.assertIn("if (pw.value) { return 0; }", passwords.PASSWORD_JS)

    def test_sets_values_through_the_prototype_descriptor(self):
        # Plain `el.value = x` leaves React's tracker stale and the form submits
        # empty. See the comment in passwords.py.
        self.assertIn("getOwnPropertyDescriptor(HTMLInputElement.prototype",
                      passwords.PASSWORD_JS)


if __name__ == "__main__":
    unittest.main()
