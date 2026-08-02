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

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

from . import agent, ai, extract, perf, style  # noqa: E402
from .urls import normalize  # noqa: E402

HOME = os.environ.get("CB_HOME", "about:blank")
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

PANEL_MODES = {
    "ask": {"label": "Ask Claude about this page",
            "placeholder": "Ask about this page…   (Esc to close)", "takes_input": True},
    "tldr": {"label": "TL;DR",
             "placeholder": "Ask a follow-up…", "takes_input": True},
    "research": {"label": "Research across all open tabs",
                 "placeholder": "Optional: what to compare…  (Enter to re-run)",
                 "takes_input": True},
    "agent": {"label": "Command — Claude drives the browser",
              "placeholder": "Describe a goal…  e.g. find the pricing page and list the tiers",
              "takes_input": True},
}


class Tab:
    """A web view plus the bookkeeping the API needs: a stable id, and the list
    of callbacks waiting for this tab's current load to finish."""

    _next_id = 1

    def __init__(self, manager, related=None):
        self.id = Tab._next_id
        Tab._next_id += 1
        # Creating a view "related" to an existing one puts both in the same web
        # process. This is the only mechanism that still works for that in
        # WebKitGTK 2.52 -- set_process_model was deprecated to a no-op -- and it
        # is what keeps a fourth tab from meaning a fourth few-hundred-MB process
        # on a machine that is already swapping. A related view inherits the
        # content manager and context from its relative, so the console shim and
        # the content blocker come along with it.
        if related is not None:
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
        }


