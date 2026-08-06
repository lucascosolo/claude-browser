"""cb:settings -- the table, the validator, and what reaches the page.

GTK-free, like the module it tests. Everything here works on an explicit
settings-file path and an explicit environ dict, so no test can read or write
the developer's own ~/.config/claude-browser/env; the two that go through the
default path point XDG_CONFIG_HOME at a temporary directory instead.

The interesting assertions are the refusals. A settings page that can write a
value the consuming code chokes on is worse than no settings page at all --
CB_PORT and CB_MAX_TABS are int()ed on the startup path, so a bad one is a
browser that does not open next time -- and that is a failure the user cannot
undo from inside the browser, because the browser is what is broken.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import envfile, pages, personas, settings, style  # noqa: E402

#: Every knob the browser reads. Listed here rather than derived from the table
#: so that a setting deleted from settings.py fails a test instead of quietly
#: becoming uneditable again.
EVERY_KEY = (
    "CB_AUTH", "CB_AUTOSTART", "CB_BLOCK", "CB_COOKIES", "CB_GPU", "CB_HOME",
    "CB_ITP", "CB_LIGHT", "CB_MAX_TABS", "CB_MEM_LIMIT", "CB_PACE",
    "CB_PERSONA", "CB_PORT", "CB_PRIVATE_AI", "CB_PRIVATE_DOWNLOADS",
    "CB_QUEUE_LIST",
    "CB_SCRUB", "CB_SEARCH", "CB_SEARCH_LANG", "CB_SITERULES", "CB_THEME", "CB_TOKEN", "CB_URL", "CB_VPN",
    "CB_VPN_PROXY", "CB_WEBGL", "CB_YT_EMBED",
)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "env"
        self.environ = {}

    def apply(self, key, value):
        return settings.apply(key, value, path=self.path, environ=self.environ)

    def reset(self, key):
        return settings.reset(key, path=self.path, environ=self.environ)

    def described(self):
        return settings.describe(path=self.path, environ=self.environ)

    def item(self, key):
        for section in self.described()["sections"]:
            for block in section["settings"]:
                if block["key"] == key:
                    return block
        raise AssertionError("%s is not on the page" % key)

    def render(self, notice=None):
        return pages.settings_page(style.palette("dark"), "NONCE", self.described(),
                                   notice=notice)


class TestTable(Isolated):
    def test_every_setting_the_browser_reads_is_here(self):
        self.assertEqual(sorted(settings.BY_KEY), sorted(EVERY_KEY))

    def test_every_setting_belongs_to_a_section_that_exists(self):
        headings = {title for title, _note in settings.SECTIONS}
        for knob in settings.SETTINGS:
            self.assertIn(knob.section, headings, knob.key)

    def test_every_section_has_at_least_one_setting(self):
        for section in self.described()["sections"]:
            self.assertTrue(section["settings"], section["title"])

    def test_every_setting_says_when_it_lands(self):
        """The whole point of the effect column. An empty one would render as a
        blank badge, which reads as "immediately"."""
        for knob in settings.SETTINGS:
            self.assertTrue(knob.effect.strip(), knob.key)
            self.assertTrue(knob.effect_note.strip(), knob.key)
            self.assertTrue(knob.explain.strip(), knob.key)

    def test_only_the_settings_that_are_re_read_claim_to_apply_now(self):
        """Honesty check with teeth: a knob may only advertise an immediate
        effect if the code reading it re-reads it (or this window re-applies it,
        which CB_THEME does). Adding a setting copies an existing row, so the
        badge is exactly the field most likely to be copied unexamined."""
        live = {"CB_THEME", "CB_LIGHT", "CB_SCRUB", "CB_PERSONA", "CB_AUTH",
                "CB_PACE", "CB_URL", "CB_AUTOSTART",
                # Both read at the moment they are consulted -- ai.private_ai_-
                # enabled when a Claude feature is handed a tab, and
                # storage.private_downloads_enabled when a download starts.
                "CB_PRIVATE_AI", "CB_PRIVATE_DOWNLOADS",
                # The window applies CB_VPN the moment it changes, the way it
                # re-applies CB_THEME; CB_VPN_PROXY is read out of the file
                # every time the mode is engaged, so correcting it and toggling
                # is enough.
                "CB_VPN", "CB_VPN_PROXY",
                # Read inside the worker that fetches the queue, on every
                # fetch, so Refresh on cb:queue is enough to pick up a change.
                "CB_QUEUE_LIST",
                # Read by siterules.enabled() on every page commit, so the
                # next load of a page picks up the change with no restart.
                "CB_SITERULES",
                # search.language() is read inside search.search(), which runs
                # once per query, so the next search already obeys it.
                "CB_SEARCH_LANG",
                # youtube.enabled() is read inside the decide-policy handler,
                # so the next link clicked already obeys it.
                "CB_YT_EMBED"}
        for knob in settings.SETTINGS:
            immediate = "restart" not in knob.effect.lower()
            self.assertEqual(immediate, knob.key in live,
                             "%s claims %r" % (knob.key, knob.effect))

    def test_each_default_is_a_value_its_own_validator_accepts(self):
        """The default is what the Default button restores to, and what the
        page renders before anything is set. One that fails validation is a
        setting nobody can save without changing it first."""
        for knob in settings.SETTINGS:
            if knob.kind == "secret" or (knob.default == "" and knob.allow_empty):
                continue
            self.assertEqual(knob.clean(knob.default), knob.default, knob.key)

    def test_the_choices_offered_are_the_ones_accepted(self):
        for knob in settings.SETTINGS:
            if knob.kind != "choice":
                continue
            for value, _label in knob.choices:
                self.assertEqual(knob.clean(value), value, knob.key)

    def test_the_personas_come_from_personas_py(self):
        """Not a second list. A persona added there has to appear here."""
        offered = [c["value"] for c in self.item("CB_PERSONA")["choices"]]
        self.assertEqual(offered, personas.keys())


class TestValidation(Isolated):
    def bad(self, key, value):
        with self.assertRaises(ValueError, msg="%s=%r was accepted" % (key, value)):
            self.apply(key, value)
        self.assertNotIn(key, envfile.values(self.path))

    def test_a_port_that_would_not_bind_is_refused(self):
        for value in ("80", "0", "70000", "-1", "eight thousand", "8765x", ""):
            self.bad("CB_PORT", value)

    def test_a_port_in_range_is_taken(self):
        self.assertEqual(self.apply("CB_PORT", " 9000 "), "9000")

    def test_a_search_template_that_would_raise_on_use_is_refused(self):
        # urls.normalize does SEARCH % quote(query); each of these is a
        # TypeError, a ValueError, or a URL that silently drops the query.
        for value in ("https://x/?q=%d", "https://x/?q=%s&p=%s", "https://x/?q=",
                      "https://x/?q=%", "duckduckgo.com/?q=%s", "%s"):
            self.bad("CB_SEARCH", value)

    def test_a_real_search_template_is_taken(self):
        self.assertEqual(self.apply("CB_SEARCH", "https://x/?q=%s&hl=en"),
                         "https://x/?q=%s&hl=en")

    def test_a_start_page_has_to_be_somewhere_to_go(self):
        for value in ("", "   ", "not a url at all", "javascript:alert(1)",
                      "data:text/html,<script>x()</script>"):
            self.bad("CB_HOME", value)

    def test_start_pages_that_work_are_taken(self):
        for value in ("cb:home", "about:blank", "https://example.com/start",
                      "localhost:8080"):
            self.assertEqual(self.apply("CB_HOME", value), value)

    def test_numbers_are_held_to_the_range_the_code_survives(self):
        self.bad("CB_MAX_TABS", "0")
        self.bad("CB_MAX_TABS", "ten")
        self.bad("CB_MEM_LIMIT", "8")
        self.bad("CB_PACE", "9")            # agent.PACE_MAX is 5
        self.bad("CB_PACE", "nan")
        self.assertEqual(self.apply("CB_PACE", "0.5"), "0.5")
        self.assertEqual(self.apply("CB_MAX_TABS", "4"), "4")

    def test_a_whole_number_setting_refuses_a_fraction(self):
        self.bad("CB_MAX_TABS", "2.5")

    def test_an_unknown_choice_is_refused_rather_than_stored(self):
        self.bad("CB_THEME", "solarized")
        self.bad("CB_COOKIES", "some")
        self.bad("CB_PERSONA", "pirate")

    def test_a_boolean_only_takes_the_words_it_understands(self):
        self.bad("CB_BLOCK", "maybe")
        self.assertEqual(self.apply("CB_BLOCK", "no"), "0")

    def test_the_private_knobs_default_to_the_private_answer(self):
        """Both are the inverse of every other boolean here: off is the safe
        reading, so an unrecognised value has to land on off rather than on."""
        for key in ("CB_PRIVATE_AI", "CB_PRIVATE_DOWNLOADS"):
            knob = settings.get(key)
            self.assertEqual(knob.default, "0", key)
            self.assertFalse(knob.truth(""), key)
            self.assertFalse(knob.truth("yeah"), key)
            self.assertFalse(knob.truth(None), key)
            self.assertTrue(knob.truth("1"), key)
            self.assertTrue(knob.truth("on"), key)

    def test_the_private_knobs_are_written_as_ones_and_zeroes(self):
        for key in ("CB_PRIVATE_AI", "CB_PRIVATE_DOWNLOADS"):
            self.assertEqual(self.apply(key, "yes"), "1")
            self.assertEqual(self.apply(key, "off"), "0")
            with self.assertRaises(ValueError, msg=key):
                self.apply(key, "sometimes")
            self.assertEqual(envfile.values(self.path)[key], "0")

    def test_the_private_knobs_are_what_their_consumers_read(self):
        """The table restates a spelling that lives in ai.py and storage.py;
        this is the string that ties the two together."""
        from claudebrowser import ai, storage

        for word in ("1", "on", "true", "YES"):
            self.assertTrue(ai.private_ai_enabled(word), word)
            self.assertTrue(storage.private_downloads_enabled(word), word)
            self.assertTrue(settings.get("CB_PRIVATE_AI").truth(word), word)
        for word in ("", "0", "off", "maybe"):
            self.assertFalse(ai.private_ai_enabled(word), word)
            self.assertFalse(storage.private_downloads_enabled(word), word)
            self.assertFalse(settings.get("CB_PRIVATE_AI").truth(word), word)

    def test_an_unknown_setting_is_refused(self):
        with self.assertRaises(ValueError):
            self.apply("CB_NONSENSE", "1")
        with self.assertRaises(ValueError):
            self.reset("CB_NONSENSE")

    def test_a_value_cannot_smuggle_a_second_line_in(self):
        """The API key is a line in this same file. A setting that could carry a
        newline could write one."""
        with self.assertRaises(ValueError):
            self.apply("CB_HOME", "cb:home\nANTHROPIC_API_KEY=sk-ant-injected")
        self.assertEqual(envfile.values(self.path), {})

    def test_a_control_address_has_to_be_http(self):
        for value in ("ftp://box/", "127.0.0.1:8765", "https://"):
            self.bad("CB_URL", value)
        self.assertEqual(self.apply("CB_URL", "http://127.0.0.1:9000"),
                         "http://127.0.0.1:9000")


class TestWriting(Isolated):
    def test_it_writes_the_settings_file_and_reads_back(self):
        self.apply("CB_THEME", "dark")
        self.assertEqual(envfile.values(self.path)["CB_THEME"], "dark")
        self.assertEqual(self.item("CB_THEME")["value"], "dark")
        self.assertEqual(self.item("CB_THEME")["source"], "file")

    def test_values_are_stored_canonically(self):
        """What lands in the file is what every consumer of it parses the same
        way, whatever spelling was typed at the page."""
        self.assertEqual(self.apply("CB_THEME", " Dark "), "dark")
        self.assertEqual(self.apply("CB_LIGHT", "yes"), "1")
        self.assertEqual(self.apply("CB_WEBGL", "on"), "1")
        self.assertEqual(self.apply("CB_GPU", "none"), "off")
        self.assertEqual(self.apply("CB_PERSONA", "Critic"), "critic")

    def test_a_boolean_written_here_is_read_the_way_its_consumer_reads_it(self):
        """CB_ITP is `!= "0"` and CB_WEBGL is `== "1"` in the code that reads
        them. Storing 1/0 is what keeps the page's On/Off honest for both."""
        self.apply("CB_ITP", "off")
        self.assertFalse(self.item("CB_ITP")["on"])
        self.apply("CB_ITP", "on")
        self.assertTrue(self.item("CB_ITP")["on"])
        self.apply("CB_WEBGL", "on")
        self.assertTrue(self.item("CB_WEBGL")["on"])

    def test_a_hand_edited_typo_shows_as_what_the_code_will_do(self):
        """CB_ITP=off leaves tracking prevention on, because storage.py tests
        for the literal "0". The page has to say so, not repeat the word."""
        self.path.write_text("CB_ITP=off\nCB_THEME=solarized\n")
        self.assertTrue(self.item("CB_ITP")["on"])
        self.assertEqual(self.item("CB_THEME")["value"], "")

    def test_other_lines_and_comments_survive_a_write(self):
        self.path.write_text("# my notes\nCB_PORT=9000\n#CB_THEME=dark\n")
        self.apply("CB_BLOCK", "0")
        body = self.path.read_text()
        self.assertIn("# my notes", body)
        self.assertIn("#CB_THEME=dark", body)
        self.assertEqual(envfile.values(self.path)["CB_PORT"], "9000")

    def test_the_file_stays_private(self):
        self.apply("CB_BLOCK", "0")
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_an_environment_value_is_named_as_such(self):
        """It matters for the Default button: removing a line the user does not
        have leaves the exported value still answering."""
        self.environ["CB_PORT"] = "9999"
        block = self.item("CB_PORT")
        self.assertEqual(block["source"], "environment")
        self.assertEqual(block["value"], "9999")
        self.assertFalse(block["in_file"])
        self.assertIn("from your environment", self.render())

    def test_the_file_wins_over_the_environment(self):
        self.environ["CB_PORT"] = "9999"
        self.apply("CB_PORT", "9001")
        self.assertEqual(self.item("CB_PORT")["value"], "9001")


