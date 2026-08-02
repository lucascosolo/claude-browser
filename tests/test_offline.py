"""Everything here runs without a display, GTK, or the WebKit bindings.

The GTK layer itself is not covered -- it needs gir1.2-webkit2-4.1 and an X
display. What IS covered is every piece an agent's request passes through
before and after the browser touches the page: URL intent, JS construction,
SSE parsing, control routing, the CLI, and the MCP server.
"""

import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import ai, extract  # noqa: E402
from claudebrowser.urls import looks_like_url, normalize  # noqa: E402


class TestUrlIntent(unittest.TestCase):
    def test_navigates_for_real_addresses(self):
        for text in ["example.com", "https://example.com", "localhost:5173",
                     "127.0.0.1:8788/api/health", "about:blank", "sub.domain.co.uk/x",
                     "file:///tmp/a.html"]:
            self.assertTrue(looks_like_url(text), text)

    def test_searches_for_everything_else(self):
        for text in ["webkit gtk python", "what is a typelib", "rust", "1 + 1",
                     "install gir1.2-webkit2-4.1"]:
            self.assertFalse(looks_like_url(text), text)

    def test_normalize_adds_scheme_only_when_missing(self):
        self.assertEqual(normalize("example.com"), "https://example.com")
        self.assertEqual(normalize("http://example.com"), "http://example.com")
        self.assertEqual(normalize("about:blank"), "about:blank")

    def test_search_query_is_percent_encoded(self):
        # A '&' in the query must not become a second URL parameter.
        out = normalize("gtk & webkit")
        self.assertIn("gtk%20%26%20webkit", out)
        self.assertEqual(out.count("?"), 1)

    def test_empty_input_is_harmless(self):
        self.assertEqual(normalize("   "), "about:blank")


class TestJsConstruction(unittest.TestCase):
    """A selector or value is attacker-adjacent text -- it can come from a page
    the agent is reading. It must never break out of its string literal."""

    def test_quotes_and_backslashes_survive(self):
        js = extract.fill("#q", 'he said "hi" \\ then left')
        self.assertIn(r'\"hi\"', js)
        self.assertNotIn('value="he said "hi""', js)

    def test_script_tag_cannot_close_the_block(self):
        js = extract.click("</script><img onerror=alert(1)>")
        self.assertNotIn("</script>", js)

    def test_line_separators_are_escaped(self):
        # U+2028 is legal inside a JSON string but terminates a line in JS.
        js = extract.fill("#a", "one two")
        self.assertNotIn(" ", js)
        self.assertIn("\\u2028", js)

    def test_regex_metacharacters_pass_through_intact(self):
        js = extract.find(r"error: \d+")
        self.assertIn(r"error: \\d+", js)

    def test_escaping_preserves_the_value(self):
        """Escaping must be lossless: \\u003c is still '<' once JS parses it.
        JSON and JS agree on \\uXXXX, so json.loads is a faithful stand-in."""
        for value in ["<div class='x'>", 'quote " and \\ back', "a b", "café ☕", "100% & more"]:
            literal = extract._js_str(value)
            self.assertEqual(json.loads(literal), value)

    def test_snippets_are_single_expressions(self):
        for name, src in [("TEXT", extract.TEXT), ("MARKDOWN", extract.MARKDOWN),
                          ("LINKS", extract.LINKS)]:
            self.assertIn("JSON.stringify", src, name)


class TestSseParsing(unittest.TestCase):
    def _run(self, lines):
        return "".join(ai._sse_text(iter(l.encode() for l in lines)))

    def test_collects_text_deltas_only(self):
        out = self._run([
            'data: {"type":"message_start"}\n',
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}\n',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hello "}}\n',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"world"}}\n',
            'data: {"type":"content_block_stop","index":0}\n',
        ])
        self.assertEqual(out, "Hello world")

    def test_thinking_blocks_are_not_shown_as_answer_text(self):
        out = self._run([
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking","thinking":""}}\n',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"hmm"}}\n',
            'data: {"type":"content_block_stop","index":0}\n',
            'data: {"type":"content_block_start","index":1,'
            '"content_block":{"type":"text","text":""}}\n',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"text_delta","text":"Answer"}}\n',
        ])
        self.assertEqual(out, "Answer")

    def test_refusal_is_reported_not_swallowed(self):
        out = self._run(['data: {"type":"message_delta","delta":{"stop_reason":"refusal"}}\n'])
        self.assertIn("declined", out)

    def test_malformed_frames_do_not_abort_the_stream(self):
        out = self._run([
            "\n", "event: ping\n", "data: not-json\n",
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text"}}\n',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n',
        ])
        self.assertEqual(out, "ok")


# -- a stub that speaks the control API, so the CLI and MCP layers can be
#    exercised end to end without GTK ---------------------------------------

