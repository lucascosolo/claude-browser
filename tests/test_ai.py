"""The Claude-facing layers, with the network and the browser stubbed out.

Covers the failure paths that are hard to trigger by hand and expensive to
discover in front of a user: retries, refusals, truncated turns, and an agent
loop that stops making progress.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import agent, ai  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def http_error(code, message="nope", headers=None):
    return urllib.error.HTTPError(
        "https://api", code, "err", headers or {}, io.BytesIO(
            b'{"error":{"message":"%s"}}' % message.encode()))


class ApiRequestTest(unittest.TestCase):
    def setUp(self):
        # Pin to the API-key path: this machine has a real Claude subscription
        # credential on disk, and auth.candidates() would otherwise find it and
        # make these tests depend on the developer's login state.
        self._key = ai.os.environ.get("ANTHROPIC_API_KEY")
        self._auth = ai.os.environ.get("CB_AUTH")
        ai.os.environ["CB_AUTH"] = "api"
        ai.os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.slept = []
        self.calls = 0

    def tearDown(self):
        for name, value in (("ANTHROPIC_API_KEY", self._key), ("CB_AUTH", self._auth)):
            if value is None:
                ai.os.environ.pop(name, None)
            else:
                ai.os.environ[name] = value

    def patch(self, sequence):
        """Each element is an exception to raise or a response to return."""
        def fake(req, timeout=None):
            item = sequence[min(self.calls, len(sequence) - 1)]
            self.calls += 1
            if isinstance(item, Exception):
                raise item
            return item
        ai.urllib.request.urlopen = fake

    def test_missing_key_raises_before_any_request(self):
        ai.os.environ.pop("ANTHROPIC_API_KEY", None)
        # CB_AUTH=api from setUp, so the subscription path is not consulted.
        self.patch([AssertionError("should not be reached")])
        with self.assertRaises(ai.NoKey):
            ai._open({"model": "m"}, sleep=self.slept.append)

    def test_retries_then_succeeds(self):
        self.patch([http_error(529, "overloaded"), http_error(529), FakeResponse(b"{}")])
        resp = ai._open({"model": "m"}, sleep=self.slept.append)
        self.assertIsInstance(resp, FakeResponse)
        self.assertEqual(self.calls, 3)
        self.assertEqual(len(self.slept), 2)

    def test_gives_up_after_max_retries(self):
        self.patch([http_error(500)])
        with self.assertRaises(ai.ApiError):
            ai._open({"model": "m"}, sleep=self.slept.append)
        self.assertEqual(self.calls, ai.MAX_RETRIES + 1)

    def test_client_errors_are_not_retried(self):
        """A 400 or 401 fails identically forever -- retrying just delays the
        message the user needs to see."""
        for code in (400, 401, 403, 404):
            self.calls = 0
            self.patch([http_error(code, "bad request")])
            with self.assertRaises(ai.ApiError):
                ai._open({"model": "m"}, sleep=self.slept.append)
            self.assertEqual(self.calls, 1, "status %d should not retry" % code)

    def test_retry_after_header_is_honoured(self):
        self.patch([http_error(429, "slow down", {"retry-after": "7"}), FakeResponse(b"{}")])
        ai._open({"model": "m"}, sleep=self.slept.append)
        self.assertEqual(self.slept, [7.0])

    def test_backoff_is_capped(self):
        self.patch([http_error(429, "x", {"retry-after": "9999"}), FakeResponse(b"{}")])
        ai._open({"model": "m"}, sleep=self.slept.append)
        self.assertLessEqual(self.slept[0], 30)

    def test_network_failure_retries_then_reports(self):
        self.patch([urllib.error.URLError("no route")])
        with self.assertRaises(ai.ApiError) as caught:
            ai._open({"model": "m"}, sleep=self.slept.append)
        self.assertIn("network error", str(caught.exception))

    def test_stream_raises_so_the_panel_can_mark_it_failed(self):
        """It must raise, not yield the message as if it were an answer --
        yielding made a failure render as a normal card with status "done"."""
        self.patch([http_error(500, "boom")])
        ai.time.sleep = lambda *_: None
        with self.assertRaises(ai.ApiError) as caught:
            list(ai._stream("sys", "prompt"))
        self.assertIn("server error (500)", str(caught.exception))
        self.assertIn("transient", str(caught.exception))


class ErrorMessageTest(unittest.TestCase):
    """The API's own message is often one useless word -- a rate-limited
    subscription literally returns {"message": "Error"} -- so these messages
    have to carry the meaning themselves."""

    def describe(self, status, body, label):
        return ai._describe_error(status, body, label)[0]

    def test_rate_limited_subscription_explains_the_shared_quota(self):
        text = self.describe(429, '{"error":{"type":"rate_limit_error","message":"Error"}}',
                             "max subscription")
        self.assertIn("rate-limited", text)
        self.assertIn("Claude Code", text, "should say what shares the quota")
        self.assertNotIn('"message": "Error"', text)

    def test_bad_api_key_says_the_key_is_the_problem(self):
        text = self.describe(401, '{"error":{"message":"API key is invalid."}}', "API key")
        self.assertIn("API key was rejected", text)
        self.assertIn("sk-ant-", text, "should say what a valid key looks like")

    def test_rejected_subscription_points_at_login(self):
        text = self.describe(403, '{"error":{"message":"nope"}}', "max subscription")
        self.assertIn("/login", text)

    def test_unparseable_body_still_produces_something_useful(self):
        text = self.describe(418, "<html>gateway</html>", "API key")
        self.assertIn("418", text)


class AuthTest(unittest.TestCase):
    def setUp(self):
        from claudebrowser import auth
        self.auth = auth
        self._env = {k: os.environ.get(k) for k in ("CB_AUTH", "ANTHROPIC_API_KEY")}
        self._read = auth._read_subscription

    def tearDown(self):
        self.auth._read_subscription = self._read
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def fake_subscription(self, present=True, expired=False):
        import time as _t
        expires = (_t.time() - 60) * 1000 if expired else (_t.time() + 3600) * 1000
        self.auth._read_subscription = (
            (lambda: ("tok", "max", expires)) if present else (lambda: None))

    def test_api_key_preferred_over_subscription(self):
        """The API key is the credential Anthropic issues for third-party use,
        so it goes first. A subscription token is a fallback for someone who
        has no key -- not the default path."""
        os.environ["CB_AUTH"] = "auto"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        self.fake_subscription()
        labels = [label for _h, label in self.auth.candidates()]
        self.assertEqual(labels, ["API key", "max subscription"])

    def test_falls_back_to_next_credential_on_rate_limit(self):
        """The bug that made every Claude feature look dead.

        Fallback used to require e.auth_failure, which is only 401/403. A
        rate-limited first credential returns 429, so the second -- a perfectly
        good key -- was never tried and the panel just reported the 429.
        """
        os.environ["CB_AUTH"] = "auto"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        self.fake_subscription()
        seen = []

        def fake(req, timeout=None):
            seen.append(req.headers.get("X-api-key") or req.headers.get("Authorization"))
            if len(seen) == 1:
                raise http_error(429, "rate limited")
            return FakeResponse(b'{"content":[{"type":"text","text":"hi"}]}')

        original = ai.urllib.request.urlopen
        ai.urllib.request.urlopen = fake
        try:
            resp = ai._open({"stream": False}, sleep=lambda _s: None)
            self.assertEqual(json.loads(resp.read())["content"][0]["text"], "hi")
        finally:
            ai.urllib.request.urlopen = original
        self.assertEqual(len(seen), 2, "should have tried the second credential")
        self.assertEqual(ai.LAST_CREDENTIAL, "max subscription")

    def test_non_final_credential_does_not_burn_the_retry_budget(self):
        """Retrying a credential we are about to abandon is pure latency."""
        os.environ["CB_AUTH"] = "auto"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        self.fake_subscription()
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            raise http_error(429, "rate limited")

        original = ai.urllib.request.urlopen
        ai.urllib.request.urlopen = fake
        try:
            with self.assertRaises(ai.ApiError):
                ai._open({"stream": False}, sleep=lambda _s: None)
        finally:
            ai.urllib.request.urlopen = original
        # 1 attempt for the API key (no retries, it is not last) + 1 + MAX_RETRIES
        # for the subscription, which is.
        self.assertEqual(len(calls), 1 + 1 + ai.MAX_RETRIES)

    def test_subscription_uses_bearer_and_beta_header_not_x_api_key(self):
        """OAuth tokens go on Authorization, never x-api-key."""
        self.fake_subscription()
        os.environ["CB_AUTH"] = "subscription"
        headers, _label = self.auth.candidates()[0]
        self.assertTrue(headers["authorization"].startswith("Bearer "))
        self.assertEqual(headers["anthropic-beta"], self.auth.OAUTH_BETA)
        self.assertNotIn("x-api-key", headers)

    def test_expired_subscription_is_skipped(self):
        self.fake_subscription(expired=True)
        os.environ["CB_AUTH"] = "auto"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        self.assertEqual([l for _h, l in self.auth.candidates()], ["API key"])

    def test_expired_subscription_status_says_how_to_fix(self):
        self.fake_subscription(expired=True)
        _kind, note = self.auth.subscription_status()
        self.assertIn("/login", note)

    def test_api_preference_ignores_subscription(self):
        self.fake_subscription()
        os.environ["CB_AUTH"] = "api"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-x"
        self.assertEqual([l for _h, l in self.auth.candidates()], ["API key"])

    def test_no_credential_explains_both_paths(self):
        self.fake_subscription(present=False)
        os.environ["CB_AUTH"] = "auto"
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(self.auth.NoCredential) as caught:
            self.auth.candidates()
        text = str(caught.exception)
        self.assertIn("/login", text)
        self.assertIn("ANTHROPIC_API_KEY", text)
        self.assertIn("desktop menu", text, "should mention the launcher trap")


class SseTest(unittest.TestCase):
    def run_sse(self, lines):
        return "".join(ai._sse_text(iter(l.encode() for l in lines)))

    def test_max_tokens_is_reported(self):
        out = self.run_sse(['data: {"type":"message_delta",'
                            '"delta":{"stop_reason":"max_tokens"}}\n'])
        self.assertIn("truncated", out)

    def test_refusal_is_reported(self):
        out = self.run_sse(['data: {"type":"message_delta",'
                            '"delta":{"stop_reason":"refusal"}}\n'])
        self.assertIn("declined", out)


class SynthesizeTest(unittest.TestCase):
    def test_no_pages_says_so(self):
        self.assertIn("No readable pages", "".join(ai.synthesize([])))


# -- the agent loop ---------------------------------------------------------

class FakeBrowser:
    """Stands in for the GTK side. Records what the agent asked it to do."""

    def __init__(self, page_text="hello world"):
        self.calls = []
        self.page_text = page_text

    def __call__(self, method, *args, **kwargs):
        self.calls.append((method, args))
        if method == "api_eval":
            return {"ok": True, "result": {"title": "T", "url": "http://x/",
                                           "text": self.page_text}}
        if method == "api_tabs":
            return {"ok": True, "tabs": []}
        return {"ok": True, "url": "http://x/", "title": "T"}


def turn(*blocks, stop="tool_use"):
    return {"content": list(blocks), "stop_reason": stop}


def text_block(t):
    return {"type": "text", "text": t}


def tool_block(name, args, id="tu_1"):
    return {"type": "tool_use", "id": id, "name": name, "input": args}


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.real_turn = ai.tool_turn
        self.output = []
        self.browser = FakeBrowser()

    def tearDown(self):
        ai.tool_turn = self.real_turn

    def script(self, responses):
        self.sent = []

        def fake(messages, tools, system, max_tokens=16000):
            self.sent.append(messages)
            return responses[min(len(self.sent) - 1, len(responses) - 1)]
        ai.tool_turn = fake

    def make(self):
        return agent.Agent(self.browser, self.output.append)

    def text(self):
        return "".join(self.output)

    def test_empty_goal_is_rejected_without_calling_the_api(self):
        self.script([turn(text_block("x"), stop="end_turn")])
        self.make().run("   ")
        self.assertIn("Give me a goal", self.text())
        self.assertEqual(self.sent, [])

    def test_plain_answer_finishes_in_one_turn(self):
        self.script([turn(text_block("The answer is 42."), stop="end_turn")])
        self.make().run("what is the answer")
        self.assertIn("42", self.text())
        self.assertEqual(self.browser.calls, [])

    def test_tool_call_reaches_the_browser_and_loops(self):
        self.script([
            turn(tool_block("navigate", {"url": "http://x/"})),
            turn(text_block("done"), stop="end_turn"),
        ])
        self.make().run("go to x")
        self.assertEqual(self.browser.calls[0][0], "api_navigate")
        self.assertIn("done", self.text())
        # The assistant turn must be echoed back verbatim -- it carries the
        # thinking blocks Opus 5 requires to be returned unmodified.
        self.assertEqual(self.sent[1][1]["role"], "assistant")
        self.assertEqual(self.sent[1][2]["role"], "user")
        self.assertEqual(self.sent[1][2]["content"][0]["type"], "tool_result")

    def test_refusal_stops_cleanly(self):
        self.script([turn(text_block(""), stop="refusal")])
        self.make().run("something")
        self.assertIn("declined", self.text())

    def test_truncated_turn_stops_instead_of_sending_a_partial_tool_call(self):
        self.script([turn(tool_block("navigate", {"url": "http://x/"}), stop="max_tokens")])
        self.make().run("go")
        self.assertIn("output limit", self.text())
        self.assertEqual(self.browser.calls, [])

    def test_repeated_identical_calls_are_broken_out_of(self):
        self.script([turn(tool_block("read_page", {}))])  # same call forever
        self.make().run("read it")
        joined = self.text()
        self.assertIn("stopped after", joined)
        reads = [c for c in self.browser.calls if c[0] == "api_eval"]
        self.assertLessEqual(len(reads), agent.REPEAT_LIMIT,
                             "loop detection should stop re-running the same call")

    def test_api_error_is_reported_not_raised(self):
        def boom(*a, **k):
            raise ai.ApiError("[api error 500] boom")
        ai.tool_turn = boom
        self.make().run("go")
        self.assertIn("500", self.text())

    def test_missing_key_is_explained(self):
        def boom(*a, **k):
            raise ai.NoKey("set ANTHROPIC_API_KEY")
        ai.tool_turn = boom
        self.make().run("go")
        self.assertIn("ANTHROPIC_API_KEY", self.text())

    def test_cancel_stops_the_loop(self):
        a = self.make()

        def fake(messages, tools, system, max_tokens=16000):
            a.cancel()
            return turn(tool_block("read_page", {}))
        ai.tool_turn = fake
        a.run("go")
        self.assertIn("stopped", self.text())

    def test_malformed_tool_block_without_id_is_skipped(self):
        self.script([turn({"type": "tool_use", "name": "read_page", "input": {}})])
        self.make().run("go")
        self.assertIn("no usable tool calls", self.text())

    def test_huge_page_is_truncated_before_going_back_to_the_model(self):
        self.browser.page_text = "x" * 500_000
        self.script([
            turn(tool_block("read_page", {})),
            turn(text_block("ok"), stop="end_turn"),
        ])
        self.make().run("read")
        result = self.sent[1][2]["content"][0]["content"]
        self.assertLessEqual(len(result), agent.RESULT_CHARS)

    def test_step_budget_is_enforced(self):
        # Distinct calls each time, so loop detection does not fire first.
        responses = [turn(tool_block("navigate", {"url": "http://x/%d" % i}, id="t%d" % i))
                     for i in range(agent.MAX_STEPS + 5)]
        self.script(responses)
        self.make().run("wander")
        self.assertIn("stopped after %d steps" % agent.MAX_STEPS, self.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