class TestReset(Isolated):
    def test_it_removes_the_line_rather_than_writing_the_default(self):
        """Writing the default back would pin it: a later release that ships a
        better default would never reach anyone who pressed this button."""
        self.apply("CB_MAX_TABS", "3")
        self.assertEqual(self.reset("CB_MAX_TABS"), "10")
        self.assertNotIn("CB_MAX_TABS", self.path.read_text())
        block = self.item("CB_MAX_TABS")
        self.assertEqual(block["value"], "10")
        self.assertEqual(block["source"], "default")
        self.assertFalse(block["in_file"])

    def test_it_leaves_every_other_line_alone(self):
        self.path.write_text("# notes\nCB_PORT=9000\n#CB_THEME=dark\nCB_BLOCK=0\n")
        self.reset("CB_BLOCK")
        body = self.path.read_text()
        self.assertIn("# notes", body)
        self.assertIn("#CB_THEME=dark", body)      # the template's documentation
        self.assertEqual(envfile.values(self.path), {"CB_PORT": "9000"})

    def test_it_clears_the_value_this_process_was_given(self):
        """envfile.put pushes what it writes into the environment, and
        envfile.setting falls back to the environment -- so a reset that left it
        there would look like it had not worked."""
        self.apply("CB_SCRUB", "0")
        self.assertEqual(self.environ["CB_SCRUB"], "0")
        self.reset("CB_SCRUB")
        self.assertNotIn("CB_SCRUB", self.environ)

    def test_resetting_something_that_was_never_set_is_not_an_error(self):
        self.assertEqual(self.reset("CB_GPU"), "")

    def test_an_empty_optional_value_resets_instead_of_writing_a_blank(self):
        self.apply("CB_URL", "http://127.0.0.1:9000")
        self.assertIsNone(self.apply("CB_URL", ""))
        self.assertNotIn("CB_URL", envfile.values(self.path))

    def test_the_default_button_only_appears_when_there_is_a_line(self):
        self.assertNotIn("Default</button>", self.render())
        self.apply("CB_BLOCK", "0")
        self.assertIn("Default</button>", self.render())


