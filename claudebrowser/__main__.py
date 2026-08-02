"""Entry point: `python3 -m claudebrowser [url ...]`."""

import argparse
import os
import sys
from pathlib import Path

from . import control, envfile


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claude-browser",
        description="A minimal WebKit browser with a control API for Claude agents.",
    )
    parser.add_argument("urls", nargs="*", help="URLs or search terms to open at startup")
    parser.add_argument("--port", type=int, default=None,
                        help="agent control port on 127.0.0.1 (default: 8765 or $CB_PORT)")
    parser.add_argument("--no-control", action="store_true",
                        help="run as a plain browser with no agent API")
    parser.add_argument("--token", default=None,
                        help="require this token on control requests")
    parser.add_argument("--theme", choices=("dark", "light"), default=None,
                        help="override the system light/dark preference")
    parser.add_argument("--env-file", help="settings file (default: "
                                           "~/.config/claude-browser/env)")
    parser.add_argument("--new-window", action="store_true",
                        help="always open a new window, even if one is running")
    args = parser.parse_args(argv)

    # Before anything reads os.environ. A desktop launcher gives us no shell
    # environment, so this is where ANTHROPIC_API_KEY and the CB_* settings
    # actually come from for a menu-launched window.
    created = envfile.ensure_template() if not args.env_file else None
    applied = envfile.load(args.env_file, warn=lambda m: print("config: %s" % m, flush=True))
    if created:
        print("config: wrote an example settings file at %s" % created, flush=True)
    if applied:
        print("config: loaded %s from %s"
              % (", ".join(sorted(applied)), args.env_file or envfile.config_path()),
              flush=True)

    # Whether --port was named, recorded before the default is filled in below:
    # a caller who asked for a specific control port wants their own process,
    # not a handoff to whatever is on 8765.
    explicit_port = args.port is not None

    # Resolved here, not in the argparse defaults, so the settings file loaded
    # above is visible to them. An explicit flag still wins.
    if args.port is None:
        args.port = int(os.environ.get("CB_PORT") or control.DEFAULT_PORT)
    if args.token is None:
        args.token = os.environ.get("CB_TOKEN") or None
    if args.theme is None:
        args.theme = os.environ.get("CB_THEME") or None

    # A second launch hands its URLs to the first window and gets out of the way.
    #
    # This is what a *default browser* has to do. `xdg-open`, a mail client, a
    # chat app -- they all run `claude-browser <url>` with no idea one is already
    # running, and the old behaviour was to try to bind the control port, fail,
    # and exit 1 with "Another claude-browser is probably running". The link
    # simply never opened, which made the browser unusable as a system default
    # however it was registered.
    #
    # --new-window opts out, and so does --port/--no-control: asking for a
    # separate control surface is asking for a separate process.
    if (args.urls and not args.new_window and not args.no_control
            and not explicit_port):
        from . import client

        if client.handoff(args.urls):
            client.present()
            return 0

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
            print("control API: http://127.0.0.1:%d  (loopback only%s)"
                  % (args.port, ", token required" if args.token else ""), flush=True)
        except OSError as e:
            # A window without the agent API still browses. Refusing to open one
            # was the right call when this was a developer tool run by hand; as
            # the system default it means a clicked link goes nowhere because
            # something unrelated holds the port.
            server = None
            print("control API: disabled -- port %s is in use (%s).\n"
                  "This window browses normally. For a second agent-drivable "
                  "window, use --port." % (args.port, e), flush=True)

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
