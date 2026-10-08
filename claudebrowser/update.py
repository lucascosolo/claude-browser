"""claudebrowser/update.py

Notice when the checkout this browser runs from has moved, and decide when it
is safe to restart onto it.

The installed launcher (`~/.local/bin/claude-browser`) is a symlink into the
working tree, so the code on disk is already the latest the moment a commit
lands -- what does not follow is the *running* window, and the sessions that
make those commits cannot reach it: an agent session's sandbox sees neither
the control port nor the process. So the browser watches its own repository
and restarts itself. Nothing outside the project has to be touched for a
change to reach the installed application.

Two choices worth recording:

**The trigger is a commit, not a file save.** Reading `.git/HEAD` and the ref it
points at costs two small file reads; a modified-time sweep over every module
would fire in the middle of an editor session, restarting the browser on every
half-written file. A commit is the one moment the tree is meant to be coherent.

**No subprocess.** `git rev-parse` would be simpler to write, but it is a
process spawn every poll from inside the GTK main loop, and `.git` is a
documented format: `HEAD` is either a bare hash or `ref: refs/heads/<name>`,
and a ref is a loose file under `.git/<ref>` or a line in `.git/packed-refs`.
Both are read here with no git on PATH at all.

GTK-free, so the whole decision -- what counts as a change, what counts as
idle -- is tested without a display.
"""

import glob
import os
import subprocess
import sys
import time
from pathlib import Path

#: How often the browser looks at HEAD. Two file reads, so cheap; the delay a
#: person perceives between "pushed" and "restarted" is this plus the idle wait.
POLL_S = 15

#: A restart interrupts whatever the control API is in the middle of, so the
#: browser only restarts when nothing has asked it for anything recently. A
#: playbook step or an agent turn is a request; between steps the gap is well
#: under this, so a running sequence is never cut in half.
QUIET_REQUEST_S = 20

#: Likewise for the person: no keyboard or pointer input for this long before a
#: restart may take their window away mid-sentence.
QUIET_INPUT_S = 30


