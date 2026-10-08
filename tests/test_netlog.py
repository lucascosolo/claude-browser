"""netlog.Log: the per-tab request ring behind the network op and idle waits.

GTK-free; every clock value is passed in explicitly.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudebrowser import netlog  # noqa: E402

KEYS = {"url", "status", "mime", "bytes", "started_ms", "duration_ms", "error", "main"}


class TestEntries(unittest.TestCase):
    def setUp(self):
        self.log = netlog.Log()

    def test_start_returns_an_entry_of_the_documented_shape(self):
        e = self.log.start("k", "https://a/x", main=True, now=10.0)
        self.assertEqual(set(e), KEYS)
        self.assertEqual((e["url"], e["status"], e["mime"], e["bytes"]),
                         ("https://a/x", None, None, 0))
        self.assertIsInstance(e["started_ms"], int)
        self.assertIsNone(e["duration_ms"])
        self.assertIsNone(e["error"])
        self.assertIs(e["main"], True)
        self.assertIs(self.log.start("j", "u", now=1.0)["main"], False)

    def test_response_and_cumulative_data(self):
        self.log.start("k", "u", now=1.0)
        self.log.response("k", 200, "text/html")
        self.log.data("k", 100)
        self.log.data("k", 28)
        e = self.log.entries()[0]
        self.assertEqual((e["status"], e["mime"], e["bytes"]), (200, "text/html", 128))

    def test_finish_computes_duration_from_the_explicit_clock(self):
        self.log.start("k", "u", now=10.0)
        self.log.finish("k", now=10.2504)
        self.assertEqual(self.log.entries()[0]["duration_ms"], 250)

    def test_fail_records_error_and_duration(self):
        self.log.start("k", "u", now=5.0)
        self.log.fail("k", "boom", now=6.5)
        e = self.log.entries()[0]
        self.assertEqual((e["error"], e["duration_ms"]), ("boom", 1500))

    def test_first_of_finish_or_fail_wins(self):
        self.log.start("a", "u", now=0.0)
        self.log.finish("a", now=1.0)
        self.log.fail("a", "late", now=2.0)
        self.log.finish("a", now=3.0)
        e = self.log.entries()[0]
        self.assertEqual((e["error"], e["duration_ms"]), (None, 1000))
        self.log.start("b", "v", now=0.0)
        self.log.fail("b", "first", now=1.0)
        self.log.finish("b", now=2.0)
        e = self.log.entries()[1]
        self.assertEqual((e["error"], e["duration_ms"]), ("first", 1000))

    def test_restarting_a_key_replaces_the_entry(self):
        self.log.start("k", "old", now=0.0)
        self.log.start("k", "new", now=1.0)
        self.assertEqual([e["url"] for e in self.log.entries()], ["new"])

    def test_unknown_keys_are_ignored_and_do_not_touch_last_activity(self):
        self.assertIsNone(self.log.last_activity)
        self.log.response("nope", 200, "x")
        self.log.data("nope", 5)
        self.log.finish("nope", now=1.0)
        self.log.fail("nope", "e", now=2.0)
        self.assertEqual(self.log.entries(), [])
        self.assertIsNone(self.log.last_activity)
        self.log.start("k", "u", now=3.0)
        self.log.finish("nope", now=9.0)
        self.assertEqual(self.log.last_activity, 3.0)

    def test_last_activity_follows_known_events(self):
        self.log.start("k", "u", now=1.0)
        self.assertEqual(self.log.last_activity, 1.0)
        self.log.finish("k", now=2.0)
        self.assertEqual(self.log.last_activity, 2.0)
        self.log.start("j", "u", now=3.0)
        self.log.fail("j", "e", now=4.0)
        self.assertEqual(self.log.last_activity, 4.0)


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.log = netlog.Log()
        for i, u in enumerate(["https://a/1.js", "https://a/2.css", "https://b/3.js"]):
            self.log.start(str(i), u, now=float(i))

    def urls(self, **kw):
        return [e["url"] for e in self.log.entries(**kw)]

    def test_oldest_first(self):
        self.assertEqual(self.urls(), ["https://a/1.js", "https://a/2.css", "https://b/3.js"])

    def test_pattern_is_a_regex_search(self):
        self.assertEqual(self.urls(pattern=r"\.js$"), ["https://a/1.js", "https://b/3.js"])

    def test_bad_regex_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.log.entries(pattern="(")

    def test_limit_keeps_the_newest_matches(self):
        self.assertEqual(self.urls(limit=2), ["https://a/2.css", "https://b/3.js"])
        self.assertEqual(self.urls(pattern=r"\.js", limit=1), ["https://b/3.js"])

    def test_limit_none_means_one_hundred(self):
        log = netlog.Log(limit=500)
        for i in range(150):
            log.start(str(i), "u%d" % i, now=0.0)
        got = log.entries(limit=None)
        self.assertEqual(len(got), 100)
        self.assertEqual(got[-1]["url"], "u149")

    def test_returned_entries_are_copies(self):
        self.log.entries()[0]["url"] = "tampered"
        self.log.entries()[0]["bytes"] = 99
        self.assertEqual(self.urls()[0], "https://a/1.js")
        self.assertEqual(self.log.entries()[0]["bytes"], 0)


class TestRing(unittest.TestCase):
    def test_oldest_is_dropped_past_the_limit(self):
        log = netlog.Log(limit=3)
        for i in range(5):
            log.start(str(i), "u%d" % i, now=float(i))
        self.assertEqual([e["url"] for e in log.entries()], ["u2", "u3", "u4"])

    def test_a_dropped_entry_is_forgotten(self):
        log = netlog.Log(limit=2)
        log.start("0", "u0", now=0.0)
        log.start("1", "u1", now=1.0)
        log.start("2", "u2", now=2.0)
        log.response("0", 200, "x")
        log.data("0", 10)
        log.finish("0", now=9.0)
        self.assertEqual([e["url"] for e in log.entries()], ["u1", "u2"])
        self.assertEqual(log.last_activity, 2.0)

    def test_a_dropped_entry_no_longer_counts_as_in_flight(self):
        log = netlog.Log(limit=1)
        log.start("0", "u0", now=0.0)
        log.start("1", "u1", now=1.0)
        log.finish("1", now=2.0)
        self.assertTrue(log.quiet_for(100, now=10.0))


class TestClearAndReset(unittest.TestCase):
    def setUp(self):
        self.log = netlog.Log()

    def test_clear_empties(self):
        self.log.start("k", "u", now=0.0)
        self.log.clear()
        self.assertEqual(self.log.entries(), [])

    def test_reset_without_keep_drops_everything(self):
        self.log.start("a", "u", now=0.0)
        self.log.start("b", "v", now=0.0)
        self.log.reset_for_navigation()
        self.assertEqual(self.log.entries(), [])
        self.assertTrue(self.log.quiet_for(0, now=1.0))

    def test_reset_with_keep_preserves_only_that_entry_and_keeps_it_live(self):
        self.log.start("a", "old", now=0.0)
        main = self.log.start("m", "https://main", main=True, now=1.0)
        self.log.reset_for_navigation(keep=main)
        got = self.log.entries()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["url"], "https://main")
        self.assertFalse(self.log.quiet_for(0, now=100.0))
        self.log.response("m", 200, "text/html")
        self.log.data("m", 7)
        self.log.finish("m", now=2.0)
        e = self.log.entries()[0]
        self.assertEqual((e["status"], e["bytes"], e["duration_ms"]), (200, 7, 1000))
        self.assertTrue(self.log.quiet_for(0, now=100.0))

    def test_entries_dropped_by_reset_are_forgotten(self):
        self.log.start("a", "old", now=0.0)
        main = self.log.start("m", "main", now=1.0)
        self.log.reset_for_navigation(keep=main)
        self.log.response("a", 200, "x")
        self.log.finish("a", now=5.0)
        self.assertEqual([e["url"] for e in self.log.entries()], ["main"])
        self.log.finish("m", now=6.0)
        self.assertTrue(self.log.quiet_for(0, now=6.0))


class TestQuiet(unittest.TestCase):
    def test_fresh_log_is_quiet(self):
        self.assertTrue(netlog.Log().quiet_for(500, now=0.0))

    def test_in_flight_blocks_regardless_of_time(self):
        log = netlog.Log()
        log.start("k", "u", now=0.0)
        self.assertFalse(log.quiet_for(500, now=1000.0))

    def test_boundary_is_inclusive(self):
        log = netlog.Log()
        log.start("k", "u", now=10.0)
        log.finish("k", now=10.0)
        self.assertFalse(log.quiet_for(500, now=10.499))
        self.assertTrue(log.quiet_for(500, now=10.5))
        self.assertTrue(log.quiet_for(500, now=11.0))

    def test_failed_request_is_not_in_flight(self):
        log = netlog.Log()
        log.start("k", "u", now=0.0)
        log.fail("k", "e", now=1.0)
        self.assertTrue(log.quiet_for(500, now=2.0))

    def test_activity_resets_the_quiet_window(self):
        log = netlog.Log()
        log.start("k", "u", now=0.0)
        log.data("k", 1)  # default now is monotonic; pin via finish below
        log.finish("k", now=5.0)
        self.assertFalse(log.quiet_for(1000, now=5.5))


if __name__ == "__main__":
    unittest.main()
