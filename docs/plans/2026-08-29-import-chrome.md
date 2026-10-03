# Import Chrome Implementation Plan

> **For agentic workers:** if this plan has more than ~4 tasks, use the `scoped-delivery` skill to implement it in 1-3 task chunks via fresh subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a one-shot, user-triggered `import-chrome` operation that pulls
bookmarks, history and saved passwords out of the local Google Chrome
profile into Claude Browser's existing storage, with no plaintext credential
ever touching disk, a log line, or an HTTP response.

**Architecture:** a new GTK-free module (`chrome_import.py`) does all the
reading and decrypting and returns/yields plain tuples; `store.py` gets two
gap-filling importer methods; `Browser.api_import_chrome` is the only thing
that calls both and the one place a decrypted password is held, for exactly
long enough to hand it to `passwords.Vault.save`. `mcp=False` throughout —
this is a user-run migration action, never an agent tool.

**Tech Stack:** Python 3 stdlib only (`json`, `sqlite3`, `hashlib.pbkdf2_hmac`,
`tempfile`, `shutil`), plus `gi.repository.Secret` (libsecret) — the one
existing non-stdlib dependency this project already has, already imported by
`passwords.py`. AES-128-CBC is implemented from scratch in a small new module
(`_aes.py`) since neither the stdlib nor `gi.repository.Secret` provides a
general-purpose block cipher, and this project takes no new dependency to get
one — see "Why a hand-written AES" in Task 2.

## Global Constraints

- No decrypted password value is ever written to disk, logged, printed, put
  in an HTTP response, or returned from more than one function frame.
  `read_passwords` is a generator; `api_import_chrome` is the only consumer.
- `mcp=False` on the `import-chrome` Op — never agent-invocable.
- Existing bookmarks/history/passwords are never overwritten by an import —
  an import only fills gaps (`INSERT OR IGNORE` / a pre-read existing-set
  check, matching how `bookmarks`/`history` are already keyed by URL).
- No new third-party dependency beyond `gi.repository.Secret`, which
  `passwords.py`/`profile.py` already use. No `pip install` anywhere in this
  plan.
- No test ever calls the real Secret Service / keyring. Every test that needs
  a "secret" passes a hand-built, hardcoded test value directly into the
  function under test.
- Chrome's single `Default` profile only. No profile picker.
- Scope is passwords, bookmarks and history only — no cookies, no session
  transfer, no other browsers.

---

### Task 1: `claudebrowser/chrome_import.py` — bookmarks and history readers

No crypto in this task — Chrome's `Bookmarks` file is plain JSON and its
`History` file is a plain, unencrypted SQLite database. This task also
establishes the module's shared epoch-conversion helper that Task 3 reuses.

**Files:**
- Create: `claudebrowser/chrome_import.py`
- Test: `tests/test_chrome_import.py`

**Interfaces:**
- Produces: `CHROME_PROFILE_DEFAULT` (str constant, default profile path),
  `_chrome_time_to_epoch(chrome_us) -> int`, `read_bookmarks(profile_dir=CHROME_PROFILE_DEFAULT) -> list[tuple[str, str, int]]`
  (url, title, added_epoch), `read_history(profile_dir=CHROME_PROFILE_DEFAULT, limit=100000) -> list[tuple[str, str, int, int]]`
  (url, title, visit_count, last_visit_epoch).

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_chrome_import.py"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import chrome_import  # noqa: E402

# Chrome's epoch is microseconds since 1601-01-01. This is the well-known
# constant for 2024-01-01T00:00:00Z in that epoch, used across every fixture
# in this file so every test agrees on what "epoch 1704067200" means.
CHROME_TS_2024_01_01 = 13344825600000000
UNIX_TS_2024_01_01 = 1704067200


class ReadBookmarksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_dir = self.tmp.name

    def _write_bookmarks(self, data):
        with open(os.path.join(self.profile_dir, "Bookmarks"), "w") as f:
            json.dump(data, f)

    def test_reads_urls_from_bar_and_nested_folder(self):
        self._write_bookmarks({
            "roots": {
                "bookmark_bar": {
                    "type": "folder",
                    "children": [
                        {"type": "url", "name": "Example",
                         "url": "https://example.com/",
                         "date_added": str(CHROME_TS_2024_01_01)},
                        {"type": "folder", "name": "Sub", "children": [
                            {"type": "url", "name": "Nested",
                             "url": "https://nested.example/",
                             "date_added": str(CHROME_TS_2024_01_01)},
                        ]},
                    ],
                },
                "other": {"type": "folder", "children": []},
            }
        })
        rows = chrome_import.read_bookmarks(self.profile_dir)
        urls = {r[0] for r in rows}
        self.assertEqual(urls, {"https://example.com/", "https://nested.example/"})
        self.assertEqual(len(rows), 2)

    def test_converts_chrome_epoch_to_unix_epoch(self):
        self._write_bookmarks({
            "roots": {"bookmark_bar": {"type": "folder", "children": [
                {"type": "url", "name": "Example", "url": "https://example.com/",
                 "date_added": str(CHROME_TS_2024_01_01)},
            ]}}
        })
        rows = chrome_import.read_bookmarks(self.profile_dir)
        self.assertEqual(rows[0], ("https://example.com/", "Example", UNIX_TS_2024_01_01))

    def test_no_urls_is_an_empty_list(self):
        self._write_bookmarks({"roots": {"bookmark_bar": {"type": "folder", "children": []}}})
        self.assertEqual(chrome_import.read_bookmarks(self.profile_dir), [])


class ReadHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_dir = self.tmp.name
        self.history_path = os.path.join(self.profile_dir, "History")
        conn = sqlite3.connect(self.history_path)
        conn.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
            "visit_count INTEGER, last_visit_time INTEGER)")
        conn.execute(
            "INSERT INTO urls (url, title, visit_count, last_visit_time) "
            "VALUES (?, ?, ?, ?)",
            ("https://example.com/", "Example", 5, CHROME_TS_2024_01_01))
        conn.commit()
        conn.close()

    def test_reads_and_converts_rows(self):
        rows = chrome_import.read_history(self.profile_dir)
        self.assertEqual(rows, [("https://example.com/", "Example", 5, UNIX_TS_2024_01_01)])

    def test_does_not_leave_a_temp_copy_behind(self):
        before = set(Path(tempfile.gettempdir()).glob("cb-chrome-import-*"))
        chrome_import.read_history(self.profile_dir)
        after = set(Path(tempfile.gettempdir()).glob("cb-chrome-import-*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_chrome_import -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'claudebrowser.chrome_import'`

- [ ] **Step 3: Write the implementation**

```python
"""claudebrowser/chrome_import.py

Read-only import from a local Google Chrome profile: bookmarks, history, and
saved passwords. GTK-free so it is testable without a display, following the
existing convention of playbooks.py / profile.py / fills.py.

Every function here reads from a *copy* of Chrome's files, never the live
one -- Chrome holds History and Login Data open, and a plain filesystem copy
sidesteps that without needing Chrome closed. The copy lives in a
TemporaryDirectory and is always cleaned up, even on an exception, so a crash
mid-import never leaves a copy of the user's browsing history sitting in /tmp.

read_passwords is the one function that touches encrypted data, and it is
this module's entire reason for existing carefully: see chrome_import.py's
own docstring on read_passwords for the constraint that governs it.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from hashlib import pbkdf2_hmac

from claudebrowser import _aes

CHROME_PROFILE_DEFAULT = os.path.expanduser("~/.config/google-chrome/Default")

# Seconds between the Windows FILETIME epoch (1601-01-01) Chrome timestamps
# use and the Unix epoch (1970-01-01) every other part of this browser uses.
CHROME_EPOCH_OFFSET_SECONDS = 11644473600

# Chrome's own schema for the "Chrome Safe Storage" libsecret item (from
# Chromium's components/os_crypt/key_storage_libsecret.cc), confirmed against
# this machine's keyring via a metadata-only search -- see read_passwords.
CHROME_SAFE_STORAGE_SCHEMA = "chrome_libsecret_os_crypt_password_v2"
CHROME_APPLICATION_CANDIDATES = ("chrome", "google-chrome", "chromium")

# Chromium's own documented fallback: if no OS keyring was available the
# first time Chrome ran, it encrypts with this literal passphrase instead of
# refusing to store passwords at all.
CHROME_FALLBACK_SECRET = b"peanuts"


def _chrome_time_to_epoch(chrome_us):
    """Chrome stores timestamps as microseconds since 1601-01-01. 0 means
    "never set" in Chrome's own data and maps to Unix epoch 0 here too."""
    chrome_us = int(chrome_us or 0)
    if not chrome_us:
        return 0
    return int(chrome_us / 1_000_000 - CHROME_EPOCH_OFFSET_SECONDS)


def read_bookmarks(profile_dir=CHROME_PROFILE_DEFAULT):
    """Chrome's Bookmarks file is plain JSON, never encrypted. Walks every
    root (bookmark_bar, other, synced) and returns (url, title, added_epoch)
    for every url-type node, folders included at any depth."""
    path = os.path.join(profile_dir, "Bookmarks")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []

    def walk(node):
        if node.get("type") == "url":
            out.append((
                node.get("url", ""),
                node.get("name", ""),
                _chrome_time_to_epoch(node.get("date_added", 0)),
            ))
        for child in node.get("children", ()):
            walk(child)

    for root in data.get("roots", {}).values():
        if isinstance(root, dict):
            walk(root)
    return out


def read_history(profile_dir=CHROME_PROFILE_DEFAULT, limit=100000):
    """Chrome's History file is a plain, unencrypted SQLite database that
    Chrome typically holds open -- copied to a temp dir first so a read never
    waits on or disturbs Chrome's own lock, and the copy never outlives this
    call."""
    path = os.path.join(profile_dir, "History")
    with tempfile.TemporaryDirectory(prefix="cb-chrome-import-") as tmp:
        copy_path = os.path.join(tmp, "History")
        shutil.copy2(path, copy_path)
        conn = sqlite3.connect(copy_path)
        try:
            rows = conn.execute(
                "SELECT url, title, visit_count, last_visit_time "
                "FROM urls ORDER BY last_visit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            conn.close()
    return [
        (url, title or "", visits or 0, _chrome_time_to_epoch(last_visit))
        for url, title, visits, last_visit in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_chrome_import -v`
Expected: PASS (all `ReadBookmarksTest` / `ReadHistoryTest` cases). The
import of `claudebrowser._aes` will fail until Task 2 lands — if running
Task 1 standalone before Task 2 exists, comment out the `from claudebrowser
import _aes` line and the `CHROME_SAFE_STORAGE_SCHEMA` block temporarily, or
simply do Task 2 first (it has no dependency on Task 1 and is safe to land
first).

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/chrome_import.py tests/test_chrome_import.py
git commit -m "chrome_import: read Chrome bookmarks and history"
```

---

### Task 2: `claudebrowser/_aes.py` — pure-Python AES-128-CBC

**Why a hand-written AES:** this project is stdlib-only plus the one
existing `gi.repository.Secret` dependency. Python's stdlib has no block
cipher (`hashlib`/`hmac` cover hashing and PBKDF2, nothing more), and
libsecret has no bearing on this — it protects Chrome's *key*, not the
*password bytes* Chrome encrypted with it. The two remaining options were
shelling out to the system `openssl` binary (rejected: passing the AES key
as a subprocess argument exposes it in `/proc/<pid>/cmdline` to any other
user on the machine for the life of the call) or `ctypes` into `libcrypto`
(rejected: brittle across distro `.so` naming/versions for a decrypt this
small). AES-128 is a bounded, fully public, well-tested algorithm; this
module implements decrypt *and* encrypt (encrypt is needed by Task 3's own
tests, to build a realistic encrypted fixture without ever touching a real
keyring) and is verified against the public FIPS-197 Appendix C.1 known-answer
test vector, not just a round-trip, so a bug that is symmetric in both
directions (e.g. the same wrong shift direction in both ShiftRows and
InvShiftRows) cannot hide behind "encrypt-then-decrypt gives back the input".

**Files:**
- Create: `claudebrowser/_aes.py`
- Test: `tests/test_aes.py`

**Interfaces:**
- Produces: `aes128_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes`,
  `aes128_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes`,
  `pad_pkcs7(data: bytes, block_size=16) -> bytes`, `strip_pkcs7(data: bytes) -> bytes`
  (raises `ValueError` on invalid padding). `key` and `iv` must each be 16
  bytes; `plaintext`/`ciphertext` must be a multiple of 16 bytes (callers pad
  first with `pad_pkcs7`).
- Consumes: nothing — no dependency on Task 1.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_aes.py

FIPS-197 Appendix C.1 is the standard AES-128 known-answer test: a single
16-byte block, a fixed public key, and its documented ciphertext. Used here
as CBC with an all-zero IV over exactly one block, which is mathematically
identical to plain ECB on that block -- the simplest way to pin this
implementation against a value nobody in this codebase computed by hand.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import _aes  # noqa: E402

FIPS_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
FIPS_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_CIPHERTEXT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
ZERO_IV = b"\x00" * 16


class FipsKnownAnswerTest(unittest.TestCase):
    def test_decrypt_matches_fips_vector(self):
        result = _aes.aes128_cbc_decrypt(FIPS_KEY, ZERO_IV, FIPS_CIPHERTEXT)
        self.assertEqual(result, FIPS_PLAINTEXT)

    def test_encrypt_matches_fips_vector(self):
        result = _aes.aes128_cbc_encrypt(FIPS_KEY, ZERO_IV, FIPS_PLAINTEXT)
        self.assertEqual(result, FIPS_CIPHERTEXT)


class RoundTripTest(unittest.TestCase):
    def test_encrypt_then_decrypt_recovers_arbitrary_plaintext(self):
        key = b"0123456789abcdef"
        iv = b" " * 16
        plaintext = b"a chrome saved password, 37 chars!!"
        padded = _aes.pad_pkcs7(plaintext)
        ciphertext = _aes.aes128_cbc_encrypt(key, iv, padded)
        recovered = _aes.strip_pkcs7(_aes.aes128_cbc_decrypt(key, iv, ciphertext))
        self.assertEqual(recovered, plaintext)

    def test_multi_block_round_trip(self):
        key = b"0123456789abcdef"
        iv = b"\x01" * 16
        plaintext = b"x" * 100
        padded = _aes.pad_pkcs7(plaintext)
        ciphertext = _aes.aes128_cbc_encrypt(key, iv, padded)
        recovered = _aes.strip_pkcs7(_aes.aes128_cbc_decrypt(key, iv, ciphertext))
        self.assertEqual(recovered, plaintext)


class Pkcs7Test(unittest.TestCase):
    def test_pad_then_strip_is_identity(self):
        for length in range(0, 33):
            data = bytes(range(length % 256)) if length else b""
            self.assertEqual(_aes.strip_pkcs7(_aes.pad_pkcs7(data)), data)

    def test_strip_rejects_invalid_padding(self):
        with self.assertRaises(ValueError):
            _aes.strip_pkcs7(b"not valid pkcs7 padding!")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_aes -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: No module named 'claudebrowser._aes'`

- [ ] **Step 3: Write the implementation**

```python
"""claudebrowser/_aes.py

A from-scratch AES-128-CBC implementation. See Task 2 of
docs/plans/2026-08-29-import-chrome.md for why this exists instead of a
dependency or a subprocess call to openssl.

Verified against the public FIPS-197 Appendix C.1 known-answer test vector
in tests/test_aes.py -- both directions independently, not just a round trip.
"""

SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
# Built by inversion, never a second hand-transcribed table: INV_SBOX[SBOX[x]] == x
# for every x, by construction, so there is nothing here to transcribe wrong.
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36]

NB = 4   # words per state
NK = 4   # words per AES-128 key
NR = 10  # AES-128 rounds


def _gmul(a, b):
    """Multiplication in GF(2^8) with AES's reduction polynomial (x^8+x^4+x^3+x+1,
    0x11B) -- the standard "Russian peasant" construction."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        carry = a & 0x80
        a = (a << 1) & 0xFF
        if carry:
            a ^= 0x1B
        b >>= 1
    return p


def _sub_word(word):
    return bytes(SBOX[b] for b in word)


def _rot_word(word):
    return word[1:] + word[:1]


def _key_expansion(key):
    w = [key[4 * i:4 * i + 4] for i in range(NK)]
    for i in range(NK, NB * (NR + 1)):
        temp = w[i - 1]
        if i % NK == 0:
            temp = _sub_word(_rot_word(temp))
            temp = bytes([temp[0] ^ RCON[i // NK - 1]]) + temp[1:]
        w.append(bytes(a ^ b for a, b in zip(w[i - NK], temp)))
    return [b"".join(w[4 * r:4 * r + 4]) for r in range(NR + 1)]


def _add_round_key(state, round_key):
    return bytes(a ^ b for a, b in zip(state, round_key))


def _shift_rows(state):
    s = list(state)
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c + r) % 4)]
    return bytes(out)


def _inv_shift_rows(state):
    s = list(state)
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
    return bytes(out)


def _mix_column(c):
    c0, c1, c2, c3 = c
    return bytes([
        _gmul(c0, 2) ^ _gmul(c1, 3) ^ c2 ^ c3,
        c0 ^ _gmul(c1, 2) ^ _gmul(c2, 3) ^ c3,
        c0 ^ c1 ^ _gmul(c2, 2) ^ _gmul(c3, 3),
        _gmul(c0, 3) ^ c1 ^ c2 ^ _gmul(c3, 2),
    ])


def _inv_mix_column(c):
    c0, c1, c2, c3 = c
    return bytes([
        _gmul(c0, 0x0e) ^ _gmul(c1, 0x0b) ^ _gmul(c2, 0x0d) ^ _gmul(c3, 0x09),
        _gmul(c0, 0x09) ^ _gmul(c1, 0x0e) ^ _gmul(c2, 0x0b) ^ _gmul(c3, 0x0d),
        _gmul(c0, 0x0d) ^ _gmul(c1, 0x09) ^ _gmul(c2, 0x0e) ^ _gmul(c3, 0x0b),
        _gmul(c0, 0x0b) ^ _gmul(c1, 0x0d) ^ _gmul(c2, 0x09) ^ _gmul(c3, 0x0e),
    ])


def _mix_columns(state, fn):
    cols = [state[4 * c:4 * c + 4] for c in range(4)]
    return b"".join(fn(col) for col in cols)


def _encrypt_block(plaintext, round_keys):
    state = _add_round_key(plaintext, round_keys[0])
    for rnd in range(1, NR):
        state = bytes(SBOX[b] for b in state)
        state = _shift_rows(state)
        state = _mix_columns(state, _mix_column)
        state = _add_round_key(state, round_keys[rnd])
    state = bytes(SBOX[b] for b in state)
    state = _shift_rows(state)
    return _add_round_key(state, round_keys[NR])


def _decrypt_block(ciphertext, round_keys):
    state = _add_round_key(ciphertext, round_keys[NR])
    for rnd in range(NR - 1, 0, -1):
        state = _inv_shift_rows(state)
        state = bytes(INV_SBOX[b] for b in state)
        state = _add_round_key(state, round_keys[rnd])
        state = _mix_columns(state, _inv_mix_column)
    state = _inv_shift_rows(state)
    state = bytes(INV_SBOX[b] for b in state)
    return _add_round_key(state, round_keys[0])


def _check_args(key, iv, data):
    if len(key) != 16:
        raise ValueError("AES-128 requires a 16-byte key")
    if len(iv) != 16:
        raise ValueError("AES-CBC requires a 16-byte IV")
    if len(data) % 16 != 0:
        raise ValueError("data must be a multiple of the 16-byte block size")


def aes128_cbc_encrypt(key, iv, plaintext):
    _check_args(key, iv, plaintext)
    round_keys = _key_expansion(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(plaintext), 16):
        block = bytes(a ^ b for a, b in zip(plaintext[i:i + 16], prev))
        enc = _encrypt_block(block, round_keys)
        out.extend(enc)
        prev = enc
    return bytes(out)


def aes128_cbc_decrypt(key, iv, ciphertext):
    _check_args(key, iv, ciphertext)
    round_keys = _key_expansion(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        dec = _decrypt_block(block, round_keys)
        out.extend(a ^ b for a, b in zip(dec, prev))
        prev = block
    return bytes(out)


def pad_pkcs7(data, block_size=16):
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def strip_pkcs7(data):
    if not data:
        raise ValueError("empty data has no PKCS7 padding")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or pad_len > len(data):
        raise ValueError("invalid PKCS7 padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad_len]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_aes -v`
Expected: PASS — both FIPS-vector tests must pass exactly as written; if
either fails, there is a transcription error in `SBOX` or a logic error in
`_shift_rows`/`_inv_shift_rows`/`_mix_column`/`_inv_mix_column` — do not
adjust the test to match a wrong output.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/_aes.py tests/test_aes.py
git commit -m "_aes: pure-Python AES-128-CBC, verified against the FIPS-197 vector"
```

---

### Task 3: `chrome_import.read_passwords` — the encrypted path

**Files:**
- Modify: `claudebrowser/chrome_import.py` (add `read_passwords` and
  `_chrome_safe_storage_secret`)
- Test: `tests/test_chrome_import.py` (add `ReadPasswordsTest`)

**Interfaces:**
- Consumes: `_aes.aes128_cbc_decrypt`, `_aes.aes128_cbc_encrypt` (test only),
  `_aes.pad_pkcs7` (test only), `_aes.strip_pkcs7` (Task 1's
  `CHROME_SAFE_STORAGE_SCHEMA` / `CHROME_APPLICATION_CANDIDATES` /
  `CHROME_FALLBACK_SECRET` constants, already added in Task 1's file).
- Produces: `read_passwords(profile_dir=CHROME_PROFILE_DEFAULT, secret=None) -> Iterator[tuple[str, str, str]]`
  (origin, username, password) — a generator, never a list, per the global
  constraint that a decrypted value never accumulates. `secret`, if given, is
  used as-is (bytes) and no keyring is touched — this is the seam every test
  in this task uses instead of a real Secret Service call.

**Precondition check — already performed, recorded here so this task does
not repeat it:** a safe, metadata-only enumeration was run against this
machine's Secret Service during design (`Secret.Collection.for_alias_sync(...).get_items()`,
which returned zero items). Investigation traced this to the Secret Service
provider on this machine being KDE's `ksecretd`/`kwalletd6`, not GNOME
Keyring — its freedesktop-secrets compatibility layer does not surface items
through the `Collection`/`Item` enumeration API the way GNOME Keyring does.
`passwords.py`'s own `SecretBackend`, which uses the *simple password* API
(`password_search_sync` / `password_lookup_sync` / `password_store_sync`
against a declared `Secret.Schema`) rather than `Collection.get_items()`,
was independently verified end-to-end (save → read → delete, disposable test
credential) against this same KWallet-backed service and round-tripped
correctly. **Conclusion for this task: use the simple password API
(`password_search_sync`/`password_lookup_sync`), never `Collection.get_items()`,**
matching `SecretBackend`'s own precedent exactly. The implementation below
already reflects this. Do not re-run any exploratory schema discovery
against the real keyring — `_chrome_safe_storage_secret` tries the
well-documented Chromium schema/attribute candidates via `password_search_sync`
(`SearchFlags.ALL`, no `LOAD_SECRETS` — never reveals a value) and only calls
`password_lookup_sync` once a specific candidate is confirmed to match,
falling back to Chromium's own documented literal fallback passphrase
(`b"peanuts"`) if no keyring item matches at all — this mirrors Chrome's own
real behavior on a machine where no OS keyring was available the first time
Chrome ran, so this code is correct in either case without needing to know in
advance which one this machine turns out to be. If this candidate list turns
out not to match on the machine actually running the import (checkable after
this task lands via `python3 -c "from claudebrowser import chrome_import as c; print(c._chrome_safe_storage_secret())"`
run manually and interactively by the user, never by an agent, since this is
exactly the class of action Claude Code's own permission classifier already
flagged once this session), extend `CHROME_APPLICATION_CANDIDATES` rather
than changing the lookup mechanism.

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_chrome_import.py, inside a new class:

from claudebrowser import _aes  # noqa: E402


class ReadPasswordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile_dir = self.tmp.name
        self.test_secret = b"a-test-only-secret-value"
        self._build_fixture([
            ("https://example.com/login", "alice", "hunter2-example"),
            ("https://other.example/login", "bob", "another-real-password"),
        ])

    def _encrypt(self, plaintext):
        key = pbkdf2_hmac("sha1", self.test_secret, b"saltysalt", 1, dklen=16)
        padded = _aes.pad_pkcs7(plaintext.encode("utf-8"))
        return b"v10" + _aes.aes128_cbc_encrypt(key, b" " * 16, padded)

    def _build_fixture(self, entries):
        path = os.path.join(self.profile_dir, "Login Data")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE logins (origin_url TEXT, username_value TEXT, "
            "password_value BLOB)")
        for origin, username, password in entries:
            conn.execute(
                "INSERT INTO logins (origin_url, username_value, password_value) "
                "VALUES (?, ?, ?)",
                (origin, username, self._encrypt(password)))
        conn.commit()
        conn.close()

    def test_decrypts_every_row_with_the_given_secret(self):
        from hashlib import pbkdf2_hmac  # local import matches module usage
        rows = list(chrome_import.read_passwords(self.profile_dir, secret=self.test_secret))
        self.assertEqual(set(rows), {
            ("https://example.com/login", "alice", "hunter2-example"),
            ("https://other.example/login", "bob", "another-real-password"),
        })

    def test_wrong_secret_skips_rows_instead_of_raising(self):
        rows = list(chrome_import.read_passwords(self.profile_dir, secret=b"totally-wrong-secret"))
        self.assertEqual(rows, [])

    def test_read_passwords_is_a_generator(self):
        result = chrome_import.read_passwords(self.profile_dir, secret=self.test_secret)
        self.assertTrue(hasattr(result, "__next__"))
```

Note: `pbkdf2_hmac` needs importing at module level in the test file too —
add `from hashlib import pbkdf2_hmac` to the top-level imports alongside the
existing ones, and remove the redundant local import shown inline above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_chrome_import -v`
Expected: FAIL — `AttributeError: module 'claudebrowser.chrome_import' has no attribute 'read_passwords'`

- [ ] **Step 3: Write the implementation**

```python
# Append to claudebrowser/chrome_import.py:

def _chrome_safe_storage_secret():
    """The Chrome Safe Storage passphrase: from the keyring if Chrome ever
    stored one there, else Chromium's own documented fallback for a machine
    that had no keyring available when Chrome first ran.

    Metadata-only search first (password_search_sync, never LOAD_SECRETS) so
    a wrong application-name guess can never fetch a value this call does not
    need -- password_lookup_sync (which does return the secret) is only
    reached once a specific attribute set is already confirmed to match an
    item. See Task 3's "Precondition check" in the implementation plan for
    why this uses the simple password API rather than Collection.get_items().
    """
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret

    schema = Secret.Schema.new(
        CHROME_SAFE_STORAGE_SCHEMA, Secret.SchemaFlags.NONE,
        {"application": Secret.SchemaAttributeType.STRING})
    for application in CHROME_APPLICATION_CANDIDATES:
        attrs = {"application": application}
        found = Secret.password_search_sync(schema, attrs, Secret.SearchFlags.ALL, None)
        if found:
            secret = Secret.password_lookup_sync(schema, attrs, None)
            if secret:
                return secret.encode("utf-8")
    return CHROME_FALLBACK_SECRET


def read_passwords(profile_dir=CHROME_PROFILE_DEFAULT, secret=None):
    """Yields (origin, username, password) for every decryptable row in
    Chrome's Login Data. A generator, never a list -- see the module
    docstring and the plan's global constraints on why a decrypted value
    never accumulates anywhere.

    `secret` overrides the keyring lookup entirely when given (bytes) -- this
    is the seam every test in this suite uses; production code always omits
    it and gets Chrome's real key via _chrome_safe_storage_secret().

    A row whose prefix is not v10/v11, or that fails to decrypt under the
    given secret (wrong key, corrupt row, a future Chrome scheme this
    function does not recognise), is silently skipped rather than raised --
    one bad row must never abort the whole import.
    """
    if secret is None:
        secret = _chrome_safe_storage_secret()
    path = os.path.join(profile_dir, "Login Data")
    with tempfile.TemporaryDirectory(prefix="cb-chrome-import-") as tmp:
        copy_path = os.path.join(tmp, "Login Data")
        shutil.copy2(path, copy_path)
        conn = sqlite3.connect(copy_path)
        try:
            rows = conn.execute(
                "SELECT origin_url, username_value, password_value FROM logins"
            ).fetchall()
        finally:
            conn.close()
    key = pbkdf2_hmac("sha1", secret, b"saltysalt", 1, dklen=16)
    for origin, username, encrypted in rows:
        if not encrypted or encrypted[:3] not in (b"v10", b"v11"):
            continue
        try:
            padded = _aes.aes128_cbc_decrypt(key, b" " * 16, encrypted[3:])
            password = _aes.strip_pkcs7(padded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        yield origin, username or "", password
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_chrome_import -v`
Expected: PASS — all of `ReadBookmarksTest`, `ReadHistoryTest`, `ReadPasswordsTest`.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/chrome_import.py tests/test_chrome_import.py
git commit -m "chrome_import: decrypt saved passwords via the confirmed libsecret schema"
```

---

### Task 4: `store.py` — `import_bookmarks` / `import_history`

`Store._write` (background mode) is fire-and-forget and returns nothing, so
"how many were actually new" is computed by reading the existing URL set
first and diffing against it in Python — not from a write's row count.

**Files:**
- Modify: `claudebrowser/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from Task 1-3 — entries are plain tuples the caller
  (Task 5) gets from `chrome_import`.
- Produces: `Store.import_bookmarks(entries: Iterable[tuple[str, str, int]]) -> int`,
  `Store.import_history(entries: Iterable[tuple[str, str, int, int]]) -> int`
  — both return the count of rows actually inserted (pre-existing URLs are
  left untouched and not counted).

- [ ] **Step 1: Write the failing tests**

```python
# Append to tests/test_store.py, inside StoreTest or a new class using the
# same setUp pattern (store.Store(":memory:", background=False)):

class ImportTest(unittest.TestCase):
    def setUp(self):
        self.s = store.Store(":memory:", background=False)
        self.addCleanup(self.s.close)

    def test_import_bookmarks_fills_gaps_only(self):
        self.s.bookmark("https://existing.example/", "Existing")
        inserted = self.s.import_bookmarks([
            ("https://existing.example/", "Should Not Overwrite", 1000),
            ("https://new.example/", "New", 2000),
        ])
        self.assertEqual(inserted, 1)
        rows = {r["url"]: r["title"] for r in self.s.bookmarks()}
        self.assertEqual(rows["https://existing.example/"], "Existing")
        self.assertEqual(rows["https://new.example/"], "New")

    def test_import_history_fills_gaps_only(self):
        self.s.record("https://existing.example/", "Existing")
        inserted = self.s.import_history([
            ("https://existing.example/", "Should Not Overwrite", 99, 1000),
            ("https://new.example/", "New", 5, 2000),
        ])
        self.assertEqual(inserted, 1)
        rows = {r["url"]: r["visits"] for r in self.s.history()}
        self.assertEqual(rows["https://existing.example/"], 1)
        self.assertEqual(rows["https://new.example/"], 5)

    def test_import_is_idempotent(self):
        entries = [("https://a.example/", "A", 1000)]
        first = self.s.import_bookmarks(entries)
        second = self.s.import_bookmarks(entries)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_store -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'import_bookmarks'`

- [ ] **Step 3: Write the implementation**

```python
# Add to claudebrowser/store.py, in the "-- bookmarks --" section (after
# bookmarks()) and the "-- history --" section (after prune()) respectively:

    def import_bookmarks(self, entries):
        """Fill-gaps-only import: entries are (url, title, added_epoch), and
        a url already present is left completely untouched -- an import must
        never overwrite something the user saved natively. Returns the count
        actually inserted."""
        existing = {r["url"] for r in self._query("SELECT url FROM bookmarks")}
        inserted = 0
        for url, title, added in entries:
            if not recordable(url) or url in existing:
                continue
            self._write(
                "INSERT OR IGNORE INTO bookmarks (url, title, added) VALUES (?, ?, ?)",
                (url, title or "", added))
            existing.add(url)
            inserted += 1
        return inserted

    def import_history(self, entries):
        """Fill-gaps-only import: entries are (url, title, visits,
        last_visit_epoch). Same untouched-if-present rule as
        import_bookmarks. Returns the count actually inserted."""
        existing = {r["url"] for r in self._query("SELECT url FROM history")}
        inserted = 0
        for url, title, visits, last_visit in entries:
            if not recordable(url) or url in existing:
                continue
            self._write(
                "INSERT OR IGNORE INTO history (url, title, visits, last_visit) "
                "VALUES (?, ?, ?, ?)",
                (url, title or "", visits, last_visit))
            existing.add(url)
            inserted += 1
        return inserted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_store -v`
Expected: PASS — all `ImportTest` cases, and the full existing `test_store.py`
suite still green.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/store.py tests/test_store.py
git commit -m "store: add gap-filling import_bookmarks/import_history"
```

---

### Task 5: `api.py` Op + `Browser.api_import_chrome` wiring

**Files:**
- Modify: `claudebrowser/api.py` (add the `import-chrome` Op, at the end of
  the OPS list alongside `downloads`)
- Modify: `claudebrowser/browser.py` (add `api_import_chrome`, near the other
  `api_bookmark*`/`api_history*` methods)
- Test: `tests/test_offline.py` (extend `TestApiRegistry`)

**Interfaces:**
- Consumes: `chrome_import.read_bookmarks`, `chrome_import.read_history`,
  `chrome_import.read_passwords` (Tasks 1 and 3), `chrome_import.CHROME_PROFILE_DEFAULT`,
  `Store.import_bookmarks`/`Store.import_history` (Task 4), `passwords.Vault.save`
  (already exists — `self.vault` is set in `Browser.__init__`, confirmed at
  `browser.py:537`).
- Produces: `Op("import-chrome", "/import-chrome", "POST", ..., mcp=False)`
  in `api.py`; `Browser.api_import_chrome(kinds, done)` in `browser.py`,
  reachable as `cbctl import-chrome [--passwords] [--bookmarks] [--history]`.

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_offline.py, inside TestApiRegistry (self.api is set to the
# api module in that class's setUp -- see its existing test_op_shapes and
# test_mcp_exposes_every_agent_facing_op for the exact pattern this mirrors):

    def test_import_chrome_is_registered_and_not_an_mcp_tool(self):
        op = next(o for o in self.api.OPS if o.name == "import-chrome")
        self.assertEqual(op.method, "POST")
        self.assertFalse(op.mcp)
        self.assertFalse(op.tab)
        tools = {t["name"] for t in self.api.mcp_tools()}
        self.assertNotIn("browser_import-chrome", tools)
```

Add this as its own new test method in `TestApiRegistry`; also add
`self.assertNotIn("browser_import-chrome", tools)` to the existing
`test_mcp_exposes_every_agent_facing_op` method (around line 562, next to
the other `assertNotIn` lines for `bookmark-remove`/`history-clear`) so the
one test that already enumerates every non-agent-facing op stays the single
place that list is maintained.

- [ ] **Step 2: Run test to verify it fails**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_offline -v`
Expected: FAIL — `StopIteration` (no Op named `import-chrome` yet).

- [ ] **Step 3: Write the implementation**

`claudebrowser/api.py` currently ends (confirmed by direct read) with:

```python
    Op("downloads", "/downloads", "GET",
       "List this session's downloads and their status.",
       call=lambda c, a: ("api_downloads", ()), tab=False),
]

BY_NAME = {op.name: op for op in OPS}
BY_ROUTE = {op.route: op for op in OPS}

#: cbctl exposes `shot` and `go` as friendlier names for two operations whose
#: API names read badly at a shell prompt.
CLI_ALIASES = {"shot": "screenshot", "go": "navigate"}


def mcp_tools():
    return [op.mcp_tool() for op in OPS if op.mcp]
```

Insert the new `Op` right before the closing `]` of `OPS`, and the
`_import_chrome_kinds` helper right after `mcp_tools()` at the end of the
file:

```python
    Op("downloads", "/downloads", "GET",
       "List this session's downloads and their status.",
       call=lambda c, a: ("api_downloads", ()), tab=False),

    # Not an MCP tool: this is a user-triggered one-time migration of their
    # own Chrome data, never a step an agent should take toward some other
    # goal, and it is the one op in this table that ever holds a decrypted
    # password -- see chrome_import.py and Browser.api_import_chrome.
    Op("import-chrome", "/import-chrome", "POST",
       "Import bookmarks, history and saved passwords from the local Google "
       "Chrome profile. Existing entries are never overwritten -- this only "
       "fills gaps. Reports counts only, never values.",
       params=[Param("passwords", "boolean", "Import saved passwords.",
                     cli="opt", default=False),
               Param("bookmarks", "boolean", "Import bookmarks.",
                     cli="opt", default=False),
               Param("history", "boolean", "Import browsing history.",
                     cli="opt", default=False)],
       call=lambda c, a: ("api_import_chrome", (_import_chrome_kinds(a),)),
       tab=False, mcp=False, timeout=90),
]

BY_NAME = {op.name: op for op in OPS}
BY_ROUTE = {op.route: op for op in OPS}

#: cbctl exposes `shot` and `go` as friendlier names for two operations whose
#: API names read badly at a shell prompt.
CLI_ALIASES = {"shot": "screenshot", "go": "navigate"}


def mcp_tools():
    return [op.mcp_tool() for op in OPS if op.mcp]


def _import_chrome_kinds(args):
    """No flag given at all means "all three" -- cbctl import-chrome with no
    arguments is the common case, not an error asking the user to spell out
    every kind."""
    requested = {k for k in ("passwords", "bookmarks", "history")
                 if _truthy(args.get(k), False)}
    return sorted(requested) if requested else ["passwords", "bookmarks", "history"]
```

`_truthy(value, default=True)` already exists in this file at line 124 (used
by `settings`'s `reset` param) — reuse it as-is, called here with
`default=False` so an omitted flag means "not requested" rather than
"requested".

```python
# Add to claudebrowser/browser.py, near api_downloads:

    def api_import_chrome(self, kinds, done):
        """Import bookmarks, history and saved passwords from the local
        Chrome profile. Not an MCP tool -- see the Op in api.py.

        Each kind is attempted independently: a failed password decrypt must
        never abort the bookmarks or history import, and vice versa. Only
        counts ever leave this function -- a decrypted password lives in a
        local variable for exactly as long as the loop body that hands it to
        self.vault.save, never longer, never returned, never logged.
        """
        from claudebrowser import chrome_import

        profile_dir = chrome_import.CHROME_PROFILE_DEFAULT
        if not os.path.isdir(profile_dir):
            return done({"ok": False,
                         "error": "Chrome profile not found at %s" % profile_dir})

        result = {"ok": True}

        if "bookmarks" in kinds:
            try:
                entries = chrome_import.read_bookmarks(profile_dir)
                inserted = self.store.import_bookmarks(entries) if self.store else 0
                result["bookmarks"] = {"imported": inserted,
                                        "skipped": len(entries) - inserted}
            except (OSError, ValueError) as exc:
                result["bookmarks"] = {"error": str(exc)}

        if "history" in kinds:
            try:
                entries = chrome_import.read_history(profile_dir)
                inserted = self.store.import_history(entries) if self.store else 0
                result["history"] = {"imported": inserted,
                                      "skipped": len(entries) - inserted}
            except (OSError, ValueError) as exc:
                result["history"] = {"error": str(exc)}

        if "passwords" in kinds:
            imported = skipped = failed = 0
            try:
                for origin, username, password in chrome_import.read_passwords(profile_dir):
                    if self.vault is None:
                        failed += 1
                        continue
                    existing = self.vault.credentials(origin)
                    if any(e["username"] == username for e in existing):
                        skipped += 1
                        continue
                    if self.vault.save(origin, username, password):
                        imported += 1
                    else:
                        failed += 1
                result["passwords"] = {"imported": imported, "skipped": skipped,
                                        "failed": failed}
            except (OSError, ValueError) as exc:
                result["passwords"] = {"error": str(exc)}

        if self.store is not None:
            self.store.flush()
        self._reload_internal()
        done(result)
```

`os` is already imported at the top of `browser.py` (line 11, confirmed by
direct read) — do not add a duplicate import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `CB_AUTOSTART=0 python3 -m unittest tests.test_offline -v`
Expected: PASS. Then run the full suite:
`CB_AUTOSTART=0 python3 -m unittest discover -s tests` and confirm 0 new
failures against the pre-Task-5 baseline. Also run `python3 -m py_compile
claudebrowser/*.py` since `browser.py` is not otherwise imported by every
test environment.

- [ ] **Step 5: Commit**

```bash
git add claudebrowser/api.py claudebrowser/browser.py tests/test_offline.py
git commit -m "api/browser: wire import-chrome (mcp=False), counts only"
```

---

## Execution

5 tasks — use `scoped-delivery`. Suggested chunking:

- **Chunk 1:** Task 1 + Task 2 (no dependency on each other; both are
  self-contained new files). These can also be dispatched as two concurrent
  single-task chunks if speed matters more than agent-count.
- **Chunk 2:** Task 3 (depends on Task 2's `_aes.py` existing) + Task 4
  (independent of everything else, bundled here only for chunk efficiency).
- **Chunk 3:** Task 5 (depends on Tasks 1, 3 and 4 all being committed —
  it is the only task touching `browser.py`, so it must not run concurrently
  with anything else that does).

After Chunk 3 lands and the full suite is green, the user can run
`./cbctl import-chrome` (all three kinds by default) to bring in their
Patreon password (and everything else) from Chrome, then have Claude Browser
log into Patreon natively via its existing autofill.
