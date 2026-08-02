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

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2  # noqa: E402

from . import agent, ai, auth, extract, pages, panel_html, perf, store, style  # noqa: E402
from .urls import normalize  # noqa: E402

HOME = os.environ.get("CB_HOME", "cb:home")
INTERNAL = ("cb:home", "cb:deck", "cb:bookmarks", "cb:history")
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



class Tab:
    """A web view plus the bookkeeping the API needs: a stable id, and the list
    of callbacks waiting for this tab's current load to finish."""

    _next_id = 1

    def __init__(self, manager, related=None, private=False):
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
                web_context=WebKit2.WebContext.get_default(),
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
            self.view = WebKit2.WebView.new_with_user_content_manager(manager)
        self.waiters = []
        self.loading = False
        self.failed = None
        # Bumped on every navigation we initiate. WebKit keeps delivering events
        # for a load after a newer one has replaced it, so a waiter records the
        # generation it belongs to and stale events are dropped instead of
        # resolving the wrong request.
        self.generation = 0

    def info(self):
        return {
            "id": self.id,
            "url": self.view.get_uri() or "",
            "title": self.view.get_title() or "",
            "loading": self.loading,
            "private": self.private,
        }


class Browser(Gtk.Window):
    def __init__(self, urls=None, dark=None):
        super().__init__(title="claude-browser")
        self.set_default_size(1180, 780)
        self.tabs = []

        settings = Gtk.Settings.get_default()
        if dark is None:
            dark = bool(settings and settings.get_property("gtk-application-prefer-dark-theme"))
        self.dark = dark
        self._apply_css(dark)

        # One shared content manager. The console shim runs at document-start,
        # before page scripts, so it catches errors thrown during startup.
        # TOP_FRAME, not ALL_FRAMES: an ad-heavy page can carry dozens of
        # iframes, and injecting into each one is pure cost for output nobody
        # reads. The tradeoff is that console output from inside an iframe is
        # not captured.
        self.content = WebKit2.UserContentManager()
        self.content.add_script(
            WebKit2.UserScript.new(
                CONSOLE_SHIM,
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

        # Proves a script message came from one of our own pages. See pages.py:
        # the handler is on the shared content manager, so every page in the
        # browser can reach it, and only ours can produce this value.
        self.nonce = secrets.token_urlsafe(24)
        self.content.register_script_message_handler("cbui")
        self.content.connect("script-message-received::cbui", self._on_ui_message)

        # Context tuning must happen before the first WebView exists, since the
        # process model is fixed once a web process has been spawned.
        context = WebKit2.WebContext.get_default()
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

        self.connect("destroy", self._on_destroy)
        for url in (urls or [HOME]):
            self.new_tab(url)

    def _on_destroy(self, *_a):
        """Let queued history writes land before the process goes away. The
        last page you visited before quitting is exactly the one most likely to
        be sitting in the queue."""
        if self.store:
            try:
                self.store.flush()
                self.store.close()
            except Exception:
                pass
        Gtk.main_quit()

    # -- construction -------------------------------------------------------

    def _apply_css(self, dark):
        provider = Gtk.CssProvider()
        provider.load_from_data(style.css(dark))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
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
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("cb-root")
        self.add(root)

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
            self._icon_button("format-justify-left-symbolic", "TL;DR this page (Ctrl+Shift+S)",
                              lambda *_: self.tldr()), False, False, 0)
        right.pack_start(
            self._icon_button("view-list-symbolic", "Research across all tabs (Ctrl+Shift+R)",
                              lambda *_: self.research()), False, False, 0)
        right.pack_start(
            self._icon_button("system-run-symbolic", "Command Claude to drive (Ctrl+G)",
                              lambda *_: self.open_panel("agent")), False, False, 0)
        right.pack_start(
            self._icon_button("dialog-question-symbolic",
                              "Ask Claude about this page (Ctrl+K)",
                              lambda *_: self.toggle_ask()), False, False, 0)
        right.pack_start(
            self._icon_button("view-grid-symbolic", "Deck — every tab as a card "
                                                    "(Ctrl+Shift+A)",
                              lambda *_: self._open_internal("cb:deck")), False, False, 0)
        right.pack_start(
            self._icon_button("tab-new-symbolic", "New tab (Ctrl+T)",
                              lambda *_: self.new_tab(HOME)), False, False, 0)
        bar.pack_start(right, False, False, 0)
        root.pack_start(bar, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.get_style_context().add_class("cb-progress")
        self.progress.set_no_show_all(True)
        root.pack_start(self.progress, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.notebook.get_style_context().add_class("cb-tabs")
        self.notebook.set_show_border(False)
        self.notebook.set_scrollable(True)
        self.notebook.connect("switch-page", self._on_switch)
        root.pack_start(self.notebook, True, True, 0)

        self.panel_mode = "ask"
        self.active_agent = None
        # Every run gets a token. Stop (and any newer run) bumps it, so a worker
        # thread that is mid-stream discovers it is stale and drops its output
        # instead of interleaving with whatever replaced it. A generator doing
        # blocking socket reads cannot be interrupted from outside; this makes it
        # harmless instead.
        self.run_id = 0
        self.panel_busy = False
        self.panel = self._build_panel()
        root.pack_start(self.panel, False, False, 0)

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
        self.panel_view.set_size_request(-1, 215)
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

    def close_panel(self):
        if getattr(self, "panel_busy", False):
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
        if getattr(self, "active_agent", None):
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
            ("s", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK): self.tldr,
            ("r", Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
                lambda: self.research(),
            ("d", Gdk.ModifierType.CONTROL_MASK): self.toggle_bookmark,
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
            if self.panel.get_visible():
                if getattr(self, "panel_busy", False):
                    self.stop_run()
                self.panel.hide()
                tab = self.current()
                if tab:
                    tab.view.grab_focus()
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
        self.omnibox.connect("changed", self._on_omnibox_changed)

    def _on_omnibox_changed(self, entry):
        if self.store is None or not entry.has_focus():
            return
        text = entry.get_text().strip()
        self._suggest_model.clear()
        if len(text) < 1:
            return
        for row in self.store.suggest(text, limit=8):
            title = GLib.markup_escape_text(row["title"] or "")
            url = GLib.markup_escape_text(row["url"])
            display = ("<b>%s</b>  <span size='small' alpha='60%%'>%s</span>"
                       % (title, url)) if title else url
            self._suggest_model.append(
                [display, row["url"], "★" if row.get("bookmark") else ""])

    def _on_suggestion(self, _completion, model, treeiter):
        url = model[treeiter][1]
        self.omnibox.set_text(url)
        tab = self.current() or self.new_tab()
        self._begin_load(tab)
        tab.view.load_uri(normalize(url))
        tab.view.grab_focus()
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

    def _go_home(self):
        tab = self.current() or self.new_tab(HOME)
        self._begin_load(tab)
        tab.view.load_uri(HOME)
        return tab


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

        if self.store is None:
            return pages.shell(
                name.title(), palette, self.nonce, "cb:" + name,
                pages._empty("&#9888;", "History and bookmarks are unavailable",
                             "The browser could not open its database."))

        if name == "history":
            rows = self.store.history(term or None)
            marked = {r["url"] for r in self.store.bookmarks()}
            return pages.history_page(palette, self.nonce, rows, term, marked)
        if name == "bookmarks":
            return pages.bookmarks_page(palette, self.nonce,
                                        self.store.bookmarks(term or None), term)
        if name == "deck":
            current = self.current()
            return pages.deck(palette, self.nonce, [
                dict(t.info(), current=(t is current)) for t in self.tabs])
        return pages.home(palette, self.nonce, self.store.bookmarks(limit=12),
                          self.store.history(limit=12), self.store.counts())

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
            tab = self.current() or self.new_tab()
            self._begin_load(tab)
            tab.view.load_uri(normalize(url))
            tab.view.grab_focus()
        elif action == "bookmark" and self.store:
            self.store.bookmark(url, title)
            self._sync_star()
        elif action == "unbookmark" and self.store:
            self.store.unbookmark(url)
            self._sync_star()
        elif action == "forget" and self.store:
            self.store.forget(url)
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
        tab = Tab(self.content, related=related, private=private)
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
        view.load_uri(normalize(url) if url else "about:blank")
        return tab

    def _tab_label(self, tab):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        if tab.private:
            badge = Gtk.Label(label="private")
            badge.get_style_context().add_class("cb-priv-badge")
            box.pack_start(badge, False, False, 0)
        tab.label = Gtk.Label(label="New tab")
        tab.label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        tab.label.set_max_width_chars(18)
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

    def _refresh(self, tab):
        title = tab.view.get_title() or tab.view.get_uri() or "New tab"
        if getattr(tab, "label", None):
            tab.label.set_text(title)
            tab.label.set_tooltip_text(tab.view.get_uri() or "")
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

    def _on_switch(self, _nb, view, _index):
        tab = next((t for t in self.tabs if t.view is view), None)
        if tab:
            GLib.idle_add(self._refresh, tab)

    # -- chrome actions -----------------------------------------------------

    def _on_omnibox(self, entry):
        tab = self.current() or self.new_tab()
        tab.view.load_uri(normalize(entry.get_text()))
        tab.view.grab_focus()

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
        self.panel_entry.grab_focus()
        if was_hidden or not getattr(self, "_card_id", None):
            # An empty panel should explain itself rather than sit blank.
            self._js("cb.hint(%s)" % json.dumps(panel_html.empty_hint(mode)))
            self._card_id = None
        if not getattr(self, "panel_busy", False):
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

    def _run_stream(self, make_generator, title="Claude", subtitle="", clear=True):
        """Drive a text-producing generator on a worker thread.

        The generator does blocking network I/O, so it cannot run on the GTK
        thread; every write comes back through idle_add, gated on the run token.
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

        def work():
            try:
                for chunk in make_generator():
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
        self._with_page(lambda page: self._run_stream(
            lambda: ai.summarize(page),
            title="TL;DR",
            subtitle=page.get("title") or page.get("url") or "this page"))

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
                return self._run_stream(
                    lambda: ai.synthesize(pages, question),
                    title="Research",
                    subtitle="%d tab%s" % (len(pages), "" if len(pages) == 1 else "s"))

            def got(page):
                if (page.get("text") or "").strip():
                    pages.append(page)
                next_tab(index + 1)

            self._with_page(got, tab_id=tabs[index].id)

        next_tab(0)

    # -- mode: agentic command bar ------------------------------------------

    def call_sync(self, method, *args, timeout=90):
        """Run an api_* method on the GTK main loop and block until it answers.

        Same bridge control.py uses for HTTP requests, exposed for the in-browser
        agent. Only ever call this from a worker thread -- calling it from the
        GTK thread would deadlock waiting on a loop that cannot run.
        """
        import queue

        box = queue.Queue(1)

        def on_main():
            try:
                getattr(self, method)(*args, box.put)
            except Exception as e:
                box.put({"ok": False, "error": repr(e)})
            return GLib.SOURCE_REMOVE

        GLib.idle_add(on_main)
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "timed out"}

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
            self._with_page(lambda page: self._run_stream(
                lambda: ai.ask(text, page), title="Claude", clear=False))

    # -- agent API ----------------------------------------------------------
    # Every method here takes a trailing `done` callback and calls it once.

    def api_tabs(self, done):
        current = self.current()
        done({"ok": True, "current": current.id if current else None,
              "tabs": [t.info() for t in self.tabs]})

    def api_open(self, url, background, wait, done):
        tab = self.new_tab(url, background=background)
        self._begin_load(tab)
        self._await_load(tab, wait, done)

    def api_navigate(self, tab_id, url, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        self._begin_load(tab)
        tab.view.load_uri(normalize(url))
        self._await_load(tab, wait, done)

    def api_history(self, tab_id, direction, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
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

    def api_reload(self, tab_id, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        self._begin_load(tab)
        tab.view.reload()
        self._await_load(tab, wait, done)

    def api_close(self, tab_id, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        self.close_tab(tab)
        done({"ok": True, "closed": tab.id})

    def api_wait(self, tab_id, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        self._await_load(tab, True, done)

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

    def _await_load(self, tab, wait, done):
        if not wait:
            return done({"ok": True, **tab.info()})
        if not tab.loading:
            # Already settled -- report now rather than block until the *next*
            # navigation, which is what waiting unconditionally would do.
            return done({"ok": tab.failed is None, **tab.info(),
                         **({"error": tab.failed} if tab.failed else {})})
        tab.waiters.append((tab.generation, done))

    def api_eval(self, tab_id, script, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})

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

    def api_screenshot(self, tab_id, path, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})

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
