"""The popup contract that OAuth depends on, without creating a GTK window."""

import types
import unittest

from claudebrowser import browser


class PopupContract(unittest.TestCase):
    def setUp(self):
        self.window = browser.Browser.__new__(browser.Browser)
        self.origin_view = object()
        self.origin_tab = types.SimpleNamespace(view=self.origin_view, private=False)
        self.window.tabs = [self.origin_tab]
        self.child_view = object()
        self.calls = []

        def new_tab(**kwargs):
            self.calls.append(kwargs)
            return types.SimpleNamespace(view=self.child_view)

        self.window.new_tab = new_tab

    def action(self, uri="https://accounts.google.com/o/oauth2/auth"):
        request = types.SimpleNamespace(get_uri=lambda: uri)
        return types.SimpleNamespace(get_request=lambda: request)

    def test_popup_returns_the_child_view_to_webkit(self):
        returned = self.window._on_popup(self.origin_view, self.action())

        self.assertIs(returned, self.child_view)
        self.assertEqual(self.calls[0], {
            "url": None,
            "background": True,
            "private": False,
            "related_view": self.origin_view,
            "load_initial": False,
        })

    def test_popup_without_initial_uri_still_gets_a_child(self):
        returned = self.window._on_popup(self.origin_view, self.action(uri=""))

        self.assertIs(returned, self.child_view)
        self.assertFalse(self.calls[0]["load_initial"])


if __name__ == "__main__":
    unittest.main()
