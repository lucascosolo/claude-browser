"""files -- which directories the agent's file operations may touch.

Every fixture is a temporary directory; containment is judged on realpaths, so
the symlink cases are the ones that matter.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claudebrowser import files  # noqa: E402


class Roots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = os.path.realpath(self.tmp.name)

    def test_default_expands_home(self):
        with mock.patch.dict(os.environ, {"HOME": self.t}):
            got = files.roots(None)
        self.assertEqual(got, [os.path.realpath(os.path.join(self.t, "Downloads")),
                               os.path.realpath(os.path.join(self.t, ".cache/claude-browser"))])

    def test_blank_means_default(self):
        with mock.patch.dict(os.environ, {"HOME": self.t}):
            self.assertEqual(files.roots("  "), files.roots(files.DEFAULT))

    def test_split_strip_skip_empty_keep_order(self):
        a, b = os.path.join(self.t, "a"), os.path.join(self.t, "b")
        self.assertEqual(files.roots(" %s :: %s " % (b, a)), [b, a])

    def test_relative_entries_dropped_and_all_dropped_falls_back(self):
        a = os.path.join(self.t, "a")
        self.assertEqual(files.roots("rel/x:" + a), [a])
        with mock.patch.dict(os.environ, {"HOME": self.t}):
            self.assertEqual(files.roots("rel/x:also/rel"), files.roots(files.DEFAULT))

    def test_entries_are_realpathed(self):
        real = os.path.join(self.t, "real")
        os.mkdir(real)
        link = os.path.join(self.t, "link")
        os.symlink(real, link)
        self.assertEqual(files.roots(link), [real])


class Containment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = os.path.realpath(self.tmp.name)
        self.root = os.path.join(t, "root")
        self.out = os.path.join(t, "out")
        os.mkdir(self.root)
        os.mkdir(self.out)
        self.roots = [self.root]

    def touch(self, *parts):
        p = os.path.join(*parts)
        open(p, "w").close()
        return p

    def test_relative_or_empty_is_refused_as_not_absolute(self):
        for fn in (files.contain_read, files.contain_write):
            for bad in ("rel/x.txt", ""):
                with self.assertRaises(ValueError) as c:
                    fn(bad, self.roots)
                self.assertIn("absolute", str(c.exception))

    def test_read_inside_root_and_root_itself(self):
        f = self.touch(self.root, "a.txt")
        self.assertEqual(files.contain_read(f, self.roots), f)
        self.assertEqual(files.contain_read(self.root, self.roots), self.root)

    def test_read_outside_is_refused_with_message(self):
        f = self.touch(self.out, "a.txt")
        with self.assertRaises(ValueError) as c:
            files.contain_read(f, self.roots)
        self.assertIn("outside CB_AGENT_DIRS", str(c.exception))
        self.assertIn(f, str(c.exception))

    def test_sibling_prefix_trick_is_refused(self):
        evil = self.root + "-evil"
        os.mkdir(evil)
        f = self.touch(evil, "x")
        for fn in (files.contain_read, files.contain_write):
            with self.assertRaises(ValueError):
                fn(f, self.roots)

    def test_symlink_inside_pointing_out_is_refused_for_read(self):
        target = self.touch(self.out, "secret")
        link = os.path.join(self.root, "l")
        os.symlink(target, link)
        with self.assertRaises(ValueError):
            files.contain_read(link, self.roots)

    def test_symlink_outside_pointing_in_is_allowed_for_read(self):
        target = self.touch(self.root, "ok")
        link = os.path.join(self.out, "l")
        os.symlink(target, link)
        self.assertEqual(files.contain_read(link, self.roots), target)

    def test_root_that_is_a_symlink_works(self):
        link = os.path.join(self.out, "rootlink")
        os.symlink(self.root, link)
        f = self.touch(self.root, "a")
        self.assertEqual(files.contain_read(f, [link]), f)
        self.assertEqual(files.contain_write(os.path.join(self.root, "n"), [link]),
                         os.path.join(self.root, "n"))

    def test_write_new_file_in_root(self):
        p = os.path.join(self.root, "new.pdf")
        self.assertEqual(files.contain_write(p, self.roots), p)

    def test_write_to_root_itself_is_refused(self):
        with self.assertRaises(ValueError) as c:
            files.contain_write(self.root, self.roots)
        self.assertIn("outside CB_AGENT_DIRS", str(c.exception))

    def test_write_outside_and_dotdot_escape_refused(self):
        for p in (os.path.join(self.out, "x"), self.root + "/../x"):
            with self.assertRaises(ValueError) as c:
                files.contain_write(p, self.roots)
            self.assertIn("outside CB_AGENT_DIRS", str(c.exception))

    def test_write_through_parent_symlink_out_is_refused(self):
        os.symlink(self.out, os.path.join(self.root, "d"))
        with self.assertRaises(ValueError):
            files.contain_write(os.path.join(self.root, "d", "x"), self.roots)

    def test_write_through_file_symlink_out_is_refused(self):
        existing = self.touch(self.out, "e")
        os.symlink(existing, os.path.join(self.root, "live"))
        os.symlink(os.path.join(self.out, "missing"), os.path.join(self.root, "dangling"))
        for name in ("live", "dangling"):
            with self.assertRaises(ValueError):
                files.contain_write(os.path.join(self.root, name), self.roots)


if __name__ == "__main__":
    unittest.main()
