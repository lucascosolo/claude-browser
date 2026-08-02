"""The Claude-facing layers, with the network and the browser stubbed out.

Covers the failure paths that are hard to trigger by hand and expensive to
discover in front of a user: retries, refusals, truncated turns, and an agent
loop that stops making progress.
"""

import io
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
        self._key = ai.os.environ.get("ANTHROPIC_API_KEY")
        ai.os.environ["ANTHROPIC_API_KEY"] = "test-key"
        self.slept = []
        self.calls = 0

    def tearDown(self):
        if self._key is None:
            ai.os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            ai.os.environ["ANTHROPIC_API_KEY"] = self._key

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

    def test_stream_surfaces_error_instead_of_raising(self):
        """The panel renders whatever the generator yields; an exception
        escaping here would leave it blank with no explanation."""
        self.patch([http_error(500, "boom")])
        ai.time.sleep = lambda *_: None
        out = "".join(ai._stream("sys", "prompt"))
        self.assertIn("api error 500", out)


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
