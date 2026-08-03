"""The window: one bar, a stack of WebKit views, and an optional Claude panel.

Everything an agent can do lives on the `api_*` methods at the bottom. They all
share one shape -- `api_thing(..., done)` where `done` is called exactly once
with a JSON-serializable dict. That is what lets control.py block an HTTP
thread on a GTK main-loop operation without either side knowing about the other.
"""

import io
import json
import os
import re
import secrets
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

from . import (agent, ai, auth, extract, findbar, pages, pagetext, panel_html,  # noqa: E402
               passwords, perf, personas, playbooks, reader, resources, scrub,
               settings, storage, store, style, tabnames, urls)
from .urls import normalize  # noqa: E402

HOME = os.environ.get("CB_HOME", "cb:home")

# How long the omnibox must sit still before its host is worth resolving.
# Firing on the keystroke itself would resolve every prefix of a hostname --
# "e", "exa", "exampl.c" -- names that do not exist, several per navigation.
PREFETCH_DELAY_MS = 300

# The omnibox's page-text search, on the same reasoning as the DNS prefetch
# above: an FTS5 query is far more expensive than the history lookup beside it,
# so it waits for a pause in typing and never runs for a prefix short enough to
# match half the index. Four characters is roughly where a query stops being a
# prefix of everything; the results are appended after the history matches and
# capped, so the ordinary suggestions for a short word keep the top of the list.
RECALL_DELAY_MS = 260
RECALL_MIN_CHARS = 4
RECALL_SUGGESTIONS = 4

# What the hamburger holds: (heading, ((icon, label, accelerator, action), ...)).
# Claude comes first because it is the reason this browser exists; everything
# below it is ordinary browser furniture. Accelerators are shown rather than
# hidden in tooltips -- a menu is where you learn the shortcut that means you
# never open the menu again.
MENU_SECTIONS = (
    ("Claude", (
        ("dialog-question-symbolic", "Ask about this page", "Ctrl+K", "ask"),
        ("format-justify-left-symbolic", "TL;DR this page", "Ctrl+Shift+S", "tldr"),
        ("view-list-symbolic", "Research across all tabs", "Ctrl+Shift+R", "research"),
        ("system-run-symbolic", "Let Claude drive", "Ctrl+G", "agent"),
    )),
    ("New", (
        ("tab-new-symbolic", "Tab", "Ctrl+T", "newtab"),
        # user-not-tracked rather than a keyhole or a shield: the promise of a
        # private tab is that nothing is written down, not that it is encrypted.
        ("user-not-tracked-symbolic", "Private tab", "Ctrl+Shift+P", "private"),
    )),
    ("Library", (
        ("view-grid-symbolic", "Deck", "Ctrl+Shift+A", "cb:deck"),
        ("user-bookmarks-symbolic", "Bookmarks", "Ctrl+Shift+O", "cb:bookmarks"),
        ("document-open-recent-symbolic", "History", "Ctrl+H", "cb:history"),
        ("dialog-password-symbolic", "Saved logins", "", "cb:passwords"),
        ("media-playback-start-symbolic", "Playbooks", "", "cb:playbooks"),
    )),
    ("This page", (
        ("edit-find-symbolic", "Find on page", "Ctrl+F", "find"),
        ("view-paged-symbolic", "Reader mode", "Ctrl+Alt+R", "reader"),
    )),
    ("Machine", (
        ("drive-harddisk-symbolic", "Cookies & cache", "", "cb:data"),
        ("preferences-system-symbolic", "Settings", "", "cb:settings"),
    )),
)
INTERNAL = ("cb:home", "cb:deck", "cb:bookmarks", "cb:history", "cb:data",
            "cb:playbooks", "cb:settings")
# console.* is not exposed to the embedder in webkit2gtk, so we shim it in the
# page at document-start and read the ring buffer back out with JS later.
CONSOLE_SHIM = """
(function () {
  if (window.__cb_console) return;
  var log = window.__cb_console = [];
  ['log', 'info', 'warn', 'error', 'debug'].forEach(function (level) {
    var original = console[level] ? console[level].bind(console) : function () {};
    console[level] = function () {
      try {
        var parts = Array.prototype.map.call(arguments, function (a) {
          if (typeof a === 'string') return a;
          try { return JSON.stringify(a); } catch (e) { return String(a); }
        });
        log.push({ level: level, text: parts.join(' '), t: Date.now() });
        if (log.length > 500) log.shift();
      } catch (e) {}
      return original.apply(console, arguments);
    };
  });
  window.addEventListener('error', function (e) {
    log.push({ level: 'error', text: String(e.message) + ' @ ' + e.filename + ':' + e.lineno,
               t: Date.now() });
  });
  window.addEventListener('unhandledrejection', function (e) {
    log.push({ level: 'error', text: 'unhandled rejection: ' + String(e.reason), t: Date.now() });
  });
})();
"""

READ_CONSOLE = "JSON.stringify({entries: window.__cb_console || []})"

PANEL_MODES = [
    ("ask",      "Ask",      "Ask about this page…",
     "Ask Claude about the page you are on"),
    ("tldr",     "TL;DR",    "Ask a follow-up…",
     "Summarize this page (runs immediately)"),
    ("research", "Research", "What should I compare? (optional)",
     "Read every open tab and synthesize across them"),
    ("agent",    "Command",  "Describe a goal — Claude will drive the browser…",
     "Give Claude a goal and watch it work"),
]
MODE_INDEX = {key: i for i, (key, _l, _p, _t) in enumerate(PANEL_MODES)}

# The console's default height, and the floors that keep either half usable.
# PANEL_MIN is what the drag can shrink the console to; PAGE_MIN is the strip of
# page the console will not eat, however tall the user asks for.
PANEL_HEIGHT = 280
PANEL_MIN = 84
PAGE_MIN = 120


# How long a tab keeps its "Claude is driving" glow after the last agent call.
# Long enough to bridge the gap between steps in an agent loop, short enough
# that the glow is gone before the user wonders whether it is stuck on.
AGENT_GLOW_MS = 2600

# How often the resource guard reads /proc. Two small file reads, so the cost is
# not the poll -- it is that a poll too far apart lets a tab storm get all the
# way to swap before anything notices. Four seconds is inside the window between
# "an agent asked for a tab" and "the machine is in trouble".
GUARD_POLL_S = 4

# Page loads the control API will run at once on a healthy machine. Two, not
# more: on two cores, five concurrent loads finish later *in total* than five
# sequential ones and their memory peaks coincide, which is precisely the
# failure this is here to prevent. Under any pressure at all it drops to one.
MAX_CONCURRENT_LOADS = 2

# How many more may wait their turn. Deep enough that an agent working through
# a list is never told no, shallow enough that one that has lost the plot and is
# firing opens in a loop finds out immediately rather than in five minutes.
MAX_QUEUED_LOADS = 6

# How long a queued load will wait for memory before giving up. Well under the
# 90s the control API allows an open, so the caller gets a reason rather than a
# timeout -- "the machine is out of memory" is actionable and "timed out" is not.
QUEUE_WAIT_S = 55

# The whole time a request may spend queued, whatever the reason. Being sixth in
# line behind five slow pages is not a machine problem, but it is still a long
# wait, and it has to end in an answer the caller can read rather than in the
# control API's own timeout. Kept under the 150s the open/navigate ops allow.
QUEUE_TOTAL_S = 100

# After this long, a load stops counting against the concurrency limit. A page
# that never finishes -- a hung server, an endless stream -- would otherwise
# hold the only slot for as long as it stayed open.
STUCK_LOAD_S = 40

# The most tabs an agent may have open at once. The user is never held to this
# -- Ctrl+T always works, because a person opening a tab has looked at the
# screen and an agent has not. resources.tab_ceiling() lowers it further on a
# machine that is already struggling.
MAX_AGENT_TABS = int(os.environ.get("CB_MAX_TABS", "10"))


def needs_tab(method):
    """Resolve the leading tab id, or answer "no such tab" and stop.

    Seven api_* methods opened with the same three lines. Doing it here also
    makes the contract visible in the signature: a decorated method is handed a
    Tab, never an id, so it cannot forget the check.

    It is also the one place every agent-initiated, tab-targeted call passes
    through, which makes it the honest place to light the "Claude is driving"
    indicator -- a marker set anywhere further in would miss whatever forgot
    to call it.
    """
    import functools

    @functools.wraps(method)
    def wrapper(self, tab_id, *rest):
        done = rest[-1]
        tab = self.find(tab_id)
        if tab is None:
            return done({"ok": False, "error": "no such tab"})
        self.note_agent_activity(tab)
        return method(self, tab, *rest)

    return wrapper



class Tab:
    """A web view plus the bookkeeping the API needs: a stable id, and the list
    of callbacks waiting for this tab's current load to finish."""

    _next_id = 1

    def __init__(self, manager, context, related=None, private=False):
        self.id = Tab._next_id
        Tab._next_id += 1
        self.private = private
        # A private view gets its own ephemeral WebsiteDataManager: separate
        # cookie jar, no disk cache, nothing written when it closes. It cannot
        # also be a *related* view -- related views inherit their relative's
        # storage, which is the entire thing we are avoiding -- so private tabs
        # give up the shared-web-process trick and cost one more process.
        if private:
            self.view = WebKit2.WebView(
                web_context=context,
                user_content_manager=manager,
                is_ephemeral=True,
            )
        elif related is not None:
        # Creating a view "related" to an existing one puts both in the same web
        # process. This is the only mechanism that still works for that in
        # WebKitGTK 2.52 -- set_process_model was deprecated to a no-op -- and it
        # is what keeps a fourth tab from meaning a fourth few-hundred-MB process
        # on a machine that is already swapping. A related view inherits the
        # content manager and context from its relative, so the console shim and
        # the content blocker come along with it.
            self.view = WebKit2.WebView.new_with_related_view(related)
        else:
            # NOT new_with_user_content_manager(): that constructor takes the
            # *default* context, so the very first tab -- the one every other
            # tab is created related to, and therefore the one that decides the
            # whole window's storage -- would silently opt out of the persistent
            # cookie jar this browser just built. It has to be the property
            # constructor to name a context at all.
            self.view = WebKit2.WebView(web_context=context,
                                        user_content_manager=manager)
        self.waiters = []
        self.loading = False
        self.failed = None
        # Bumped on every navigation we initiate. WebKit keeps delivering events
        # for a load after a newer one has replaced it, so a waiter records the
        # generation it belongs to and stale events are dropped instead of
        # resolving the wrong request.
        self.generation = 0

        # -- discard state ---------------------------------------------------
        # `used` is a monotonic timestamp of the last time this tab was looked
        # at or driven; it is what makes "least recently used" mean something.
        # `discarded` holds the URL and title of a tab whose page has been
        # dropped to reclaim memory -- the tab is still there, still in the same
        # place in the strip, and reloads when it is next selected.
        self.used = time.monotonic()
        self.discarded = None
        self.scroll = 0

    def touch(self):
        self.used = time.monotonic()

    def info(self):
        out = {
            "id": self.id,
            "url": (self.discarded or {}).get("url") or self.view.get_uri() or "",
            "title": (self.discarded or {}).get("title") or self.view.get_title() or "",
            "loading": self.loading,
            "private": self.private,
            "discarded": bool(self.discarded),
        }
        if self.discarded:
            # Only on a discarded tab: a live tab's content can be read, so a
            # summary of it would be a stale copy of something already available.
            out["summary"] = self.discarded.get("summary") or ""
        return out


