"""Talking to a running browser from outside it.

`cbctl`, `cb-mcp` and the second-instance handoff in __main__ all need the same
three things: find the port and token the way the browser itself would, build a
request, and turn a dead socket into a sentence rather than a traceback. That
was written out three times, with three slightly different answers for the
failure case.

Nothing here imports GTK. It is the client half, and it has to work when the
browser is not running at all -- which is the interesting case.
"""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode

from . import envfile

DEFAULT_PORT = 8765


def base_url():
    """Where the running browser should be. Settings file first, same as the
    browser -- a CB_PORT set there has to reach every entry point or `cbctl`
    quietly talks to the wrong place."""
    explicit = envfile.setting("CB_URL")
    if explicit:
        return explicit.rstrip("/")
    return "http://127.0.0.1:%s" % (envfile.setting("CB_PORT") or DEFAULT_PORT)


def token():
    return envfile.setting("CB_TOKEN") or None


def call(route, method="GET", params=None, timeout=180, raw=False):
    """One control-API request. Returns the decoded JSON, or an ok:false dict.

    Never raises for a browser that is down: every caller here is either a CLI
    printing to a terminal or an MCP tool answering a model, and both want the
    reason as data. `raw` returns the response bytes for endpoints that can
    answer with a PNG instead of JSON.
    """
    params = dict(params or {})
    url = base_url() + route
    headers = {}
    tok = token()
    if tok:
        headers["authorization"] = "Bearer " + tok

    data = None
    if method == "POST":
        data = json.dumps(params).encode()
        headers["content-type"] = "application/json"
    elif params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urlencode(clean)

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.headers.get("content-type", "").startswith("application/json"):
                return json.loads(body)
            return {"ok": True, "bytes": len(body), "_raw": body} if raw else {"ok": True}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        try:
            return json.loads(detail)
        except ValueError:
            return {"ok": False, "error": "HTTP %s: %s" % (e.code, detail)}
    except urllib.error.URLError as e:
        return {"ok": False, "error": unreachable(e)}
    except OSError as e:
        return {"ok": False, "error": unreachable(e)}


def unreachable(reason):
    return ("cannot reach claude-browser at %s (%s). Is it running? Try: cb"
            % (base_url(), reason))


def is_running(timeout=2):
    """True when something on the control port answers as our browser.

    The identity check matters: the handoff in __main__ hands a URL to whatever
    is on 8765, and giving a user's URL to an unrelated service because it
    happened to take the port would be worse than opening a second window.
    """
    result = call("/health", timeout=timeout)
    return bool(result.get("ok")) and result.get("browser") == "claude-browser"


def handoff(urls, timeout=5):
    """Ask the running browser to open `urls`, for a second launch.

    Returns True when every URL was accepted. This is what makes the browser
    usable as the system default: `xdg-open` runs `claude-browser <url>` with no
    idea one is already up, and without this that second process just dies on a
    bound port and the link never opens.

    `wait` is false on purpose -- the caller is a launcher that should exit as
    soon as the window has the URL, not sit through the page load.
    """
    if not is_running(timeout=timeout):
        return False
    for url in urls:
        result = call("/open", "POST", {"url": url, "wait": False}, timeout=timeout)
        if not result.get("ok"):
            return False
    return True


def present(timeout=5):
    """Raise the existing window to the front. Best-effort: a URL that landed in
    a window you cannot see has only half worked, but a window manager that
    refuses the request is not a reason to report failure."""
    return bool(call("/present", "POST", timeout=timeout).get("ok"))


def autostart(command=None, wait_seconds=25):
    """Start the browser and block until its control port answers.

    For cb-mcp: a tool call that fails with "start it with `cb`" makes the
    browser something Claude has to be told about, which is exactly what stops
    it being the default. Starting it on demand makes the first tool call work
    from cold.

    Detached deliberately -- setsid + its own session -- so the browser outlives
    the MCP server that spawned it and never inherits its stdio.
    """
    import subprocess
    import time

    if is_running():
        return True
    if str(os.environ.get("CB_AUTOSTART", "1")).lower() in ("0", "false", "no"):
        return False

    command = command or [str(_launcher())]
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False

    # Polled rather than slept: on this machine a cold start is a few seconds,
    # but a warm one is well under one, and a fixed sleep would pay the worst
    # case every time.
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if is_running(timeout=1):
            return True
        time.sleep(0.4)
    return False


def _launcher():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent / "cb"
