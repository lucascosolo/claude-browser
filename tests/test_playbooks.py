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

from claudebrowser import api, pages, playbooks, style  # noqa: E402


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


class TestTabPrivacy(unittest.TestCase):
    """The mirror the recorder reads instead of GtkNotebook."""

    def setUp(self):
        self.privacy = playbooks.TabPrivacy()

    def test_an_ordinary_tab_is_not_private(self):
        self.privacy.opened(1, False)
        self.privacy.focused(1)
        self.assertFalse(self.privacy.is_private(1))
        self.assertFalse(self.privacy.is_private())

    def test_a_private_tab_is(self):
        self.privacy.opened(2, True)
        self.privacy.focused(2)
        self.assertTrue(self.privacy.is_private(2))
        self.assertTrue(self.privacy.is_private())

    def test_no_tab_id_means_the_focused_one(self):
        self.privacy.opened(1, False)
        self.privacy.opened(2, True)
        self.privacy.focused(1)
        self.assertFalse(self.privacy.is_private())
        self.privacy.focused(2)
        self.assertTrue(self.privacy.is_private())

    def test_an_unknown_id_answers_private(self):
        """Fail closed: a stale id costs one step the user re-adds by hand, and
        the other answer writes a private URL to disk."""
        self.privacy.opened(1, False)
        self.assertTrue(self.privacy.is_private(99))

    def test_a_closed_tab_is_forgotten(self):
        self.privacy.opened(1, False)
        self.privacy.focused(1)
        self.privacy.closed(1)
        self.assertTrue(self.privacy.is_private(1))
        self.assertTrue(self.privacy.is_private())

    def test_no_focused_tab_answers_private(self):
        self.assertTrue(self.privacy.is_private())

    def test_a_tab_id_is_read_out_of_the_arguments(self):
        self.assertEqual(playbooks.target_tab({"tab": 4}), 4)
        self.assertEqual(playbooks.target_tab({"tab": "4"}), 4)
        self.assertIsNone(playbooks.target_tab({}))
        self.assertIsNone(playbooks.target_tab({"tab": ""}))
        self.assertIsNone(playbooks.target_tab({"tab": "nonsense"}))