def repo_root(start=None):
    """The checkout this module runs from, or None when it is not a git tree
    (an installed tarball, a copied directory)."""
    here = Path(start or __file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def head_revision(repo):
    """The commit HEAD points at, as a full hex string, or None if it cannot
    be read. Never raises: a watcher that crashes the main loop over an
    unreadable `.git` is worse than one that silently stops watching."""
    try:
        git = Path(repo) / ".git"
        if git.is_file():
            # A worktree: `.git` is a pointer file to the real gitdir.
            line = git.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            git = Path(line[len("gitdir:"):].strip())
            if not git.is_absolute():
                git = (Path(repo) / git).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head[len("ref:"):].strip()
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
        # A worktree's HEAD ref lives in the common dir, not the worktree's.
        common = git / "commondir"
        if common.is_file():
            shared = (git / common.read_text(encoding="utf-8").strip()).resolve()
            loose = shared / ref
            if loose.is_file():
                return loose.read_text(encoding="utf-8").strip() or None
            git = shared
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


class Watcher:
    """Remembers the revision the running process started on and reports when
    HEAD has moved away from it."""

    def __init__(self, repo=None):
        self.repo = repo if repo is not None else repo_root()
        self.started_on = head_revision(self.repo) if self.repo else None

    def changed(self):
        """The new short revision if HEAD differs from the one this process
        started on, else None. An unreadable HEAD is "no change": the only
        thing a restart could do about it is run the same code again."""
        if not self.repo or not self.started_on:
            return None
        now = head_revision(self.repo)
        if now and now != self.started_on:
            return now[:10]
        return None


def idle(state, now=None):
    """Whether a restart would interrupt anything.

    `state` is a plain dict so the caller can assemble it from GTK objects on
    the main loop and this function can be tested with literals:

        loading               any tab mid-load
        agent_running         the in-browser agent has a turn in flight
        recording             a playbook recording is open
        downloads_active      a download is in progress
        last_request_at       monotonic stamp of the last control-API request,
                              or None if there has never been one
        last_input_at         monotonic stamp of the last key/pointer event
        requests_active       control-API requests not yet answered
        playbook_running      a playbook replay is in progress
        private_tabs          a private tab exists (it would not come back)
        restore_off           CB_RESTORE_SESSION is off (no tab would)

    Returns (ok, reason): `reason` names the first thing in the way, in the
    words the flash message uses, so a user can see why the browser is holding.
    """
    now = time.monotonic() if now is None else now
    if state.get("loading"):
        return False, "a page is loading"
    if state.get("agent_running"):
        return False, "Claude is driving a tab"
    if state.get("recording"):
        return False, "a playbook is recording"
    if state.get("downloads_active"):
        return False, "a download is in progress"
    if state.get("requests_active"):
        return False, "a control-API request is in flight"
    if state.get("playbook_running"):
        return False, "a playbook is running"
    if state.get("private_tabs"):
        # A private tab is not saved in the session, so a restart closes it.
        return False, "a private tab is open"
    if state.get("restore_off"):
        return False, "session restore is off"
    last_request = state.get("last_request_at")
    if last_request is not None and now - last_request < QUIET_REQUEST_S:
        return False, "the control API was used %ds ago" % int(now - last_request)
    last_input = state.get("last_input_at")
    if last_input is not None and now - last_input < QUIET_INPUT_S:
        return False, "the window is in use"
    return True, ""


def enabled(value):
    """`CB_AUTOUPDATE` is on unless spelled off; mirrors settings._off_words."""
    return (value if value is not None else "1").strip().lower() not in (
        "0", "off", "false", "no")


def launcher(repo=None):
    """The `cb` script to exec on restart. Through `cb` and not `python3 -m`,
    because `cb` is the one launcher (CLAUDE.md: the CPU/memory cap lives
    there) -- a restart that bypassed it would be a second launcher."""
    root = repo if repo is not None else repo_root()
    return str(Path(root) / "cb") if root else None


def restart_environ(environ=None):
    """The environment for the exec'd launcher.

    `CB_IN_SCOPE` is kept deliberately: this process is already inside the
    systemd scope `cb` created for it, and `exec` keeps the PID, so the scope
    and its CPU/memory limits stay in force. Re-entering `cb`'s wrapper would
    ask systemd to move a process that is already in a transient scope into a
    new one, which it refuses -- and the browser would never come back.
    """
    env = dict(os.environ if environ is None else environ)
    env["CB_IN_SCOPE"] = "1"
    env["CB_RESTARTED"] = "1"
    return env


#: What the preflight imports. The modules every launch path needs before the
#: window exists; a failure here is a browser that would not come back.
PREFLIGHT_IMPORTS = ("import claudebrowser.api, claudebrowser.extract, "
                     "claudebrowser.settings, claudebrowser.playbooks")
PREFLIGHT_TIMEOUT_S = 60


def preflight(repo, python=sys.executable):
    """(ok, message): does the checkout compile and import?

    Run in a subprocess before an exec, because exec is one-way: a syntax
    error committed mid-edit would otherwise replace a working browser with
    one that never starts. `message` is the tail of stderr on failure.
    """
    sources = sorted(os.path.relpath(p, repo) for p in
                     glob.glob(os.path.join(str(repo), "claudebrowser", "*.py")))
    for cmd in ([python, "-m", "py_compile", *sources],
                [python, "-c", PREFLIGHT_IMPORTS]):
        try:
            done = subprocess.run(cmd, cwd=str(repo), capture_output=True,
                                  text=True, timeout=PREFLIGHT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, "preflight timed out after %ds" % PREFLIGHT_TIMEOUT_S
        except OSError as e:
            return False, "preflight could not run %s: %s" % (python, e)
        if done.returncode != 0:
            err = (done.stderr or "").strip() or "exit status %d" % done.returncode
            return False, err[-500:]
    return True, ""
