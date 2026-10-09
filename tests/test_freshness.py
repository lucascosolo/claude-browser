"""freshness: the state token, the stale decision and the text diff."""

import unittest

from claudebrowser import freshness


class TestToken(unittest.TestCase):
    def test_epoch_and_mutations(self):
        self.assertEqual(freshness.token({"epoch": "ab1", "mutations": 7}), "ab1:7")

    def test_no_epoch_is_no_token(self):
        self.assertIsNone(freshness.token({"mutations": 3}))
        self.assertIsNone(freshness.token(None))
        self.assertIsNone(freshness.token({"epoch": ""}))

    def test_split(self):
        self.assertEqual(freshness.split("ab1:7"), ("ab1", 7))
        self.assertEqual(freshness.split(None), (None, 0))
        self.assertEqual(freshness.split("x:bad"), ("x", 0))


class TestStale(unittest.TestCase):
    def state(self, epoch="e1", mutations=0):
        return {"epoch": epoch, "mutations": mutations}

    def test_unseen_page_is_stale(self):
        ok, why = freshness.stale(None, self.state())
        self.assertTrue(ok)
        self.assertIn("not read", why)

    def test_same_token_is_fresh(self):
        seen = freshness.Seen("e1:4", at=100.0)
        self.assertEqual(freshness.stale(seen, self.state(mutations=4), now=10_000.0),
                         (False, ""))

    def test_navigation_is_always_stale(self):
        seen = freshness.Seen("e1:4", at=100.0)
        ok, why = freshness.stale(seen, self.state(epoch="e2", mutations=0), now=100.5)
        self.assertTrue(ok)
        self.assertIn("navigated", why)

    def test_recent_look_tolerates_mutations(self):
        seen = freshness.Seen("e1:4", at=100.0)
        self.assertEqual(freshness.stale(seen, self.state(mutations=9), now=105.0),
                         (False, ""))

    def test_old_look_on_a_moved_page_is_stale(self):
        seen = freshness.Seen("e1:4", at=100.0)
        ok, why = freshness.stale(seen, self.state(mutations=9), now=100.0 + freshness.FRESH_S)
        self.assertTrue(ok)
        self.assertIn("5 times", why)
        self.assertIn("%ds ago" % freshness.FRESH_S, why)

    def test_old_look_on_a_still_page_is_fresh(self):
        seen = freshness.Seen("e1:4", at=100.0)
        self.assertEqual(freshness.stale(seen, self.state(mutations=4), now=9_000.0),
                         (False, ""))

    def test_page_without_a_token_is_never_stale(self):
        self.assertEqual(freshness.stale(None, {"url": "about:blank"}), (False, ""))

    def test_fresh_window_is_configurable(self):
        seen = freshness.Seen("e1:1", at=0.0)
        self.assertTrue(freshness.stale(seen, self.state(mutations=2), now=3.0, fresh_s=2)[0])
        self.assertFalse(freshness.stale(seen, self.state(mutations=2), now=3.0, fresh_s=5)[0])


class TestDiff(unittest.TestCase):
    def test_added_and_removed_lines(self):
        d = freshness.diff("a\nb\nc", "a\nx\nc\nd")
        self.assertEqual(d["added"], ["x", "d"])
        self.assertEqual(d["removed"], ["b"])
        self.assertFalse(d["truncated"])

    def test_blank_lines_are_ignored(self):
        d = freshness.diff("a\n\n  \nb", "a\nb")
        self.assertEqual(d, {"added": [], "removed": [], "truncated": False})

    def test_none_is_empty(self):
        d = freshness.diff(None, "one\ntwo")
        self.assertEqual(d["added"], ["one", "two"])

    def test_cap_and_truncated(self):
        new = "\n".join("line %d" % i for i in range(100))
        d = freshness.diff("", new, cap=10)
        self.assertEqual(len(d["added"]), 10)
        self.assertTrue(d["truncated"])


if __name__ == "__main__":
    unittest.main()
