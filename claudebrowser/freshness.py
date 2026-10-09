"""claudebrowser/freshness.py

Is the caller acting on the page as it is now, or on the page as it was?

An agent reads a page, thinks, and acts. Between the read and the act the page
can navigate, re-render, or grow a modal; the act then lands on something the
agent never saw. Screenshots after every step are the expensive fix. This is
the cheap one: every page carries a mutation counter (extract.STATE_SHIM,
installed at document start) and a per-document epoch, and together they are
a *state token*. Every op that touches the page hands the token back and
stamps the tab as "seen at this token"; every acting op is refused by
control._handle when the token has moved on and the agent has not looked
since. The refusal carries the current state and the text that changed, so
the one round trip it costs is also the round trip that catches the agent up.

Two things the rule is careful about, because a gate that fires on every
ticking clock is a gate that gets switched off:

- A navigation is always stale: the epoch changed, so nothing the agent read
  describes this document.
- Mutations alone are stale only once the last look is older than FRESH_S.
  Live pages mutate constantly (timers, ads, typing echoes), so an agent that
  read two seconds ago and the page ticked once is still acting on what it
  saw; an agent that read a minute ago on a page that has moved is not.

GTK-free: the decision and the diff are tested without a display.
"""

import difflib
import time

#: How long a look at the page stays good on a page that keeps mutating.
FRESH_S = 20

#: The most lines a diff reports on each side; the agent can read the page if
#: it needs more, and a cap keeps a re-rendered feed from flooding a refusal.
DIFF_CAP = 40


def token(state):
    """The state token for a page-state dict, or None when the page has not
    said (no shim yet: about:blank, a cb: page before its script ran)."""
    if not isinstance(state, dict):
        return None
    epoch = state.get("epoch")
    if not epoch:
        return None
    return "%s:%s" % (epoch, int(state.get("mutations") or 0))


def split(tok):
    """(epoch, mutations) of a token, or (None, 0)."""
    if not tok or ":" not in tok:
        return None, 0
    epoch, _, count = tok.rpartition(":")
    try:
        return epoch, int(count)
    except ValueError:
        return epoch, 0


class Seen:
    """What the agent last saw of a tab: the token, when, and the text if a
    read fetched it (so `changes` and a refusal can diff against it)."""

    __slots__ = ("token", "at", "text")

    def __init__(self, token, at=None, text=None):
        self.token = token
        self.at = time.monotonic() if at is None else at
        self.text = text


def stale(seen, state, now=None, fresh_s=FRESH_S):
    """(stale, reason) for acting on a page in `state` after last seeing
    `seen`. Never stale when the page has no token -- a page that cannot
    report cannot be checked, and refusing every act on it is a gate that
    gets disabled."""
    current = token(state)
    if current is None:
        return False, ""
    if seen is None or not seen.token:
        return True, "you have not read this page yet"
    if seen.token == current:
        return False, ""
    now = time.monotonic() if now is None else now
    old_epoch, old_count = split(seen.token)
    new_epoch, new_count = split(current)
    if old_epoch != new_epoch:
        return True, "the page navigated since you last read it"
    age = now - seen.at
    if age < fresh_s:
        return False, ""
    return True, ("the page changed %d times since you last read it, %ds ago"
                  % (max(0, new_count - old_count), int(age)))


def diff(old, new, cap=DIFF_CAP):
    """Line diff of two page texts: what appeared and what disappeared, each
    capped. `truncated` says a side was cut so the agent knows to read."""
    old_lines = [l for l in (old or "").splitlines() if l.strip()]
    new_lines = [l for l in (new or "").splitlines() if l.strip()]
    added, removed = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_lines[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(new_lines[j1:j2])
    return {
        "added": added[:cap],
        "removed": removed[:cap],
        "truncated": len(added) > cap or len(removed) > cap,
    }
