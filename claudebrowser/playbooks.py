"""Named, ordered lists of API operations -- recorded once, replayed on demand.

"Open the dashboard, log in, click through to the report" is a sequence someone
does every morning. `api.py` already describes every operation this browser can
perform as data, so a playbook needs no new vocabulary: it is a list of
`(op, params)` pairs drawn from that one registry, and replay is the same
dispatch the HTTP route would have done.

No GTK here on purpose -- recording, validation and storage are the parts worth
testing without a display. `browser.py` owns only the replay loop, because that
is the part that has to touch tabs.

**Why a JSON file and not a table in browsing.db.** `store.py`'s SQLite file
exists because history is thousands of rows that have to be ranked, searched and
evicted; none of that is true here. A playbook collection is a handful of small
documents, always read whole, always written whole, and worth being able to open
in an editor, diff, and copy between machines. A table would buy indexes nobody
queries and cost the ability to read your own automation. It sits beside the
database in the same directory (`store.data_dir()`) so "the browser's data" is
still one place.

**Replayed input is untrusted input.** A playbook is a file on disk that an
earlier session -- or a text editor, or an agent -- wrote. Every step is checked
against the registry before anything is dispatched: the op must exist, must be
one of the replayable ones, and every parameter must be declared by that op and
carry a value of the declared shape. There is no `eval` of the file's contents
and no way for a playbook to name a method that is not an `api_*` op.

**Credentials are never recorded.** A step that fills or scripts a
credential-shaped field is dropped at capture time rather than written and
redacted later, so the secret never reaches the file at all. Replay leans on the
browser's own autofill for those fields, which reads the keyring against the
*focused view's* URL -- the only path in this project allowed to hold a secret.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

from . import api, store

#: Bumped only if the on-disk shape changes incompatibly. A file from the
#: future is left alone rather than truncated.
FILE_VERSION = 1

#: Caps, so a runaway recording cannot fill the disk or hand the replay loop a
#: sequence that outlives any timeout the control API could wait on.
MAX_STEPS = 200
MAX_PLAYBOOKS = 100

#: Playbook names double as CLI arguments and dictionary keys, so they are
#: deliberately boring: no slashes (a name is not a path), no leading dot.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")

#: The playbook ops themselves. Recording them would capture the act of
#: recording, and replaying one would let a playbook run another -- a loop with
#: no depth limit and no reason to exist.
NOT_REPLAYABLE = frozenset({
    "playbook-record", "playbook-list", "playbook-run", "playbook-delete",
})

#: What a credential field looks like from the outside. Matched against a CSS
#: selector or a script, never against the value: the value is the secret, and a
#: pattern that has to look at it has already loaded it into a variable we would
#: then have to be careful with. Deliberately broad -- a false positive costs one
#: step the user re-adds by hand, a false negative writes a password to disk.
SECRET_HINT = re.compile(
    r"pass(word|wd|phrase)?|secret|passcode|otp|totp|2fa|mfa|"
    r"one[-_ ]?time|token|api[-_ ]?key|cvv|cvc|\bpin\b|security[-_ ]?code",
    re.IGNORECASE,
)


class PlaybookError(Exception):
    """A playbook (or a step in one) cannot be replayed, and why."""


def default_path():
    return store.data_dir() / "playbooks.json"


# -- the registry rules -----------------------------------------------------

def replayable(name):
    """The Op a step names, or None if it is not something a playbook may run.

    Three ways to fail, all of them the same answer to the caller: the name is
    not in the registry at all, the op has no browser method behind it
    (`/health` is served without touching the browser), or it is one of the
    playbook ops.
    """
    if not isinstance(name, str) or name in NOT_REPLAYABLE:
        return None
    op = api.BY_NAME.get(name)
    if op is None or op.call is None:
        return None
    return op


def is_secret_step(op_name, params):
    """Would recording this step write a credential to disk?

    Only two ops carry free text that could be one: `fill` types a value into a
    field the selector names, and `eval` can carry anything at all. Everything
    else is a URL, a selector, or a number.
    """
    if op_name == "fill":
        return bool(SECRET_HINT.search(str(params.get("selector") or "")))
    if op_name == "eval":
        return bool(SECRET_HINT.search(str(params.get("js") or "")))
    return False


def _coerce(param, value):
    """One parameter value, checked against the type the registry declares.

    Lenient about strings carrying numbers and booleans, because that is how
    they arrive: a query string and an argparse `--flag` have no types, so a
    recorded `--font 20` is the string "20" and rejecting it would make the
    recorder produce playbooks it could not replay.
    """
    if isinstance(value, (dict, list)):
        raise PlaybookError("%s must be a scalar, not %s"
                            % (param.name, type(value).__name__))
    if param.kind == "integer":
        if isinstance(value, bool):
            raise PlaybookError("%s must be an integer" % param.name)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise PlaybookError("%s must be an integer, got %r"
                                % (param.name, value))
    if param.kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise PlaybookError("%s must be true or false, got %r"
                            % (param.name, value))
    if value is None:
        raise PlaybookError("%s has no value" % param.name)
    return str(value)


def validate_step(step):
    """(Op, params) for one step, or raise PlaybookError saying what is wrong.

    This is the whole trust boundary between a file on disk and a dispatch into
    the browser. Nothing downstream re-checks, so nothing here may be skipped
    for a step that "looks fine".
    """
    if not isinstance(step, dict):
        raise PlaybookError("a step must be an object, got %s"
                            % type(step).__name__)
    name = step.get("op")
    op = replayable(name)
    if op is None:
        raise PlaybookError("no such operation %r" % (name,))

    raw = step.get("params") or {}
    if not isinstance(raw, dict):
        raise PlaybookError("%s: params must be an object" % name)

    declared = {p.name: p for p in op.params}
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        # `tab` included: a playbook deliberately does not carry one, so a file
        # that names one is either hand-edited or from a recorder that changed.
        raise PlaybookError("%s: unknown parameter%s %s"
                            % (name, "" if len(unknown) == 1 else "s",
                               ", ".join(unknown)))

    params = {}
    for key, param in declared.items():
        if key in raw:
            params[key] = _coerce(param, raw[key])
        elif param.required:
            raise PlaybookError("%s: missing required parameter %s" % (name, key))
    return op, params


def validate(steps):
    """Every step of a playbook, checked before the first one runs.

    All-or-nothing on purpose: discovering the fourth step is bogus after the
    first three have already navigated and clicked leaves the browser somewhere
    nobody asked for.
    """
    if not isinstance(steps, list) or not steps:
        raise PlaybookError("a playbook needs at least one step")
    if len(steps) > MAX_STEPS:
        raise PlaybookError("a playbook may hold at most %d steps" % MAX_STEPS)
    out = []
    for index, step in enumerate(steps):
        try:
            out.append(validate_step(step))
        except PlaybookError as e:
            raise PlaybookError("step %d: %s" % (index + 1, e))
    return out


def clean_name(name):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise PlaybookError(
            "a playbook name must be 1-64 characters of letters, digits, "
            "spaces, dots, dashes or underscores")
    return name


# -- capture ----------------------------------------------------------------

class TabPrivacy:
    """Which tabs are private, and which one is in front -- without GTK.

    The recorder runs on the control server's HTTP thread, and the answer it
    needs ("is the tab this operation acts on private?") lives in GtkNotebook,
    which that thread may not touch. So the browser mirrors the two facts here
    from the main loop as tabs open, close and are switched to, and the
    recorder reads the mirror.

    Unknown ids answer *private*. A tab id the mirror has never heard of is
    either a stale one or a bug in the bookkeeping, and the cost of the two
    answers is not symmetric: refusing to record a step is a line the user
    re-adds by hand, while recording one writes a private URL to disk.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._private = set()
        self._known = set()
        self._focused = None

    def opened(self, tab_id, private):
        with self._lock:
            self._known.add(tab_id)
            if private:
                self._private.add(tab_id)
            else:
                self._private.discard(tab_id)

    def closed(self, tab_id):
        with self._lock:
            self._known.discard(tab_id)
            self._private.discard(tab_id)
            if self._focused == tab_id:
                self._focused = None

    def focused(self, tab_id):
        with self._lock:
            self._focused = tab_id

    def is_private(self, tab_id=None):
        """Is that tab private? `None` means the tab in front.

        With no tab in front at all -- a window mid-teardown -- the answer is
        private, on the same reasoning as an unknown id.
        """
        with self._lock:
            if tab_id is None:
                tab_id = self._focused
            if tab_id is None or tab_id not in self._known:
                return True
            return tab_id in self._private


