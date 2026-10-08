"""tests/test_aes.py

Known-answer tests for the from-scratch AES-128-CBC in claudebrowser/_aes.py.

Both directions are checked independently against the FIPS-197 Appendix C.1
vector, not just as a round trip: a round trip passes when encrypt and decrypt
share the same mistake, and a Chrome "Login Data" row is ciphertext produced by
*Chrome's* AES, so only agreement with the published vector means anything.

Rewritten on 2026-10-08 after the 2026-10-02 disk wipe: the original test file
was never carved; `_aes.py` itself came back from the recovery index.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claudebrowser import _aes  # noqa: E402

# FIPS-197, Appendix C.1 (AES-128). A single block, so with a zero IV CBC is
# the raw cipher and the vector applies unchanged.
FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PLAIN = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHER = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
ZERO_IV = bytes(16)

# NIST SP 800-38A, F.2.1 (CBC-AES128.Encrypt): four blocks, chained, so the
# IV and the feedback path are exercised rather than just the block cipher.
SP_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
SP_IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
SP_PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710")
SP_CIPHER = bytes.fromhex(
    "7649abac8119b246cee98e9b12e9197d"
    "5086cb9b507219ee95db113a917678b2"
    "73bed6b8e3c1743b7116e69e22229516"
    "3ff1caa1681fac09120eca307586e1a7")


class KnownAnswers(unittest.TestCase):
    def test_fips197_encrypt(self):
        self.assertEqual(_aes.aes128_cbc_encrypt(FIPS_KEY, ZERO_IV, FIPS_PLAIN), FIPS_CIPHER)

    def test_fips197_decrypt(self):
        self.assertEqual(_aes.aes128_cbc_decrypt(FIPS_KEY, ZERO_IV, FIPS_CIPHER), FIPS_PLAIN)

    def test_sp800_38a_cbc_encrypt(self):
        self.assertEqual(_aes.aes128_cbc_encrypt(SP_KEY, SP_IV, SP_PLAIN), SP_CIPHER)

    def test_sp800_38a_cbc_decrypt(self):
        self.assertEqual(_aes.aes128_cbc_decrypt(SP_KEY, SP_IV, SP_CIPHER), SP_PLAIN)

    def test_inverse_sbox_is_an_inverse(self):
        for x in range(256):
            self.assertEqual(_aes.INV_SBOX[_aes.SBOX[x]], x)


class Arguments(unittest.TestCase):
    def test_wrong_key_length_refused(self):
        with self.assertRaises(ValueError):
            _aes.aes128_cbc_encrypt(b"short", ZERO_IV, FIPS_PLAIN)

    def test_wrong_iv_length_refused(self):
        with self.assertRaises(ValueError):
            _aes.aes128_cbc_decrypt(FIPS_KEY, b"short", FIPS_CIPHER)

    def test_partial_block_refused(self):
        with self.assertRaises(ValueError):
            _aes.aes128_cbc_encrypt(FIPS_KEY, ZERO_IV, FIPS_PLAIN[:-1])


class Padding(unittest.TestCase):
    def test_pad_always_adds_a_full_block_when_aligned(self):
        padded = _aes.pad_pkcs7(b"x" * 16)
        self.assertEqual(len(padded), 32)
        self.assertEqual(padded[16:], bytes([16]) * 16)

    def test_round_trip(self):
        for n in range(0, 40):
            data = bytes(range(n))
            self.assertEqual(_aes.strip_pkcs7(_aes.pad_pkcs7(data)), data)

    def test_strip_refuses_bad_padding(self):
        for bad in (b"", b"abc\x00", b"abc\x11", b"ab\x03\x02", b"\x05"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _aes.strip_pkcs7(bad)


if __name__ == "__main__":
    unittest.main()