class TestRecordingPrivateTabs(unittest.TestCase):
    """H2: a step aimed at a private tab is refused, not written.

    The recorder cannot see a tab id -- the registry's default is "the focused
    tab" and clients omit it -- so the browser is the authority and this is
    where that is pinned.
    """

    def setUp(self):
        self.privacy = playbooks.TabPrivacy()
        self.privacy.opened(1, False)
        self.privacy.opened(2, True)
        self.rec = playbooks.Recorder(self.privacy)
        self.rec.start("run")

    def test_a_navigate_in_a_private_tab_is_not_recorded(self):
        self.privacy.focused(2)
        self.assertFalse(self.rec.observe(
            "navigate", {"url": "https://mail.example.com/?access_token=abc"}))
        self.assertEqual(self.rec.stop()[1], [])

    def test_the_url_never_reaches_the_file(self):
        self.privacy.focused(2)
        self.rec.observe("navigate",
                         {"url": "https://x.example/?t=magic-link-value"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "playbooks.json"
            _name, steps, skipped = self.rec.stop()
            playbooks.Playbooks(path).save(
                "run", steps or [{"op": "reload", "params": {}}], skipped)
            self.assertNotIn("magic-link-value", path.read_text())

    def test_an_ordinary_tab_still_records(self):
        self.privacy.focused(1)
        self.assertTrue(self.rec.observe("navigate",
                                         {"url": "https://example.com"}))

    def test_an_explicit_private_tab_id_is_refused_too(self):
        """The focused tab is ordinary; the operation names the private one."""
        self.privacy.focused(1)
        self.assertFalse(self.rec.observe("click", {"selector": "#go", "tab": 2}))

    def test_the_refusals_are_counted_and_reported(self):
        self.privacy.focused(2)
        self.rec.observe("reload", {})
        self.rec.observe("click", {"selector": "#go"})
        status = self.rec.status()
        self.assertEqual(status["skipped_private"], 2)
        self.assertEqual(status["steps"], 0)
        self.assertTrue(status["private_now"])

    def test_a_recorder_with_no_privacy_mirror_records_as_before(self):
        rec = playbooks.Recorder()
        rec.start("run")
        self.assertTrue(rec.observe("reload", {}))
        self.assertFalse(rec.status()["private_now"])

    def test_nothing_is_counted_while_not_recording(self):
        self.rec.stop()
        self.privacy.focused(2)
        self.assertFalse(self.rec.observe("reload", {}))
        self.assertEqual(self.rec.status()["skipped_private"], 0)


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

    def test_the_file_is_readable_only_by_its_owner(self):
        """H3: it holds the URLs and selectors of the user's own logged-in
        workflows, and the default umask would leave it world-readable."""
        self.books.save("report", self.steps())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

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


class TestPage(unittest.TestCase):
    """cb:playbooks -- the only surface where a person, rather than an agent,
    can see what has been recorded and get rid of it.

    Rendered directly, with no display: pages.py is deliberately GTK-free, and
    the store is fed in as the plain dicts `Playbooks.summaries()` returns.
    """

    palette = style.palette("dark")

    def render(self, books=(), recording=None, available=True):
        return pages.playbooks_page(self.palette, "NONCE", list(books),
                                    recording=recording, available=available)

    @staticmethod
    def book(name="report", steps=2, ops=("open", "click"), **extra):
        return dict({"name": name, "steps": steps, "ops": list(ops),
                     "created": None, "skipped_secrets": 0}, **extra)

    def test_the_rail_links_to_it(self):
        self.assertIn("cb:playbooks", [url for url, _label, _d in pages.NAV])

    def test_it_lists_name_step_count_and_what_it_does(self):
        html = self.render([self.book()])
        self.assertIn("report", html)
        self.assertIn("2 steps", html)
        self.assertIn("open → click", html)

    def test_one_step_is_not_pluralised(self):
        self.assertIn("1 step<", self.render([self.book(steps=1, ops=["reload"])]))

    def test_repeated_ops_are_collapsed_rather_than_repeated(self):
        self.assertEqual(pages._what_it_does(["open", "click", "click", "text"]),
                         "open → click ×2 → text")

    def test_a_long_sequence_is_cut_rather_than_run_off_the_row(self):
        line = pages._what_it_does(["a", "b", "c", "d", "e", "f", "g", "h"])
        self.assertTrue(line.endswith("…"), line)
        self.assertNotIn("h", line)

    def test_an_empty_collection_says_how_to_make_one(self):
        html = self.render([])
        self.assertIn("No playbooks yet", html)
        self.assertIn("Start recording", html)

    def test_run_and_delete_go_through_the_nonce_path(self):
        """cbui.send is what stamps the per-session token onto a message. A
        button that reached the browser any other way would be one a website
        could imitate."""
        html = self.render([self.book()])
        self.assertIn("cbui.pbrun(event", html)
        self.assertIn("cbui.pbdrop(event", html)
        self.assertIn("action: 'pb_run'", html)
        self.assertIn("action: 'pb_delete'", html)
        self.assertIn("cbui.pbstart(event)", html)

    def test_delete_is_armed_in_the_page_and_never_a_modal(self):
        """window.confirm() inside a WebView blocks this embedder's GTK main
        loop, so the confirmation is a second click on the same button."""
        html = self.render([self.book()])
        handler = html.split("pbdrop: function")[1].split("confirmData:")[0]
        self.assertIn("dataset.armed", handler)
        self.assertIn("Click again", handler)
        self.assertNotIn("confirm(", handler)

    def test_a_hostile_name_cannot_break_out_of_the_markup_or_the_js(self):
        hostile = 'a" onclick="steal()'
        html = self.render([self.book(name=hostile)])
        self.assertNotIn(hostile, html)
        self.assertIn(pages._js(hostile), html)
        self.assertIn('<span class="rt">a&quot; onclick=&quot;steal()</span>', html)

    def test_a_recording_in_progress_is_impossible_to_miss(self):
        html = self.render([], recording={"recording": True, "name": "login",
                                          "steps": 4, "skipped_secrets": 1})
        self.assertIn("Recording", html)
        self.assertIn("login", html)
        self.assertIn("4 steps captured", html)
        self.assertIn("1 credential field skipped", html)
        self.assertIn("pb_stop", html)
        self.assertIn("pb_cancel", html)
        # The field that starts one is gone while a recording is running: the
        # recorder holds a single capture, and a second Start would be refused.
        self.assertNotIn('id="pbname"', html)

    def test_idle_offers_a_name_and_says_what_gets_captured(self):
        html = self.render([], recording={"recording": False, "name": None,
                                          "steps": 0, "skipped_secrets": 0})
        self.assertIn('id="pbname"', html)
        self.assertIn("Start recording", html)
        # The capture point is the control API, so hand-driving records
        # nothing -- a user who is not told that saves an empty playbook.
        self.assertIn("Browsing by hand is not captured", html)

    def test_no_recorder_state_at_all_still_renders(self):
        self.assertIn("Start recording", self.render([self.book()]))

    def test_a_disabled_store_degrades_instead_of_raising(self):
        """`Browser.playbooks` is None when the data directory could not be
        opened. The page has to say so, the way cb:passwords does for a missing
        keyring, rather than traceback into the scheme handler."""
        html = self.render([], available=False)
        self.assertIn("Playbooks are unavailable", html)
        self.assertNotIn("pb_start", html.split("<script>")[0])

    def test_it_renders_what_the_store_actually_hands_over(self):
        """Guards the seam: summaries() is the page's only input, so a key
        renamed there has to fail here rather than in a running browser."""
        with tempfile.TemporaryDirectory() as tmp:
            books = playbooks.Playbooks(Path(tmp) / "playbooks.json")
            books.save("morning", [{"op": "open",
                                    "params": {"url": "https://example.com"}},
                                   {"op": "click", "params": {"selector": "a"}}])
            html = self.render(books.summaries())
        self.assertIn("morning", html)
        self.assertIn("2 steps", html)
        self.assertIn("open → click", html)
        self.assertIn("saved just now", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