def target_tab(args):
    """The tab id an operation's arguments name, or None for "the one in front".

    `tab` is optional everywhere in the registry precisely so a caller can mean
    the focused tab, which is why the recorder cannot read privacy off the
    parameters alone -- it has to ask the browser what the focused tab is.
    """
    raw = (args or {}).get("tab")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class Recorder:
    """What the browser is currently capturing, if anything.

    Lives on the Browser but is touched from the control server's HTTP thread,
    which is why it holds a lock and why it is plain Python with no GTK in it:
    the capture point is `Control._handle`, the one funnel every API-initiated
    operation already passes through, so nothing has to be remembered to add a
    new op to a recording later.
    """

    def __init__(self, privacy=None):
        self._lock = threading.Lock()
        # The browser's privacy mirror, or None in a test that has no browser.
        # Without one nothing is private, which is the pre-existing behaviour
        # and the only sane answer when there are no tabs to ask about.
        self.privacy = privacy
        self.name = None
        self.steps = []
        self.skipped = 0
        self.skipped_private = 0
        self.started = None

    @property
    def active(self):
        return self.name is not None

    def start(self, name):
        name = clean_name(name)
        with self._lock:
            self.name = name
            self.steps = []
            self.skipped = 0
            self.skipped_private = 0
            self.started = time.time()
        return name

    def observe(self, op_name, args, ok=True):
        """Note one completed operation, if it belongs in the recording.

        Failures are dropped: a click on a selector that was not there is not
        part of the sequence worth repeating, and keeping it would make every
        replay fail at the same step.

        `tab` is deliberately never recorded. A tab id is valid for one session;
        a playbook replayed tomorrow that hard-codes tab 3 acts on whatever
        happens to hold that id. Omitting it means every step targets the
        focused tab, which is what the registry already treats as the default
        and what a recorded sequence actually did.

        That same omission is why privacy has to be resolved here from the
        browser rather than from `args`: a step carrying no tab id is a step
        aimed at whatever is in front, and a navigate in a private tab writes
        its URL to disk verbatim -- `is_secret_step` never looks at a URL, so
        a magic link or an `?access_token=` would land in the file intact.
        """
        if not ok:
            return False
        op = replayable(op_name)
        if op is None:
            return False
        if self.privacy is not None and self.privacy.is_private(target_tab(args)):
            with self._lock:
                if self.name is None:
                    return False
                self.skipped_private += 1
            return False
        declared = {p.name for p in op.params}
        params = {k: v for k, v in (args or {}).items()
                  if k in declared and v not in (None, "")}
        with self._lock:
            if self.name is None:
                return False
            if is_secret_step(op_name, params):
                # Not written and not redacted-in-place: the value never enters
                # the recording, so there is no copy to leak later.
                self.skipped += 1
                return False
            if len(self.steps) >= MAX_STEPS:
                return False
            self.steps.append({"op": op_name, "params": params})
            return True

    def stop(self):
        with self._lock:
            state = (self.name, list(self.steps), self.skipped)
            self.name, self.steps, self.skipped, self.started = None, [], 0, None
            self.skipped_private = 0
        return state

    def cancel(self):
        return self.stop()[0]

    def status(self):
        """What is being captured, including what is being refused.

        `skipped_private` and `private_now` are here because a recorder that
        silently declines every step in a private tab is indistinguishable from
        a broken one: the page and the API both render this, so "nothing was
        recorded" comes with the reason attached.
        """
        with self._lock:
            state = {"recording": self.name is not None, "name": self.name,
                     "steps": len(self.steps), "skipped_secrets": self.skipped,
                     "skipped_private": self.skipped_private,
                     "since": int(self.started) if self.started else None}
        state["private_now"] = bool(self.privacy is not None
                                    and self.privacy.is_private())
        return state


