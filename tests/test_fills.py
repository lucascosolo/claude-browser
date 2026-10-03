import unittest
from claudebrowser import fills, profile


class TestFills(unittest.TestCase):
    def setUp(self):
        self.vault = profile.Vault(profile.MemoryBackend())
        self.vault.set("email", "jane@example.com")

    def test_is_placeholder_true_for_profile_ref(self):
        self.assertTrue(fills.is_placeholder("{profile:email}"))

    def test_is_placeholder_false_for_plain_value(self):
        self.assertFalse(fills.is_placeholder("jane@example.com"))
        self.assertFalse(fills.is_placeholder(""))
        self.assertFalse(fills.is_placeholder("{not a placeholder}"))

    def test_resolve_value_looks_up_the_vault(self):
        self.assertEqual(fills.resolve_value("{profile:email}", self.vault),
                          "jane@example.com")

    def test_resolve_value_passes_through_non_placeholders(self):
        self.assertEqual(fills.resolve_value("literal", self.vault), "literal")

    def test_resolve_value_raises_on_missing_field(self):
        with self.assertRaises(fills.FillError):
            fills.resolve_value("{profile:phone}", self.vault)

    def test_resolve_value_raises_when_vault_unavailable(self):
        with self.assertRaises(fills.FillError):
            fills.resolve_value("{profile:email}", None)

    def test_resolve_value_never_mutates_the_vault(self):
        before = self.vault.get_all()
        fills.resolve_value("{profile:email}", self.vault)
        self.assertEqual(self.vault.get_all(), before)


if __name__ == "__main__":
    unittest.main()