class Browser(Gtk.Window):
    def __init__(self, urls=None, dark=None):
        super().__init__(title="claude-browser")
        self.set_default_size(1180, 780)
        self.tabs = []

        settings = Gtk.Settings.get_default()
        if dark is None:
            dark = bool(settings and settings.get_property("gtk-application-prefer-dark-theme"))
        self._apply_css(dark)

        # One shared content manager: the console shim is injected into every
        # page of every tab, at document-start, before page scripts run.
        self.content = WebKit2.UserContentManager()
        self.content.add_script(
            WebKit2.UserScript.new(
                CONSOLE_SHIM,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
                None,
                None,
            )
        )

        # Context tuning must happen before the first WebView exists, since the
        # process model is fixed once a web process has been spawned.
        for note in perf.tune_context(WebKit2.WebContext.get_default()):
            print("perf: %s" % note, flush=True)
        perf.load_content_filter(
            self.content,
            lambda n: print("perf: content blocker active (%s rules)" % n, flush=True),
        )

        self._build_chrome()
        self._bind_keys()

        self.connect("destroy", Gtk.main_quit)
        for url in (urls or [HOME]):
            self.new_tab(url)

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
        for b in (self.btn_back, self.btn_fwd, self.btn_reload):
            nav.pack_start(b, False, False, 0)
        bar.pack_start(nav, False, False, 0)

        self.omnibox = Gtk.Entry()
        self.omnibox.get_style_context().add_class("cb-omnibox")
        self.omnibox.set_placeholder_text("Search or enter address")
        self.omnibox.connect("activate", self._on_omnibox)
        bar.pack_start(self.omnibox, True, True, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        right.get_style_context().add_class("cb-nav")
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
            self._icon_button("starred-symbolic", "Ask Claude about this page (Ctrl+K)",
                              lambda *_: self.toggle_ask()), False, False, 0)
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
        self.panel = self._build_panel()
        root.pack_start(self.panel, False, False, 0)

    def _build_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.get_style_context().add_class("cb-ask")
        box.set_no_show_all(True)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.panel_label = Gtk.Label(label="Ask Claude")
        self.panel_label.set_xalign(0)
        self.panel_label.get_style_context().add_class("cb-hint")
        head.pack_start(self.panel_label, True, True, 0)
        stop = Gtk.Button(label="Stop")
        stop.get_style_context().add_class("cb-tabclose")
        stop.set_can_focus(False)
        stop.connect("clicked", lambda *_: self._stop_agent())
        head.pack_start(stop, False, False, 0)
        close = self._icon_button("window-close-symbolic", "Close (Esc)",
                                  lambda *_: self.panel.hide())
        head.pack_start(close, False, False, 0)
        box.pack_start(head, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(170)
        self.panel_view = Gtk.TextView()
        self.panel_view.get_style_context().add_class("cb-ask-view")
        self.panel_view.set_editable(False)
        self.panel_view.set_cursor_visible(False)
        self.panel_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(self.panel_view)
        self.panel_scroll = scroll
        box.pack_start(scroll, True, True, 0)

        self.panel_entry = Gtk.Entry()
        self.panel_entry.get_style_context().add_class("cb-omnibox")
        self.panel_entry.connect("activate", self._on_panel_entry)
        box.pack_start(self.panel_entry, False, False, 0)
        return box

    def _stop_agent(self):
        if getattr(self, "active_agent", None):
            self.active_agent.cancel()

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
                self._stop_agent()
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

    # -- tabs ---------------------------------------------------------------

    def new_tab(self, url=HOME, background=False):
        related = self.tabs[0].view if self.tabs else None
        tab = Tab(self.content, related=related)
        view = tab.view

        perf.tune_view(view)

        view.connect("load-changed", self._on_load, tab)
        view.connect("load-failed", self._on_fail, tab)
        view.connect("notify::title", lambda *_: self._refresh(tab))
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
            self._settle(tab, {"ok": tab.failed is None, **tab.info(),
                               **({"error": tab.failed} if tab.failed else {})})
        self._refresh(tab)

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
        self.set_title("%s — claude-browser" % title)
        if not self.omnibox.has_focus():
            self.omnibox.set_text(tab.view.get_uri() or "")
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
        self.panel_mode = mode
        self.panel_label.set_text(PANEL_MODES[mode]["label"])
        self.panel_entry.set_placeholder_text(PANEL_MODES[mode]["placeholder"])
        self.panel_entry.set_sensitive(PANEL_MODES[mode]["takes_input"])
        self.panel.show()
        self.panel.show_all()
        if PANEL_MODES[mode]["takes_input"]:
            self.panel_entry.grab_focus()
        return self.panel

    def toggle_ask(self):
        if self.panel.get_visible() and self.panel_mode == "ask":
            self.panel.hide()
        else:
            self.open_panel("ask")

    def _panel_write(self, text, replace=False):
        buf = self.panel_view.get_buffer()
        if replace:
            buf.set_text(text)
        else:
            buf.insert(buf.get_end_iter(), text)
        adj = self.panel_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _run_stream(self, make_generator, header):
        """Drive a text-producing generator on a worker thread.

        The generator does blocking network I/O, so it cannot run on the GTK
        thread; every write comes back through idle_add.
        """
        import threading

        self._panel_write(header, replace=True)

        def work():
            try:
                for chunk in make_generator():
                    GLib.idle_add(self._panel_write, chunk)
            except ai.NoKey as e:
                GLib.idle_add(self._panel_write, str(e))
            except Exception as e:
                GLib.idle_add(self._panel_write, "\n[error] %r" % (e,))
            GLib.idle_add(self._panel_write, "\n")

        threading.Thread(target=work, daemon=True).start()

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
        self._panel_write("Reading page…", replace=True)
        self._with_page(lambda page: self._run_stream(
            lambda: ai.summarize(page),
            "TL;DR — %s\n\n" % (page.get("title") or page.get("url") or "this page")))

    # -- mode: research across tabs -----------------------------------------

    def research(self, question=None):
        """Read every open tab and synthesize across them."""
        self.open_panel("research")
        tabs = list(self.tabs)
        if not tabs:
            return self._panel_write("No tabs open.", replace=True)
        self._panel_write("Reading %d tab%s…\n" % (len(tabs), "" if len(tabs) == 1 else "s"),
                          replace=True)

        pages = []

        def next_tab(index):
            if index >= len(tabs):
                titles = "\n".join(
                    "  %d. %s" % (i + 1, p.get("title") or p.get("url") or "(untitled)")
                    for i, p in enumerate(pages))
                return self._run_stream(
                    lambda: ai.synthesize(pages, question),
                    "Across %d tabs:\n%s\n\n" % (len(pages), titles))

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

        self._panel_write("⌘ %s\n\n" % goal, replace=True)

        def emit(text):
            GLib.idle_add(self._panel_write, text)

        self.active_agent = agent.Agent(self.call_sync, emit)

        def work():
            try:
                self.active_agent.run(goal)
            except Exception as e:
                emit("\n[error] %r\n" % (e,))
            emit("\n")

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
            self._with_page(lambda page: self._run_stream(
                lambda: ai.ask(text, page), "You: %s\n\nClaude: " % text))

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
