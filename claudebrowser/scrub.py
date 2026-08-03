"""Redact obvious personal data out of page text before it leaves the machine.

Ask, TL;DR, Research and the agent loop all put the text of whatever page you
are on into a request to Anthropic. If that page is a bank statement, a webmail
inbox or a filled-in form, personal data is uploaded that nobody consciously
decided to upload. This module is the filter on that path: regex and a couple of
checksums, no model, no network, no GTK -- so it is testable without a display
and costs nothing on a page that has none of this in it.

**Precision over recall, deliberately.** A scrubber that mangles ordinary prose
is worse than no scrubber at all, because the answers stop matching the page the
user is looking at and they stop trusting the feature -- and an answer they do
not trust is one they will not use, which protects nothing. So every pattern
here is either self-validating (Luhn for cards, mod-97 for IBANs) or shaped
tightly enough that ordinary text does not hit it, and anything that would need
judgement is left alone.

What it deliberately does NOT try to catch:

- **Names.** "Lucas" and "Fedora" are the same shape to a regex. Detecting a
  person's name needs a model, which this project does not have and will not
  add (see CLAUDE.md's rejected architectures).
- **Street addresses.** Same problem, plus every country writes them
  differently. "12 Mill Lane" and "12 pull requests" differ only in vocabulary.
- **Dates of birth**, which are indistinguishable from any other date.
- **Bare 9-digit numbers as SSNs.** Only the hyphenated form is taken; an
  unpunctuated run of nine digits is far more often an order or part number.
- **Free-text medical, financial or legal detail.** It is personal, and it is
  prose; there is nothing to match on.

The honest summary for a user is therefore: this stops the *structured*
identifiers -- the ones that are dangerous precisely because they are copyable
-- and it does not make an arbitrary page anonymous.

Each hit becomes a typed placeholder (`[email]`, `[card]`) rather than being
deleted, so the model can still reason about the shape of the text: "the invoice
lists two email addresses" survives, the addresses do not.
"""

import os
import re

#: The environment knob, following the CB_* convention. Default is ON: the
#: privacy-preserving default is the one that costs the user nothing to get
#: wrong, and `CB_SCRUB=0` is one variable away for anyone who wants raw text.
SCRUB_ENV = "CB_SCRUB"

#: Category -> placeholder. Stable strings, because they end up in an answer the
#: user reads and in whatever they copy out of it.
PLACEHOLDERS = {
    "email": "[email]",
    "phone": "[phone]",
    "card": "[card]",
    "iban": "[iban]",
    "ssn": "[ssn]",
    "account": "[account]",
}

#: Plurals for the one-line notice. English is irregular enough that a bare
#: "+s" would produce "2 ibans", which reads as a typo rather than a category.
_LABELS = {
    "email": ("email", "emails"),
    "phone": ("phone number", "phone numbers"),
    "card": ("card number", "card numbers"),
    "iban": ("IBAN", "IBANs"),
    "ssn": ("SSN", "SSNs"),
    "account": ("account number", "account numbers"),
}

#: Order matters: a later pattern must not be able to eat part of what an
#: earlier one would have matched whole. Emails go first because they contain
#: digit runs and dots that the number patterns would otherwise chew on, and
#: phone numbers go last because their shape is the loosest of the six.
ORDER = ("email", "iban", "ssn", "card", "account", "phone")

# A TLD of two or more letters is required, so `user@localhost` and the `@`
# handles that litter social pages are left alone.
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})*"
                   r"\.[A-Za-z]{2,24}\b")

# ISO 13616: two letters, two check digits, then up to 30 alphanumerics. The
# shape alone matches plenty of ordinary uppercase tokens (a git SHA prefixed
# with two letters, a product code), so the mod-97 checksum decides.
IBAN = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}"
                  r"(?:[ ]?[A-Z0-9]{1,3})?)(?![A-Za-z0-9])")

# Hyphenated only, and only the ranges the SSA actually issues: area 000, 666
# and 900-999 are never assigned, nor group 00 or serial 0000. Without those
# exclusions this matches version strings and part numbers like `000-00-0000`.
SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")

# 13 to 19 digits, optionally grouped by single spaces or hyphens. The
# lookarounds mean a 24-digit reference number is not matched at all rather
# than being matched in part, and Luhn throws out the order numbers and
# database ids that survive the shape test.
# The trailing `(?![ -]\d)` is load-bearing and was found by a test: without it
# the engine happily matches the *first* 16 digits of a longer grouped run --
# the tail of a phone number plus the head of the card after it -- which fails
# Luhn and, having consumed the region, hides the real card behind it.
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])(?![ -]\d)")

# A digit run that is only interesting because of the word next to it. This is
# the catch-all for the national and bank-specific formats there is no checksum
# for: sort codes, routing numbers, policy and membership numbers. The wording
# is the evidence, so the list stays short and literal -- "number" on its own is
# not enough, or every numbered list on the web becomes an account number.
ACCOUNT_WORDS = (r"account|acct|a/c|iban|bic|swift|sort[\s-]?code|routing|aba|"
                 r"card|policy|membership|customer|national insurance|nino|"
                 r"tax\s?(?:id|file)|vat|passport|licen[cs]e")