# -- storage ----------------------------------------------------------------

class Playbooks:
    """The saved collection, read fresh from disk on every call.

    Uncached for the same reason `envfile.values()` is: the file is small, the
    calls are rare and user-initiated, and a cache means an edit made in a text
    editor is invisible until a restart.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()

    def _read(self):
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(doc, dict):
            return {}
        books = doc.get("playbooks")
        return books if isinstance(books, dict) else {}

    def _write(self, books):
        """Replace the file atomically.

        A half-written playbook file is a collection that no longer parses, and
        `_read` would answer "you have no playbooks" -- which reads as data loss
        rather than as a failed write.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": FILE_VERSION, "playbooks": books},
                             indent=2, sort_keys=True, ensure_ascii=False)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        # Before the rename, not after, and on the temporary file: a playbook
        # holds the URLs and selectors of the user's own logged-in workflows,
        # and the default umask would leave both this file and a crash-orphaned
        # `.tmp` world-readable. Same order as every atomic write in envfile.py.
        tmp.chmod(0o600)
        os.replace(tmp, self.path)

    def get(self, name):
        book = self._read().get((name or "").strip())
        return book if isinstance(book, dict) else None

    def names(self):
        return sorted(self._read())

    def summaries(self):
        """One line per playbook, for `playbook-list` and the CLI."""
        out = []
        for name in self.names():
            book = self.get(name) or {}
            steps = book.get("steps") or []
            out.append({
                "name": name,
                "steps": len(steps),
                "ops": [s.get("op") for s in steps if isinstance(s, dict)],
                "created": book.get("created"),
                "skipped_secrets": book.get("skipped_secrets", 0),
            })
        return out

    def save(self, name, steps, skipped=0):
        name = clean_name(name)
        validate(steps)          # never store something that cannot be replayed
        books = self._read()
        if name not in books and len(books) >= MAX_PLAYBOOKS:
            raise PlaybookError("at most %d playbooks; delete one first"
                                % MAX_PLAYBOOKS)
        books[name] = {
            "name": name,
            "created": int(time.time()),
            "skipped_secrets": int(skipped),
            "steps": [{"op": s["op"], "params": dict(s.get("params") or {})}
                      for s in steps],
        }
        self._write(books)
        return books[name]

    def delete(self, name):
        books = self._read()
        if (name or "").strip() not in books:
            return False
        books.pop(name.strip())
        self._write(books)
        return True
