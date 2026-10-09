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
from claudebrowser.urls import (HostWarmer, looks_like_url,  # noqa: E402
                                normalize, omnibox_allows, prefetch_host)


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


class TestPrefetchHost(unittest.TestCase):
    """DNS preconnect must agree with the navigate-vs-search decision. Warming
    a name for something that is about to be a search query is a lookup for a
    host nobody visits -- and, on a shared resolver, a leak of what was typed."""

    def test_navigable_hosts_are_extracted(self):
        cases = {
            "example.com": "example.com",
            "https://example.com/a/b?c=d#e": "example.com",
            "http://sub.domain.co.uk:8443/x": "sub.domain.co.uk",
            "EXAMPLE.COM/Path": "example.com",
            "https://user:pw@example.com/x": "example.com",
            "  example.com  ": "example.com",
        }
        for text, expected in cases.items():
            self.assertEqual(prefetch_host(text), expected, text)

    def test_search_queries_are_never_prefetched(self):
        for text in ["webkit gtk python", "what is a typelib", "rust", "",
                     "install gir1.2-webkit2-4.1", "   "]:
            self.assertIsNone(prefetch_host(text), text)

    def test_nothing_with_a_name_to_resolve_is_prefetched(self):
        # Internal pages, the filesystem, inline data, literal addresses and
        # loopback all resolve nothing; a lookup for them is pure waste.
        for text in ["cb:home", "about:blank", "file:///tmp/a.html",
                     "data:text/html,hi", "localhost:5173",
                     "127.0.0.1:8788/api/health", "[::1]:8080/x",
                     "ftp://files.example.com/x"]:
            self.assertIsNone(prefetch_host(text), text)


class TestHostWarmer(unittest.TestCase):
    def setUp(self):
        self.warmed = []
        self.warmer = HostWarmer(self.warmed.append)

    def test_a_host_is_warmed_once_however_often_it_is_typed(self):
        for text in ["example.com", "example.com/a", "https://example.com/b",
                     "example.com"]:
            self.warmer.consider(text)
        self.assertEqual(self.warmed, ["example.com"])

    def test_distinct_hosts_each_get_one_lookup(self):
        self.warmer.consider("example.com")
        self.warmer.consider("other.example.org/x")
        self.assertEqual(self.warmed, ["example.com", "other.example.org"])

    def test_a_search_query_warms_nothing(self):
        self.assertIsNone(self.warmer.consider("how do i center a div"))
        self.assertEqual(self.warmed, [])

    def test_the_seen_set_is_bounded(self):
        """A window open for days must not accumulate every host ever typed."""
        warmer = HostWarmer(self.warmed.append, limit=3)
        for i in range(7):
            warmer.consider("host%d.example" % i)
        self.assertEqual(len(self.warmed), 7)
        self.assertLessEqual(len(warmer._seen), 3)


class TestOmniboxInAPrivateTab(unittest.TestCase):
    """H7: there is one omnibox for the window, so everything hanging off its
    "changed" signal has to ask whether the tab in front is private."""

    def test_an_ordinary_tab_gets_all_three(self):
        self.assertEqual(omnibox_allows(False),
                         {"prefetch": True, "history": True, "recall": True})

    def test_a_private_tab_gets_none_of_them(self):
        allows = omnibox_allows(True)
        self.assertFalse(allows["prefetch"])   # a DNS query leaves the machine
        self.assertFalse(allows["history"])    # persistent rows in the dropdown
        self.assertFalse(allows["recall"])     # snippets of pages read earlier

    def test_the_policy_covers_every_helper_hung_off_the_signal(self):
        """A fourth suggestion source must be added here, not beside it."""
        self.assertEqual(sorted(omnibox_allows(False)),
                         ["history", "prefetch", "recall"])


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

                if parsed.path == "/health":
                    payload = {"ok": True, "browser": "claude-browser",
                               "engine": "webkit2gtk"}
                elif parsed.path == "/text":
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
        # shutdown() only stops serve_forever; the listening socket stays open
        # and the run ended on a `ResourceWarning: unclosed socket`.
        self.server.server_close()


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


