"""dialogs.py: what to do with alert/confirm/prompt/beforeunload, and the log."""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claudebrowser import dialogs  # noqa: E402


class TestPolicy(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(dialogs.KINDS, ("alert", "confirm", "prompt", "beforeunload"))
        self.assertEqual(dialogs.POLICIES, ("auto", "ask"))

    def test_ask_is_the_only_non_default(self):
        for v in ("ask", " ASK ", "Ask"):
            self.assertEqual(dialogs.policy(v), "ask", v)
        for v in (None, "", "auto", "junk", 0):
            self.assertEqual(dialogs.policy(v), "auto", v)


class TestAnswer(unittest.TestCase):
    def test_auto(self):
        a = dialogs.answer
        self.assertEqual(a("alert", "auto"), ("close", None))
        self.assertEqual(a("confirm", "auto"), ("accept", True))
        self.assertEqual(a("prompt", "auto", "dflt"), ("accept", "dflt"))
        self.assertEqual(a("prompt", "auto"), ("accept", ""))
        self.assertEqual(a("beforeunload", "auto"), ("accept", True))

    def test_ask_defers_every_kind(self):
        for kind in dialogs.KINDS:
            self.assertEqual(dialogs.answer(kind, "ask", "x"), ("ask", None), kind)

    def test_unknown_kind_raises_under_either_policy(self):
        for pol in dialogs.POLICIES:
            with self.assertRaises(ValueError):
                dialogs.answer("popup", pol)


class TestLog(unittest.TestCase):
    def test_entry_shape_and_timestamp(self):
        e = dialogs.Log().add("prompt", "name?", "bob", "accept")
        self.assertEqual(set(e), {"kind", "message", "default", "answered", "at"})
        self.assertEqual(e["kind"], "prompt")
        self.assertEqual(e["default"], "bob")
        self.assertEqual(e["answered"], "accept")
        self.assertTrue(e["at"].endswith("Z"))
        datetime.strptime(e["at"][:-1].split(".")[0], "%Y-%m-%dT%H:%M:%S")

    def test_message_truncated_and_none_is_empty(self):
        log = dialogs.Log()
        self.assertEqual(len(log.add("alert", "x" * 5000, None, "close")["message"]), 2000)
        self.assertEqual(log.add("alert", None, None, "close")["message"], "")

    def test_entries_oldest_first_and_a_copy(self):
        log = dialogs.Log()
        log.add("alert", "a", None, "close")
        log.add("alert", "b", None, "close")
        got = log.entries()
        self.assertIsInstance(got, list)
        self.assertEqual([e["message"] for e in got], ["a", "b"])
        got.clear()
        self.assertEqual(len(log.entries()), 2)

    def test_ring_keeps_the_last_limit(self):
        log = dialogs.Log(limit=3)
        for i in range(5):
            log.add("alert", str(i), None, "close")
        self.assertEqual([e["message"] for e in log.entries()], ["2", "3", "4"])

    def test_clear(self):
        log = dialogs.Log()
        log.add("alert", "a", None, "close")
        log.clear()
        self.assertEqual(log.entries(), [])


if __name__ == "__main__":
    unittest.main()
