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
"""

import json
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8765


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
                    return control._send(self, 400, {"ok": False, "error": "body must be an object"})
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

    def _call(self, method, *args, timeout=45):
        """Run `browser.<method>(*args, callback)` on the GTK main loop and
        block this HTTP thread until the callback fires."""
        from gi.repository import GLib

        box = queue.Queue(1)

        def on_main():
            try:
                getattr(self.browser, method)(*args, box.put)
            except Exception:
                box.put({"ok": False, "error": traceback.format_exc(limit=3)})
            return GLib.SOURCE_REMOVE

        GLib.idle_add(on_main)
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "timed out after %ss" % timeout}

    def _handle(self, handler, body):
        url = urlparse(handler.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        args = dict(query)
        args.update(body)  # a POST body wins over a duplicated query param

        if self.token and args.pop("token", None) != self.token:
            supplied = (handler.headers.get("authorization") or "").removeprefix("Bearer ")
            if supplied != self.token:
                return self._send(handler, 401, {"ok": False, "error": "bad token"})

        route = ROUTES.get(url.path)
        if route is None:
            return self._send(
                handler, 404, {"ok": False, "error": "no such route", "routes": sorted(ROUTES)}
            )
        try:
            status, payload = route(self, args)
        except KeyError as e:
            status, payload = 400, {"ok": False, "error": "missing parameter %s" % e}
        except Exception:
            status, payload = 500, {"ok": False, "error": traceback.format_exc(limit=4)}
        self._send(handler, status, payload)


# -- routes -----------------------------------------------------------------
# Each takes (control, args) and returns (status, payload). `tab` is optional
# everywhere; omitting it means "the tab the user is looking at".


def _tab(args):
    raw = args.get("tab")
    return int(raw) if raw not in (None, "") else None


def _truthy(value, default=True):
    if value is None:
        return default
    return str(value).lower() not in ("0", "false", "no", "")


def r_health(c, a):
    return 200, {"ok": True, "browser": "claude-browser", "engine": "webkit2gtk"}


def r_tabs(c, a):
    return 200, c._call("api_tabs")


def r_open(c, a):
    return 200, c._call(
        "api_open", a["url"], _truthy(a.get("background"), False),
        _truthy(a.get("wait")), timeout=90,
    )


def r_navigate(c, a):
    return 200, c._call("api_navigate", _tab(a), a["url"], _truthy(a.get("wait")), timeout=90)


def r_back(c, a):
    return 200, c._call("api_history", _tab(a), -1, _truthy(a.get("wait")), timeout=90)


def r_forward(c, a):
    return 200, c._call("api_history", _tab(a), 1, _truthy(a.get("wait")), timeout=90)


def r_reload(c, a):
    return 200, c._call("api_reload", _tab(a), _truthy(a.get("wait")), timeout=90)


def r_close(c, a):
    return 200, c._call("api_close", _tab(a))


def r_wait(c, a):
    return 200, c._call("api_wait", _tab(a), timeout=120)


def r_text(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.TEXT)


def r_markdown(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.MARKDOWN)


def r_links(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.LINKS)


def r_html(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.HTML)


def r_find(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.find(a["q"]))


def r_click(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.click(a["selector"]))


def r_fill(c, a):
    from . import extract

    return 200, c._call("api_eval", _tab(a), extract.fill(a["selector"], a["value"]))


def r_eval(c, a):
    return 200, c._call("api_eval", _tab(a), a["js"])


def r_console(c, a):
    return 200, c._call("api_console", _tab(a), a.get("pattern"))


def r_screenshot(c, a):
    result = c._call("api_screenshot", _tab(a), a.get("path"), timeout=60)
    if result.get("ok") and result.get("png"):
        return 200, result.pop("png")  # raw bytes when no path was given
    return 200, result


ROUTES = {
    "/health": r_health,
    "/tabs": r_tabs,
    "/open": r_open,
    "/navigate": r_navigate,
    "/back": r_back,
    "/forward": r_forward,
    "/reload": r_reload,
    "/close": r_close,
    "/wait": r_wait,
    "/text": r_text,
    "/markdown": r_markdown,
    "/links": r_links,
    "/html": r_html,
    "/find": r_find,
    "/click": r_click,
    "/fill": r_fill,
    "/eval": r_eval,
    "/console": r_console,
    "/screenshot": r_screenshot,
}
