"""uploads.py path checking and pending hand-off, plus the upload JS builders."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudebrowser import extract, uploads  # noqa: E402

NASTY = "</script> x"


class TestCheckPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = os.path.join(self.tmp.name, "a.txt")
        self.b = os.path.join(self.tmp.name, "b.txt")
        for p in (self.a, self.b):
            with open(p, "w") as f:
                f.write("x")

    def test_single_path(self):
        self.assertEqual(uploads.check_paths(self.a), [self.a])

    def test_json_array_keeps_order(self):
        self.assertEqual(uploads.check_paths(json.dumps([self.b, self.a])), [self.b, self.a])

    def test_relative_path_names_the_offender(self):
        with self.assertRaises(ValueError) as cm:
            uploads.check_paths("rel/file.txt")
        self.assertIn("rel/file.txt", str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(ValueError):
            uploads.check_paths(os.path.join(self.tmp.name, "nope"))

    def test_directory(self):
        with self.assertRaises(ValueError):
            uploads.check_paths(self.tmp.name)

    def test_unreadable(self):
        with mock.patch.object(uploads.os, "access", return_value=False):
            with self.assertRaises(ValueError):
                uploads.check_paths(self.a)

    def test_empty_list_and_malformed_json(self):
        for raw in ("[]", "[not json", '["' + self.a + '"'):
            with self.assertRaises(ValueError, msg=raw):
                uploads.check_paths(raw)

    def test_one_bad_entry_refuses_the_lot(self):
        with self.assertRaises(ValueError):
            uploads.check_paths(json.dumps([self.a, "rel.txt"]))


class TestPending(unittest.TestCase):
    def test_take_once(self):
        p = uploads.Pending(ttl_s=10)
        p.put(1, ["/x"], now=100.0)
        self.assertEqual(p.take(1, now=101.0), ["/x"])
        self.assertIsNone(p.take(1, now=101.0))

    def test_unknown_tab(self):
        self.assertIsNone(uploads.Pending().take(9, now=1.0))

    def test_expiry(self):
        p = uploads.Pending(ttl_s=10)
        p.put(1, ["/x"], now=100.0)
        self.assertIsNone(p.take(1, now=110.5))
        p.put(1, ["/x"], now=100.0)
        self.assertEqual(p.take(1, now=110.0), ["/x"])

    def test_tabs_are_independent(self):
        p = uploads.Pending()
        p.put(1, ["/a"], now=0.0)
        p.put(2, ["/b"], now=0.0)
        self.assertEqual(p.take(2, now=1.0), ["/b"])
        self.assertEqual(p.take(1, now=1.0), ["/a"])

    def test_put_replaces(self):
        p = uploads.Pending()
        p.put(1, ["/a"], now=0.0)
        p.put(1, ["/b"], now=0.0)
        self.assertEqual(p.take(1, now=1.0), ["/b"])
        self.assertIsNone(p.take(1, now=1.0))


class TestUploadJs(unittest.TestCase):
    def test_builders_escape_the_selector(self):
        for fn in (extract.upload_click, extract.upload_check):
            js = fn(NASTY)
            self.assertIsInstance(js, str)
            self.assertIn(extract._js_str(NASTY), js, fn.__name__)
            self.assertNotIn("</script>", js, fn.__name__)

    def test_click_reports_multiple_and_checks_file(self):
        js = extract.upload_click("input")
        self.assertIn("multiple", js)
        self.assertIn("file", js)


if __name__ == "__main__":
    unittest.main()
