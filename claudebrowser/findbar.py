"""Ctrl+F: find on this page.

WebKit does the actual searching -- every view owns a WebKitFindController that
walks the rendered text, scrolls the hit into view and paints the highlights.
This file is only the strip of chrome that drives it, plus the two pieces of
bookkeeping that make it behave like a browser rather than a demo:

  * **The controller is per view, the bar is per window.** Switching tabs has to
    tear down the search on the tab you left, or its highlights stay painted on
    a page nobody is looking at and the next search on it starts from a stale
    position. `attach()` is called on every tab switch and does exactly that.
  * **Counting is a second, separate pass.** `search()` reports found/not-found;
    the "3 of 17" needs `count_matches()`, which answers later on its own
    signal. Both are wired, so the count fills in a beat after the first hit is
    already highlighted -- which is the right order, because the hit is what the
    user is waiting for.

The match ordinal is tracked here rather than read from WebKit, which does not
expose one. It is a count of how many times the user has stepped, wrapped
against the total -- correct in the only way the user can check.
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

#: Stop counting here. On a page with tens of thousands of matches the count is
#: both useless and expensive, and WebKit's own API takes this cap for exactly
#: that reason -- reaching it reports the cap, which we render as "999+".
MAX_MATCHES = 999

#: How long to sit on a keystroke before searching. Typing "election" would
#: otherwise run eight full-page searches, seven of which are thrown away; on a
#: 1.6GHz core that is the difference between a bar that feels live and one that
#: stutters a character behind you.
TYPING_DELAY_MS = 180


class FindBar(Gtk.Box):
    """A find strip. Hidden until Ctrl+F, and owns no state about the page."""

    def __init__(self, get_view):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.get_view = get_view
        self.controller = None
        self.ordinal = 0
        self.total = 0
        self._pending = None
        self._counted_id = None
        self._failed_id = None
        self._found_id = None

        self.get_style_context().add_class("cb-findbar")

        self.entry = Gtk.Entry()
        self.entry.get_style_context().add_class("cb-findentry")
        self.entry.set_placeholder_text("Find on page")
        self.entry.set_width_chars(28)
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("activate", lambda *_: self.step(1))
        self.entry.connect("key-press-event", self._on_entry_key)
        self.pack_start(self.entry, False, False, 0)

        self.count = Gtk.Label(label="")
        self.count.get_style_context().add_class("cb-findcount")
        self.count.set_width_chars(9)      # reserve the width so nothing jumps
        self.count.set_xalign(0)
        self.pack_start(self.count, False, False, 0)

        self.pack_start(self._button("go-up-symbolic", "Previous (Shift+Enter)",
                                     lambda *_: self.step(-1)), False, False, 0)
        self.pack_start(self._button("go-down-symbolic", "Next (Enter)",
                                     lambda *_: self.step(1)), False, False, 0)

        self.case = Gtk.ToggleButton(label="Aa")
        self.case.get_style_context().add_class("cb-findtoggle")
        self.case.set_tooltip_text("Match case")
        self.case.set_can_focus(False)
        self.case.connect("toggled", lambda *_: self.search(reset=True))
        self.pack_start(self.case, False, False, 0)

        spacer = Gtk.Box()
        self.pack_start(spacer, True, True, 0)
        self.pack_start(self._button("window-close-symbolic", "Close (Esc)",
                                     lambda *_: self.close()), False, False, 0)

        # Show the children NOW, then mark the container no-show-all and hide
        # it. The order is the whole thing, and getting it backwards produces a
        # bar that opens as a blank strip: `no_show_all` makes the window's
        # `show_all()` skip this subtree entirely, so the children are never
        # shown, and a later `show()` on the container reveals a box of
        # invisible widgets. `show_all()` on the container does not rescue it
        # either -- that call also returns early on a no-show-all widget. This
        # is the same trap the Claude panel documents in browser.py; it is worth
        # writing down twice because it fails silently and looks like CSS.
        self.show_all()
        self.set_no_show_all(True)
        self.hide()

    def _button(self, icon, tooltip, handler):
        btn = Gtk.Button()
        btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text(tooltip)
        btn.set_can_focus(False)
        btn.get_style_context().add_class("cb-findbtn")
        btn.connect("clicked", handler)
        return btn

    # -- opening and closing ------------------------------------------------

    def open(self):
        """Show the bar and take the caret. Re-opening with text already in it
        selects that text, so Ctrl+F twice means "search for something else"
        rather than "put the cursor after what I typed last time"."""
        self.attach()
        self.show()
        self.entry.grab_focus()
        if self.entry.get_text():
            self.entry.select_region(0, -1)
            self.search(reset=True)

    def close(self):
        """Hide the bar and take the highlights down with it.

        `search_finish()` is what clears them. Skipping it -- which is easy to
        do, because hiding the widget looks like it finished the job -- leaves
        the page yellow-striped until its next navigation.
        """
        self._cancel_pending()
        if self.controller is not None:
            self.controller.search_finish()
        self.hide()
        self.count.set_text("")
        self.ordinal = self.total = 0
        view = self.get_view()
        if view is not None:
            view.grab_focus()

    def attach(self):
        """Point the bar at the focused tab's controller.

        Called on open and on every tab switch. Disconnecting the old handlers
        matters: a controller outlives the bar's interest in it, and a stale
        `counted-matches` from the tab you just left would otherwise overwrite
        the count for the tab you are on.
        """
        view = self.get_view()
        controller = view.get_find_controller() if view is not None else None
        if controller is self.controller:
            return
        if self.controller is not None:
            for handler in (self._counted_id, self._failed_id, self._found_id):
                if handler is not None:
                    self.controller.disconnect(handler)
            self.controller.search_finish()
        self.controller = controller
        self._counted_id = self._failed_id = self._found_id = None
        self.ordinal = self.total = 0
        if controller is None:
            return
        self._counted_id = controller.connect("counted-matches", self._on_counted)
        self._failed_id = controller.connect("failed-to-find-text", self._on_failed)
        self._found_id = controller.connect("found-text", self._on_found)

    def on_tab_switched(self):
        """Follow the user to the new tab if the bar is open; otherwise just
        forget the old controller so the next open() rebinds cleanly."""
        if self.get_visible():
            self.attach()
            self.search(reset=True)
        else:
            self.controller = None

    # -- searching ----------------------------------------------------------

    def options(self, backwards=False):
        flags = WebKit2.FindOptions.WRAP_AROUND
        if not self.case.get_active():
            flags |= WebKit2.FindOptions.CASE_INSENSITIVE
        if backwards:
            flags |= WebKit2.FindOptions.BACKWARDS
        return flags

    def _on_changed(self, _entry):
        """Coalesce keystrokes. Every character would otherwise be a full-page
        search plus a full-page count -- two passes over the DOM per key."""
        self._cancel_pending()
        self._pending = GLib.timeout_add(TYPING_DELAY_MS, self._fire)

    def _fire(self):
        self._pending = None
        self.search(reset=True)
        return GLib.SOURCE_REMOVE

    def _cancel_pending(self):
        if self._pending is not None:
            GLib.source_remove(self._pending)
            self._pending = None

    def search(self, reset=False):
        """Start (or restart) the search for whatever is in the entry."""
        self._cancel_pending()
        if self.controller is None:
            self.attach()
        if self.controller is None:
            return
        text = self.entry.get_text()
        if not text:
            self.controller.search_finish()
            self.count.set_text("")
            self.ordinal = self.total = 0
            self._mark(found=True)
            return
        if reset:
            self.ordinal = 1
        self.controller.search(text, self.options(), MAX_MATCHES)
        self.controller.count_matches(text, self.options(), MAX_MATCHES)

    def step(self, direction):
        """Next or previous hit. No-op with an empty box, so holding Enter on an
        empty bar does not spin the engine."""
        if self.controller is None or not self.entry.get_text():
            return
        if self.total:
            # Wrap the displayed ordinal the same way WRAP_AROUND wraps the
            # actual search, so the number never disagrees with the highlight.
            self.ordinal = (self.ordinal - 1 + direction) % self.total + 1
        if direction < 0:
            self.controller.search_previous()
        else:
            self.controller.search_next()
        self._paint_count()

    # -- results ------------------------------------------------------------

    def _on_counted(self, _controller, count):
        self.total = count
        if count and not self.ordinal:
            self.ordinal = 1
        self._paint_count()

    def _on_found(self, _controller, _count=None):
        self._mark(found=True)

    def _on_failed(self, _controller):
        self.total = 0
        self.ordinal = 0
        self._paint_count()
        self._mark(found=False)

    def _paint_count(self):
        if not self.entry.get_text():
            return self.count.set_text("")
        if not self.total:
            return self.count.set_text("no results")
        if self.total >= MAX_MATCHES:
            return self.count.set_text("%d of %d+" % (self.ordinal, MAX_MATCHES))
        self.count.set_text("%d of %d" % (self.ordinal, self.total))

    def _mark(self, found):
        """Tint the entry when there is nothing to find. A colour rather than a
        dialog: the answer is already on screen, it just needs to be legible."""
        ctx = self.entry.get_style_context()
        (ctx.remove_class if found else ctx.add_class)("cb-find-miss")

    # -- keys ---------------------------------------------------------------

    def _on_entry_key(self, _entry, event):
        """Escape and Shift+Enter, handled before the window's accelerators.

        The entry sees the key first, which is what lets Escape close the bar
        instead of closing the Claude panel behind it.
        """
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        if shift and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.step(-1)
            return True
        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.step(-1 if event.keyval == Gdk.KEY_Up else 1)
            return True
        return False
