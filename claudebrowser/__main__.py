"""Entry point: `python3 -m claudebrowser [url ...]`."""

import argparse
import os
import sys
from pathlib import Path

from . import control


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-browser",
        description="A minimal WebKit browser with a control API for Claude agents.",
    )
    parser.add_argument("urls", nargs="*", help="URLs or search terms to open at startup")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CB_PORT", control.DEFAULT_PORT)),
                        help="agent control port on 127.0.0.1 (default: %(default)s)")
    parser.add_argument("--no-control", action="store_true",
                        help="run as a plain browser with no agent API")
    parser.add_argument("--token", default=os.environ.get("CB_TOKEN"),
                        help="require this token on control requests")
    parser.add_argument("--theme", choices=("dark", "light"), default=os.environ.get("CB_THEME"),
                        help="override the system light/dark preference")
    args = parser.parse_args(argv)

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
    except (ImportError, ValueError) as e:
        sys.exit(
            "claude-browser needs GTK3 + WebKit2GTK GObject bindings, which are missing:\n"
            "  %s\n\n"
            "On Debian/Ubuntu:\n"
            "  sudo apt install gir1.2-webkit2-4.1 gir1.2-gtk-3.0 python3-gi python3-gi-cairo\n"
            % (e,)
        )

    from gi.repository import GLib, Gtk

    from .browser import Browser

    # Without this the process is called "python3", so the XFCE taskbar groups
    # our windows under a generic entry with no icon instead of matching the
    # StartupWMClass in claude-browser.desktop.
    GLib.set_prgname("claude-browser")
    GLib.set_application_name("Claude Browser")

    # Prefer the themed icon, which install.sh puts in hicolor at every size so
    # the panel picks the one it wants. Running straight from a checkout there
    # is no themed icon, so fall back to the generated PNG on disk rather than
    # showing the stock GTK placeholder.
    Gtk.Window.set_default_icon_name("claude-browser")
    if not Gtk.IconTheme.get_default().has_icon("claude-browser"):
        fallback = Path(__file__).resolve().parent.parent / "packaging" / "icons" / "claude-browser.png"
        if fallback.exists():
            try:
                Gtk.Window.set_default_icon_from_file(str(fallback))
            except GLib.Error:
                pass  # a missing window icon is not worth failing to launch over

    dark = None if args.theme is None else (args.theme == "dark")
    browser = Browser(urls=args.urls or None, dark=dark)
    browser.show_all()
    browser.panel.hide()
    browser.progress.hide()
    browser.notebook.set_show_tabs(len(browser.tabs) > 1)

    server = None
    if not args.no_control:
        server = control.Control(browser, port=args.port, token=args.token)
        try:
            server.start()
        except OSError as e:
            sys.exit("cannot bind control port %s: %s\n"
                     "Another claude-browser is probably running. Use --port or --no-control."
                     % (args.port, e))
        print("control API: http://127.0.0.1:%d  (loopback only%s)"
              % (args.port, ", token required" if args.token else ""), flush=True)

    try:
        Gtk.main()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