class TestApiRegistry(unittest.TestCase):
    """api.OPS is the single description of the browser's surface. control.py,
    cbctl and cb-mcp are all generated from it, so the things worth asserting
    are that it is internally consistent and that nothing has been dropped."""

    def setUp(self):
        from claudebrowser import api

        self.api = api
        self.seen = []

    def dispatch(self, route, args):
        """Run an op's call builder without a browser behind it."""
        op = self.api.BY_ROUTE[route]
        method, call_args = op.call(None, dict(args))
        self.seen.append((method, call_args))
        return method, call_args

    def test_every_op_builds_a_call(self):
        cases = {
            "/tabs": {}, "/present": {}, "/restart": {}, "/open": {"url": "x.com"},
            "/navigate": {"url": "x.com"}, "/back": {}, "/forward": {}, "/reload": {},
            "/close": {}, "/wait": {}, "/text": {}, "/markdown": {}, "/links": {},
            "/html": {}, "/reader": {}, "/simplify": {},
            "/find": {"q": "a"}, "/snapshot": {}, "/click": {"selector": "a"},
            "/state": {}, "/changes": {},
            "/fill": {"selector": "a", "value": "b"},
            "/fill/many": {"fields": '{"a": "b"}'}, "/eval": {"js": "1"},
            "/console": {}, "/screenshot": {}, "/recall": {"q": "a"},
            "/machine": {}, "/discard": {}, "/storage": {},
            "/clear": {"kind": "pagetext"},
            "/playbook/record": {"action": "status"}, "/playbook/list": {},
            "/playbook/run": {"name": "login"}, "/playbook/delete": {"name": "login"},
            "/persona": {}, "/settings": {}, "/vpn": {},
            "/blocked": {}, "/profile": {}, "/profile/set": {"key": "first_name"},
            "/bookmarks": {}, "/bookmark/add": {},
            "/bookmark/remove": {"url": "https://example.com"},
            "/history": {"q": "a"}, "/history/clear": {}, "/downloads": {},
            "/import-chrome": {}, "/passwords/import-csv": {"path": "x.csv"},
            "/passwords/save": {"origin": "https://example.com", "username": "a",
                                "password": "b"},
            "/wait/for": {"selector": "a"}, "/scroll": {"to": "bottom"},
            "/tables": {},
            "/press": {"key": "Enter"}, "/type": {"text": "hi"},
            "/clear/field": {"selector": "#q"},
            "/select": {"selector": "#s", "value": "a"},
            "/hover": {"selector": "a"}, "/submit": {},
            "/dialogs": {}, "/upload": {"selector": "input", "path": "/x"},
            "/download": {"url": "https://x", "path": "/x"},
            "/network": {}, "/pdf": {"path": "/x.pdf"},
        }
        # /health is served without touching the browser, so it has no builder.
        callable_routes = {op.route for op in self.api.OPS if op.call}
        self.assertEqual(set(cases), callable_routes,
                         "an operation is missing test coverage")
        for route, args in cases.items():
            method, call_args = self.dispatch(route, args)
            self.assertTrue(method.startswith("api_"), route)
            self.assertIsInstance(call_args, tuple, route)

    def test_scoped_reads_use_the_constant_without_a_selector(self):
        for route, const, key in [("/text", "TEXT", None), ("/markdown", "MARKDOWN", None),
                                  ("/links", "LINKS", None), ("/html", "HTML", None)]:
            method, (tab, js) = self.dispatch(route, {})
            self.assertEqual(method, "api_eval", route)
            self.assertEqual(js, getattr(extract, const), route)
            method, (tab, js) = self.dispatch(route, {"selector": "#main"})
            self.assertEqual(method, "api_eval", route)
            self.assertIn(extract._js_str("#main"), js, route)
            self.assertNotEqual(js, getattr(extract, const), route)
        method, (tab, js) = self.dispatch("/find", {"q": "x"})
        self.assertEqual(method, "api_eval")
        self.assertEqual(js, extract.find("x"))
        method, (tab, js) = self.dispatch("/find", {"q": "x", "selector": "#main"})
        self.assertIn(extract._js_str("#main"), js)

    def test_chunk1_ops_dispatch_to_their_methods(self):
        self.assertEqual(self.dispatch("/wait/for", {"selector": "a"})[0], "api_wait_for")
        self.assertEqual(self.dispatch("/scroll", {"to": "bottom"})[0], "api_scroll")
        self.assertEqual(self.dispatch("/tables", {})[0], "api_eval")

    def test_chunk3_ops_dispatch(self):
        d = self.dispatch
        self.assertEqual(d("/dialogs", {}), ("api_dialogs", (None, False)))
        self.assertEqual(d("/dialogs", {"clear": "1"}), ("api_dialogs", (None, True)))
        self.assertEqual(d("/upload", {"selector": "input", "path": "/x", "tab": "3"}),
                         ("api_upload", (3, "input", "/x")))
        self.assertEqual(d("/download", {"url": "https://x", "path": "/x"}),
                         ("api_download", (None, "https://x", "/x", False)))
        got = d("/download", {"url": "https://x", "path": "/x", "overwrite": "true"})
        self.assertEqual(got, ("api_download", (None, "https://x", "/x", True)))

    def test_chunk3_op_shape(self):
        by = self.api.BY_NAME
        self.assertEqual(by["download"].timeout, 300)
        self.assertEqual(by["upload"].timeout, 45)
        for name in ("dialogs", "upload", "download"):
            self.assertTrue(by[name].mcp, name)
        self.assertEqual(by["dialogs"].method, "GET")
        self.assertEqual(by["upload"].method, "POST")
        self.assertEqual(by["download"].method, "POST")

        def required(name):
            return {p.name for p in by[name].params if p.required}
        self.assertTrue({"selector", "path"} <= required("upload"))
        self.assertTrue({"url", "path"} <= required("download"))

    def test_chunk4_ops_dispatch(self):
        d = self.dispatch
        self.assertEqual(d("/network", {}), ("api_network", (None, None, None, False)))
        self.assertEqual(
            d("/network", {"pattern": "x", "limit": "5", "clear": "1", "tab": "2"}),
            ("api_network", (2, "x", 5, True)))
        self.assertEqual(d("/pdf", {"path": "/x.pdf"}),
                         ("api_pdf", (None, "/x.pdf", False)))
        self.assertEqual(d("/pdf", {"path": "/x.pdf", "overwrite": "true"}),
                         ("api_pdf", (None, "/x.pdf", True)))
        self.assertEqual(d("/wait/for", {"selector": "a"}),
                         ("api_wait_for", (None, "a", None, None, False, None, False, 500, None)))
        self.assertEqual(d("/wait/for", {"idle": "1"}),
                         ("api_wait_for", (None, None, None, None, False, None, True, 500, None)))
        self.assertEqual(d("/wait/for", {"idle": "true", "quiet_ms": "800"})[1][-3:-1],
                         (True, 800))
        self.assertEqual(d("/screenshot", {}),
                         ("api_screenshot", (None, None, False, None)))
        self.assertEqual(
            d("/screenshot", {"path": "/x.png", "full": "1", "selector": "#a"}),
            ("api_screenshot", (None, "/x.png", True, "#a")))

    def test_chunk4_op_shape(self):
        by = self.api.BY_NAME

        def params(name):
            return {p.name: p for p in by[name].params}
        net = by["network"]
        self.assertEqual(net.method, "GET")
        self.assertTrue(net.mcp)
        self.assertTrue({"pattern", "limit", "clear"} <= set(params("network")))
        self.assertEqual(params("network")["limit"].kind, "integer")
        self.assertEqual(params("network")["clear"].kind, "boolean")
        pdf = by["pdf"]
        self.assertEqual((pdf.method, pdf.route, pdf.timeout), ("POST", "/pdf", 90))
        self.assertTrue(pdf.mcp)
        self.assertTrue(params("pdf")["path"].required)
        self.assertEqual(params("wait-for")["idle"].kind, "boolean")
        self.assertEqual(params("wait-for")["quiet_ms"].kind, "integer")
        self.assertEqual(params("screenshot")["full"].kind, "boolean")
        self.assertIn("selector", params("screenshot"))
        tools = {t["name"] for t in self.api.mcp_tools()}
        self.assertIn("browser_network", tools)
        self.assertIn("browser_pdf", tools)

    def test_chunk2_ops_dispatch(self):
        for route, args, needle in [
                ("/press", {"key": "Ctrl+K"}, "KeyK"),
                ("/type", {"text": "</script>hi"}, extract._js_str("</script>hi")),
                ("/type", {"text": "x", "clear": True}, "selectNodeContents"),
                ("/clear/field", {"selector": "</script>"}, extract._js_str("</script>")),
                ("/select", {"selector": "#s", "value": "a"}, extract._js_str("#s")),
                ("/hover", {"selector": "a.x"}, extract._js_str("a.x"))]:
            method, (tab, js) = self.dispatch(route, args)
            self.assertEqual(method, "api_eval", route)
            self.assertIn(needle, js, route)
        self.assertEqual(self.dispatch("/submit", {})[0], "api_submit")

    def test_acting_ops_carry_force_and_reads_do_not(self):
        acting = {o.name for o in self.api.OPS if o.acts}
        self.assertEqual(acting, {"click", "fill", "fill-many", "press", "type",
                                  "clear-field", "select", "hover", "submit",
                                  "upload"})
        for op in self.api.OPS:
            names = [p.name for p in op.params]
            self.assertEqual("force" in names, op.acts, op.name)

    def test_state_and_changes_dispatch(self):
        self.assertEqual(self.dispatch("/state", {})[0], "api_state")
        self.assertEqual(self.dispatch("/changes", {"tab": "3"}), ("api_changes", (3,)))

    def test_wait_for_takes_changed_since(self):
        method, args = self.dispatch("/wait/for", {"changed_since": "e:4"})
        self.assertEqual(method, "api_wait_for")
        self.assertEqual(args[-1], "e:4")

    def test_clear_carries_its_kind_through_and_documents_pagetext(self):
        """The page-text cache is the most personal thing on disk, so it is
        clearable from the same op as the cookies -- and api.py is the only
        place that says so."""
        self.assertEqual(self.dispatch("/clear", {"kind": "pagetext"})[1],
                         ("pagetext",))
        self.assertEqual(self.dispatch("/clear", {})[1], ("cache",))
        kind = next(p for p in self.api.BY_NAME["clear"].params if p.name == "kind")
        self.assertIn("pagetext", kind.help)

    def test_health_needs_no_browser(self):
        self.assertIsNone(self.api.BY_NAME["health"].call)

    def test_snapshot_dispatches_to_its_own_browser_method(self):
        method, call_args = self.dispatch("/snapshot", {"tab": "3"})
        self.assertEqual(method, "api_snapshot")
        self.assertEqual(call_args, (3,))

    def test_fill_many_dispatches_the_raw_fields_json_untouched(self):
        # api.py never parses `fields` itself -- resolving {profile:key}
        # placeholders happens natively in Browser.api_fill_many, never here.
        method, call_args = self.dispatch(
            "/fill/many", {"fields": '{"#email": "{profile:email}"}'})
        self.assertEqual(method, "api_fill_many")
        self.assertEqual(call_args, (None, '{"#email": "{profile:email}"}'))

    def test_playbook_run_params_are_optional_and_passed_raw(self):
        # Resolution of the JSON object happens in Browser.api_playbook_run,
        # as fill-many does with its fields; api.py never parses it.
        method, call_args = self.dispatch("/playbook/run", {"name": "login"})
        self.assertEqual(method, "api_playbook_run")
        self.assertEqual(call_args, ("login", None))
        method, call_args = self.dispatch(
            "/playbook/run", {"name": "x", "params": '{"a":"b"}'})
        self.assertEqual(method, "api_playbook_run")
        self.assertEqual(call_args, ("x", '{"a":"b"}'))

    def test_click_and_fill_document_ref_targeting(self):
        # Snapshot's refs are the whole point of the feature -- an agent must
        # be told it can pass one to click/fill without reading extract.py.
        self.assertIn("@ref", self.api.BY_NAME["click"].summary)
        self.assertIn("@ref", self.api.BY_NAME["fill"].summary)
        self.assertIn("@ref", self.api.BY_NAME["fill-many"].summary)

    def test_missing_parameter_is_reported_as_a_key_error(self):
        with self.assertRaises(KeyError):
            self.dispatch("/click", {})

    def test_bookmark_add_defaults_to_the_tabs_own_url_and_title(self):
        # api.py never resolves the tab's own url/title itself -- that is
        # Browser.api_bookmark_add's job, reusing the same lookup
        # toggle_bookmark already does. Here it must simply pass None through.
        method, call_args = self.dispatch("/bookmark/add", {})
        self.assertEqual(method, "api_bookmark_add")
        self.assertEqual(call_args, (None, None, None))

    def test_bookmark_add_carries_an_explicit_url_and_title(self):
        method, call_args = self.dispatch(
            "/bookmark/add", {"url": "https://example.com", "title": "Example"})
        self.assertEqual(call_args, (None, "https://example.com", "Example"))

    def test_bookmark_remove_requires_a_url(self):
        with self.assertRaises(KeyError):
            self.dispatch("/bookmark/remove", {})

    def test_history_requires_a_query(self):
        with self.assertRaises(KeyError):
            self.dispatch("/history", {})
        method, call_args = self.dispatch("/history", {"q": "example", "limit": "5"})
        self.assertEqual(method, "api_history")
        self.assertEqual(call_args, ("example", "5"))

    def test_bookmarks_history_and_downloads_are_tab_free(self):
        for name in ("bookmarks", "history", "downloads"):
            self.assertFalse(self.api.BY_NAME[name].tab, name)

    def test_tab_defaults_to_focused(self):
        self.assertIsNone(self.dispatch("/text", {})[1][0])
        self.assertEqual(self.dispatch("/text", {"tab": "3"})[1][0], 3)

    def test_wait_defaults_on_for_navigation(self):
        self.assertIs(self.dispatch("/navigate", {"url": "x.com"})[1][2], True)
        self.assertIs(
            self.dispatch("/navigate", {"url": "x.com", "wait": "false"})[1][2], False)

    def test_names_routes_and_schemas_are_unique_and_well_formed(self):
        names = [op.name for op in self.api.OPS]
        routes = [op.route for op in self.api.OPS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(routes), len(set(routes)))
        for op in self.api.OPS:
            self.assertIn(op.method, ("GET", "POST"), op.name)
            self.assertTrue(op.route.startswith("/"), op.name)
            self.assertTrue(op.summary.strip(), op.name)
            schema = op.schema()
            for required in schema["required"]:
                self.assertIn(required, schema["properties"], op.name)

    def test_mcp_exposes_every_agent_facing_op(self):
        """The three surfaces drifted before this test existed: cb-mcp was
        missing forward, wait and health, so an agent could start a load it had
        no way to wait for."""
        tools = {t["name"] for t in self.api.mcp_tools()}
        for name in ("browser_forward", "browser_wait", "browser_text",
                     "browser_console", "browser_screenshot"):
            self.assertIn(name, tools)
        # Raising the window is a launcher action, not something an agent should
        # be able to do to the user's focus mid-task.
        self.assertNotIn("browser_present", tools)
        self.assertNotIn("browser_health", tools)
        self.assertIn("browser_blocked", tools)
        self.assertIn("browser_profile", tools)
        self.assertIn("browser_snapshot", tools)
        self.assertIn("browser_fill-many", tools)
        # Writing the user's own profile is not something an agent should be
        # able to do as a side effect of some other goal -- same reasoning as
        # settings/persona.
        self.assertNotIn("browser_profile-set", tools)
        # Reading bookmarks/history/downloads and adding a bookmark are fine
        # for an agent; deleting a bookmark, wiping all history, and other
        # destructive/user-preference actions are not -- same reasoning as
        # `clear` and `settings`.
        self.assertIn("browser_bookmarks", tools)
        self.assertIn("browser_bookmark-add", tools)
        self.assertIn("browser_history", tools)
        self.assertIn("browser_downloads", tools)
        self.assertNotIn("browser_bookmark-remove", tools)
        self.assertNotIn("browser_history-clear", tools)
        self.assertNotIn("browser_import-chrome", tools)
        self.assertNotIn("browser_save-password", tools)

    def test_import_chrome_is_registered_and_not_an_mcp_tool(self):
        op = next(o for o in self.api.OPS if o.name == "import-chrome")
        self.assertEqual(op.method, "POST")
        self.assertFalse(op.mcp)
        self.assertFalse(op.tab)
        tools = {t["name"] for t in self.api.mcp_tools()}
        self.assertNotIn("browser_import-chrome", tools)

    def test_save_password_is_registered_not_an_mcp_tool_and_not_replayable(self):
        from claudebrowser import playbooks

        op = next(o for o in self.api.OPS if o.name == "save-password")
        self.assertEqual(op.method, "POST")
        self.assertFalse(op.mcp)
        self.assertFalse(op.tab)
        password_param = next(p for p in op.params if p.name == "password")
        self.assertEqual(password_param.cli, "secret")
        self.assertIsNone(playbooks.replayable("save-password"))


class TestCbctlSurface(unittest.TestCase):
    """cbctl's subcommands are generated, so the test is that the generation
    covers every op and keeps the two shell-friendly aliases."""

    def parser(self):
        import importlib.util

        spec = importlib.util.spec_from_loader(
            "cbctl_mod", loader=None, origin=str(ROOT / "cbctl"))
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(ROOT / "cbctl")
        exec(compile((ROOT / "cbctl").read_text(), str(ROOT / "cbctl"), "exec"),
             module.__dict__)
        return module.build_parser()

    def test_every_op_has_a_subcommand(self):
        from claudebrowser import api

        actions = [a for a in self.parser()._actions if hasattr(a, "choices") and a.choices]
        commands = set(actions[0].choices)
        expected = {next((alias for alias, target in api.CLI_ALIASES.items()
                          if target == op.name), op.name) for op in api.OPS}
        self.assertEqual(commands, expected)
        self.assertIn("shot", commands)
        self.assertIn("go", commands)


if __name__ == "__main__":
    unittest.main(verbosity=2)
