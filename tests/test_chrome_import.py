"""tests/test_chrome_import.py"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from hashlib import pbkdf2_hmac
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import _aes  # noqa: E402
from claudebrowser import chrome_import  # noqa: E402

# Chrome's epoch is microseconds since 1601-01-01. This is the well-known
# constant for 2024-01-01T00:00:00Z in that epoch, used across every fixture
# in this file so every test agrees on what "epoch 1704067200" means.
CHROME_TS_2024_01_01 = 13348540800000000
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

    def test_neither_file_present_is_an_empty_list(self):
        """A profile with no bookmarks yet has no Bookmarks file at all --
        that must not be treated as an error."""
        self.assertEqual(chrome_import.read_bookmarks(self.profile_dir), [])

    def test_reads_account_bookmarks_when_classic_file_is_absent(self):
        """A signed-in profile with account bookmark storage keeps its
        bookmarks in AccountBookmarks instead of the classic Bookmarks file --
        seen on a real profile during manual testing of this feature."""
        with open(os.path.join(self.profile_dir, "AccountBookmarks"), "w") as f:
            json.dump({"roots": {"bookmark_bar": {"type": "folder", "children": [
                {"type": "url", "name": "Account", "url": "https://account.example/",
                 "date_added": str(CHROME_TS_2024_01_01)},
            ]}}}, f)
        rows = chrome_import.read_bookmarks(self.profile_dir)
        self.assertEqual(rows, [("https://account.example/", "Account", UNIX_TS_2024_01_01)])

    def test_merges_classic_and_account_bookmarks_deduping_by_url(self):
        self._write_bookmarks({"roots": {"bookmark_bar": {"type": "folder", "children": [
            {"type": "url", "name": "Local", "url": "https://local.example/",
             "date_added": str(CHROME_TS_2024_01_01)},
            {"type": "url", "name": "Shared", "url": "https://shared.example/",
             "date_added": str(CHROME_TS_2024_01_01)},
        ]}}})
        with open(os.path.join(self.profile_dir, "AccountBookmarks"), "w") as f:
            json.dump({"roots": {"bookmark_bar": {"type": "folder", "children": [
                {"type": "url", "name": "Account", "url": "https://account.example/",
                 "date_added": str(CHROME_TS_2024_01_01)},
                {"type": "url", "name": "Shared (dup)", "url": "https://shared.example/",
                 "date_added": str(CHROME_TS_2024_01_01)},
            ]}}}, f)
        rows = chrome_import.read_bookmarks(self.profile_dir)
        urls = [r[0] for r in rows]
        self.assertEqual(sorted(urls), sorted([
            "https://local.example/", "https://shared.example/", "https://account.example/"]))
        self.assertEqual(len(urls), 3, "a url in both files must be reported once")


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

    def _build_fixture(self, entries, filename="Login Data"):
        path = os.path.join(self.profile_dir, filename)
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
        rows = list(chrome_import.read_passwords(self.profile_dir, secret=self.test_secret))
        self.assertEqual(set(rows), {
            ("https://example.com/login", "alice", "hunter2-example"),
            ("https://other.example/login", "bob", "another-real-password"),
        })

    def test_also_reads_login_data_for_account(self):
        """A profile signed into a Google Account keeps account-synced logins
        in a second file, Login Data For Account -- seen on a real profile
        during manual testing, where nearly every saved password lived there
        and the classic Login Data file was empty."""
        self._build_fixture(
            [("https://patreon.example/login", "carol", "creator-secret")],
            filename="Login Data For Account")
        rows = set(chrome_import.read_passwords(self.profile_dir, secret=self.test_secret))
        self.assertIn(("https://patreon.example/login", "carol", "creator-secret"), rows)
        # And the original fixture's Login Data rows are still read too.
        self.assertIn(("https://example.com/login", "alice", "hunter2-example"), rows)

    def test_neither_file_present_yields_nothing(self):
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty_dir, ignore_errors=True)
        rows = list(chrome_import.read_passwords(empty_dir, secret=self.test_secret))
        self.assertEqual(rows, [])

    def test_wrong_secret_skips_rows_instead_of_raising(self):
        rows = list(chrome_import.read_passwords(self.profile_dir, secret=b"totally-wrong-secret"))
        self.assertEqual(rows, [])

    def test_read_passwords_is_a_generator(self):
        result = chrome_import.read_passwords(self.profile_dir, secret=self.test_secret)
        self.assertTrue(hasattr(result, "__next__"))


if __name__ == "__main__":
    unittest.main()
