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

from . import ai, extract, style  # noqa: E402
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


class Tab:
    """A web view plus the bookkeeping the API needs: a stable id, and the list
    of callbacks waiting for this tab's current load to finish."""

    _next_id = 1

    def __init__(self, manager):
        self.id = Tab._next_id
        Tab._next_id += 1
        self.view = WebKit2.WebView.new_with_user_content_manager(manager)
        self.waiters = []
        self.loading = False
        self.failed = None

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

        self.ask_box = self._build_ask()
        root.pack_start(self.ask_box, False, False, 0)

    def _build_ask(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.get_style_context().add_class("cb-ask")
        box.set_no_show_all(True)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(150)
        self.ask_view = Gtk.TextView()
        self.ask_view.get_style_context().add_class("cb-ask-view")
        self.ask_view.set_editable(False)
        self.ask_view.set_cursor_visible(False)
        self.ask_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroll.add(self.ask_view)
        self.ask_scroll = scroll
        box.pack_start(scroll, True, True, 0)

        self.ask_entry = Gtk.Entry()
        self.ask_entry.get_style_context().add_class("cb-omnibox")
        self.ask_entry.set_placeholder_text("Ask Claude about this page…  (Esc to close)")
        self.ask_entry.connect("activate", self._on_ask)
        box.pack_start(self.ask_entry, False, False, 0)
        return box

    def _bind_keys(self):
        accel = {
            ("l", Gdk.ModifierType.CONTROL_MASK): lambda: (
                self.omnibox.grab_focus(), self.omnibox.select_region(0, -1)),
            ("t", Gdk.ModifierType.CONTROL_MASK): lambda: self.new_tab(HOME),
            ("w", Gdk.ModifierType.CONTROL_MASK): lambda: self.close_tab(self.current()),
            ("r", Gdk.ModifierType.CONTROL_MASK): self._reload,
            ("k", Gdk.ModifierType.CONTROL_MASK): self.toggle_ask,
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
            if self.ask_box.get_visible():
                self.ask_box.hide()
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
        tab = Tab(self.content)
        view = tab.view

        s = view.get_settings()
        s.set_enable_developer_extras(True)
        s.set_enable_smooth_scrolling(True)
        s.set_enable_page_cache(True)
        s.set_javascript_can_open_windows_automatically(False)
        if os.environ.get("CB_GPU", "").lower() in ("off", "0", "none"):
            # Old integrated GPUs render worse than software here; opt-in escape hatch.
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)

        view.connect("load-changed", self._on_load, tab)
        view.connect("load-failed", self._on_fail, tab)
        view.connect("notify::title", lambda *_: self._refresh(tab))
        view.connect("notify::uri", lambda *_: self._refresh(tab))
        view.connect("notify::estimated-load-progress", lambda *_: self._refresh(tab))
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
        tab.failed = "%s (%s)" % (error.message, uri)
        tab.loading = False
        self._settle(tab, {"ok": False, "error": tab.failed, **tab.info()})
        self._refresh(tab)
        return False

    def _settle(self, tab, payload):
        """Hand the same result to everyone waiting on this tab's load, once."""
        waiters, tab.waiters = tab.waiters, []
        for done in waiters:
            done(payload)

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

    # -- ask Claude ---------------------------------------------------------

    def toggle_ask(self):
        if self.ask_box.get_visible():
            self.ask_box.hide()
        else:
            self.ask_box.show()
            self.ask_box.show_all()
            self.ask_entry.grab_focus()

    def _ask_write(self, text, replace=False):
        buf = self.ask_view.get_buffer()
        if replace:
            buf.set_text(text)
        else:
            buf.insert(buf.get_end_iter(), text)
        adj = self.ask_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return GLib.SOURCE_REMOVE

    def _on_ask(self, entry):
        question = entry.get_text().strip()
        if not question:
            return
        entry.set_text("")
        self._ask_write("You: %s\n\nClaude: " % question, replace=True)

        def with_page(result):
            page = result.get("result") if isinstance(result, dict) else None
            if not isinstance(page, dict):
                page = {"url": "", "title": "", "text": ""}
            self._stream_answer(question, page)

        self.api_eval(None, extract.TEXT, with_page)

    def _stream_answer(self, question, page):
        import threading

        def work():
            try:
                for chunk in ai.ask(question, page):
                    GLib.idle_add(self._ask_write, chunk)
            except ai.NoKey as e:
                GLib.idle_add(self._ask_write, str(e))
            except Exception as e:  # a crashed thread must not silently do nothing
                GLib.idle_add(self._ask_write, "\n[error] %r" % (e,))
            GLib.idle_add(self._ask_write, "\n\n")

        threading.Thread(target=work, daemon=True).start()

    # -- agent API ----------------------------------------------------------
    # Every method here takes a trailing `done` callback and calls it once.

    def api_tabs(self, done):
        current = self.current()
        done({"ok": True, "current": current.id if current else None,
              "tabs": [t.info() for t in self.tabs]})

    def api_open(self, url, background, wait, done):
        tab = self.new_tab(url, background=background)
        self._await_load(tab, wait, done)

    def api_navigate(self, tab_id, url, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        tab.view.load_uri(normalize(url))
        self._await_load(tab, wait, done)

    def api_history(self, tab_id, direction, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
        if direction < 0:
            if not tab.view.can_go_back():
                return done({"ok": False, "error": "no history behind", **tab.info()})
            tab.view.go_back()
        else:
            if not tab.view.can_go_forward():
                return done({"ok": False, "error": "no history ahead", **tab.info()})
            tab.view.go_forward()
        self._await_load(tab, wait, done)

    def api_reload(self, tab_id, wait, done):
        tab = self.find(tab_id)
        if not tab:
            return done({"ok": False, "error": "no such tab"})
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

    def _await_load(self, tab, wait, done):
        if not wait:
            return done({"ok": True, **tab.info()})
        if not tab.loading:
            # Already settled -- report now rather than block until the *next*
            # navigation, which is what waiting unconditionally would do.
            return done({"ok": tab.failed is None, **tab.info(),
                         **({"error": tab.failed} if tab.failed else {})})
        tab.waiters.append(done)

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