class StubBrowser:
    """Records what it was asked to do and answers in the real response shape."""

    def __init__(self):
        self.calls = []
        self.server = None
        self.port = None

    def start(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def _reply(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                args = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                args.update(body)
                stub.calls.append((parsed.path, args))

                if parsed.path == "/text":
                    payload = {"ok": True, "result": {"title": "Stub", "url": "http://stub/",
                                                      "text": "hello from the stub page"}}
                elif parsed.path == "/click":
                    payload = {"ok": bool(args.get("selector") == ".real"),
                               "error": None if args.get("selector") == ".real" else "no match"}
                elif parsed.path == "/tabs":
                    payload = {"ok": True, "current": 1,
                               "tabs": [{"id": 1, "url": "http://stub/", "title": "Stub"}]}
                else:
                    payload = {"ok": True, "echo": args}

                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _reply
            do_POST = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        self.server.shutdown()


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = StubBrowser()
        cls.port = cls.stub.start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()

    def run_cli(self, *args):
        import os

        env = dict(os.environ, CB_PORT=str(self.port))
        env.pop("CB_URL", None)
        env.pop("CB_TOKEN", None)
        return subprocess.run([sys.executable, str(ROOT / "cbctl"), *args],
                              capture_output=True, text=True, env=env, timeout=30)

    def test_text_prints_json(self):
        proc = self.run_cli("text")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hello from the stub page", proc.stdout)

    def test_open_posts_the_url(self):
        proc = self.run_cli("open", "https://example.com")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        path, args = self.stub.calls[-1]
        self.assertEqual(path, "/open")
        self.assertEqual(args["url"], "https://example.com")

    def test_failure_sets_nonzero_exit(self):
        # `cbctl click .missing && cbctl text` must not run the second command.
        self.assertEqual(self.run_cli("click", ".real").returncode, 0)
        self.assertEqual(self.run_cli("click", ".missing").returncode, 1)

    def test_unreachable_browser_explains_itself(self):
        import os

        env = dict(os.environ, CB_URL="http://127.0.0.1:1")
        env.pop("CB_TOKEN", None)
        proc = subprocess.run([sys.executable, str(ROOT / "cbctl"), "tabs"],
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot reach claude-browser", proc.stderr)


class TestMcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = StubBrowser()
        cls.port = cls.stub.start()

    @classmethod
    def tearDownClass(cls):
        cls.stub.stop()

    def talk(self, requests):
        import os

        env = dict(os.environ, CB_PORT=str(self.port))
        env.pop("CB_URL", None)
        env.pop("CB_TOKEN", None)
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, str(ROOT / "cb-mcp")], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_handshake_and_tool_list(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "claude-browser")
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertIn("browser_text", names)
        self.assertIn("browser_console", names)
        for tool in replies[1]["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_tool_call_reaches_the_browser(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "browser_open",
                        "arguments": {"url": "https://example.com"}}},
        ])
        self.assertFalse(replies[0]["result"]["isError"])
        path, args = self.stub.calls[-1]
        self.assertEqual(path, "/open")
        self.assertEqual(args["url"], "https://example.com")

    def test_browser_failure_surfaces_as_tool_error(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "browser_click", "arguments": {"selector": ".missing"}}},
        ])
        self.assertTrue(replies[0]["result"]["isError"])

    def test_unknown_tool_is_an_rpc_error(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "browser_nope", "arguments": {}}},
        ])
        self.assertIn("error", replies[0])

    def test_notifications_get_no_reply(self):
        replies = self.talk([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 7)


class TestControlRouting(unittest.TestCase):
    """control.py's dispatch, with the GTK call replaced by a recorder."""

    def setUp(self):
        from claudebrowser import control

        self.control = control.Control.__new__(control.Control)
        self.control.token = None
        self.seen = []

        def fake_call(method, *args, timeout=45):
            self.seen.append((method, args))
            return {"ok": True, "method": method, "args": [str(a)[:40] for a in args]}

        self.control._call = fake_call
        self.routes = control.ROUTES

    def test_every_route_dispatches(self):
        cases = {
            "/health": {}, "/tabs": {}, "/open": {"url": "x.com"},
            "/navigate": {"url": "x.com"}, "/back": {}, "/forward": {}, "/reload": {},
            "/close": {}, "/wait": {}, "/text": {}, "/markdown": {}, "/links": {},
            "/html": {}, "/find": {"q": "a"}, "/click": {"selector": "a"},
            "/fill": {"selector": "a", "value": "b"}, "/eval": {"js": "1"},
            "/console": {}, "/screenshot": {},
        }
        self.assertEqual(set(cases), set(self.routes), "a route is missing test coverage")
        for path, args in cases.items():
            status, payload = self.routes[path](self.control, dict(args))
            self.assertEqual(status, 200, path)
            self.assertTrue(payload.get("ok"), path)

    def test_missing_parameter_is_reported_as_a_key_error(self):
        with self.assertRaises(KeyError):
            self.routes["/click"](self.control, {})

    def test_tab_defaults_to_focused(self):
        self.routes["/text"](self.control, {})
        self.assertEqual(self.seen[-1][1][0], None)
        self.routes["/text"](self.control, {"tab": "3"})
        self.assertEqual(self.seen[-1][1][0], 3)

    def test_wait_defaults_on_for_navigation(self):
        self.routes["/navigate"](self.control, {"url": "x.com"})
        _method, args = self.seen[-1]
        self.assertIs(args[2], True)
        self.routes["/navigate"](self.control, {"url": "x.com", "wait": "false"})
        self.assertIs(self.seen[-1][1][2], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