class TestSecrets(Isolated):
    def test_the_control_token_is_never_rendered(self):
        self.path.write_text("CB_TOKEN=hunter2-the-real-token\n")
        block = self.item("CB_TOKEN")
        self.assertTrue(block["set"])
        self.assertNotIn("value", block)
        self.assertNotIn("hunter2", repr(self.described()))
        self.assertNotIn("hunter2", self.render())

    def test_the_page_says_whether_one_is_set_without_saying_what(self):
        self.assertIn("No token", self.render())
        self.path.write_text("CB_TOKEN=hunter2\n")
        self.assertIn("A token is set", self.render())
        self.assertNotIn("hunter2", self.render())

    def test_a_new_token_can_be_set_and_cleared(self):
        self.assertEqual(self.apply("CB_TOKEN", "s3cret"), "s3cret")
        self.assertEqual(envfile.values(self.path)["CB_TOKEN"], "s3cret")
        self.reset("CB_TOKEN")
        self.assertNotIn("CB_TOKEN", envfile.values(self.path))

    def test_the_token_field_is_a_password_field_with_no_value(self):
        self.path.write_text("CB_TOKEN=hunter2\n")
        self.assertIn('type="password" value=""', self.render())

    def test_no_secret_is_writable_from_here(self):
        """Every key envfile calls a secret is refused by this table too.

        The API key is not in the table at all. CB_VPN_PROXY is -- it is a
        genuine setting a user needs to see the state of -- but it is there as
        a report, not as a control: it holds the proxy's password, so `apply`
        and `reset` both refuse it before envfile gets the chance to.
        """
        self.assertNotIn("ANTHROPIC_API_KEY", settings.BY_KEY)
        for key in envfile.SECRET_KEYS:
            with self.assertRaises(ValueError, msg=key):
                self.apply(key, "sk-ant-nope")
            with self.assertRaises(ValueError, msg=key):
                self.reset(key)
            knob = settings.BY_KEY.get(key)
            if knob is not None:
                self.assertFalse(knob.writable, key)
                self.assertTrue(knob.unwritable_note.strip(), key)

    def test_the_proxy_address_is_reported_but_never_rendered(self):
        """cb:vpn and cb:settings both need to say whether one is configured.
        Neither may say what it is: the password is inside the URL."""
        self.path.write_text(
            "CB_VPN_PROXY=http://cb:hunter2@10.0.0.1:8888\n")
        block = self.item("CB_VPN_PROXY")
        self.assertTrue(block["set"])
        self.assertNotIn("value", block)
        self.assertNotIn("hunter2", repr(self.described()))
        page = self.render()
        self.assertNotIn("hunter2", page)
        self.assertNotIn("10.0.0.1", page)
        # And no control at all -- a Save button wired to a call that always
        # fails is worse than no button.
        self.assertNotIn('data-k="CB_VPN_PROXY"', page)

    def test_an_api_key_in_the_file_never_reaches_the_page(self):
        self.path.write_text("ANTHROPIC_API_KEY=sk-ant-secret\nCB_BLOCK=0\n")
        self.assertNotIn("sk-ant-secret", self.render())
        self.assertNotIn("ANTHROPIC_API_KEY", self.render())


