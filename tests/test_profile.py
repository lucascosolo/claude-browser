"""The profile vault, against a fake keyring. No display, no Secret Service."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claudebrowser.profile import MemoryBackend, Vault


class Fields(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(MemoryBackend())

    def test_empty_vault_has_no_fields(self):
        self.assertEqual(self.vault.get_all(), {})

    def test_set_and_get_a_field(self):
        self.assertTrue(self.vault.set("first_name", "Ada"))
        self.assertEqual(self.vault.get("first_name"), "Ada")

    def test_unknown_key_is_none(self):
        self.assertIsNone(self.vault.get("nope"))

    def test_several_fields_coexist(self):
        self.vault.set("first_name", "Ada")
        self.vault.set("last_name", "Lovelace")
        self.assertEqual(self.vault.get_all(),
                         {"first_name": "Ada", "last_name": "Lovelace"})

    def test_setting_again_updates_rather_than_duplicates(self):
        self.vault.set("email", "old@example.com")
        self.vault.set("email", "new@example.com")
        self.assertEqual(self.vault.get("email"), "new@example.com")

    def test_setting_with_no_value_deletes_the_field(self):
        self.vault.set("phone", "555-0100")
        self.vault.set("phone", None)
        self.assertIsNone(self.vault.get("phone"))
        self.assertEqual(self.vault.get_all(), {})

    def test_setting_with_an_empty_string_also_deletes(self):
        self.vault.set("phone", "555-0100")
        self.vault.set("phone", "")
        self.assertIsNone(self.vault.get("phone"))

    def test_deleting_an_unset_field_does_not_raise(self):
        self.assertTrue(self.vault.set("nope", None))
        self.assertEqual(self.vault.get_all(), {})

    def test_missing_key_is_refused(self):
        self.assertFalse(self.vault.set("", "value"))
        self.assertFalse(self.vault.set(None, "value"))

    def test_values_needing_json_escaping_round_trip(self):
        tricky = 'Line1\nLine2 "quoted"  '
        self.vault.set("note", tricky)
        self.assertEqual(self.vault.get("note"), tricky)

    def test_other_fields_survive_a_delete(self):
        self.vault.set("first_name", "Ada")
        self.vault.set("last_name", "Lovelace")
        self.vault.set("last_name", None)
        self.assertEqual(self.vault.get_all(), {"first_name": "Ada"})


if __name__ == "__main__":
    unittest.main()