class Browser(Gtk.Window):
    def __init__(self, urls=None, dark=None):
        super().__init__(title="claude-browser")
        self.set_default_size(1180, 780)
        self.tabs = []

        gtk_settings = Gtk.Settings.get_default()
        # Read before _apply_css overwrites it: that call sets the same property,
        # so this is the only moment the desktop's own preference is still
        # legible -- and cb:settings offers "follow the desktop" as a choice.
        self.system_dark = bool(
            gtk_settings
            and gtk_settings.get_property("gtk-application-prefer-dark-theme"))
        if dark is None:
            dark = self.system_dark
        self.dark = dark
        self._apply_css(dark)

        # Before any WebView exists: WebKit reads the GTK settings block once at
        # web-process start as well as watching it, so flipping this after the
        # first page is already laid out would be a re-layout for nothing.
        # Remembered because the switch on cb:data can change CB_LIGHT later, and
        # the animations are then no longer whatever the setting says they are --
        # that page reports what was applied, not what would be applied now.
        self.light_at_start = perf.light_enabled()
        # A refused setting has to be explained on the page that refused it; the
        # omnibox flash is gone in a second and a half. Consumed by the next
        # render of cb:settings, which the write path triggers.
        self._settings_notice = None
        for note in perf.tune_gtk(gtk_settings):
            print("perf: %s" % note, flush=True)

        # One shared content manager. The console shim runs at document-start,
        # before page scripts, so it catches errors thrown during startup.
        # TOP_FRAME, not ALL_FRAMES: an ad-heavy page can carry dozens of
        # iframes, and injecting into each one is pure cost for output nobody
        # reads. The tradeoff is that console output from inside an iframe is
        # not captured.
        self.content = WebKit2.UserContentManager()
        for script in (CONSOLE_SHIM, passwords.PASSWORD_JS):
            self.content.add_script(
                WebKit2.UserScript.new(
                    script,
                    WebKit2.UserContentInjectedFrames.TOP_FRAME,
                    WebKit2.UserScriptInjectionTime.START,
                    None,
                    None,
                )
            )

        # History and bookmarks. A failure here must not stop the browser from
        # opening -- a read-only home directory should cost you your history,
        # not your browser -- so the store is optional from here on.
        try:
            self.store = store.Store()
            self.store.prune()
        except Exception as e:
            print("store: disabled (%s)" % e, flush=True)
            self.store = None

        # The page-text cache is optional in the same way, and one step more so:
        # an sqlite3 built without FTS5 costs the recall search, not the cache
        # and certainly not the browser.
        try:
            self.pagetext = pagetext.PageText()
            if not self.pagetext.available:
                print("pagetext: search disabled (%s)" % self.pagetext.reason,
                      flush=True)
        except Exception as e:
            print("pagetext: disabled (%s)" % e, flush=True)
            self.pagetext = None

        # Playbooks. Optional in the same way, and for the same reason: a home
        # directory the browser cannot write to costs you saved sequences, not
        # the browser. The recorder itself holds no disk state, so it exists
        # even when the collection does not -- `stop` is the only step that
        # needs a file.
        self.recorder = playbooks.Recorder()
        try:
            self.playbooks = playbooks.Playbooks()
        except Exception as e:
            print("playbooks: disabled (%s)" % e, flush=True)
            self.playbooks = None

        # Proves a script message came from one of our own pages. See pages.py:
        # the handler is on the shared content manager, so every page in the
        # browser can reach it, and only ours can produce this value.
        self.nonce = secrets.token_urlsafe(24)
        self.content.register_script_message_handler("cbui")
        self.content.connect("script-message-received::cbui", self._on_ui_message)

        # Saved logins. The keyring is optional in exactly the way history is:
        # a machine without a Secret Service loses password saving, not its
        # browser. `cb:passwords` says so rather than rendering an empty list.
        self.vault = passwords.open_vault()
        self.content.register_script_message_handler("cbpw")
        self.content.connect("script-message-received::cbpw", self._on_pw_message)
        self.pw_offer = None

        # Context tuning must happen before the first WebView exists, since the
        # process model is fixed once a web process has been spawned. So must
        # the context itself: a WebContext's data manager -- which is what makes
        # cookies and the disk cache persist -- can only be set at construction.
        # Everything downstream uses self.context, never WebContext.get_default();
        # mixing the two would give private tabs a different jar than normal ones
        # in the one direction that is not a feature.
        context = self.context = storage.make_context_once()
        for note in perf.tune_context(context):
            print("perf: %s" % note, flush=True)

        context.register_uri_scheme("cb", self._serve_internal)
        security = context.get_security_manager()
        if security:
            # Without this our pages are treated as an opaque origin, which
            # blocks the inline script that drives every button on them.
            security.register_uri_scheme_as_secure("cb")
        # Deferred to idle so the window paints first: compiling the blocklist
        # takes real time on a slow CPU and there is no reason to stare at a
        # blank screen through it.
        GLib.idle_add(lambda: perf.load_content_filter(
            self.content,
            lambda n: print("perf: content blocker active (%s rules)" % n, flush=True),
        ) or GLib.SOURCE_REMOVE)

        self._build_chrome()
        self._bind_keys()
        self._start_guard()

        self.connect("destroy", self._on_destroy)
        for url in (urls or [HOME]):
            self.new_tab(url)

    def _on_destroy(self, *_a):
        """Let queued history writes land before the process goes away. The
        last page you visited before quitting is exactly the one most likely to
        be sitting in the queue."""
        for sink in (self.store, getattr(self, "pagetext", None)):
            if sink:
                try:
                    sink.flush()
                    sink.close()
                except Exception:
                    pass
        Gtk.main_quit()

    # -- construction -------------------------------------------------------

    def _apply_css(self, dark):
        # One provider, reloaded. It used to build a new one per call, which was
        # harmless while this only ran at startup; cb:settings can now re-theme a
        # running window, and adding a provider per switch leaves every previous
        # sheet attached to the screen for the life of the process.
        provider = getattr(self, "_css_provider", None)
        if provider is None:
            provider = self._css_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        provider.load_from_data(style.css(dark))
        s = Gtk.Settings.get_default()
        if s:
            s.set_property("gtk-application-prefer-dark-theme", dark)

    def _icon_button(self, icon, tooltip, handler):
        btn = Gtk.Button()
        btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR))
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.set_tooltip_text(tooltip)
        btn.set_can_focus(False)
        btn.connect("clicked", handler)
        return btn

    def _build_chrome(self):
        root = self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("cb-root")
        self.add(root)
        self._agent_glow_id = None

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.get_style_context().add_class("cb-bar")
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        nav.get_style_context().add_class("cb-nav")

        self.btn_back = self._icon_button("go-previous-symbolic", "Back (Alt+←)",
                                          lambda *_: self._go(-1))
        self.btn_fwd = self._icon_button("go-next-symbolic", "Forward (Alt+→)",
                                         lambda *_: self._go(1))
        self.btn_reload = self._icon_button("view-refresh-symbolic", "Reload (Ctrl+R)",
                                            lambda *_: self._reload())
        self.btn_home = self._icon_button("go-home-symbolic", "Start page (Alt+Home)",
                                          lambda *_: self._go_home())
        for b in (self.btn_back, self.btn_fwd, self.btn_reload, self.btn_home):
            nav.pack_start(b, False, False, 0)
        bar.pack_start(nav, False, False, 0)

        self.omnibox = Gtk.Entry()
        self.omnibox.get_style_context().add_class("cb-omnibox")
        self.omnibox.set_placeholder_text("Search or enter address")
        self.omnibox.connect("activate", self._on_omnibox)
        self._build_completion()
        bar.pack_start(self.omnibox, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        right.get_style_context().add_class("cb-nav")
        self.btn_star = self._icon_button("non-starred-symbolic",
                                          "Bookmark this page (Ctrl+D)",
                                          lambda *_: self.toggle_bookmark())
        self.btn_star.get_style_context().add_class("cb-star")
        right.pack_start(self.btn_star, False, False, 0)
        right.pack_start(
            self._icon_button("tab-new-symbolic", "New tab (Ctrl+T)",
                              lambda *_: self.new_tab(HOME)), False, False, 0)
        right.pack_start(self._build_menu(), False, False, 0)
        bar.pack_start(right, False, False, 0)
        root.pack_start(bar, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.get_style_context().add_class("cb-progress")
        self.progress.set_no_show_all(True)
        root.pack_start(self.progress, False, False, 0)

        self.pw_bar = self._build_pw_bar()
        root.pack_start(self.pw_bar, False, False, 0)

        # Above the page rather than below it, where Firefox puts it: this
        # window already has a console docked at the bottom, and a second strip
        # under it would be two bars competing for the same edge.
        self.findbar = findbar.FindBar(self._current_view)
        root.pack_start(self.findbar, False, False, 0)

        # Page and panel share a draggable split rather than a fixed stack. As a
        # plain box the panel asked for a fixed 279px it could never give back:
        # a WebView's minimum height is 0, so on any window under ~900px the page
        # collapsed toward nothing and the console looked like it had gone
        # fullscreen. A Paned lets the page keep its share, and lets the greeter
        # drag the divider.
        self.split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.split.get_style_context().add_class("cb-split")
        root.pack_start(self.split, True, True, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.get_style_context().add_class("cb-tabs")
        self.notebook.set_show_border(False)
        self.notebook.set_scrollable(True)
        self.notebook.connect("switch-page", self._on_switch)
        self.split.pack1(self.notebook, True, True)

        self.panel_mode = "ask"
        self.active_agent = None
        # Every run gets a token. Stop (and any newer run) bumps it, so a worker
        # thread that is mid-stream discovers it is stale and drops its output
        # instead of interleaving with whatever replaced it. A generator doing
        # blocking socket reads cannot be interrupted from outside; this makes it
        # harmless instead.
        self.run_id = 0
        self.panel_busy = False
        # How tall the console is, in pixels, as the user last left it. Only the
        # number is remembered; every open re-derives the divider from the
        # window's current height so a smaller window never inherits a split
        # that does not fit it.
        self.panel_height = PANEL_HEIGHT
        self.panel_expanded = False
        self.panel = self._build_panel()
        self.split.pack2(self.panel, False, False)
        self.split.connect("button-release-event", self._on_split_released)
        self._reclamp_id = None
        self.connect("configure-event", self._on_configure)

    def _build_panel(self):
        """A console docked at the bottom, in the shape of an inspector:
        mode pills, a status line, scrollback, and a prompt."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.get_style_context().add_class("cb-panel")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        head.get_style_context().add_class("cb-panel-head")

        self.mode_buttons = {}
        first = None
        for key, label, _placeholder, tip in PANEL_MODES:
            btn = Gtk.RadioButton.new_with_label_from_widget(first, label)
            if first is None:
                first = btn
            btn.set_mode(False)          # render as a toggle, not a radio dot
            btn.get_style_context().add_class("cb-mode")
            btn.set_tooltip_text(tip)
            btn.set_can_focus(False)
            btn.connect("toggled", self._on_mode_toggled, key)
            head.pack_start(btn, False, False, 0)
            self.mode_buttons[key] = btn

        # How Claude answers, next to what it is being asked. A combo rather
        # than more pills: five options would double the width of the mode row,
        # and unlike the modes this is a setting you pick once.
        self.persona_combo = Gtk.ComboBoxText()
        self.persona_combo.get_style_context().add_class("cb-persona")
        for key, name in personas.choices():
            self.persona_combo.append(key, name)
        self.persona_combo.set_active_id(personas.current())
        self.persona_combo.set_tooltip_text(
            "How Claude answers in this panel. Remembered across restarts; it "
            "adds to the panel's instructions rather than replacing them.")
        self.persona_combo.set_can_focus(False)
        self.persona_combo.connect("changed", self._on_persona_changed)
        head.pack_start(self.persona_combo, False, False, 4)

        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.get_style_context().add_class("cb-status")
        head.pack_start(self.status, True, True, 0)

        self.panel_stop = Gtk.Button(label="Stop")
        self.panel_stop.get_style_context().add_class("cb-panel-btn")
        self.panel_stop.get_style_context().add_class("stop")
        self.panel_stop.set_can_focus(False)
        self.panel_stop.set_sensitive(False)
        self.panel_stop.connect("clicked", lambda *_: self.stop_run())
        head.pack_start(self.panel_stop, False, False, 0)

        self.panel_expand = Gtk.Button(label="⤢")
        self.panel_expand.get_style_context().add_class("cb-panel-btn")
        self.panel_expand.set_tooltip_text("Full height (Ctrl+Shift+K) — "
                                           "or drag the top edge to resize")
        self.panel_expand.set_can_focus(False)
        self.panel_expand.connect("clicked", lambda *_: self.toggle_panel_expanded())
        head.pack_start(self.panel_expand, False, False, 0)

        clear = Gtk.Button(label="Clear")
        clear.get_style_context().add_class("cb-panel-btn")
        clear.set_can_focus(False)
        clear.connect("clicked", lambda *_: self._panel_write("", replace=True))
        head.pack_start(clear, False, False, 0)

        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("cb-panel-btn")
        close.set_can_focus(False)
        close.connect("clicked", lambda *_: self.close_panel())
        head.pack_start(close, False, False, 0)
        box.pack_start(head, False, False, 0)

        # The output surface is a WebView rendering local HTML, not a text
        # widget. We are already running a browser engine, so cards, colour and
        # typography come free; it is driven entirely by evaluate_javascript.
        self.panel_view = WebKit2.WebView()
        self.panel_view.set_background_color(Gdk.RGBA(0, 0, 0, 0))
        pv = self.panel_view.get_settings()
        pv.set_enable_developer_extras(False)
        pv.set_enable_webgl(False)
        pv.set_javascript_can_open_windows_automatically(False)
        # A floor, not a height. The divider decides how tall the console
        # actually is; this only stops a drag from squashing it to nothing.
        self.panel_view.set_size_request(-1, PANEL_MIN)
        # An answer can cite links, and the panel is a document like any other:
        # a click would navigate the console itself away, leaving the whole
        # Claude surface replaced by a web page with no way back. Every
        # navigation after the initial load goes to a tab instead.
        self.panel_view.connect("decide-policy", self._on_panel_policy)
        self.panel_ready = False
        self.panel_queue = []
        self.panel_view.connect("load-changed", self._on_panel_loaded)
        self.panel_view.load_html(panel_html.page(style.palette(self.dark)), None)
        box.pack_start(self.panel_view, True, True, 0)

        prompt_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        prompt_row.get_style_context().add_class("cb-prompt-row")
        chevron = Gtk.Label(label="›")
        chevron.get_style_context().add_class("cb-chevron")
        prompt_row.pack_start(chevron, False, False, 0)
        self.panel_entry = Gtk.Entry()
        self.panel_entry.get_style_context().add_class("cb-prompt")
        self.panel_entry.set_has_frame(False)
        self.panel_entry.connect("activate", self._on_panel_entry)
        prompt_row.pack_start(self.panel_entry, True, True, 0)
        box.pack_start(prompt_row, False, False, 0)

        # Show the children NOW, then mark the container no-show-all and hide it.
        #
        # Getting this backwards is what made every Claude feature look broken:
        # with no_show_all set first, the window's show_all() never reached the
        # children, and a later panel.show_all() is a no-op for the same reason.
        # The panel appeared as an empty strip while answers were written into a
        # TextView that was never shown.
        box.show_all()
        box.set_no_show_all(True)
        box.hide()
        return box

    def _on_persona_changed(self, combo):
        """The panel's selector, writing straight through to the settings file.

        Compared against what is on disk first, so setting the persona from the
        API -- which updates this widget to match -- does not bounce back into a
        second write of the value it just stored.
        """
        key = combo.get_active_id()
        if not key or key == personas.current():
            return
        try:
            personas.remember(key)
        except (OSError, ValueError) as e:
            return self._flash("could not save the persona: %s" % e)
        self._set_status("persona: %s" % personas.label(key), "ok")

    def _on_panel_policy(self, _view, decision, kind):
        """Send a link clicked in an answer to a tab, never to the panel."""
        if kind not in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                        WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        req = decision.get_navigation_action().get_request()
        uri = req.get_uri() or ""
        # The panel's own document is loaded from a string, which arrives here
        # as about:blank; that one load must be allowed through.
        if not uri or uri.startswith("about:"):
            return False
        decision.ignore()
        if uri.startswith(("http://", "https://")):
            self.new_tab(uri)
        return True

    # -- console geometry ---------------------------------------------------
    #
    # One number is authoritative: `panel_height`, the console height in pixels
    # as the user last dragged it. The divider is always derived from it against
    # the window we currently have, never the other way round -- a squeezed
    # divider must not become the remembered size, or a shrink-then-grow leaves
    # the console stuck small.

    def _panel_target(self, height=None):
        """The divider position a console of `height` pixels wants, clamped.

        Returns None while the split has no allocation yet."""
        total = self.split.get_allocated_height()
        if total <= 1:
            return None
        if self.panel_expanded:
            return 0
        want = self.panel_height if height is None else height
        want = max(PANEL_MIN, min(want, max(PANEL_MIN, total - PAGE_MIN)))
        return total - want

    def _apply_panel_height(self, height=None):
        """Move the divider to where `height` asks for, clamped to the window."""
        target = self._panel_target(height)
        if target is None:             # not allocated yet; retry after layout
            GLib.idle_add(lambda: (self._apply_panel_height(height), False)[1])
            return
        if height is not None:
            self.panel_height = height
        # No guard is needed against reading this back as a drag: the remembered
        # height comes from the button release on the handle, which a
        # programmatic move never produces.
        self.split.set_position(target)

    def _on_configure(self, *_):
        """Re-clamp after a window resize, from an idle rather than in-line.

        A remembered height is in pixels, so a shorter window would otherwise
        keep the old console and squeeze the page to a sliver -- the exact
        failure this replaced. It has to run *after* the resize settles:
        set_position() called during the allocation the resize triggers is
        overwritten by that same allocation, so the clamp silently did nothing.
        configure-event fires per motion tick, so the pending re-clamp is
        collapsed to one."""
        if self._reclamp_id is None and self.panel.get_visible():
            self._reclamp_id = GLib.idle_add(self._reclamp)
        return False

    def _reclamp(self):
        self._reclamp_id = None
        if self.panel.get_visible():
            self._apply_panel_height()
        return GLib.SOURCE_REMOVE

    def _on_split_released(self, *_):
        """Remember the height the user just dragged to.

        Memory is taken from the button release on the handle, not from
        notify::position: GtkPaned also moves the divider itself whenever the
        window no longer fits, and at the notify handler that is
        indistinguishable from a deliberate drag."""
        if not self.panel.get_visible():
            return False
        height = self.split.get_allocated_height() - self.split.get_position()
        if height >= PANEL_MIN:
            self.panel_height = height
        # Dragging the divider back down is itself a request to stop being full
        # height.
        if self.panel_expanded and self.split.get_position() > 0:
            self.panel_expanded = False
        return False

    def toggle_panel_expanded(self):
        """Full window height, and back to the docked console."""
        if not self.panel.get_visible():
            self.open_panel(self.panel_mode)
        self.panel_expanded = not self.panel_expanded
        self._apply_panel_height()
        return True

    def close_panel(self):
        if self.panel_busy:
            self.stop_run()
        self.panel.hide()
        tab = self.current()
        if tab:
            tab.view.grab_focus()

    def _on_mode_toggled(self, button, key):
        """Clicking a pill switches mode. TL;DR and Research act immediately --
        they need no input, and making the user press Enter on an empty prompt
        to get a summary would be a pointless extra step."""
        if not button.get_active() or getattr(self, "_setting_mode", False):
            return
        self.open_panel(key)
        if key == "tldr":
            self.tldr()
        elif key == "research":
            self.research()

    def _set_status(self, text, kind=""):
        ctx = self.status.get_style_context()
        for name in ("busy", "ok", "warn"):
            ctx.remove_class(name)
        if kind:
            ctx.add_class(kind)
        self.status.set_text(text)
        return GLib.SOURCE_REMOVE

    def _stop_agent(self):
        if self.active_agent:
            self.active_agent.cancel()
            self.active_agent = None

    def stop_run(self):
        """Cancel whatever the panel is doing: agent loop or streamed answer."""
        self._stop_agent()
        self.run_id += 1          # strands any in-flight worker
        self.panel_busy = False
        self.panel_stop.set_sensitive(False)
        self._flush_pending()
        self._panel_done("")
        self._set_status("stopped", "warn")
        return GLib.SOURCE_REMOVE

    def _bind_keys(self):
        accel = {
            ("l", Gdk.ModifierType.CONTROL_MASK): lambda: (
                self.omnibox.grab_focus(), self.omnibox.select_region(0, -1)),
            ("t", Gdk.ModifierType.CONTROL_MASK): lambda: self.new_tab(HOME),
            ("w", Gdk.ModifierType.CONTROL_MASK): lambda: self.close_tab(self.current()),
            ("r", Gdk.ModifierType.CONTROL_MASK): self._reload,
            ("k", Gdk.ModifierType.CONTROL_MASK): self.toggle_ask,
            ("g", Gdk.ModifierType.CONTROL_MASK): lambda: self.open_panel("agent"),
            ("k", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                self.toggle_panel_expanded,
            ("s", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK): self.tldr,
            ("r", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self.research(),
            ("f", Gdk.ModifierType.CONTROL_MASK): self.findbar.open,
            # Chrome's and Firefox's second binding for the same thing. Costs a
            # line; saves the muscle memory of anyone who learned either.
            ("F3", 0): lambda: self.findbar.step(1),
            ("g", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self.findbar.step(-1),
            ("d", Gdk.ModifierType.CONTROL_MASK): self.toggle_bookmark,
            # Firefox's binding for reader view. Ctrl+Shift+R is already the
            # research panel here, so the Alt variant is the free one that
            # anyone's fingers already know.
            ("r", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
                self.toggle_reader,
            ("h", Gdk.ModifierType.CONTROL_MASK):
                lambda: self._open_internal("cb:history"),
            ("o", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self._open_internal("cb:bookmarks"),
            ("a", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self._open_internal("cb:deck"),
            ("p", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self.new_tab(HOME, private=True),
            ("n", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self.new_tab(HOME, private=True),
            ("Home", Gdk.ModifierType.MOD1_MASK): self._go_home,
            ("q", Gdk.ModifierType.CONTROL_MASK): Gtk.main_quit,
            ("equal", Gdk.ModifierType.CONTROL_MASK): lambda: self._zoom(0.1),
            ("minus", Gdk.ModifierType.CONTROL_MASK): lambda: self._zoom(-0.1),
            ("0", Gdk.ModifierType.CONTROL_MASK): lambda: self._zoom(None),
            ("Left", Gdk.ModifierType.MOD1_MASK): lambda: self._go(-1),
            ("Right", Gdk.ModifierType.MOD1_MASK): lambda: self._go(1),
        }
        self._accels = {(Gdk.keyval_from_name(k), m): fn for (k, m), fn in accel.items()}
        self.connect("key-press-event", self._on_key)

    def _on_key(self, _widget, event):
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        action = self._accels.get((Gdk.keyval_to_lower(event.keyval), mods))
        if action:
            action()
            return True
        if event.keyval == Gdk.KEY_Escape:
            # The find bar wins Escape over the Claude panel, and both of them
            # are handled here rather than in the focused widget: a handler
            # connected to Gtk.Window sees the key *before* the default handler
            # propagates it down, so the bar's own Escape binding never gets a
            # look in while this one is connected.
            if self.findbar.get_visible():
                self.findbar.close()
                return True
            if self.panel.get_visible():
                self.close_panel()
                return True
        if event.keyval == Gdk.KEY_F12:
            tab = self.current()
            if tab:
                tab.view.get_inspector().show()
            return True
        return False

    def _build_completion(self):
        """Address-bar suggestions from history and bookmarks.

        The model is rebuilt on each keystroke and the match function always
        returns True, because the ranking (bookmarks first, then frecency) is
        done in SQL -- GtkEntryCompletion's own matching is a substring test
        that would undo it.
        """
        self._suggest_model = Gtk.ListStore(str, str, str)   # display, url, mark
        completion = Gtk.EntryCompletion()
        completion.set_model(self._suggest_model)
        completion.set_match_func(lambda *_a: True)
        completion.set_minimum_key_length(1)
        completion.set_popup_completion(True)
        completion.set_popup_single_match(True)

        mark = Gtk.CellRendererText()
        mark.set_property("xalign", 0.5)
        completion.pack_start(mark, False)
        completion.add_attribute(mark, "text", 2)
        label = Gtk.CellRendererText()
        label.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        completion.pack_start(label, True)
        completion.add_attribute(label, "markup", 0)

        completion.connect("match-selected", self._on_suggestion)
        self.omnibox.set_completion(completion)
        self._prefetch_id = None
        self._recall_id = None
        self._recall_serial = 0
        self._warmer = urls.HostWarmer(self._prefetch_dns)
        self.omnibox.connect("changed", self._on_omnibox_changed)

    def _prefetch_dns(self, host):
        """Resolve a name into WebKit's own cache before the navigation needs it.

        Deprecated upstream in 2.46 with nothing to replace it in the 4.1 API,
        so it is called defensively: on a build without it the browser simply
        does not preconnect, which is exactly where it was before.
        """
        try:
            self.context.prefetch_dns(host)
        except (AttributeError, TypeError, GLib.Error):
            pass

    def _schedule_prefetch(self, entry):
        """Warm the typed host's DNS a beat after typing stops.

        Debounced rather than deduped alone: the dedupe in `HostWarmer` cannot
        tell "example.c" from "example.com", so without the pause a single
        domain still costs a lookup for each of its prefixes.
        """
        if self._prefetch_id is not None:
            GLib.source_remove(self._prefetch_id)
            self._prefetch_id = None
        if not entry.has_focus():
            return
        text = entry.get_text()

        def fire():
            self._prefetch_id = None
            self._warmer.consider(text)
            return GLib.SOURCE_REMOVE

        self._prefetch_id = GLib.timeout_add(PREFETCH_DELAY_MS, fire)

    def _on_omnibox_changed(self, entry):
        self._schedule_prefetch(entry)
        if not entry.has_focus():
            return
        text = entry.get_text().strip()
        self._suggest_model.clear()
        self._schedule_recall(text)
        if self.store is None or len(text) < 1:
            return
        for row in self.store.suggest(text, limit=8):
            title = GLib.markup_escape_text(row["title"] or "")
            url = GLib.markup_escape_text(row["url"])
            display = ("<b>%s</b>  <span size='small' alpha='60%%'>%s</span>"
                       % (title, url)) if title else url
            self._suggest_model.append(
                [display, row["url"], "★" if row.get("bookmark") else ""])

    def _schedule_recall(self, text):
        """Search the text of read pages a beat after typing stops.

        Bumping the serial on every keystroke is what makes a late answer safe
        to apply: a query that returns after another character was typed is
        answering a question nobody is asking any more, and appending it would
        put rows in the popup that do not match the box.
        """
        if self._recall_id is not None:
            GLib.source_remove(self._recall_id)
            self._recall_id = None
        self._recall_serial += 1
        if not self.pagetext or not self.pagetext.available:
            return
        if len(text) < RECALL_MIN_CHARS:
            return
        import threading

        serial = self._recall_serial

        def fire():
            self._recall_id = None
            # Off the main loop: this is a disk read through an index, on a
            # machine chosen for being slow, with a keystroke waiting to be
            # painted. pagetext keeps one connection per thread, so reading here
            # does not disturb its writer.
            threading.Thread(target=self._recall_worker, args=(text, serial),
                             daemon=True).start()
            return GLib.SOURCE_REMOVE

        self._recall_id = GLib.timeout_add(RECALL_DELAY_MS, fire)

    def _recall_worker(self, text, serial):
        try:
            hits = self.pagetext.search(text, limit=RECALL_SUGGESTIONS)
        except Exception:
            return  # a suggestion that failed is not worth taking the box down
        GLib.idle_add(self._show_recall, text, serial, hits)

    def _show_recall(self, text, serial, hits):
        """Append page-text hits below the history matches already in the model."""
        if serial != self._recall_serial or self.omnibox.get_text().strip() != text:
            return GLib.SOURCE_REMOVE
        seen = {row[1] for row in self._suggest_model}
        for hit in hits:
            if hit["url"] in seen:
                continue  # already offered as a title or URL match
            title = GLib.markup_escape_text(hit["title"] or hit["url"])
            snippet = GLib.markup_escape_text(hit["snippet"] or hit["url"])
            self._suggest_model.append([
                "<b>%s</b>  <span size='small' alpha='60%%'>%s</span>"
                % (title, snippet),
                hit["url"],
                # A different glyph from the bookmark star, in the column that
                # already exists to say what kind of match a row is.
                "¶"])
        return GLib.SOURCE_REMOVE

    def _on_suggestion(self, _completion, model, treeiter):
        url = model[treeiter][1]
        self.omnibox.set_text(url)
        self._load(url)
        return True

    # -- the browser's own pages --------------------------------------------

    def _open_internal(self, url):
        """Reuse an already-open internal page rather than stacking duplicates
        -- pressing Ctrl+H four times should not leave four history tabs."""
        for tab in self.tabs:
            if (tab.view.get_uri() or "").lower().rstrip("/") == url:
                self.notebook.set_current_page(self.notebook.page_num(tab.view))
                tab.view.reload()
                return tab
        tab = self.current()
        if tab and (tab.view.get_uri() or "") in ("", "about:blank"):
            self._begin_load(tab)
            tab.view.load_uri(url)
            return tab
        return self.new_tab(url)

    def _load(self, url, focus=True, raw=False):
        """Send `url` to the tab in front, opening one if there is none.

        Four callers did this by hand and one of them -- the omnibox -- left out
        `_begin_load`, so a typed navigation never marked the tab busy and an
        agent's `wait` immediately after could answer about the previous page.
        `raw` skips omnibox normalization for a URL we already trust.
        """
        tab = self.current() or self.new_tab()
        self._begin_load(tab)
        perf.load_url(tab.view, url if raw else normalize(url))
        if focus:
            tab.view.grab_focus()
        return tab

    def _go_home(self):
        return self._load(HOME, focus=False, raw=True)


    def _serve_internal(self, request):
        """Render cb:* on demand. These are generated per request rather than
        cached, because their whole content is state that just changed."""
        uri = request.get_uri() or "cb:home"
        body = uri[3:] if uri.lower().startswith("cb:") else uri
        name, _sep, query = body.partition("?")
        name = name.strip("/").lower() or "home"
        try:
            data = self._render_internal(name, query).encode("utf-8")
        except Exception as e:
            data = ("<meta charset=utf-8><body style='font:14px system-ui;padding:40px'>"
                    "<h1>cb:%s failed to render</h1><pre>%s: %s</pre>"
                    % (name, type(e).__name__, e)).encode("utf-8")
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        request.finish(stream, len(data), "text/html; charset=utf-8")

    def _render_internal(self, name, query=""):
        palette = style.palette(self.dark)
        term = ""
        for part in (query or "").split("&"):
            key, _sep, value = part.partition("=")
            if key == "q":
                term = GLib.uri_unescape_string(value, None) or ""

        # Checked before the store, because saved logins live in the keyring and
        # do not care whether the history database opened.
        # cb:data reports the machine and the disk, neither of which needs the
        # history database, so it is answered before the store check below --
        # a browser whose database failed to open is exactly when you want to
        # look at what else is wrong.
        if name == "data":
            self._refresh_storage_facts()
            machine = dict(self.machine.as_dict(), tabs=len(self.tabs))
            states = self._tab_states()
            machine["discarded"] = sum(1 for s in states if s["discarded"])
            machine["tab_ceiling"] = resources.tab_ceiling(self.machine, MAX_AGENT_TABS)
            machine["loading"] = sum(1 for t in self.tabs if t.loading)
            return pages.data_page(palette, self.nonce, machine,
                                   self._storage_facts, storage.human,
                                   pagetext_info=(self.pagetext.stats()
                                                  if self.pagetext else None),
                                   light={"enabled": perf.light_enabled(),
                                          "hints": perf.hint_headers(),
                                          "motion": self.light_at_start})

        # Answered before the store check below for the same reason cb:data is:
        # the settings file has nothing to do with the history database, and a
        # browser whose database failed to open is exactly when you want to
        # reach the settings.
        if name == "settings":
            notice, self._settings_notice = self._settings_notice, None
            return pages.settings_page(palette, self.nonce, settings.describe(),
                                       notice=notice)

        if name == "passwords":
            if self.vault is None:
                return pages.passwords_page(palette, self.nonce, [], available=False)
            return pages.passwords_page(palette, self.nonce, self.vault.entries(),
                                        never=self.vault.never_list())

        # Playbooks live in their own file, so like the two above they render
        # whether or not the history database opened. When even that file could
        # not be reached `self.playbooks` is None and the page says so rather
        # than raising into the scheme handler.
        if name == "playbooks":
            return pages.playbooks_page(
                palette, self.nonce,
                self.playbooks.summaries() if self.playbooks else [],
                recording=self.recorder.status(),
                available=self.playbooks is not None)

        if self.store is None:
            return pages.shell(
                name.title(), palette, self.nonce, "cb:" + name,
                pages._empty("&#9888;", "History and bookmarks are unavailable",
                             "The browser could not open its database."))

        if name == "history":
            rows = self.store.history(term or None)
            marked = {r["url"] for r in self.store.bookmarks()}
            return pages.history_page(palette, self.nonce, rows, term, marked,
                                      fulltext=self._text_matches(term))
        if name == "bookmarks":
            return pages.bookmarks_page(palette, self.nonce,
                                        self.store.bookmarks(term or None), term)
        if name == "deck":
            current = self.current()
            return pages.deck(palette, self.nonce, [
                dict(t.info(), current=(t is current)) for t in self.tabs])
        return pages.home(palette, self.nonce, self.store.bookmarks(limit=12),
                          self.store.history(limit=12), self.store.counts())

    def _text_matches(self, term, limit=8):
        """What a cb:history query matched in the *text* of pages, if anything.

        Empty rather than loud when there is no page-text store or this sqlite3
        was built without FTS5: cb:history is history's page, and its search box
        should not start reporting on a feature the user never asked for.
        """
        if not self.pagetext or not self.pagetext.available:
            return []
        if len((term or "").strip()) < RECALL_MIN_CHARS:
            return []
        return self.pagetext.search(term, limit=limit, highlight=True)

    def _on_ui_message(self, _manager, result):
        """Actions posted by cb: pages. Every one is nonce-checked first."""
        try:
            value = result.get_js_value() if hasattr(result, "get_js_value") else result
            data = json.loads(value.to_json(0) if hasattr(value, "to_json")
                              else value.to_string())
        except Exception:
            return
        if not isinstance(data, dict) or data.get("t") != self.nonce:
            # Either malformed, or a page that is not ours trying its luck.
            return

        action = data.get("action") or ""
        url, title = data.get("url") or "", data.get("title") or ""

        if action == "go":
            self._load(url)
        elif action == "bookmark" and self.store:
            self.store.bookmark(url, title)
            self._sync_star()
        elif action == "unbookmark" and self.store:
            self.store.unbookmark(url)
            self._sync_star()
        elif action == "forget" and self.store:
            self.store.forget(url)
        elif action == "pw_forget" and self.vault:
            self.vault.delete(url, title)
        elif action == "pw_allow" and self.vault:
            self.vault.clear_never(url)
        elif action == "pw_reveal" and self.vault:
            # The page asked for one secret by name. It gets exactly that one,
            # written back into the row it came from -- cb:passwords is rendered
            # without any password in it, which is the point of the eye button.
            secret = self.vault.secret(url, title)
            tab = self.current()
            if secret is not None and tab is not None:
                self._pw_js(tab, "cbui.reveal(%s, %s)"
                            % (json.dumps(data.get("idx")), json.dumps(secret)))
        elif action == "clear_data":
            # `title` carries the kind -- the message shape is fixed at
            # {action, url, title} and adding a field for one page is not worth
            # a third parameter every other sender has to ignore.
            def cleared(result):
                self._flash("Cleared %s" % title if result.get("ok")
                            else "Could not clear: %s" % result.get("error"))
                self._reload_internal()

            self._clear_kind(title, cleared)
        elif action == "set_light":
            # `title` carries the wanted state ("on"/"off") for the same reason
            # clear_data puts the kind there, and it is the state rather than a
            # flip so a stale cb:data tab cannot invert a setting it is no longer
            # showing. Parsed by the same spelling-tolerant reader as CB_LIGHT.
            wanted = perf.light_enabled(title)
            try:
                perf.remember(wanted)
            except (OSError, ValueError) as e:
                self._flash("Could not save that setting: %s" % e)
            else:
                self._flash("Lighter pages on — from the next page load"
                            if wanted else "Lighter pages off")
                self._reload_internal()
        elif action in ("set_setting", "reset_setting"):
            # `url` carries the key and `title` the new value, on the same
            # reasoning as clear_data's kind: the message shape is fixed at
            # {action, url, title}, and a fourth field would be one every other
            # sender has to ignore. A reset has no value at all.
            self._change_setting(url, None if action == "reset_setting" else title)
        elif action.startswith("pb_"):
            # `title` carries the playbook name, for the same reason clear_data
            # puts the kind there: the message shape is fixed at
            # {action, url, title}.
            self._playbook_action(action, title)
        elif action == "clear_history" and self.store:
            self.store.clear_history()
            self.store.flush()
            self._reload_internal()
        elif action == "switch":
            tab = self.find(data.get("tab"))
            if tab:
                self.notebook.set_current_page(self.notebook.page_num(tab.view))
        elif action == "closetab":
            self.close_tab(self.find(data.get("tab")))
            self._reload_internal()
        elif action == "newtab":
            self.new_tab(HOME)
        elif action == "private":
            self.new_tab(HOME, private=True)
        elif action.startswith("claude:"):
            mode = action.split(":", 1)[1]
            if mode == "tldr":
                self.tldr()
            elif mode == "research":
                self.research()
            else:
                self.open_panel(mode)

    def _refresh_storage_facts(self):
        """Recompute what cb:data shows, and re-render only if it changed.

        The page has to be handed bytes synchronously, but the cookie count
        arrives on a callback, so the first render of cb:data shows a dash where
        the number goes. This refills it -- and the "only if it changed" guard
        is what keeps that from being an infinite reload loop, because the
        second render finds the same number and stops.
        """
        fresh = storage.facts()
        previous = getattr(self, "_storage_facts", None) or {}
        fresh["domains"] = previous.get("domains")
        self._storage_facts = fresh

        def landed(count):
            if count == previous.get("domains"):
                return
            self._storage_facts = dict(fresh, domains=count)
            self._reload_internal()

        storage.domains(self.context, landed)

    def _reload_internal(self):
        """Re-render any open cb: page after the data behind it changed."""
        for tab in self.tabs:
            if (tab.view.get_uri() or "").lower().startswith("cb:"):
                tab.view.reload()

    # -- bookmarks ----------------------------------------------------------

    def toggle_bookmark(self):
        tab = self.current()
        if not tab or not self.store:
            return
        url = tab.view.get_uri() or ""
        if not store.recordable(url):
            return
        on = self.store.toggle_bookmark(url, tab.view.get_title() or "")
        self._paint_star(on)
        self._flash("Bookmarked" if on else "Bookmark removed")
        self._reload_internal()

    def _paint_star(self, on):
        self.btn_star.set_image(Gtk.Image.new_from_icon_name(
            "starred-symbolic" if on else "non-starred-symbolic",
            Gtk.IconSize.SMALL_TOOLBAR))
        ctx = self.btn_star.get_style_context()
        (ctx.add_class if on else ctx.remove_class)("on")
        self.btn_star.set_tooltip_text(
            "Remove bookmark (Ctrl+D)" if on else "Bookmark this page (Ctrl+D)")

    def _sync_star(self):
        """Repaint the star for whatever tab is in front.

        Cached on the URL: _refresh runs on every progress tick, and while one
        indexed lookup is cheap, doing it ten times a second during a page load
        is work stolen from the load itself.
        """
        tab = self.current()
        url = (tab.view.get_uri() or "") if tab else ""
        if url == getattr(self, "_star_url", None):
            return
        self._star_url = url
        self._paint_star(bool(self.store and self.store.is_bookmarked(url)))

    def _flash(self, message):
        """A short confirmation in the omnibox's place. Bookmarking with no
        feedback at all leaves you pressing Ctrl+D twice to check."""
        self.omnibox.set_text(message)
        self._flashing = True
        self._flash_token = getattr(self, "_flash_token", 0) + 1
        token = self._flash_token

        def restore():
            if token == self._flash_token:
                self._flashing = False
                tab = self.current()
                if tab and not self.omnibox.has_focus():
                    self.omnibox.set_text(tab.view.get_uri() or "")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(1400, restore)

    # -- tabs ---------------------------------------------------------------

    def new_tab(self, url=HOME, background=False, private=False):
        # A private tab must not be created *related* to a normal one, or it
        # inherits the very storage it exists to avoid.
        related = self.tabs[0].view if (self.tabs and not private) else None
        tab = Tab(self.content, self.context, related=related, private=private)
        view = tab.view

        perf.tune_view(view)

        view.connect("load-changed", self._on_load, tab)
        view.connect("load-failed", self._on_fail, tab)
        view.connect("notify::title", lambda *_: (self._retitle(tab), self._refresh(tab)))
        view.connect("notify::uri", lambda *_: self._refresh(tab))
        # Progress fires many times a second per frame. Repainting the whole bar
        # each time is work stolen from the layout we are waiting on, so this one
        # is coalesced onto a timer while the others stay immediate.
        view.connect("notify::estimated-load-progress", lambda *_: self._refresh_soon(tab))
        view.connect("create", self._on_popup)

        label = self._tab_label(tab)
        view.show()
        index = self.notebook.append_page(view, label)
        self.notebook.set_tab_reorderable(view, True)
        self.tabs.append(tab)
        self.notebook.set_show_tabs(len(self.tabs) > 1)
        if not background:
            self.notebook.set_current_page(index)
        if url:
            perf.load_url(view, normalize(url))
        else:
            view.load_uri("about:blank")
        return tab

    def _tab_label(self, tab):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.get_style_context().add_class("cb-tablabel")
        tab.label_box = box
        if tab.private:
            badge = Gtk.Label(label="private")
            badge.get_style_context().add_class("cb-priv-badge")
            box.pack_start(badge, False, False, 0)
        tab.label = Gtk.Label(label="New tab")
        tab.label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        # width_chars is the *minimum*, and it is the whole reason tabs were
        # readable or not. An ellipsizing GtkLabel reports its minimum width as
        # the width of the ellipsis alone, so GtkNotebook is free to shrink every
        # tab to a single "...". Setting max_width_chars without width_chars --
        # which is what this did -- caps the maximum and leaves the minimum at
        # nothing, so the tabs collapsed as soon as there were two of them.
        tab.label.set_width_chars(14)
        tab.label.set_max_width_chars(24)
        tab.label.set_xalign(0)
        box.pack_start(tab.label, True, True, 0)
        close = Gtk.Button()
        close.get_style_context().add_class("cb-tabclose")
        close.set_image(Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU))
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.connect("clicked", lambda *_: self.close_tab(tab))
        box.pack_start(close, False, False, 0)
        box.show_all()
        return box

    def _on_popup(self, _view, action):
        """Target=_blank and window.open() become tabs, never new windows."""
        uri = action.get_request().get_uri()
        if uri:
            self.new_tab(uri, background=True)
        return None

    def close_tab(self, tab):
        if tab is None:
            return
        index = self.notebook.page_num(tab.view)
        if index < 0:
            return
        self._settle(tab, {"ok": True, "closed": True})
        self.notebook.remove_page(index)
        self.tabs.remove(tab)
        if not self.tabs:
            Gtk.main_quit()
            return
        self.notebook.set_show_tabs(len(self.tabs) > 1)

    def current(self):
        index = self.notebook.get_current_page()
        if index < 0:
            return None
        view = self.notebook.get_nth_page(index)
        return next((t for t in self.tabs if t.view is view), None)

    def find(self, tab_id):
        if tab_id is None:
            return self.current()
        return next((t for t in self.tabs if t.id == tab_id), None)

    # -- load state ---------------------------------------------------------

    def _on_load(self, _view, event, tab):
        if event == WebKit2.LoadEvent.STARTED:
            tab.loading = True
            tab.failed = None
        elif event == WebKit2.LoadEvent.FINISHED:
            tab.loading = False
            self._remember(tab)
            self._pw_expire(tab)
            self._pw_autofill(tab)
            self._settle(tab, {"ok": tab.failed is None, **tab.info(),
                               **({"error": tab.failed} if tab.failed else {})})
        self._refresh(tab)

    def _remember(self, tab):
        """Record a visit -- unless this tab is private, which is the whole
        contract of a private tab and so is checked here rather than anywhere
        a caller might forget."""
        if tab.private or self.store is None or tab.failed:
            return
        url = tab.view.get_uri() or ""
        if not store.recordable(url):
            return
        self.store.record(url, tab.view.get_title() or "")
        tab._recorded = url
        self._cache_text(tab, url)

    def _cache_text(self, tab, url):
        """Keep the page's text for `cbctl recall`.

        Hung off the same point as the history write, and for the same reason:
        this is where we know the load finished and the tab is not private.
        Nothing here blocks -- evaluate_javascript is asynchronous, and the
        callback hands the text straight to a queue drained on a writer thread,
        so neither the extraction nor the commit sits on the load's frame.
        """
        if self.pagetext is None:
            return

        def on_result(view, result, _data=None):
            try:
                value = view.evaluate_javascript_finish(result)
                payload = json.loads(value.to_string()) if value else None
            except (GLib.Error, json.JSONDecodeError, TypeError, AttributeError):
                return  # a page we could not read is not worth a log line
            if not isinstance(payload, dict):
                return
            self.pagetext.record(url, payload.get("title") or "",
                                 payload.get("text") or "")

        try:
            tab.view.evaluate_javascript(extract.TEXT, -1, None, None, None,
                                         on_result, None)
        except GLib.Error:
            pass

    def _retitle(self, tab):
        """Titles usually arrive after the load finishes. Update in place --
        recording again would count one page load as several visits and skew
        every ranking built on the count."""
        if tab.private or self.store is None:
            return
        url = tab.view.get_uri() or ""
        if url and url == getattr(tab, "_recorded", None):
            self.store.retitle(url, tab.view.get_title() or "")

    def _on_fail(self, _view, _event, uri, error, tab):
        # A load that was cancelled or interrupted because a *newer* navigation
        # started is not a failure of anything the caller asked for. WebKit
        # reports both, and treating them as errors is what made the benchmark
        # report "Frame load interrupted (theverge.com)" while waiting on CNN.
        if (error.matches(WebKit2.network_error_quark(), WebKit2.NetworkError.CANCELLED)
                or error.matches(WebKit2.policy_error_quark(),
                                 WebKit2.PolicyError.FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE)):
            return False
        tab.failed = "%s (%s)" % (error.message, uri)
        tab.loading = False
        self._settle(tab, {"ok": False, "error": tab.failed, **tab.info()})
        self._refresh(tab)
        return False

    def _settle(self, tab, payload):
        """Resolve everyone waiting on this tab's *current* load, once.

        Waiters from an older generation are resolved too -- their navigation was
        superseded, and leaving them hanging until the control timeout is worse
        than telling them so.
        """
        self._pump_soon()
        waiters, tab.waiters = tab.waiters, []
        for generation, done in waiters:
            if generation == tab.generation:
                done(payload)
            else:
                done({"ok": False, "error": "superseded by a newer navigation",
                      **tab.info()})

    def _refresh_soon(self, tab):
        """Coalesce progress-driven repaints to ~10/s instead of every tick."""
        if getattr(tab, "_refresh_pending", False):
            return
        tab._refresh_pending = True

        def fire():
            tab._refresh_pending = False
            self._refresh(tab)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(100, fire)

    def _relabel_tabs(self):
        """Repaint every tab's name. The rule lives in tabnames.py, which has no
        GTK import so it can be tested -- the interesting cases are collisions
        between tabs, not any single tab."""
        names = tabnames.label_tabs([
            ((t.discarded or {}).get("url") or t.view.get_uri() or "",
             (t.discarded or {}).get("title") or t.view.get_title() or "",
             t.loading)
            for t in self.tabs])
        for tab, name in zip(self.tabs, names):
            if getattr(tab, "label", None):
                tab.label.set_text(name)
                info = tab.info()
                tab.label.set_tooltip_text(tabnames.tab_tooltip(
                    info["url"], info["discarded"], info.get("summary", "")))
            if getattr(tab, "label_box", None):
                # Dimmed rather than badged: a discarded tab is still that tab,
                # and a row of "zzz" markers would make an invisible optimisation
                # look like something went wrong.
                ctx = tab.label_box.get_style_context()
                (ctx.add_class if tab.discarded else ctx.remove_class)("cb-tab-dim")

    def _refresh(self, tab):
        self._relabel_tabs()
        title = tab.view.get_title() or tab.view.get_uri() or "New tab"
        if tab is not self.current():
            return
        self.set_title("%s — claude-browser%s"
                       % (title, " (private)" if tab.private else ""))
        # A flash message owns the omnibox for its second; repainting the URL
        # over it on the next progress tick would make it invisible.
        if not self.omnibox.has_focus() and not getattr(self, "_flashing", False):
            self.omnibox.set_text(tab.view.get_uri() or "")
        self._sync_star()
        root = self.get_style_context()
        (root.add_class if tab.private else root.remove_class)("cb-private")
        self.btn_back.set_sensitive(tab.view.can_go_back())
        self.btn_fwd.set_sensitive(tab.view.can_go_forward())
        progress = tab.view.get_estimated_load_progress()
        if tab.loading and progress < 1.0:
            self.progress.set_fraction(progress)
            self.progress.show()
        else:
            self.progress.hide()

    # -- "Claude is driving" indicator --------------------------------------

    def note_agent_activity(self, tab):
        """Mark `tab` as agent-driven for the next few seconds.

        Every control-API and in-browser-agent call lands here, so the glow
        tracks real activity rather than a flag someone remembered to set. The
        deadline is refreshed rather than queued: an agent taking twelve steps
        against one tab should light it once and hold, not stack twelve timers.
        """
        # Being driven counts as being used. Without this, a tab an agent is
        # working through -- reading, clicking, reading again -- looks idle to
        # the memory guard, which discards it and makes the agent's next read
        # pay for a reload. `needs_tab` funnels every tab-targeted API call
        # through here, which is the same reason the glow lives here.
        tab.touch()
        tab.agent_until = time.monotonic() + AGENT_GLOW_MS / 1000.0
        if getattr(tab, "label_box", None):
            tab.label_box.get_style_context().add_class("cb-agent")
        self._paint_agent_frame()
        if self._agent_glow_id is None:
            self._agent_glow_id = GLib.timeout_add(400, self._expire_agent_glow)

    def _expire_agent_glow(self):
        now = time.monotonic()
        live = False
        for tab in self.tabs:
            if getattr(tab, "agent_until", 0) > now:
                live = True
            elif getattr(tab, "label_box", None):
                tab.label_box.get_style_context().remove_class("cb-agent")
        self._paint_agent_frame()
        if live:
            return GLib.SOURCE_CONTINUE
        self._agent_glow_id = None
        return GLib.SOURCE_REMOVE

    def _paint_agent_frame(self):
        """The window frame glows only while the driven tab is the one on
        screen -- a background tab being read is not something to alarm the
        user about, and the tab's own glow already says it is happening."""
        tab = self.current()
        active = bool(tab and getattr(tab, "agent_until", 0) > time.monotonic())
        ctx = self.root.get_style_context()
        (ctx.add_class if active else ctx.remove_class)("cb-agent-window")

    # -- the resource guard -------------------------------------------------
    # See resources.py for the policy and why it exists. This half is the part
    # that has to touch GTK: polling, discarding, restoring, and saying no.

    def _start_guard(self):
        """Begin watching the machine. Called once, at the end of __init__."""
        self.machine = resources.Snapshot.take()
        self._guard_note = ""
        self._queue = []
        self._pump_id = None
        self._pump_queued = False
        # First renice is deferred: no web process exists yet at construction
        # time, and the first one appears when the first tab starts loading.
        GLib.timeout_add_seconds(2, self._nice_web_processes)
        GLib.timeout_add_seconds(GUARD_POLL_S, self._poll_machine)

    def _poll_machine(self):
        """Take a reading, shed what the reading says to shed, repeat forever."""
        self.machine = resources.Snapshot.take()
        if self.machine.memory_level() != resources.OK:
            self._shed()
        # Cheap, and it has to be repeated rather than done once: every new tab
        # can spawn a process, and a process born after the last pass would
        # otherwise run at the same priority as the window manager.
        self._nice_web_processes()
        self._paint_machine()
        return GLib.SOURCE_CONTINUE

    def _nice_web_processes(self):
        try:
            resources.renice_children()
        except Exception:
            pass          # a kernel without /proc, or a hardened container
        return GLib.SOURCE_REMOVE

    def _freeable(self):
        """How many tabs could still be discarded if memory demanded it."""
        return len(resources.pick_victims(self._tab_states(), len(self.tabs)))

    def _tab_states(self):
        current = self.current()
        return [{"id": t.id, "used": t.used, "current": t is current,
                 "discarded": bool(t.discarded), "loading": t.loading,
                 "private": t.private, "url": t.view.get_uri() or ""}
                for t in self.tabs]

    def _shed(self):
        """Discard as many idle background tabs as the pressure warrants."""
        states = self._tab_states()
        background = sum(1 for s in states
                         if not s["current"] and not s["discarded"]
                         and not s["private"] and s["url"])
        count = resources.discard_count(self.machine, background)
        for tab_id in resources.pick_victims(states, count):
            tab = self.find(tab_id)
            if tab:
                self.discard_tab(tab)

    def discard_tab(self, tab):
        """Drop this tab's page, keeping the tab.

        What is actually reclaimed: the parsed DOM, the layout tree, the
        JavaScript heap and every image the page decoded, all of which live in a
        web process shared with the other tabs -- so this frees real memory even
        though the process itself stays. What is lost: that tab's back/forward
        history, because WebKit offers no way to reinstate one. That is the
        honest cost, and it is why a tab is only ever discarded when the machine
        is genuinely short and never merely because it has been idle a while.

        `about:blank` rather than terminate_web_process(): tabs deliberately
        share one web process, so terminating it would take every other tab down
        with the one being discarded.
        """
        if tab.discarded or tab.private or tab is self.current():
            return False
        url = tab.view.get_uri() or ""
        if not url or url.startswith("about:"):
            return False
        tab.discarded = {"url": url, "title": tab.view.get_title() or "",
                         "summary": ""}
        # Resolve anyone waiting on this tab before the page goes: they asked
        # about a load that is now never going to finish.
        self._settle(tab, {"ok": False, "error": "tab discarded to free memory",
                           **tab.info()})
        tab.loading = False
        tab.view.load_uri("about:blank")
        self._capture_summary(tab, tab.discarded, url)
        self._relabel_tabs()
        self._flash("Freed a background tab — %s" % self.machine.reason())
        return True

    def _capture_summary(self, tab, state, url):
        """Leave a standing note of what a discarded tab held.

        Derived locally from the page-text cache, never from the API. The API
        answer would be better prose, and it would be paid for by a network
        round trip fired *by memory pressure* -- on a machine that is by
        definition already struggling, for a tab the user may never look at
        again. A lead extract of text that is already on disk costs a single
        indexed read.

        It runs from an idle rather than inline because the point of a discard
        is to free memory *now*: the sqlite read is small, but charging it to
        the discard's own frame is exactly the stutter the discard exists to
        avoid, and by then `load_uri` has already been issued.

        Nothing extra is needed to keep private pages out of this. `discard_tab`
        refuses a private tab outright, and the cache being read is only ever
        written behind `store.recordable` in `_record` -- the one privacy choke
        point -- so a page that was never recordable simply has no text here.
        """
        if self.pagetext is None:
            return

        def later():
            # Identity of the dict, not a boolean: a tab restored and discarded
            # again while this was queued carries a *new* state dict, so a
            # summary of the older page can never land on the newer one.
            if tab.discarded is state:
                state["summary"] = tabnames.lead_extract(
                    self.pagetext.text_for(url) or "")
                self._relabel_tabs()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(later)

    def restore_tab(self, tab):
        """Bring a discarded tab back. Called when it is selected, or by API."""
        if not tab.discarded:
            return False
        url = tab.discarded["url"]
        tab.discarded = None
        tab.touch()
        self._begin_load(tab)
        perf.load_url(tab.view, url)
        self._relabel_tabs()
        return True

    def _paint_machine(self):
        """Show the machine's state only when it is worth showing.

        A permanent memory gauge in the toolbar is a thing to worry about; a
        line that appears when the browser is actually holding back is a thing
        to act on. So this writes to the Claude panel's status only when the
        panel is open, and otherwise stays quiet.
        """
        note = "" if self.machine.level() == resources.OK else self.machine.reason()
        if note == self._guard_note:
            return
        self._guard_note = note
        if note and self.panel.get_visible() and not self.panel_busy:
            self._set_status("machine busy — %s" % note, "warn")

    def _admit(self, then, done):
        """Queue a page load until the machine has room for it.

        This is the fix for the reported bug, and it is a *queue* rather than a
        gate for a reason worth writing down. The first version of this let each
        request poll independently and refuse itself after twenty seconds. On a
        two-core laptop where a heavy page takes half a minute, six concurrent
        opens meant one load and five refusals -- the machine survived, which
        was the point, but the browser had become useless, which was not.

        A queue fixes both halves. Loads run one or two at a time, so their
        memory peaks never coincide (that simultaneity, not the tab count, is
        what took the machine down). Everything else waits its turn in order and
        starts the moment a slot frees, so six opens become six pages instead of
        one page and five apologies.

        Refusals still exist, because an unbounded queue is just a slower way to
        run out of memory. They are for the two cases that are genuinely not
        going to resolve: a queue that is already deep, and memory that stays
        exhausted for a full wait.
        """
        if len(self._queue) >= MAX_QUEUED_LOADS:
            return done({
                "ok": False, "machine": self.machine.as_dict(),
                "error": "refused: %d page loads are already queued on a machine "
                         "that loads them one at a time. Wait for them, or work "
                         "with the tabs that are already open."
                         % len(self._queue)})
        self._queue.append({"then": then, "done": done, "since": time.monotonic()})
        self._pump()

    def _pump(self):
        """Start whatever the machine can take, in the order it was asked for."""
        self._expire_queue()
        while self._queue:
            self.machine = snapshot = resources.Snapshot.take()
            limit = 1 if snapshot.level() != resources.OK else MAX_CONCURRENT_LOADS
            if self._inflight() >= limit:
                break

            entry = self._queue[0]
            waited = time.monotonic() - entry["since"]
            verdict, _delay, reason = resources.admit(snapshot, waited)

            if verdict == "wait":
                if waited < QUEUE_WAIT_S:
                    self._shed()      # the wait is spent freeing memory, not idling
                    break
                verdict, reason = "no", (
                    "refused: waited %ds for memory and the machine did not "
                    "recover (%s). Close a tab, or discard one." % (waited, reason))

            self._queue.pop(0)
            if verdict == "no":
                entry["done"]({"ok": False, "error": reason,
                               "machine": snapshot.as_dict()})
            else:
                entry["then"]()

        self._schedule_pump()

    def _expire_queue(self):
        """Answer anything that has been queued too long, before the HTTP side
        gives up on it.

        Being last in a queue of slow loads is the one way to wait a long time
        without the machine being in any trouble at all, and the first version
        of this let that run into the control API's own timeout. "Timed out
        after 90s" tells an agent nothing it can act on; "you were behind five
        page loads" tells it to stop opening tabs. Same delay, useful answer.
        """
        if not self._queue:
            return
        now = time.monotonic()
        keep = []
        for entry in self._queue:
            waited = now - entry["since"]
            if waited < QUEUE_TOTAL_S:
                keep.append(entry)
                continue
            entry["done"]({
                "ok": False, "machine": self.machine.as_dict(),
                "error": "refused: queued %ds behind other page loads, which this "
                         "machine runs one at a time. The tabs ahead of it are "
                         "open; retry this one when they have settled."
                         % waited})
        self._queue = keep

    def _inflight(self):
        """Page loads currently running -- for admission accounting only.

        A load that has been going for longer than STUCK_LOAD_S stops counting.
        Without that, one page that never fires FINISHED (a hung server, a
        stream) would hold the only slot forever and every later request would
        queue behind it until it timed out. The tab is still loading; it has
        just stopped being evidence about what the machine can take on.
        """
        now = time.monotonic()
        return sum(1 for t in self.tabs
                   if t.loading and now - getattr(t, "load_started", now) < STUCK_LOAD_S)

    def _schedule_pump(self):
        """Keep a timer alive exactly while something is queued.

        The pump is also called when a load settles, which is what makes the
        queue responsive; this timer is the backstop for the case where nothing
        settles because the hold-up is memory rather than a load in progress.
        """
        if not self._queue:
            if self._pump_id is not None:
                GLib.source_remove(self._pump_id)
                self._pump_id = None
            return
        if self._pump_id is None:
            self._pump_id = GLib.timeout_add(700, self._pump_tick)

    def _pump_tick(self):
        self._pump_id = None
        self._pump()
        return GLib.SOURCE_REMOVE

    def _pump_soon(self):
        """Pump on the next idle, at most once however many times this is asked.

        Called from `_settle`, so a finished load releases the next one without
        waiting for a timer tick. It needs the coalescing flag because `_settle`
        also fires for every tab the pump itself discards: pump, shed three
        tabs, three settles, three queued pumps, each of which may shed again.
        That converges on its own -- there are only so many tabs -- but it does
        so by running the whole loop once per discarded tab, and one idle
        callback does the same job.
        """
        if not self._queue or self._pump_queued:
            return
        self._pump_queued = True

        def fire():
            self._pump_queued = False
            self._pump()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(fire)

    def _on_switch(self, _nb, view, _index):
        tab = next((t for t in self.tabs if t.view is view), None)
        if tab:
            tab.touch()
            GLib.idle_add(self._refresh, tab)
            GLib.idle_add(self._paint_agent_frame)
            # Selecting a discarded tab is the moment it comes back. Deferred to
            # idle so the switch itself paints first: on this hardware a reload
            # started inline makes the tab click feel like it did not register.
            if tab.discarded:
                GLib.idle_add(lambda: (self.restore_tab(tab), GLib.SOURCE_REMOVE)[1])
            GLib.idle_add(lambda: (self.findbar.on_tab_switched(),
                                   GLib.SOURCE_REMOVE)[1])

    def _current_view(self):
        tab = self.current()
        return tab.view if tab else None

    # -- chrome actions -----------------------------------------------------

    def _on_omnibox(self, entry):
        self._load(entry.get_text())

    def _go(self, direction):
        tab = self.current()
        if not tab:
            return
        tab.view.go_back() if direction < 0 else tab.view.go_forward()

    def _reload(self):
        tab = self.current()
        if tab:
            tab.view.reload()

    def _zoom(self, delta):
        tab = self.current()
        if not tab:
            return
        tab.view.set_zoom_level(1.0 if delta is None
                                else max(0.3, min(4.0, tab.view.get_zoom_level() + delta)))

    # -- the menu -----------------------------------------------------------
    # The four Claude actions used to be four unlabelled icons in the toolbar,
    # which is four chances to misread a wrench or a bulleted list. Under one
    # heading they explain each other, and the toolbar goes back to holding only
    # what acts on the page in front of you.

    def _build_menu(self):
        button = Gtk.MenuButton()
        button.set_tooltip_text("Menu")
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_image(
            Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.MENU))
        button.get_style_context().add_class("cb-menubtn")

        popover = Gtk.Popover()
        popover.get_style_context().add_class("cb-menu")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.get_style_context().add_class("cb-menucard")

        for index, (heading, items) in enumerate(MENU_SECTIONS):
            title = Gtk.Label(label=heading, xalign=0)
            ctx = title.get_style_context()
            ctx.add_class("cb-menuhead")
            if index:
                ctx.add_class("cb-menuhead-gap")
            card.pack_start(title, False, False, 0)
            for icon, text, accel, key in items:
                card.pack_start(self._menu_row(icon, text, accel, key, popover),
                                False, False, 0)

        card.show_all()
        popover.add(card)
        button.set_popover(popover)
        self.menu_button = button
        return button

    def _menu_row(self, icon, text, accel, key, popover):
        row = Gtk.Button()
        row.set_relief(Gtk.ReliefStyle.NONE)
        row.get_style_context().add_class("cb-menuitem")

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        inner.pack_start(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU),
                         False, False, 0)
        inner.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
        if accel:
            hint = Gtk.Label(label=accel, xalign=1)
            hint.get_style_context().add_class("cb-accel")
            inner.pack_start(hint, False, False, 0)
        row.add(inner)

        def fire(*_):
            popover.popdown()
            # Run the action after the popover has actually gone. Doing both in
            # one frame leaves the popover painted over the window when the
            # action opens the Claude panel or moves focus.
            GLib.idle_add(lambda: (self._menu_action(key), GLib.SOURCE_REMOVE)[1])

        row.connect("clicked", fire)
        return row

    def _menu_action(self, key):
        if key.startswith("cb:"):
            return self._open_internal(key)
        return {
            "ask": self.toggle_ask,
            "tldr": self.tldr,
            "research": self.research,
            "agent": lambda: self.open_panel("agent"),
            "newtab": lambda: self.new_tab(HOME),
            "private": lambda: self.new_tab(HOME, private=True),
            "find": self.findbar.open,
            "reader": self.toggle_reader,
        }[key]()

    # -- saved logins -------------------------------------------------------
    # Two halves that never meet: filling is driven from here, against an origin
    # taken from the WebView's own URL, so a page cannot ask for a password it
    # was not given. Saving starts in the page, but the page only rings a bell --
    # see the contract at the bottom of passwords.py.

    def _build_pw_bar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.get_style_context().add_class("cb-pwbar")
        box.set_no_show_all(True)

        self.pw_label = Gtk.Label(xalign=0)
        self.pw_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        box.pack_start(self.pw_label, True, True, 0)

        for text, tip, handler in (
            ("Save", "Save this login to the system keyring", self._pw_save),
            ("Never", "Never offer to save a login for this site", self._pw_never),
            ("Not now", "Dismiss until the next sign-in", self._pw_hide),
        ):
            btn = Gtk.Button(label=text)
            btn.set_tooltip_text(tip)
            btn.get_style_context().add_class("cb-pwbtn")
            if text == "Save":
                btn.get_style_context().add_class("cb-pwbtn-go")
            btn.connect("clicked", lambda _b, h=handler: h())
            box.pack_start(btn, False, False, 0)
        return box

    def _pw_js(self, tab, script, on_value=None):
        def finished(view, result, _data=None):
            try:
                value = view.evaluate_javascript_finish(result)
            except GLib.Error:
                return          # a page that navigated out from under us
            if on_value is not None:
                on_value(value.to_string() if value is not None else "")

        tab.view.evaluate_javascript(script, -1, None, None, None, finished, None)

    def _pw_expire(self, tab):
        """Drop a pending offer once the user has left the site it belongs to.

        Deliberately not done when a load *starts*: the navigation that follows
        a successful sign-in is the one that fires immediately after the offer
        appears, and cancelling on it would mean the bar never survived long
        enough to click. Leaving the origin is the real signal.
        """
        if not self.pw_offer or tab is not self.current():
            return
        if passwords.origin_of(tab.view.get_uri() or "") != self.pw_offer[0]:
            self._pw_hide()

    def _pw_autofill(self, tab):
        """Put a saved login into a page that just finished loading."""
        if self.vault is None or tab.failed:
            return
        origin = passwords.origin_of(tab.view.get_uri() or "")
        if origin is None:
            return
        found = self.vault.credentials(origin)
        if len(found) != 1:
            # Nothing to do at zero. At two or more we would be picking an
            # account on the user's behalf, and picking wrong signs them into
            # the other one without ever saying so. Silence beats a coin flip.
            return
        self._pw_js(tab, "window.__cbPwFill ? window.__cbPwFill(%s, %s) : 0" % (
            json.dumps(found[0]["username"]), json.dumps(found[0]["password"])))

    def _on_pw_message(self, _manager, _result):
        """A page reports that it has a credential worth offering.

        The message body is a literal `1`. Everything real is read back out of
        the *focused* view, because the content manager is shared by every tab
        and the signal therefore cannot say which one rang. A background page
        ringing the bell gets the foreground page's empty pocket.
        """
        if self.vault is None:
            return
        tab = self.current()
        # A private tab does not write. Filling still works there -- reading a
        # saved password leaves no trace -- but nothing new goes to the keyring.
        if tab is None or tab.private:
            return
        origin = passwords.origin_of(tab.view.get_uri() or "")
        if origin is None:
            return
        self._pw_js(tab, "window.__cbPwTake ? window.__cbPwTake() : ''",
                    lambda raw: self._pw_maybe_offer(origin, raw))

    def _pw_maybe_offer(self, origin, raw):
        try:
            found = json.loads(raw) if raw else None
        except ValueError:
            return
        if not isinstance(found, dict):
            return
        username = found.get("username") or ""
        password = found.get("password") or ""
        if not self.vault.should_offer(origin, username, password):
            return
        self.pw_offer = (origin, username, password)
        known = self.vault.secret(origin, username) is not None
        site = origin.split("://", 1)[-1]
        self.pw_label.set_text(
            "Update the saved password for %s on %s?" % (username or "this login", site)
            if known else
            "Save the password for %s on %s?" % (username or "this login", site))
        self.pw_bar.show()
        for child in self.pw_bar.get_children():
            child.show()

    def _pw_hide(self):
        self.pw_offer = None
        self.pw_bar.hide()

    def _pw_save(self):
        if self.pw_offer and self.vault:
            origin, username, password = self.pw_offer
            self.vault.save(origin, username, password)
        self._pw_hide()

    def _pw_never(self):
        if self.pw_offer and self.vault:
            self.vault.set_never(self.pw_offer[0])
        self._pw_hide()

    # -- the Claude panel ---------------------------------------------------
    # One panel, four modes. Each mode is just a different prompt over the same
    # extract-then-stream path, so they share rendering, scrolling and cancel.

    def open_panel(self, mode):
        placeholder = PANEL_MODES[MODE_INDEX[mode]][2]
        self.panel_mode = mode
        self._setting_mode = True
        self.mode_buttons[mode].set_active(True)
        self._setting_mode = False
        self.panel_entry.set_placeholder_text(placeholder)
        was_hidden = not self.panel.get_visible()
        self.panel.show()
        if was_hidden:
            self._apply_panel_height()
        self.panel_entry.grab_focus()
        if was_hidden or not getattr(self, "_card_id", None):
            # An empty panel should explain itself rather than sit blank.
            self._js("cb.hint(%s)" % json.dumps(panel_html.empty_hint(mode)))
            self._card_id = None
        if not self.panel_busy:
            self._set_status("using %s" % auth.describe(), "")
        return self.panel

    def toggle_ask(self):
        if self.panel.get_visible() and self.panel_mode == "ask":
            self.panel.hide()
        else:
            self.open_panel("ask")

    def _on_panel_loaded(self, _view, event):
        if event != WebKit2.LoadEvent.FINISHED:
            return
        self.panel_ready = True
        # Anything emitted before the document finished loading was parked;
        # replay it now so a fast first answer is never dropped on the floor.
        queued, self.panel_queue = self.panel_queue, []
        for script in queued:
            self.panel_view.evaluate_javascript(script, -1, None, None, None, None, None)

    def _js(self, script):
        """Run one statement in the panel document, queueing until it is ready."""
        if not self.panel_ready:
            self.panel_queue.append(script)
            return GLib.SOURCE_REMOVE
        self.panel_view.evaluate_javascript(script, -1, None, None, None, None, None)
        return GLib.SOURCE_REMOVE

    # -- card-level output --------------------------------------------------
    # _panel_write keeps its old signature so every caller is unchanged; it now
    # streams into the body of the current card instead of a text buffer.

    def _new_card(self, kind="", title=""):
        self._card_id = getattr(self, "_card_seq", 0) + 1
        self._card_seq = self._card_id
        self._flush_pending()
        self._js(panel_html.call("card", str(self._card_id), kind, title))
        return str(self._card_id)

    def _panel_write(self, text, replace=False, tag=None):
        if replace:
            self._js("cb.clear()")
            self._pending = ""
            self._new_card("error" if tag == "error" else "", "")
        if not text:
            return GLib.SOURCE_REMOVE
        if tag == "error":
            card = self._new_card("error", "Error")
            self._js(panel_html.call("append", card, text))
            return GLib.SOURCE_REMOVE
        # Buffered: one evaluate_javascript per streamed token would swamp a
        # 1.6GHz core. Coalesce and flush on a timer instead.
        self._pending = getattr(self, "_pending", "") + text
        if not getattr(self, "_flush_queued", False):
            self._flush_queued = True
            GLib.timeout_add(90, self._flush_pending)
        return GLib.SOURCE_REMOVE

    def _flush_pending(self):
        self._flush_queued = False
        pending, self._pending = getattr(self, "_pending", ""), ""
        if pending and getattr(self, "_card_id", None):
            self._js(panel_html.call("append", str(self._card_id), pending))
        return GLib.SOURCE_REMOVE

    def _panel_step(self, text):
        """An agent step, rendered as a chip rather than another line."""
        self._flush_pending()
        if getattr(self, "_card_id", None):
            self._js(panel_html.call("step", str(self._card_id), text))
        return GLib.SOURCE_REMOVE

    def _panel_done(self, kind=""):
        self._flush_pending()
        if getattr(self, "_card_id", None):
            self._js(panel_html.call("done", str(self._card_id), kind))
        return GLib.SOURCE_REMOVE

    def _require_key(self):
        """Nothing here may fail quietly. If no credential exists, the panel
        says so, names the file to put one in, and stops before any request."""
        try:
            auth.candidates()
        except auth.NoCredential as e:
            self._js("cb.clear()")
            card = self._new_card("error", "No credential")
            self._js(panel_html.call("append", card, str(e)))
            self._js(panel_html.call("meta", card, "Nothing was sent."))
            self._set_status("no credential", "warn")
            return False
        return True

    def _run_stream(self, make_generator, title="Claude", subtitle="", clear=True,
                    tally=None):
        """Drive a text-producing generator on a worker thread.

        The generator does blocking network I/O, so it cannot run on the GTK
        thread; every write comes back through idle_add, gated on the run token.

        `tally` is a dict the ai.* call fills with what the scrubber removed
        from the page before sending it. It is rendered as the card's meta line
        as soon as the prompt has been built -- before the answer arrives, not
        after -- because a redaction the user only learns about once they have
        finished reading is one they cannot weigh while reading.
        """
        import threading

        token = self._start_run()
        if clear:
            # Ask mode has already drawn the user's question as its own card;
            # clearing here would wipe it before the answer arrived.
            self._js("cb.clear()")
        self._pending = ""
        card = self._new_card("", ("%s \u2014 %s" % (title, subtitle)) if subtitle else title)
        self.got_output = False

        def write(chunk):
            if token == self.run_id:
                self.got_output = True
                self._panel_write(chunk)
            return GLib.SOURCE_REMOVE

        def fail(message):
            if token == self.run_id:
                self._panel_write(message, tag="error")
                self._finish_run(token, "failed", "error")
            return GLib.SOURCE_REMOVE

        def note(counts):
            if token == self.run_id and counts:
                self._js(panel_html.call("meta", card, scrub.describe(counts)))
            return GLib.SOURCE_REMOVE

        def work():
            try:
                generator = make_generator()
                # The prompt is assembled by the call above, not lazily inside
                # the generator, so the tally is complete here.
                if tally is not None:
                    GLib.idle_add(note, dict(tally))
                for chunk in generator:
                    if token != self.run_id:
                        return
                    GLib.idle_add(write, chunk)
            except (ai.NoKey, ai.ApiError) as e:
                return GLib.idle_add(fail, str(e))
            except Exception as e:
                # Nothing may fail silently: an unexpected exception is still a
                # card the user can read.
                return GLib.idle_add(fail, "%s: %s" % (type(e).__name__, e))
            GLib.idle_add(self._settle_stream, token, card)

        threading.Thread(target=work, daemon=True).start()

    def _settle_stream(self, token, card):
        """Close out a stream, distinguishing 'finished' from 'said nothing'."""
        if token != self.run_id:
            return GLib.SOURCE_REMOVE
        self._flush_pending()
        if not getattr(self, "got_output", False):
            self._js(panel_html.call(
                "append", card,
                "The model returned no text. This usually means the request was "
                "refused or the response was empty."))
            return self._finish_run(token, "empty response", "error")
        return self._finish_run(token, "done", "ok")

    def _start_run(self):
        """Invalidate anything already running and claim the panel."""
        self._stop_agent()
        self.run_id += 1
        self.panel_busy = True
        self.panel_stop.set_sensitive(True)
        self._set_status("working\u2026", "busy")
        return self.run_id

    def _finish_run(self, token, status="done", kind="ok"):
        if token == self.run_id:
            self.panel_busy = False
            self.panel_stop.set_sensitive(False)
            label = status
            if kind == "ok" and ai.LAST_CREDENTIAL:
                label = "%s \u00b7 %s" % (status, ai.LAST_CREDENTIAL)
            self._set_status(label, kind)
            self._panel_done(kind if kind in ("ok", "error") else "")
        return GLib.SOURCE_REMOVE


    def _with_page(self, then, tab_id=None):
        """Fetch the readable text of a tab, then hand it to `then`."""
        def got(result):
            page = result.get("result") if isinstance(result, dict) else None
            then(page if isinstance(page, dict) else {"url": "", "title": "", "text": ""})

        self.api_eval(tab_id, extract.TEXT, got)


    # -- mode: TL;DR (a button, never automatic) ----------------------------

    def tldr(self):
        """Summarize the current page on demand.

        Deliberately not run on page load: that would mean an API call for every
        navigation, which is slow here and costs money on pages you never read.
        """
        self.open_panel("tldr")
        if not self._require_key():
            return
        self._set_status("reading page…", "busy")

        def go(page):
            tally = {}
            self._run_stream(
                lambda: ai.summarize(page, tally=tally),
                title="TL;DR",
                subtitle=page.get("title") or page.get("url") or "this page",
                tally=tally)

        self._with_page(go)

    # -- mode: research across tabs -----------------------------------------

    def research(self, question=None):
        """Read every open tab and synthesize across them."""
        self.open_panel("research")
        if not self._require_key():
            return
        tabs = list(self.tabs)
        if not tabs:
            self._panel_write("No tabs are open to research.", replace=True, tag="error")
            return self._set_status("nothing to read", "warn")
        self._set_status("reading %d tab%s…" % (len(tabs), "" if len(tabs) == 1 else "s"),
                         "busy")

        pages = []

        def next_tab(index):
            if index >= len(tabs):
                if not pages:
                    self._panel_write("None of the open tabs had readable text.",
                                      replace=True, tag="error")
                    return self._set_status("nothing to read", "warn")
                tally = {}
                return self._run_stream(
                    lambda: ai.synthesize(pages, question, tally=tally),
                    title="Research",
                    subtitle="%d tab%s" % (len(pages), "" if len(pages) == 1 else "s"),
                    tally=tally)

            def got(page):
                if (page.get("text") or "").strip():
                    pages.append(page)
                next_tab(index + 1)

            self._with_page(got, tab_id=tabs[index].id)

        next_tab(0)

    # -- mode: agentic command bar ------------------------------------------

    def call_sync(self, method, *args, timeout=90):
        """Run an api_* method on the GTK main loop and block until it answers.

        Exactly the bridge control.py uses for HTTP requests -- it used to be
        written out twice, once here and once there. Only ever call this from a
        worker thread; from the GTK thread it would deadlock waiting on a loop
        that cannot run while it waits.
        """
        from .control import on_main_loop

        return on_main_loop(self, method, args, timeout=timeout)

    def run_agent(self, goal):
        import threading

        if not self._require_key():
            return
        token = self._start_run()
        self._js("cb.clear()")
        self._pending = ""
        self._new_card("you", "Goal")
        self._js(panel_html.call("append", str(self._card_id), goal))
        card = self._new_card("", "Claude")

        def emit(text):
            def write():
                if token != self.run_id:
                    return GLib.SOURCE_REMOVE
                # Agent progress lines become chips; prose becomes body text.
                if text.startswith("  \u2192 "):
                    self._panel_step(text.strip()[2:].strip())
                else:
                    self._panel_write(text)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(write)

        runner = agent.Agent(self.call_sync, emit)
        self.active_agent = runner

        def work():
            try:
                runner.run(goal)
            except Exception as e:
                GLib.idle_add(self._panel_write, "%s: %s" % (type(e).__name__, e),
                              False, "error")
                return GLib.idle_add(self._finish_run, token, "failed", "error")
            GLib.idle_add(self._finish_run, token, "done", "ok")

        threading.Thread(target=work, daemon=True).start()

    def _on_panel_entry(self, entry):
        text = entry.get_text().strip()
        if not text:
            return
        entry.set_text("")
        mode = self.panel_mode
        if mode == "agent":
            self.run_agent(text)
        elif mode == "research":
            self.research(text)
        else:
            if not self._require_key():
                return
            self._js("cb.clear()")
            self._pending = ""
            self._new_card("you", "You")
            self._js(panel_html.call("append", str(self._card_id), text))

            def go(page):
                tally = {}
                self._run_stream(lambda: ai.ask(text, page, tally=tally),
                                 title="Claude", clear=False, tally=tally)

            self._with_page(go)

    # -- agent API ----------------------------------------------------------
    # Every method here takes a trailing `done` callback and calls it once.

    def api_tabs(self, done):
        current = self.current()
        done({"ok": True, "current": current.id if current else None,
              "tabs": [t.info() for t in self.tabs]})

    def api_open(self, url, background, wait, done):
        """Open a tab -- if the machine can take one.

        Two gates, in this order, because they fail for different reasons and
        deserve different answers. The ceiling is a flat refusal: no amount of
        waiting makes an eleventh tab a good idea, and the agent needs to hear
        "close one" rather than sit in a retry loop. Pressure is a wait: it
        passes on its own.
        """
        ceiling = resources.tab_ceiling(self.machine, MAX_AGENT_TABS)
        if len(self.tabs) >= ceiling:
            return done({
                "ok": False, "machine": self.machine.as_dict(),
                "error": "refused: %d tabs already open (limit %d on this machine). "
                         "Close one with browser_close, or reuse a tab with "
                         "browser_navigate." % (len(self.tabs), ceiling),
                "tabs": [t.info() for t in self.tabs]})

        def go():
            tab = self.new_tab(url, background=background)
            self.note_agent_activity(tab)
            self._begin_load(tab)
            self._await_load(tab, wait, done)

        self._admit(go, done)

    @needs_tab
    def api_navigate(self, tab, url, wait, done):
        def go():
            tab.touch()
            tab.discarded = None
            self._begin_load(tab)
            perf.load_url(tab.view, normalize(url))
            self._await_load(tab, wait, done)

        self._admit(go, done)

    @needs_tab
    def api_history(self, tab, direction, wait, done):
        if direction < 0:
            if not tab.view.can_go_back():
                return done({"ok": False, "error": "no history behind", **tab.info()})
            self._begin_load(tab)
            tab.view.go_back()
        else:
            if not tab.view.can_go_forward():
                return done({"ok": False, "error": "no history ahead", **tab.info()})
            self._begin_load(tab)
            tab.view.go_forward()
        self._await_load(tab, wait, done)

    @needs_tab
    def api_reload(self, tab, wait, done):
        self._begin_load(tab)
        tab.view.reload()
        self._await_load(tab, wait, done)

    @needs_tab
    def api_close(self, tab, done):
        self.close_tab(tab)
        done({"ok": True, "closed": tab.id})

    @needs_tab
    def api_wait(self, tab, done):
        self._await_load(tab, True, done)

    def api_present(self, done):
        """Raise the window. Used by a second launch after it hands over its
        URLs -- opening a link that lands in a window behind three others has
        only half worked."""
        # present_with_time, not present(): a plain present() is widely ignored
        # by window managers as focus-stealing, and the launcher handing us this
        # URL *is* the user's click, so it has a right to the foreground.
        self.present_with_time(Gdk.CURRENT_TIME)
        done({"ok": True})

    def _begin_load(self, tab):
        """Mark the tab busy *synchronously*, at the moment navigation is asked
        for.

        load_uri() is asynchronous: WebKit does not emit load-changed STARTED
        before it returns. Without this, _await_load looks at a tab that is still
        flagged idle, concludes the load already finished, and hands back the
        previous page immediately -- so `open X` then `text` reports the page you
        navigated away from. It showed up as 0.07s "page loads" in the benchmark.
        """
        tab.generation += 1
        tab.loading = True
        tab.failed = None
        # When this load started, so _inflight() can stop counting one that has
        # clearly hung. Set here rather than on the STARTED event for the same
        # reason `loading` is: the event has not arrived yet.
        tab.load_started = time.monotonic()

    def _await_load(self, tab, wait, done):
        if not wait:
            return done({"ok": True, **tab.info()})
        if not tab.loading:
            # Already settled -- report now rather than block until the *next*
            # navigation, which is what waiting unconditionally would do.
            return done({"ok": tab.failed is None, **tab.info(),
                         **({"error": tab.failed} if tab.failed else {})})
        tab.waiters.append((tab.generation, done))

    @needs_tab
    def api_eval(self, tab, script, done):
        """Evaluate in the tab -- bringing it back first if it was discarded.

        Every read op (text, markdown, links, find, click, fill) funnels through
        here, so this one check is what keeps discarding invisible to the API.
        Without it an agent that opened a tab, worked elsewhere long enough for
        memory to get tight, and came back would silently read `about:blank` and
        report the page as empty.
        """
        if tab.discarded:
            self.restore_tab(tab)
            return self._await_load(
                tab, True, lambda _payload: self._eval_now(tab, script, done))
        self._eval_now(tab, script, done)

    def _eval_now(self, tab, script, done):
        def on_result(view, result, _data=None):
            try:
                value = view.evaluate_javascript_finish(result)
            except GLib.Error as e:
                return done({"ok": False, "error": e.message})
            if value is None:
                return done({"ok": True, "result": None})
            text = value.to_string()
            try:
                return done({"ok": True, "result": json.loads(text)})
            except (json.JSONDecodeError, TypeError):
                return done({"ok": True, "result": text})

        tab.view.evaluate_javascript(script, -1, None, None, None, on_result, None)

    def api_console(self, tab_id, pattern, done):
        def filter_entries(result):
            if not result.get("ok"):
                return done(result)
            entries = (result.get("result") or {}).get("entries", [])
            if pattern:
                try:
                    rx = re.compile(pattern)
                except re.error as e:
                    return done({"ok": False, "error": "bad pattern: %s" % e})
                entries = [e for e in entries if rx.search(e.get("text", ""))]
            done({"ok": True, "count": len(entries), "entries": entries})

        self.api_eval(tab_id, READ_CONSOLE, filter_entries)

    def api_reader(self, tab_id, font_px, width_px, done):
        """Toggle reader mode and report the state it ended in.

        Undecorated for the same reason api_console is: the tab is resolved by
        api_eval, and resolving it twice would light the "Claude is driving"
        indicator twice for one operation.
        """
        def summarize(payload):
            if not payload.get("ok"):
                return done(payload)
            result = payload.get("result") or {}
            if not isinstance(result, dict):
                return done({"ok": False, "error": "reader script returned no state"})
            state = dict(result)
            if state.get("words"):
                state["minutes"] = reader.minutes(state["words"])
            done(state)

        self.api_eval(tab_id, reader.toggle(font_px, width_px), summarize)

    def toggle_reader(self):
        """Ctrl+Alt+R, the binding Firefox uses for the same thing."""
        def announce(state):
            if not state.get("ok"):
                return self._flash(state.get("error") or "Reader mode unavailable")
            if state.get("reader"):
                minutes = state.get("minutes")
                self._flash("Reader · %d words%s"
                            % (state.get("words", 0),
                               " · %d min" % minutes if minutes else ""))
            else:
                self._flash("Reader off")

        self.api_reader(None, None, None, announce)

    @needs_tab
    def api_screenshot(self, tab, path, done):
        def on_snapshot(view, result, _data=None):
            try:
                surface = view.get_snapshot_finish(result)
            except GLib.Error as e:
                return done({"ok": False, "error": e.message})
            try:
                if path:
                    surface.write_to_png(path)
                    return done({"ok": True, "path": path,
                                 "width": surface.get_width(),
                                 "height": surface.get_height()})
                buf = io.BytesIO()
                surface.write_to_png(buf)
                return done({"ok": True, "png": buf.getvalue()})
            except Exception as e:
                # pycairo is optional on a bare install; say so instead of 500ing.
                return done({"ok": False, "error": "cannot encode PNG (%r) -- "
                                                   "install python3-gi-cairo" % (e,)})

        tab.view.get_snapshot(
            WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE,
            None, on_snapshot, None,
        )

    # -- machine and storage ------------------------------------------------

    def api_machine(self, done):
        """What the browser thinks of the machine's state right now.

        Worth exposing rather than keeping internal: an agent that has just been
        told "wait" or "refused" can read this and decide whether to close a tab
        or to do something else for a minute, which is a better answer than
        retrying the same call until the timeout.
        """
        self.machine = resources.Snapshot.take()
        states = self._tab_states()
        done({"ok": True, **self.machine.as_dict(),
              "tabs": len(self.tabs),
              "tab_ceiling": resources.tab_ceiling(self.machine, MAX_AGENT_TABS),
              "discarded": sum(1 for s in states if s["discarded"]),
              "freeable": len(resources.pick_victims(states, len(self.tabs))),
              "loading": sum(1 for t in self.tabs if t.loading),
              # So an agent reading a suspiciously thin page can tell whether we
              # asked for it rather than blaming the site.
              "light": perf.light_enabled()})

    @needs_tab
    def api_discard(self, tab, done):
        """Drop a tab's page but keep the tab. The manual version of what the
        memory guard does on its own -- useful to an agent that knows it is
        finished with a tab but wants to keep the URL to come back to."""
        if self.discard_tab(tab):
            return done({"ok": True, "discarded": tab.id, **tab.info()})
        done({"ok": False, "error": "cannot discard this tab (it is focused, "
                                    "private, empty, or already discarded)",
              **tab.info()})

    def api_recall(self, query, limit, done):
        """Search the cached text of pages already visited.

        Undecorated and tab-free: it answers from disk, not from any tab, so an
        agent can ask what it read an hour ago in a tab that is long closed.
        """
        if self.pagetext is None:
            return done({"ok": False, "error": "page text cache is disabled"})
        if not self.pagetext.available:
            return done({"ok": False, "error": self.pagetext.reason or
                         "full-text search unavailable"})
        try:
            count = int(limit) if limit not in (None, "") else 10
        except (TypeError, ValueError):
            count = 10
        matches = self.pagetext.search(query or "", max(1, min(count, 50)))
        done({"ok": True, "count": len(matches), "matches": matches})

    def api_storage(self, done):
        storage.summary(self.context, done)

    def api_clear(self, kind, done):
        def finished(result):
            self._reload_internal()
            done(result)

        self._clear_kind(kind, finished)

    def _clear_kind(self, kind, done):
        """Delete one category of stored data, whoever asked.

        The one place that knows the page-text cache is clearable next to
        WebKit's own data: both the `clear` op and the cb:data buttons come
        through here, so "everything" cannot come to mean two different sets
        depending on which surface was used.
        """
        kind = kind or "cache"
        # Validated here rather than left to storage.clear, whose message can
        # only name the WebKit categories it knows about -- a user told to "try
        # cache/cookies/storage/all" would reasonably conclude the page-text
        # cache is not clearable at all.
        if kind != "pagetext" and kind not in storage.KINDS:
            return done({"ok": False, "error": "unknown kind %r; try %s"
                         % (kind, "/".join(sorted(set(storage.KINDS) | {"pagetext"})))})
        if kind in ("pagetext", "all"):
            if self.pagetext is None:
                if kind == "pagetext":
                    return done({"ok": False,
                                 "error": "the page text cache is disabled"})
            else:
                self.pagetext.clear()
                # Flushed rather than left to the writer thread: this returns to
                # a page reload that reads the stats straight back, and two
                # DELETEs on a capped database are far cheaper than showing a
                # user who just erased their reading history that it is still
                # there.
                self.pagetext.flush()
        if kind == "pagetext":
            return done({"ok": True, "cleared": kind})
        storage.clear(self.context, kind, done)

    def api_persona(self, name, done):
        """Report the Claude panel's persona, or switch to it.

        No `name` is a read, which is what makes one op enough: `cbctl persona`
        says which one is active and `cbctl persona critic` changes it. The
        value is written to the settings file, so it survives a restart the same
        way every other preference here does.
        """
        if name in (None, ""):
            return done({"ok": True, **personas.describe()})
        try:
            key = personas.remember(name)
        except ValueError as e:
            return done({"ok": False, "error": str(e), **personas.describe()})
        except OSError as e:
            return done({"ok": False,
                         "error": "could not write the settings file: %s" % e})
        # The panel is the other place this value is visible; leaving it showing
        # the old persona would make the setting look like it had not taken.
        self.persona_combo.set_active_id(key)
        done({"ok": True, **personas.describe()})

    def _change_setting(self, key, value):
        """One control on cb:settings, answered by the api_* method behind it.

        The same arrangement as _playbook_action: the page gets no write path of
        its own, so a value refused over the API cannot be one the page quietly
        accepts. `value` is None for the per-setting reset.
        """
        if not key:
            return          # api_settings reads with no key; a control never does

        def landed(result):
            result = result if isinstance(result, dict) else {}
            if result.get("ok"):
                return self._flash(result.get("note") or "Saved")
            error = result.get("error") or "that setting could not be changed"
            # Both, and for different readers: the flash is where the user is
            # looking, and the notice survives on the page after the flash has
            # gone -- the control itself has already snapped back to the stored
            # value, so without it there is nothing left saying why.
            self._settings_notice = {"error": error}
            self._flash(error)
            self._reload_internal()

        self.api_settings(key, value, value is None, landed)

    def api_settings(self, name, value, reset, done):
        """Report every setting, or change one.

        Shaped like api_persona: no `name` is a read, which is what lets one op
        serve `cbctl settings` and `cbctl settings CB_THEME dark`. Validation
        lives in settings.py rather than here, so the page, the HTTP route and
        the CLI cannot disagree about what a setting accepts.

        Nothing in the answer carries the control token's value -- describe()
        reports only whether one is set -- so a settings read is not a way to
        exfiltrate the credential that guards this API.
        """
        if not name:
            return done({"ok": True, **settings.describe()})

        try:
            knob = settings.get(name)
            if reset:
                settings.reset(name)
            else:
                settings.apply(name, value)
        except ValueError as e:
            return done({"ok": False, "error": str(e), **settings.describe()})
        except OSError as e:
            return done({"ok": False,
                         "error": "could not write the settings file: %s" % e})

        self._settings_took_effect(knob)
        # Every open cb: page is now showing a stale value -- cb:settings most
        # of all, but cb:data reports the light-mode switch too.
        self._reload_internal()
        done({"ok": True, "setting": name, "effect": knob.effect_note,
              "note": "%s — %s" % (knob.label, knob.effect.lower()),
              **settings.describe()})

    def _settings_took_effect(self, knob):
        """The half of a settings change that can land without a restart.

        Deliberately short. Nothing here reaches into another module to replace
        a value it captured at import: that would be a second, invisible way for
        a setting to arrive, and the page would then have to guess which of the
        two a given key follows. What is here is the surface this window owns.
        """
        if knob.key == personas.SETTING:
            # The panel's selector is the other place this is visible; leaving it
            # on the old persona makes the setting look like it did not take.
            self.persona_combo.set_active_id(personas.current())
        elif knob.key == "CB_THEME":
            wanted = settings.effective(knob)[0]
            self.dark = self.system_dark if not wanted else (wanted == "dark")
            self._apply_css(self.dark)
            # The Claude panel is not re-themed: it is a loaded document, and
            # reloading it would throw away the conversation in it. cb:settings
            # says so rather than letting it look like a rendering bug.

    # -- playbooks ----------------------------------------------------------
    # Recording happens in control.py, at the one point every API-initiated
    # operation passes through. What lives here is the half that needs tabs:
    # replaying a validated sequence, one step at a time.

    def _no_playbooks(self, done):
        if self.playbooks is None:
            done({"ok": False, "error": "playbooks are disabled (the data "
                                        "directory could not be opened)"})
            return True
        return False

    def _playbook_action(self, action, name):
        """One button on cb:playbooks, answered by the api_* method behind it.

        The page gets no path of its own: `pb_run` hands the name to
        `api_playbook_run`, which validates the file and chains the steps
        through the queue exactly as the HTTP route would. A replay loop written
        for the page would be the copy that drifts. Everything ends in a
        re-render, because every one of these changes what the page shows.
        """
        def steps(n):
            n = int(n or 0)
            return "%d step%s" % (n, "" if n == 1 else "s")

        def landed(result):
            result = result if isinstance(result, dict) else {}
            if not result.get("ok"):
                self._flash(result.get("error") or "that did not work")
            elif action == "pb_start":
                self._flash("Recording %s" % (result.get("recording") or name))
            elif action == "pb_stop":
                self._flash("Saved %s — %s" % (result.get("saved") or name,
                                               steps(result.get("steps"))))
            elif action == "pb_cancel":
                self._flash("Recording discarded" if result.get("cancelled")
                            else "Nothing was being recorded")
            elif action == "pb_delete":
                self._flash("Deleted %s" % (result.get("deleted") or name))
            else:
                self._flash("%s finished — %s"
                            % (name, steps(len(result.get("steps") or []))))
            self._reload_internal()

        if action == "pb_start":
            self.api_playbook_record("start", name, landed)
        elif action in ("pb_stop", "pb_cancel"):
            self.api_playbook_record(action[3:], None, landed)
        elif action == "pb_delete":
            self.api_playbook_delete(name, landed)
        elif action == "pb_run":
            # Said before the first step rather than only after the last: a
            # replay can spend a minute in the load queue, and a button that
            # goes quiet for that long reads as one that did nothing.
            self._flash("Running %s…" % name)
            self.api_playbook_run(name, landed)

    def api_playbook_record(self, action, name, done):
        action = (action or "").strip().lower()

        if action == "status":
            return done({"ok": True, **self.recorder.status()})

        if action == "start":
            if self.recorder.active:
                return done({"ok": False,
                             "error": "already recording %r -- stop or cancel "
                                      "that first" % self.recorder.name})
            if self._no_playbooks(done):
                return None
            try:
                started = self.recorder.start(name)
            except playbooks.PlaybookError as e:
                return done({"ok": False, "error": str(e)})
            return done({"ok": True, "recording": started,
                         "note": "every operation from here until "
                                 "`playbook-record stop` is captured; "
                                 "credential fields are skipped"})

        if action == "cancel":
            dropped = self.recorder.cancel()
            return done({"ok": True, "cancelled": dropped})

        if action == "stop":
            if not self.recorder.active:
                return done({"ok": False, "error": "not recording"})
            book, steps, skipped = self.recorder.stop()
            if self._no_playbooks(done):
                return None
            if not steps:
                return done({"ok": False, "skipped_secrets": skipped,
                             "error": "nothing replayable was recorded, so %r "
                                      "was not saved" % book})
            try:
                self.playbooks.save(book, steps, skipped)
            except (playbooks.PlaybookError, OSError) as e:
                return done({"ok": False, "error": str(e)})
            return done({"ok": True, "saved": book, "steps": len(steps),
                         "ops": [s["op"] for s in steps],
                         "skipped_secrets": skipped,
                         # Said plainly rather than left to be discovered: a
                         # login playbook that silently dropped its password
                         # step would look broken on the first replay.
                         **({"note": "%d credential field(s) were not recorded; "
                                     "the browser's own autofill supplies those "
                                     "on replay" % skipped} if skipped else {})})

        done({"ok": False, "error": "unknown action %r; use start, stop, cancel "
                                    "or status" % action})

    def api_playbook_list(self, done):
        if self._no_playbooks(done):
            return None
        done({"ok": True, "playbooks": self.playbooks.summaries(),
              "recording": self.recorder.status()})

    def api_playbook_delete(self, name, done):
        if self._no_playbooks(done):
            return None
        try:
            gone = self.playbooks.delete(name)
        except OSError as e:
            return done({"ok": False, "error": str(e)})
        if not gone:
            return done({"ok": False, "error": "no playbook named %r" % (name,)})
        done({"ok": True, "deleted": name})

    def api_playbook_run(self, name, done):
        """Replay a saved playbook, strictly one step at a time.

        Two rules shape this loop.

        **Everything is validated before anything runs.** The file is replayed
        input: an op name is checked against the registry and its parameters
        against that op's declared ones, so a playbook can only ever reach an
        `api_*` method that already exists with arguments that op already
        accepts. Nothing is evaluated, and a bad fourth step is refused before
        the first three have moved the browser somewhere nobody asked for.

        **Steps run in series, and the loads among them still queue.** Each step
        starts only when the previous one has called back, and the navigating
        ops reach `_admit` exactly as they would over HTTP -- so a six-page
        playbook is six queued loads, not six simultaneous ones. Firing them at
        once is the thing that froze the machine for twenty minutes.
        """
        if self._no_playbooks(done):
            return None
        book = self.playbooks.get(name)
        if book is None:
            return done({"ok": False, "error": "no playbook named %r" % (name,),
                         "playbooks": self.playbooks.names()})
        try:
            steps = playbooks.validate(book.get("steps"))
        except playbooks.PlaybookError as e:
            return done({"ok": False,
                         "error": "%s cannot be replayed: %s" % (name, e)})

        results = []
        finished = [False]

        def finish(payload):
            # `done` exactly once, however the run ends -- a second call would
            # put a payload nobody is waiting for onto the control queue.
            if finished[0]:
                return
            finished[0] = True
            done(payload)

        def run(index):
            if index >= len(steps):
                return finish({"ok": True, "playbook": name,
                               "steps": results})
            op, params = steps[index]
            try:
                method, call_args = op.call(self, dict(params))
            except Exception as e:
                return finish({"ok": False, "playbook": name, "steps": results,
                               "error": "step %d (%s) could not be built: %s"
                                        % (index + 1, op.name, e)})

            def after(payload):
                payload = payload if isinstance(payload, dict) else {}
                ok = payload.get("ok", True)
                results.append({"step": index + 1, "op": op.name,
                                "ok": bool(ok),
                                **({"error": payload["error"]}
                                   if payload.get("error") else {})})
                if not ok:
                    return finish({
                        "ok": False, "playbook": name, "steps": results,
                        "error": "step %d (%s) failed: %s"
                                 % (index + 1, op.name,
                                    payload.get("error") or "no reason given")})
                # Through an idle rather than straight on: an op that answers
                # synchronously would otherwise recurse once per step, and the
                # main loop gets a chance to paint between steps.
                GLib.idle_add(lambda: (run(index + 1), GLib.SOURCE_REMOVE)[1])

            getattr(self, method)(*call_args, after)

        run(0)