class TestPage(Isolated):
    def test_every_setting_is_on_it(self):
        html = self.render()
        for knob in settings.SETTINGS:
            self.assertIn(knob.label, html, knob.key)
            self.assertIn(knob.effect, html, knob.key)

    def test_each_kind_gets_the_control_that_fits_it(self):
        html = self.render()
        self.assertIn('cbui.setting(event, &quot;CB_BLOCK&quot;', html)   # toggle
        self.assertIn('cbui.setpick(event, &quot;CB_THEME&quot;)', html)  # select
        self.assertIn('type="number"', html)                              # numbers
        self.assertIn('min="1024" max="65535"', html)                     # the port

    def test_a_toggle_posts_the_state_it_wants(self):
        """Not a flip, for cb:data's reason: a page left open while the value
        changed elsewhere must not invert something it is no longer showing."""
        self.assertIn('cbui.setting(event, &quot;CB_BLOCK&quot;, &quot;0&quot;)',
                      self.render())
        self.apply("CB_BLOCK", "0")
        self.assertIn('cbui.setting(event, &quot;CB_BLOCK&quot;, &quot;1&quot;)',
                      self.render())

    def test_everything_goes_through_the_nonce_path(self):
        """cbui.send is what stamps the per-session token on. A control that
        reached the browser any other way is one a website could imitate."""
        self.apply("CB_BLOCK", "0")     # so a Default button is on the page too
        html = self.render()
        self.assertIn("this.send({action: 'set_setting'", html)
        self.assertIn("this.send({action: 'reset_setting'", html)
        for handler in ("cbui.setting(", "cbui.setpick(", "cbui.setinput(",
                        "cbui.setreset("):
            self.assertIn(handler, html)
        # No form posts either: a form would navigate, and the nonce lives in
        # the script this page's messages go through.
        self.assertNotIn("<form", html)

    def test_no_modal_dialogs_in_the_controls(self):
        """One inside a WebView blocks this embedder's whole main loop. Checked
        against the rows rather than the whole document, because the shared
        script carries a comment saying exactly this about window.confirm."""
        for section in self.described()["sections"]:
            for block in section["settings"]:
                row = pages._setting_row(block)
                for banned in ("confirm(", "alert(", "prompt("):
                    self.assertNotIn(banned, row, block["key"])

    def test_a_refusal_is_shown_on_the_page_that_caused_it(self):
        html = self.render(notice={"error": "Control port cannot be below 1024"})
        self.assertIn("Control port cannot be below 1024", html)
        self.assertIn("notice bad", html)

    def test_a_hostile_stored_value_cannot_inject_markup(self):
        """The settings file is hand-editable and writable by anything running
        as this user. A value out of it is text, and this page holds the nonce
        that lets a script clear the user's history."""
        nasty = '"><script>alert(1)</script>'
        self.path.write_text("CB_HOME=%s\n" % nasty)
        html = self.render()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&quot;&gt;", html)

    def test_a_hostile_notice_cannot_inject_markup(self):
        html = self.render(notice={"error": "<img src=x onerror=alert(1)>"})
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_the_page_names_the_file_it_edits(self):
        self.assertIn(str(self.path), self.render())