# The `(?<!\[)` is not decoration: `[card]` and `[iban]` are placeholders this
# module has already written, and without it a redacted card turns the digits
# after it -- the next field on the page, or the phone number in the same line
# -- into an "account number" by association with our own output.
ACCOUNT = re.compile(
    r"(?i)(?P<label>(?<!\[)\b(?:%s)\b[^\n:=]{0,24}?[:=#\s]\s*)"
    r"(?P<value>(?<![\d-])[A-Z]{0,2}\d[\d -]{4,30}\d)(?![\d-])" % ACCOUNT_WORDS)

# Either an international +CC number, or the North-American shapes, and never a
# bare run of digits: the separators (or the leading +) are what distinguish a
# phone number from a quantity. The unparenthesised form takes a dot or a hyphen
# and *not* a space, which is the one concession recall makes to precision here:
# `555 123 4567` is a phone number, but so is every third row of a numeric
# table, and `100 200 3000` becoming `[phone]` is the kind of mangling that
# makes an answer stop matching the page. This is still the loosest pattern of
# the six, which is why it runs last, over text the others have already taken
# their own matches out of.
PHONE = re.compile(r"""
    (?<![\w+])
    (?:
        \+\d{1,3}[\s.-]?(?:\(\d{1,4}\)[\s.-]?)?\d{2,4}(?:[\s.-]?\d{2,5}){1,4}
      | \(\d{3}\)[\s.-]?\d{3}[\s.-]\d{4}
      | \d{3}[.-]\d{3}[.-]\d{4}
    )
    (?!\w)
""", re.VERBOSE)


def enabled(raw=None):
    """Is scrubbing on? Anything but an explicit off value means yes.

    Same shape as `agent.pace_scale`: a typo in the variable falls back to the
    default rather than to the dangerous reading of it. Here the dangerous
    reading is "off", so only the words that unambiguously mean off count.
    """
    if raw is None:
        raw = os.environ.get(SCRUB_ENV, "")
    return (raw or "").strip().lower() not in ("0", "off", "no", "false")


def luhn(digits):
    """The check digit banks put on card numbers. Not security -- it exists to
    catch a mistyped digit -- but it is exactly the filter needed here: nine in
    ten random digit runs of card length fail it, so order numbers, database
    ids and phone-number concatenations stop being 'cards'."""
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = ord(char) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_valid(candidate):
    """ISO 7064 mod-97: move the first four characters to the end, map letters
    to numbers, and the whole thing must be 1 mod 97."""
    text = candidate.replace(" ", "").upper()
    if not (15 <= len(text) <= 34):
        return False
    rotated = text[4:] + text[:4]
    total = 0
    for char in rotated:
        if char.isdigit():
            total = (total * 10 + (ord(char) - 48)) % 97
        elif "A" <= char <= "Z":
            total = (total * 100 + (ord(char) - 55)) % 97
        else:
            return False
    return total == 1


class Result:
    """Scrubbed text plus what was taken out of it.

    A count per category rather than the matches themselves, on purpose: the
    point of this module is that the personal data goes no further, and handing
    the UI a list of the exact strings that were found would put them straight
    back into a log line or a panel card.
    """

    def __init__(self, text, counts):
        self.text = text
        self.counts = counts

    @property
    def total(self):
        return sum(self.counts.values())

    def __bool__(self):
        return bool(self.counts)


def scrub(text, on=None):
    """Redact `text`. Returns a `Result`; never raises on any input.

    `on` overrides the environment, for callers that have already decided (and
    for tests). Nothing here is logged or printed -- the whole point is that the
    matched values stop existing outside this function.
    """
    if text is None:
        return Result("", {})
    if not (enabled() if on is None else on):
        return Result(text, {})

    counts = {}

    def take(kind):
        counts[kind] = counts.get(kind, 0) + 1
        return PLACEHOLDERS[kind]

    out = text
    for kind in ORDER:
        if kind == "email":
            out = EMAIL.sub(lambda m: take("email"), out)
        elif kind == "iban":
            out = IBAN.sub(
                lambda m: take("iban") if iban_valid(m.group(1)) else m.group(0), out)
        elif kind == "ssn":
            out = SSN.sub(lambda m: take("ssn"), out)
        elif kind == "card":
            def card_sub(match):
                digits = re.sub(r"[ -]", "", match.group(0))
                if 13 <= len(digits) <= 19 and luhn(digits):
                    return take("card")
                return match.group(0)
            out = CARD.sub(card_sub, out)
        elif kind == "account":
            out = ACCOUNT.sub(
                lambda m: m.group("label") + take("account"), out)
        elif kind == "phone":
            out = PHONE.sub(lambda m: take("phone"), out)
    return Result(out, counts)


def describe(counts):
    """"3 emails, 1 card number redacted" -- or "" when nothing was touched.

    The UI shows this verbatim. It says what happened rather than that
    "privacy protection is active", because a number the user can check against
    the page is the only version of this they can correct when it is wrong.
    """
    if not counts:
        return ""
    parts = []
    for kind in ORDER:
        n = counts.get(kind, 0)
        if n:
            singular, plural = _LABELS[kind]
            parts.append("%d %s" % (n, singular if n == 1 else plural))
    return ", ".join(parts) + " redacted"
