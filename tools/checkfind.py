#!/usr/bin/env python3
"""Exercise the find bar against a real WebKit view.

    python3 tools/checkfind.py

Lives here rather than in tests/ because it needs a display, and the rule for
that directory is that everything in it runs without one. The half of findbar.py
worth checking is precisely the half that cannot be faked: whether WebKit's
FindController reports the counts and honours the options we hand it, and
whether our own match ordinal stays in step with its wrapping.

Exits non-zero on the first disagreement, so it can be wired into a smoke check.
"""

import sys
from pathlib import Path

import gi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

from claudebrowser import findbar  # noqa: E402

# Six case-insensitive matches for "fox", four of them lowercase. "foxglove"
# is in there on purpose: a substring match must count, because that is what
# find-on-page means and what a word-boundary option would wrongly exclude.
HTML = """<html><body>
<p>The quick brown fox jumps over the lazy dog.</p>
<p>Another sentence mentioning the fox and the Fox again.</p>
<p>FOX in capitals, and foxglove which contains fox.</p>
</body></html>"""

RESULTS = []


def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print("  %s  %s%s" % ("pass" if ok else "FAIL", label,
                          "   (%s)" % detail if detail else ""))


def main():
    window = Gtk.Window()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    view = WebKit2.WebView()
    bar = findbar.FindBar(lambda: view)
    window.add(box)
    box.pack_start(bar, False, False, 0)
    box.pack_start(view, True, True, 0)
    window.set_default_size(700, 500)
    window.show_all()
    bar.hide()

    def counted(label, expected, then):
        """The count arrives on its own signal, a beat after the highlight."""
        def settled():
            check(label, bar.total == expected, "got %d" % bar.total)
            then()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1200, settled)

    def start(_view, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        bar.open()
        bar.entry.set_text("fox")
        bar.search(reset=True)
        counted("counts every case-insensitive match", 6, stepping)

    def stepping():
        check("first hit is 1 of n", bar.ordinal == 1, bar.count.get_text())
        bar.step(1)
        check("next advances", bar.ordinal == 2, bar.count.get_text())
        bar.step(-1)
        check("previous goes back", bar.ordinal == 1, bar.count.get_text())
        bar.step(-1)
        check("previous from the first wraps to the last",
              bar.ordinal == bar.total, bar.count.get_text())
        bar.case.set_active(True)
        counted("match-case narrows it", 4, missing)

    def missing():
        bar.case.set_active(False)
        bar.entry.set_text("zzzz-not-on-this-page")
        bar.search(reset=True)

        def settled():
            check("a miss says so", "no results" in bar.count.get_text(),
                  repr(bar.count.get_text()))
            check("a miss tints the entry",
                  bar.entry.get_style_context().has_class("cb-find-miss"))
            closing()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1200, settled)

    def closing():
        bar.close()
        check("close hides the bar", not bar.get_visible())
        check("close clears the count", bar.count.get_text() == "")
        Gtk.main_quit()

    view.connect("load-changed", start)
    view.load_html(HTML, "https://find.test/")
    GLib.timeout_add_seconds(45, Gtk.main_quit)   # never hang a smoke check
    Gtk.main()

    print("%d/%d passed" % (sum(RESULTS), len(RESULTS)))
    return 0 if RESULTS and all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