class TestDefaultPath(unittest.TestCase):
    """The two entry points that take no path -- the way the browser calls
    them. XDG_CONFIG_HOME is redirected so this cannot touch a real file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get("XDG_CONFIG_HOME")
        self.addCleanup(lambda: os.environ.__setitem__("XDG_CONFIG_HOME", saved)
                        if saved is not None
                        else os.environ.pop("XDG_CONFIG_HOME", None))
        os.environ["XDG_CONFIG_HOME"] = self.tmp.name
        self.config = Path(self.tmp.name) / "claude-browser" / "env"
        self.environ = {}

    def test_it_lands_in_the_settings_file_every_other_setting_uses(self):
        settings.apply("CB_MAX_TABS", "6", environ=self.environ)
        self.assertEqual(envfile.config_path(), self.config)
        self.assertEqual(envfile.values()["CB_MAX_TABS"], "6")
        settings.reset("CB_MAX_TABS", environ=self.environ)
        self.assertEqual(envfile.values(), {})

    def test_describe_reports_the_path_it_is_editing(self):
        self.assertEqual(settings.describe(environ=self.environ)["path"],
                         str(self.config))


class TestEnvfileRemove(unittest.TestCase):
    """envfile.remove is what "back to default" is built on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "env"
        self.environ = {}

    def remove(self, key):
        return envfile.remove(key, path=self.path, environ=self.environ)

    def test_it_removes_only_the_key_asked_for(self):
        self.path.write_text("CB_PORT=9000\nCB_THEME=dark\n")
        self.assertTrue(self.remove("CB_THEME"))
        self.assertEqual(envfile.values(self.path), {"CB_PORT": "9000"})

    def test_it_removes_every_assignment_of_that_key(self):
        self.path.write_text("CB_THEME=dark\nCB_PORT=9000\nexport CB_THEME=light\n")
        self.remove("CB_THEME")
        self.assertNotIn("CB_THEME=", self.path.read_text())

    def test_it_leaves_a_commented_example_alone(self):
        self.path.write_text("#CB_THEME=dark\nCB_THEME=light\n")
        self.remove("CB_THEME")
        self.assertEqual(self.path.read_text().strip(), "#CB_THEME=dark")

    def test_it_says_when_there_was_nothing_to_remove(self):
        self.path.write_text("CB_PORT=9000\n")
        self.assertFalse(self.remove("CB_THEME"))

    def test_a_missing_file_is_not_an_error(self):
        self.assertFalse(self.remove("CB_THEME"))
        self.assertFalse(self.path.exists())

    def test_it_clears_the_process_copy_too(self):
        self.environ["CB_THEME"] = "dark"
        self.remove("CB_THEME")
        self.assertNotIn("CB_THEME", self.environ)

    def test_secrets_are_refused(self):
        self.path.write_text("ANTHROPIC_API_KEY=sk-ant-mine\n")
        for key in envfile.SECRET_KEYS:
            with self.assertRaises(ValueError):
                self.remove(key)
        self.assertEqual(envfile.values(self.path),
                         {"ANTHROPIC_API_KEY": "sk-ant-mine"})

    def test_the_file_is_left_private(self):
        self.path.write_text("CB_THEME=dark\n")
        os.chmod(self.path, 0o644)
        self.remove("CB_THEME")
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)

    def test_removing_the_only_line_leaves_an_empty_file(self):
        self.path.write_text("CB_THEME=dark\n")
        self.remove("CB_THEME")
        self.assertEqual(self.path.read_text(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
