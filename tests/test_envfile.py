"""The settings file — the thing that makes a menu-launched window work."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import envfile  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_basic_pairs(self):
        self.assertEqual(envfile.parse("A=1\nB=two\n"), {"A": "1", "B": "two"})

    def test_comments_and_blank_lines_ignored(self):
        text = "# a comment\n\n  \nA=1\n   # indented comment\nB=2\n"
        self.assertEqual(envfile.parse(text), {"A": "1", "B": "2"})

    def test_export_prefix_tolerated(self):
        """So a line pasted straight out of ~/.bashrc works unchanged."""
        self.assertEqual(envfile.parse("export ANTHROPIC_API_KEY=sk-x\n"),
                         {"ANTHROPIC_API_KEY": "sk-x"})

    def test_quotes_stripped_once(self):
        parsed = envfile.parse("A=\"quoted\"\nB='single'\nC=\"has \"inner\" quotes\"\n")
        self.assertEqual(parsed["A"], "quoted")
        self.assertEqual(parsed["B"], "single")
        self.assertEqual(parsed["C"], 'has "inner" quotes')

    def test_value_may_contain_equals(self):
        # Base64 and query-string values routinely do.
        self.assertEqual(envfile.parse("CB_SEARCH=https://x/?q=%s&hl=en\n"),
                         {"CB_SEARCH": "https://x/?q=%s&hl=en"})

    def test_whitespace_trimmed(self):
        self.assertEqual(envfile.parse("  A  =  1  \n"), {"A": "1"})

    def test_garbage_lines_are_skipped_not_fatal(self):
        """A typo in a settings file must not stop the browser starting."""
        self.assertEqual(envfile.parse("nonsense\n=novalue\nA=1\n"), {"A": "1"})

    def test_empty_value_allowed(self):
        self.assertEqual(envfile.parse("CB_TOKEN=\n"), {"CB_TOKEN": ""})


class LoadTest(unittest.TestCase):
    def write(self, text, mode=0o600):
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        os.chmod(handle.name, mode)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_applies_to_environ(self):
        env = {}
        applied = envfile.load(self.write("A=1\nB=2\n"), environ=env)
        self.assertEqual(env, {"A": "1", "B": "2"})
        self.assertEqual(sorted(applied), ["A", "B"])

    def test_the_file_wins_over_the_environment(self):
        """The browser must behave the same from a menu and from a terminal."""
        env = {"A": "from-shell"}
        applied = envfile.load(self.write("A=from-file\nB=2\n"), environ=env)
        self.assertEqual(env["A"], "from-file")
        self.assertEqual(sorted(applied), ["A", "B"])

    def test_says_so_when_it_overrides_the_environment(self):
        warnings = []
        envfile.load(self.write("A=from-file\n"), environ={"A": "from-shell"},
                     warn=warnings.append)
        self.assertTrue(any("overrides" in w for w in warnings), warnings)

    def test_no_warning_when_they_agree(self):
        warnings = []
        envfile.load(self.write("A=same\n"), environ={"A": "same"}, warn=warnings.append)
        self.assertEqual(warnings, [])

    def test_secrets_never_reach_the_environment(self):
        """A child process has no business holding the user's API key."""
        env = {}
        applied = envfile.load(self.write("ANTHROPIC_API_KEY=sk-ant-file\nA=1\n"),
                               environ=env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertIn("ANTHROPIC_API_KEY", applied)

    def test_an_inherited_key_is_removed_not_merely_outranked(self):
        """The bug this exists for: a shell exported a stale key before the
        user fixed their profile, and every child kept inheriting the dead
        value. Anything that reaches for os.environ must not find it."""
        warnings = []
        env = {"ANTHROPIC_API_KEY": "sk-ant-stale"}
        envfile.load(self.write("ANTHROPIC_API_KEY=sk-ant-file\n"), environ=env,
                     warn=warnings.append)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertTrue(any("ignoring" in w for w in warnings), warnings)

    def test_setting_prefers_the_file(self):
        path = self.write("CB_AUTH=api\n")
        self.assertEqual(
            envfile.setting("CB_AUTH", path=path, environ={"CB_AUTH": "subscription"}),
            "api")

    def test_setting_falls_back_to_the_environment(self):
        path = self.write("A=1\n")
        self.assertEqual(
            envfile.setting("CB_AUTH", path=path, environ={"CB_AUTH": "api"}), "api")
        self.assertEqual(envfile.setting("CB_AUTH", "auto", path=path, environ={}),
                         "auto")

    def test_values_reflects_an_edit_without_a_restart(self):
        """A rejected key must be replaceable in the window complaining about it.

        The replacement is the same length and lands in the same instant, which
        is precisely what a size+mtime cache cannot see."""
        path = self.write("ANTHROPIC_API_KEY=one\n")
        self.assertEqual(envfile.values(path)["ANTHROPIC_API_KEY"], "one")
        Path(path).write_text("ANTHROPIC_API_KEY=two\n")
        self.assertEqual(envfile.values(path)["ANTHROPIC_API_KEY"], "two")

    def test_values_of_a_missing_file_is_empty(self):
        self.assertEqual(envfile.values("/nonexistent/env"), {})

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(envfile.load("/nonexistent/env", environ={}), [])

    def test_warns_when_world_readable(self):
        """It holds an API key; loading it silently would be worse than noisy."""
        warnings = []
        envfile.load(self.write("A=1\n", mode=0o644), environ={}, warn=warnings.append)
        self.assertTrue(any("readable by other users" in w for w in warnings), warnings)

    def test_no_warning_when_private(self):
        warnings = []
        envfile.load(self.write("A=1\n", mode=0o600), environ={}, warn=warnings.append)
        self.assertEqual(warnings, [])

    def test_template_is_valid_and_all_commented_out(self):
        """The shipped example must not silently set anything."""
        self.assertEqual(envfile.parse(envfile.TEMPLATE), {})

    def test_template_mentions_every_setting(self):
        for key in ("ANTHROPIC_API_KEY", "CB_BLOCK", "CB_HOME", "CB_SEARCH",
                    "CB_PORT", "CB_TOKEN", "CB_THEME", "CB_GPU"):
            self.assertIn(key, envfile.TEMPLATE, "%s undocumented in the template" % key)

    def test_ensure_template_never_clobbers(self):
        path = Path(self.write("A=mine\n"))
        original = path.read_text()
        real = envfile.config_path
        envfile.config_path = lambda: path
        try:
            self.assertIsNone(envfile.ensure_template())
        finally:
            envfile.config_path = real
        self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
