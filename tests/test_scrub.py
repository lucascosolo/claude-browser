"""The outbound PII scrubber.

Two things are being asserted here, and the second matters as much as the first:
that the patterns catch what they claim to, and that ordinary prose comes back
untouched. A scrubber with false positives rewrites the page out from under the
answer, and the user stops trusting a feature that was supposed to protect them.

GTK-free, like the module it tests.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import scrub  # noqa: E402


def run(text):
    """Scrub with the knob forced on, so a CB_SCRUB in the environment running
    the suite cannot quietly turn half these tests into no-ops."""
    return scrub.scrub(text, on=True)


class PatternTest(unittest.TestCase):
    def test_email(self):
        result = run("Write to ada@example.com or ada.l+tag@mail.example.co.uk.")
        self.assertEqual(result.text, "Write to [email] or [email].")
        self.assertEqual(result.counts, {"email": 2})

    def test_phone(self):
        for raw in ("+44 20 7946 0958", "(555) 123-4567", "555.123.4567",
                    "555-123-4567", "+1-202-555-0173"):
            result = run("Call %s today." % raw)
            self.assertEqual(result.text, "Call [phone] today.", raw)
            self.assertEqual(result.counts, {"phone": 1}, raw)

    def test_card(self):
        result = run("Visa 4111 1111 1111 1111 and 5500-0000-0000-0004 on file.")
        self.assertEqual(result.text, "Visa [card] and [card] on file.")
        self.assertEqual(result.counts, {"card": 2})

    def test_iban(self):
        result = run("Pay to GB82 WEST 1234 5698 7654 32 by Friday.")
        self.assertEqual(result.text, "Pay to [iban] by Friday.")
        self.assertEqual(result.counts, {"iban": 1})

    def test_ssn(self):
        result = run("SSN 123-45-6789 on the form.")
        self.assertEqual(result.text, "SSN [ssn] on the form.")
        self.assertEqual(result.counts, {"ssn": 1})

    def test_account_number_next_to_account_wording(self):
        """A sort code or a policy number has no checksum to validate against,
        so the word beside it is the only evidence there is."""
        result = run("Account number: 00123456789")
        self.assertEqual(result.text, "Account number: [account]")
        self.assertEqual(result.counts, {"account": 1})

    def test_a_bare_digit_run_with_no_wording_is_left_alone(self):
        self.assertEqual(run("Reference 00123456789 shipped.").counts, {})


class LuhnTest(unittest.TestCase):
    def test_luhn_accepts_real_card_numbers(self):
        for number in ("4111111111111111", "5500000000000004",
                       "378282246310005", "6011111111111117"):
            self.assertTrue(scrub.luhn(number), number)

    def test_luhn_rejects_a_digit_run_that_is_only_card_shaped(self):
        self.assertFalse(scrub.luhn("1234567890123"))

    def test_an_order_number_of_card_length_is_not_redacted(self):
        """The whole reason for the checksum: order numbers, database ids and
        ISBN-ish runs are card-shaped and are not cards."""
        result = run("Order 1234567890123 shipped, tracking 4000123456789010.")
        self.assertEqual(result.counts, {})
        self.assertIn("1234567890123", result.text)

    def test_a_mistyped_iban_is_not_treated_as_one(self):
        """mod-97 does for IBANs what Luhn does for cards."""
        self.assertFalse(scrub.iban_valid("GB82 WEST 1234 5698 7654 31"))
        self.assertTrue(scrub.iban_valid("GB82 WEST 1234 5698 7654 32"))


class ProseTest(unittest.TestCase):
    """The false-positive budget. Each of these is a real shape that appears on
    pages this browser reads all day."""

    SAFE = (
        "The 2.1.0 release landed on 2024-01-15 and closed issue 4821.",
        "See RFC 7231 section 6.5.1 for the 404 status code.",
        "Prices were 100 200 3000 across the three columns.",
        "Commit a1b2c3d4e5f6 reverted in 9f8e7d6c5b4a.",
        "It ran in 1234.5678 seconds over 12345678 rows.",
        "Call the office and ask for the docs team.",
        "Latitude 51.5074, longitude -0.1278, elevation 11 m.",
        "The build number is 20240115.3 and the checksum is deadbeef.",
    )

    def test_ordinary_prose_is_returned_unchanged(self):
        for text in self.SAFE:
            result = run(text)
            self.assertEqual(result.text, text, text)
            self.assertEqual(result.counts, {}, text)

    def test_a_page_with_nothing_personal_reports_nothing(self):
        self.assertFalse(run("Just some words."))


class ShapeTest(unittest.TestCase):
    def test_placeholders_are_stable_across_runs(self):
        """The same input must scrub to the same string every time: the answer
        quotes these back, and a placeholder that moved between two calls would
        make two answers about one page disagree."""
        text = "a@b.com paid with 4111 1111 1111 1111 from +44 20 7946 0958."
        first, second = run(text), run(text)
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.counts, second.counts)
        self.assertEqual(first.text,
                         "[email] paid with [card] from [phone].")

    def test_repeated_values_all_become_the_same_placeholder(self):
        result = run("ada@example.com, ada@example.com, bob@example.com")
        self.assertEqual(result.text, "[email], [email], [email]")
        self.assertEqual(result.counts, {"email": 3})

    def test_counts_are_per_category_and_total(self):
        result = run("ada@example.com 555.123.4567 4111 1111 1111 1111 "
                     "bob@example.com")
        self.assertEqual(result.counts, {"email": 2, "phone": 1, "card": 1})
        self.assertEqual(result.total, 4)

    def test_scrubbing_is_idempotent(self):
        """Every agent turn re-sends the whole transcript, so already-scrubbed
        text passes through this function again and must not change."""
        once = run("ada@example.com called 555.123.4567").text
        self.assertEqual(run(once).text, once)

    def test_describe_reads_as_a_sentence(self):
        self.assertEqual(scrub.describe({"email": 3, "card": 1}),
                         "3 emails, 1 card number redacted")
        self.assertEqual(scrub.describe({"email": 1}), "1 email redacted")
        self.assertEqual(scrub.describe({}), "")

    def test_none_and_empty_are_survivable(self):
        self.assertEqual(run(None).text, "")
        self.assertEqual(run("").text, "")


class KnobTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get(scrub.SCRUB_ENV)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop(scrub.SCRUB_ENV, None)
        else:
            os.environ[scrub.SCRUB_ENV] = self.saved

    def test_default_is_on(self):
        os.environ.pop(scrub.SCRUB_ENV, None)
        self.assertTrue(scrub.enabled())
        self.assertEqual(scrub.scrub("ada@example.com").text, "[email]")

    def test_cb_scrub_0_disables_it(self):
        os.environ[scrub.SCRUB_ENV] = "0"
        self.assertFalse(scrub.enabled())
        result = scrub.scrub("ada@example.com and 4111 1111 1111 1111")
        self.assertEqual(result.text, "ada@example.com and 4111 1111 1111 1111")
        self.assertEqual(result.counts, {})

    def test_the_other_off_spellings(self):
        for word in ("off", "no", "false", "OFF"):
            self.assertFalse(scrub.enabled(word), word)
        for word in ("", "1", "on", "yes", "banana"):
            self.assertTrue(scrub.enabled(word), word)


if __name__ == "__main__":
    unittest.main(verbosity=2)
