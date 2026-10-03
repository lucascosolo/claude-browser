import json
import unittest

from claudebrowser import agent


class TestToolsAndTabToolsGuard(unittest.TestCase):
    # Guard test in the spirit of test_every_web_process_name_survives_truncation:
    # a tool that reaches the tab but is missing from TAB_TOOLS is a private-tab
    # data leak, not a cosmetic bug.
    TAB_TOUCHING = {"navigate", "read_page", "find_in_page", "page_links",
                     "click", "type_text", "snapshot", "fill_form", "blocked"}

    def test_every_tab_touching_tool_is_in_tab_tools(self):
        names = {t["name"] for t in agent.TOOLS}
        for name in self.TAB_TOUCHING & names:
            self.assertIn(name, agent.TAB_TOOLS,
                          "%s touches the tab but is not gated" % name)

    def test_tab_touching_set_is_fully_present_in_tools(self):
        # If this ever fails, the guard above is silently checking an empty
        # intersection and proving nothing.
        names = {t["name"] for t in agent.TOOLS}
        self.assertTrue(self.TAB_TOUCHING.issubset(names))

    def test_tools_list_includes_the_new_primitives(self):
        names = {t["name"] for t in agent.TOOLS}
        self.assertIn("snapshot", names)
        self.assertIn("fill_form", names)
        self.assertIn("blocked", names)
        self.assertIn("profile", names)

    def test_profile_is_not_in_tab_tools(self):
        # profile reads the keyring, not the tab -- a private tab has no
        # bearing on it, so it must not be gated behind the tab check.
        self.assertNotIn("profile", agent.TAB_TOOLS)

    def test_max_steps_raised(self):
        self.assertGreaterEqual(agent.MAX_STEPS, 24)


class TestLoopSignature(unittest.TestCase):
    def _agent(self):
        return agent.Agent(call=lambda *a, **k: {}, emit=lambda *a: None)

    def test_loop_signature_includes_the_tab_url(self):
        # snapshot() takes no arguments, so the loop-detection key must not be
        # (tool_name, args) alone or four snapshots across four different
        # pages in one multi-page flow look like a repeat.
        a = self._agent()
        sig1 = a._loop_signature("snapshot", {}, "https://site.example/page1")
        sig2 = a._loop_signature("snapshot", {}, "https://site.example/page2")
        self.assertNotEqual(sig1, sig2)

    def test_loop_signature_same_inputs_produce_same_signature(self):
        a = self._agent()
        sig1 = a._loop_signature("click", {"selector": "#go"}, "https://x/1")
        sig2 = a._loop_signature("click", {"selector": "#go"}, "https://x/1")
        self.assertEqual(sig1, sig2)


class TestTruncateResult(unittest.TestCase):
    def test_structural_truncation_keeps_valid_json(self):
        huge = {"ok": True, "lines": [{"ref": "e%d" % i} for i in range(5000)]}
        truncated = agent.truncate_result("snapshot", huge, limit=200)
        # Must round-trip cleanly -- a raw byte-slice of the JSON text could
        # cut a row in half and fail to parse at all.
        round_tripped = json.loads(json.dumps(truncated))
        self.assertIsInstance(round_tripped, dict)
        self.assertLess(len(json.dumps(truncated)), len(json.dumps(huge)))

    def test_structural_truncation_marks_what_it_dropped(self):
        huge = {"ok": True, "lines": [{"ref": "e%d" % i} for i in range(5000)]}
        truncated = agent.truncate_result("fill_form", huge, limit=200)
        self.assertTrue(truncated.get("truncated"))
        self.assertGreater(truncated.get("omitted", 0), 0)
        self.assertLess(len(truncated["lines"]), len(huge["lines"]))

    def test_small_result_is_not_flagged_as_truncated(self):
        small = {"ok": True, "lines": [{"ref": "e1"}, {"ref": "e2"}]}
        result = agent.truncate_result("snapshot", small, limit=agent.RESULT_CHARS)
        self.assertNotIn("truncated", result)
        self.assertEqual(result["lines"], small["lines"])

    def test_non_structural_tool_is_returned_unchanged(self):
        page = {"ok": True, "text": "x" * 50}
        result = agent.truncate_result("read_page", page, limit=10)
        self.assertEqual(result, page)

    def test_dict_without_lines_key_is_returned_unchanged(self):
        result = {"ok": True, "url": "https://x"}
        self.assertEqual(agent.truncate_result("snapshot", result, limit=5), result)


class TestDispatchAndRunShape(unittest.TestCase):
    def test_dispatch_unknown_tool_reports_error(self):
        a = agent.Agent(call=lambda *a, **k: {}, emit=lambda *a: None)
        out = a.dispatch("no_such_tool", {})
        self.assertIn("error", out)

    def test_dispatch_profile_reports_keys_not_values(self):
        calls = []

        def fake_call(op, *args, **kwargs):
            calls.append(op)
            return {"available": True, "fields": {"email": "jane@example.com"}}

        a = agent.Agent(call=fake_call, emit=lambda *a: None)
        out = a.dispatch("profile", {})
        self.assertEqual(out["keys"], ["email"])
        self.assertNotIn("jane@example.com", json.dumps(out))

    def test_dispatch_fill_form_resolves_profile_placeholder(self):
        def fake_call(op, *args, **kwargs):
            if op == "api_profile":
                return {"available": True, "fields": {"email": "jane@example.com"}}
            if op == "api_eval":
                return {"ok": True, "result": {"ok": True}}
            return {}

        a = agent.Agent(call=fake_call, emit=lambda *a: None, pace=0)
        out = a.dispatch("fill_form", {"fields": {"#email": "{profile:email}"}})
        self.assertNotIn("error", out)

    def test_dispatch_fill_form_reports_unresolvable_placeholder(self):
        def fake_call(op, *args, **kwargs):
            if op == "api_profile":
                return {"available": True, "fields": {}}
            return {}

        a = agent.Agent(call=fake_call, emit=lambda *a: None, pace=0)
        out = a.dispatch("fill_form", {"fields": {"#phone": "{profile:phone}"}})
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
