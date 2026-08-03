"""Playbooks: capture, validation and storage, without a display.

The three things worth proving here are the three that would be expensive to
discover in a running browser: a playbook cannot name an operation that does not
exist, a recording never writes a credential to disk, and what comes back off
disk is what went onto it.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import api, playbooks  # noqa: E402


class TestValidation(unittest.TestCase):
    """A playbook is replayed input. Everything in it is checked against the
    registry before anything is dispatched."""

    def test_a_known_op_resolves_to_its_registry_entry(self):
        op, params = playbooks.validate_step(
            {"op": "open", "params": {"url": "https://example.com"}})
        self.assertIs(op, api.BY_NAME["open"])
        self.assertEqual(params, {"url": "https://example.com"})

    def test_replay_goes_through_the_same_call_builder_as_the_route(self):
        """Which is what puts a playbook's navigations in Browser._admit's
        queue: replay dispatches api_open, not a private shortcut."""
        op, params = playbooks.validate_step(
            {"op": "open", "params": {"url": "https://example.com"}})
        method, args = op.call(None, dict(params))
        self.assertEqual(method, "api_open")
        self.assertEqual(args[0], "https://example.com")

        op, params = playbooks.validate_step({"op": "navigate",
                                              "params": {"url": "x.com"}})
        self.assertEqual(op.call(None, dict(params))[0], "api_navigate")

    def test_unknown_op_is_rejected(self):
        for name in ("nope", "api_eval", "", None, 7, "__init__"):
            with self.assertRaises(playbooks.PlaybookError):
                playbooks.validate_step({"op": name, "params": {}})

    def test_health_is_rejected_because_it_has_no_browser_method(self):
        self.assertIsNone(playbooks.replayable("health"))
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate_step({"op": "health", "params": {}})

    def test_a_playbook_cannot_invoke_a_playbook(self):
        for name in sorted(playbooks.NOT_REPLAYABLE):
            self.assertIn(name, api.BY_NAME, "%s left the registry" % name)
            self.assertIsNone(playbooks.replayable(name))

    def test_unknown_parameter_is_rejected(self):
        with self.assertRaises(playbooks.PlaybookError) as caught:
            playbooks.validate_step({"op": "click",
                                     "params": {"selector": "a", "extra": 1}})
        self.assertIn("extra", str(caught.exception))

    def test_a_hard_coded_tab_is_rejected(self):
        """Tab ids are valid for one session; a recording never carries one."""
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate_step({"op": "click",
                                     "params": {"selector": "a", "tab": 3}})

    def test_missing_required_parameter_is_rejected(self):
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate_step({"op": "fill", "params": {"selector": "a"}})

    def test_non_scalar_values_are_rejected(self):
        for bad in ({"a": 1}, ["a"]):
            with self.assertRaises(playbooks.PlaybookError):
                playbooks.validate_step({"op": "click", "params": {"selector": bad}})

    def test_types_follow_what_the_registry_declares(self):
        _op, params = playbooks.validate_step({"op": "reader",
                                               "params": {"font": "20"}})
        self.assertEqual(params["font"], 20)      # a query string has no types
        _op, params = playbooks.validate_step(
            {"op": "open", "params": {"url": "x.com", "background": "true"}})
        self.assertIs(params["background"], True)
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate_step({"op": "reader", "params": {"font": "big"}})

    def test_a_step_must_be_an_object(self):
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate_step("open https://example.com")

    def test_an_empty_playbook_is_not_a_playbook(self):
        for bad in ([], None, {}, "open"):
            with self.assertRaises(playbooks.PlaybookError):
                playbooks.validate(bad)

    def test_a_bad_step_names_its_position(self):
        with self.assertRaises(playbooks.PlaybookError) as caught:
            playbooks.validate([{"op": "open", "params": {"url": "x.com"}},
                                {"op": "nope", "params": {}}])
        self.assertIn("step 2", str(caught.exception))

    def test_step_count_is_capped(self):
        step = {"op": "reload", "params": {}}
        with self.assertRaises(playbooks.PlaybookError):
            playbooks.validate([step] * (playbooks.MAX_STEPS + 1))


class TestNames(unittest.TestCase):
    def test_reasonable_names_pass(self):
        for name in ("login", "Daily report", "morning.v2", "a-b_c"):
            self.assertEqual(playbooks.clean_name(" %s " % name), name)

    def test_a_name_is_not_a_path(self):
        for name in ("", "   ", ".hidden", "a/b", "../etc/passwd", "x" * 65):
            with self.assertRaises(playbooks.PlaybookError):
                playbooks.clean_name(name)


class TestRecorder(unittest.TestCase):
    def setUp(self):
        self.rec = playbooks.Recorder()

    def test_nothing_is_captured_until_recording_starts(self):
        self.assertFalse(self.rec.active)
        self.assertFalse(self.rec.observe("open", {"url": "x.com"}))
        self.assertEqual(self.rec.steps, [])

    def test_a_run_is_captured_in_order(self):
        self.rec.start("login")
        self.rec.observe("open", {"url": "https://example.com"})
        self.rec.observe("click", {"selector": "#go"})
        name, steps, skipped = self.rec.stop()
        self.assertEqual(name, "login")
        self.assertEqual(skipped, 0)
        self.assertEqual([s["op"] for s in steps], ["open", "click"])
        self.assertEqual(steps[0]["params"], {"url": "https://example.com"})
        self.assertFalse(self.rec.active)

    def test_everything_captured_is_replayable(self):
        self.rec.start("run")
        self.rec.observe("open", {"url": "x.com"})
        self.rec.observe("fill", {"selector": "#q", "value": "hello"})
        playbooks.validate(self.rec.stop()[1])   # raises if it is not

    def test_a_failed_operation_is_not_part_of_the_sequence(self):
        self.rec.start("run")
        self.rec.observe("click", {"selector": ".missing"}, ok=False)
        self.assertEqual(self.rec.stop()[1], [])

    def test_tab_ids_and_undeclared_arguments_are_dropped(self):
        self.rec.start("run")
        self.rec.observe("click", {"selector": "#go", "tab": 3, "token": "abc"})
        self.assertEqual(self.rec.stop()[1][0]["params"], {"selector": "#go"})

    def test_the_playbook_ops_are_not_recorded(self):
        self.rec.start("run")
        for name in sorted(playbooks.NOT_REPLAYABLE) + ["health"]:
            self.rec.observe(name, {"name": "run"})
        self.assertEqual(self.rec.stop()[1], [])

    def test_cancel_throws_the_recording_away(self):
        self.rec.start("run")
        self.rec.observe("reload", {})
        self.assertEqual(self.rec.cancel(), "run")
        self.assertFalse(self.rec.active)
        self.assertEqual(self.rec.steps, [])


class TestSecretsAreNeverRecorded(unittest.TestCase):
    """The rule from CLAUDE.md, applied to a new on-disk sink: secrets go to
    the Secret Service, never to a file this project invents."""

    def setUp(self):
        self.rec = playbooks.Recorder()

    def test_password_fields_are_recognised(self):
        for selector in ('input[type="password"]', "#password", ".passwd",
                         "[name=otp]", "#totp-code", "input[name='api_key']",
                         "#cvv", "[name=security-code]"):
            self.assertTrue(playbooks.is_secret_step("fill",
                                                     {"selector": selector}),
                            selector)

    def test_ordinary_fields_still_record(self):
        for selector in ("#search", "input[name=email]", ".query", "#username"):
            self.assertFalse(playbooks.is_secret_step("fill",
                                                      {"selector": selector}),
                             selector)

    def test_a_password_fill_never_reaches_the_recording(self):
        self.rec.start("login")
        self.rec.observe("open", {"url": "https://example.com/login"})
        self.rec.observe("fill", {"selector": "#username", "value": "lucas"})
        self.rec.observe("fill", {"selector": "input[type=password]",
                                  "value": "hunter2"})
        self.rec.observe("click", {"selector": "#submit"})
        _name, steps, skipped = self.rec.stop()
        self.assertEqual(skipped, 1)
        self.assertEqual([s["op"] for s in steps], ["open", "fill", "click"])
        self.assertNotIn("hunter2", json.dumps(steps))

    def test_a_script_carrying_a_secret_is_dropped_too(self):
        self.rec.start("run")
        self.rec.observe("eval", {"js": "localStorage.token = 'sk-ant-123'"})
        _name, steps, skipped = self.rec.stop()
        self.assertEqual(steps, [])
        self.assertEqual(skipped, 1)

    def test_the_secret_is_absent_from_the_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "playbooks.json"
            self.rec.start("login")
            self.rec.observe("fill", {"selector": "#password",
                                      "value": "correct horse battery staple"})
            self.rec.observe("click", {"selector": "#submit"})
            _name, steps, skipped = self.rec.stop()
            playbooks.Playbooks(path).save("login", steps, skipped)
            self.assertNotIn("correct horse", path.read_text())
            self.assertNotIn("password", path.read_text())


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "playbooks.json"
        self.books = playbooks.Playbooks(self.path)

    def steps(self):
        return [{"op": "open", "params": {"url": "https://example.com"}},
                {"op": "click", "params": {"selector": "#report"}}]

    def test_round_trip(self):
        self.books.save("report", self.steps(), skipped=1)
        again = playbooks.Playbooks(self.path)      # a fresh reader, as a
        book = again.get("report")                  # restarted browser would be
        self.assertEqual(book["name"], "report")
        self.assertEqual(book["skipped_secrets"], 1)
        self.assertEqual(book["steps"], self.steps())
        self.assertEqual(again.names(), ["report"])
        playbooks.validate(book["steps"])

    def test_the_file_is_json_a_person_can_read(self):
        self.books.save("report", self.steps())
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["version"], playbooks.FILE_VERSION)
        self.assertIn("report", doc["playbooks"])

    def test_saving_refuses_something_that_could_not_be_replayed(self):
        with self.assertRaises(playbooks.PlaybookError):
            self.books.save("bad", [{"op": "nope", "params": {}}])
        self.assertEqual(self.books.names(), [])

    def test_summaries_describe_without_loading_everything(self):
        self.books.save("a", self.steps())
        self.books.save("b", [{"op": "reload", "params": {}}])
        summaries = {s["name"]: s for s in self.books.summaries()}
        self.assertEqual(summaries["a"]["steps"], 2)
        self.assertEqual(summaries["a"]["ops"], ["open", "click"])
        self.assertEqual(summaries["b"]["steps"], 1)

    def test_saving_the_same_name_replaces_it(self):
        self.books.save("a", self.steps())
        self.books.save("a", [{"op": "reload", "params": {}}])
        self.assertEqual(len(self.books.get("a")["steps"]), 1)

    def test_delete(self):
        self.books.save("a", self.steps())
        self.assertTrue(self.books.delete("a"))
        self.assertFalse(self.books.delete("a"))
        self.assertIsNone(self.books.get("a"))

    def test_a_missing_or_corrupt_file_reads_as_an_empty_collection(self):
        self.assertEqual(self.books.names(), [])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json")
        self.assertEqual(self.books.names(), [])
        self.assertIsNone(self.books.get("anything"))

    def test_the_collection_is_capped(self):
        for i in range(playbooks.MAX_PLAYBOOKS):
            self.books.save("p%d" % i, [{"op": "reload", "params": {}}])
        with self.assertRaises(playbooks.PlaybookError):
            self.books.save("one-too-many", [{"op": "reload", "params": {}}])
        # An existing name is an update, not a new entry, so it still works.
        self.books.save("p0", [{"op": "reload", "params": {}}])

    def test_it_lives_beside_the_browsing_database(self):
        from claudebrowser import store

        self.assertEqual(playbooks.default_path().parent, store.data_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
