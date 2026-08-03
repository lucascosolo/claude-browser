"""The agent-facing surface: a small JSON/HTTP server on loopback.

This is the reason the browser exists. A Claude agent drives the same window a
human is looking at -- same cookies, same logged-in session, same rendering --
instead of a headless clone that behaves subtly differently.

Two rules shape everything here:

  * Bound to 127.0.0.1 only, never 0.0.0.0. This endpoint can read any page the
    user is signed into; it must not be reachable off-box.
  * Every browser touch is marshalled onto the GTK main loop and waited on.
    WebKit and GTK are not thread-safe, and calling into them from the HTTP
    thread crashes in ways that look like unrelated rendering bugs.

The routes themselves are not written here -- they come from api.OPS, which is
also what generates `cbctl`'s subcommands and `cb-mcp`'s tools. This file is
just the server.
"""

import json
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import api

DEFAULT_PORT = 8765


def on_main_loop(browser, method, args, timeout=45):
    """Run `browser.<method>(*args, done)` on the GTK main loop and block the
    calling thread until `done` fires.

    The one bridge between "a thread that may block" and "the only thread
    allowed to touch WebKit". Both the HTTP server and the in-browser agent loop
    go through here; they used to have a copy each.

    Never call this *from* the GTK thread -- it would wait on a loop that cannot
    run while it waits.
    """
    from gi.repository import GLib

    box = queue.Queue(1)

    def invoke():
        try:
            getattr(browser, method)(*args, box.put)
        except Exception:
            box.put({"ok": False, "error": traceback.format_exc(limit=3)})
        return GLib.SOURCE_REMOVE

    GLib.idle_add(invoke)
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        return {"ok": False, "error": "timed out after %ss" % timeout}


class Control:
    def __init__(self, browser, port=DEFAULT_PORT, token=None):
        self.browser = browser
        self.port = port
        self.token = token
        self._server = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        control = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass  # the browser's stdout belongs to the user, not to access logs

            def do_GET(self):
                control._handle(self, {})

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError as e:
                    return control._send(self, 400, {"ok": False, "error": str(e)})
                if not isinstance(body, dict):
                    return control._send(
                        self, 400, {"ok": False, "error": "body must be an object"})
                control._handle(self, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        if self._server:
            self._server.shutdown()

    # -- plumbing -----------------------------------------------------------

    def _send(self, handler, status, payload):
        if isinstance(payload, bytes):
            data, ctype = payload, "application/octet-stream"
        else:
            data = json.dumps(payload).encode()
            ctype = "application/json"
        handler.send_response(status)
        handler.send_header("content-type", ctype)
        handler.send_header("content-length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _authorized(self, handler, args):
        if not self.token:
            return True
        if args.pop("token", None) == self.token:
            return True
        supplied = (handler.headers.get("authorization") or "").removeprefix("Bearer ")
        return supplied == self.token

    def _record(self, op, args, payload):
        """Offer one completed operation to the playbook recorder.

        This is the single point every API-initiated operation passes through --
        cbctl, cb-mcp and any HTTP caller all arrive here -- so a playbook picks
        up a new op the moment it is added to api.OPS, with nothing to remember.
        The recorder decides what is worth keeping; this only has to not fail.
        """
        recorder = getattr(self.browser, "recorder", None)
        if recorder is None or not recorder.active:
            return
        try:
            ok = not isinstance(payload, dict) or payload.get("ok", True)
            recorder.observe(op.name, args, ok=bool(ok))
        except Exception:
            pass  # a recording must never be the thing that fails a request

    def _handle(self, handler, body):
        url = urlparse(handler.path)
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        args.update(body)  # a POST body wins over a duplicated query param

        if not self._authorized(handler, args):
            return self._send(handler, 401, {"ok": False, "error": "bad token"})

        op = api.BY_ROUTE.get(url.path)
        if op is None:
            return self._send(handler, 404, {
                "ok": False, "error": "no such route",
                "routes": sorted(o.route for o in api.OPS)})

        # /health answers without touching the browser, so it stays truthful
        # even if the GTK loop is wedged -- which is exactly when something is
        # asking whether the browser is alive.
        if op.call is None:
            return self._send(handler, 200, {
                "ok": True, "browser": "claude-browser", "engine": "webkit2gtk",
                "routes": sorted(o.route for o in api.OPS)})

        try:
            method, call_args = op.call(self, args)
            payload = on_main_loop(self.browser, method, call_args, timeout=op.timeout)
        except KeyError as e:
            return self._send(handler, 400,
                              {"ok": False, "error": "missing parameter %s" % e})
        except Exception:
            return self._send(handler, 500,
                              {"ok": False, "error": traceback.format_exc(limit=4)})

        self._record(op, args, payload)

        # /screenshot with no path answers with the PNG itself rather than JSON.
        if isinstance(payload, dict) and payload.get("ok") and payload.get("png"):
            return self._send(handler, 200, payload.pop("png"))
        self._send(handler, 200, payload)
