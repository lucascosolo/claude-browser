"""What to do when a page calls alert/confirm/prompt or asks before unload.

GTK-free so the policy table and the log are tested. An agent driving a page
cannot see a modal dialog, and WebKit's own one blocks the page's script until
a person answers it -- so a confirm() behind a click looks to the agent like a
click that did nothing. Under "auto" the browser answers the way a user who
wanted the action would, but only on a tab an agent is driving: a person's own
tab is always "ask", because a confirm() accepted behind their back is an
action they never agreed to. Under "ask" WebKit shows its dialog as before.
Either way the dialog is logged on the tab, so the agent can read what was said.
"""
from collections import deque
from datetime import datetime, timezone

KINDS = ("alert", "confirm", "prompt", "beforeunload")
POLICIES = ("auto", "ask")
MESSAGE_MAX = 2000


def policy(value):
    return "ask" if str(value or "").strip().lower() == "ask" else "auto"


def effective_policy(policy, driven):
    """"auto" only for a tab an agent is driving; every other tab asks."""
    return "auto" if policy == "auto" and driven else "ask"


def answer(kind, policy, default_text=""):
    """(action, value): the action is what the log records as `answered`."""
    if kind not in KINDS:
        raise ValueError("unknown dialog kind: %r" % (kind,))
    if policy == "ask":
        return ("ask", None)
    if kind == "alert":
        return ("close", None)
    if kind == "prompt":
        return ("accept", default_text)
    return ("accept", True)


class Log:
    """The last `limit` dialogs on one tab. Memory only, which is why a private
    tab keeps one too."""

    def __init__(self, limit=50):
        self._ring = deque(maxlen=limit)

    def add(self, kind, message, default, answered):
        entry = {"kind": kind, "message": (message or "")[:MESSAGE_MAX],
                 "default": default, "answered": answered,
                 "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self._ring.append(entry)
        return entry

    def entries(self):
        return list(self._ring)

    def clear(self):
        self._ring.clear()
