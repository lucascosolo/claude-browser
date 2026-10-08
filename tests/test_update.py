"""tests/test_update.py -- the self-update watcher, without git or a display.

Every fixture builds its own fake `.git` in a temporary directory. Nothing
here reads the real repository: the test must pass identically on a detached
HEAD, mid-rebase, or in a tarball.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claudebrowser import update  # noqa: E402

A = "a" * 40
B = "b" * 40


class FakeRepo:
    def __init__(self, root):
        self.root = Path(root)
        self.git = self.root / ".git"
        (self.git / "refs" / "heads").mkdir(parents=True)

    def symbolic(self, branch="master", sha=A, loose=True):
        (self.git / "HEAD").write_text("ref: refs/heads/%s\n" % branch)
        if loose:
            (self.git / "refs" / "heads" / branch).write_text(sha + "\n")
        else:
            (self.git / "packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted \n"
                "%s refs/heads/%s\n^%s\n" % (sha, branch, "c" * 40))

    def detached(self, sha=A):
        (self.git / "HEAD").write_text(sha + "\n")


class HeadRevision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = FakeRepo(self.tmp.name)

    def test_loose_ref(self):
        self.repo.symbolic(sha=A)
        self.assertEqual(update.head_revision(self.repo.root), A)

    def test_packed_ref(self):
        self.repo.symbolic(sha=B, loose=False)
        self.assertEqual(update.head_revision(self.repo.root), B)

    def test_loose_beats_packed(self):
        self.repo.symbolic(sha=A, loose=False)
        (self.repo.git / "refs" / "heads" / "master").write_text(B + "\n")
        self.assertEqual(update.head_revision(self.repo.root), B)

    def test_detached_head(self):
        self.repo.detached(B)
        self.assertEqual(update.head_revision(self.repo.root), B)

    def test_missing_git_is_none_not_an_exception(self):
        self.assertIsNone(update.head_revision(self.tmp.name + "/nowhere"))

    def test_worktree_pointer_file(self):
        main = FakeRepo(tempfile.mkdtemp(dir=self.tmp.name))
        main.symbolic(sha=A, loose=False)
        wt = Path(tempfile.mkdtemp(dir=self.tmp.name))
        gitdir = main.git / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: %s\n" % gitdir)
        (gitdir / "HEAD").write_text("ref: refs/heads/master\n")
        (gitdir / "commondir").write_text("../..\n")
        self.assertEqual(update.head_revision(wt), A)


class WatcherBehaviour(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = FakeRepo(self.tmp.name)
        self.repo.symbolic(sha=A)

    def test_no_change_until_head_moves(self):
        w = update.Watcher(self.repo.root)
        self.assertIsNone(w.changed())
        (self.repo.git / "refs" / "heads" / "master").write_text(B + "\n")
        self.assertEqual(w.changed(), B[:10])

    def test_branch_switch_counts(self):
        w = update.Watcher(self.repo.root)
        (self.repo.git / "refs" / "heads" / "topic").write_text(B + "\n")
        (self.repo.git / "HEAD").write_text("ref: refs/heads/topic\n")
        self.assertEqual(w.changed(), B[:10])

    def test_unreadable_head_is_no_change(self):
        w = update.Watcher(self.repo.root)
        (self.repo.git / "HEAD").unlink()
        self.assertIsNone(w.changed())

    def test_outside_a_repository_never_fires(self):
        w = update.Watcher(self.tmp.name + "/not-a-checkout")
        self.assertIsNone(w.started_on)
        self.assertIsNone(w.changed())


class Idle(unittest.TestCase):
    def test_quiet_browser_is_idle(self):
        ok, reason = update.idle({}, now=1000.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_each_blocker_names_itself(self):
        for state, word in [({"loading": True}, "loading"),
                            ({"agent_running": True}, "Claude"),
                            ({"recording": True}, "recording"),
                            ({"downloads_active": True}, "download")]:
            ok, reason = update.idle(state, now=1000.0)
            self.assertFalse(ok, state)
            self.assertIn(word, reason)

    def test_recent_request_blocks_then_expires(self):
        state = {"last_request_at": 1000.0}
        self.assertFalse(update.idle(state, now=1000.0 + update.QUIET_REQUEST_S - 1)[0])
        self.assertTrue(update.idle(state, now=1000.0 + update.QUIET_REQUEST_S + 1)[0])

    def test_recent_input_blocks_then_expires(self):
        state = {"last_input_at": 1000.0}
        self.assertFalse(update.idle(state, now=1000.0 + update.QUIET_INPUT_S - 1)[0])
        self.assertTrue(update.idle(state, now=1000.0 + update.QUIET_INPUT_S + 1)[0])


class Misc(unittest.TestCase):
    def test_enabled_spellings(self):
        for v in (None, "", "1", "on", "yes"):
            self.assertTrue(update.enabled(v), v)
        for v in ("0", "off", "False", "no"):
            self.assertFalse(update.enabled(v), v)

    def test_restart_environ_keeps_the_scope(self):
        env = update.restart_environ({"PATH": "/bin"})
        self.assertEqual(env["CB_IN_SCOPE"], "1")
        self.assertEqual(env["CB_RESTARTED"], "1")
        self.assertEqual(env["PATH"], "/bin")

    def test_launcher_is_cb_in_the_repo(self):
        self.assertTrue(update.launcher("/x/y").endswith("/x/y/cb"))

    def test_repo_root_finds_this_checkout(self):
        root = update.repo_root()
        self.assertIsNotNone(root)
        self.assertTrue((root / "claudebrowser" / "update.py").is_file())


if __name__ == "__main__":
    unittest.main()
