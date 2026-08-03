"""Cookies, caches, and the directories both of them live in.

Before this file the browser had neither, and not in the way you would guess.

  * **Cookies were never written down.** WebKitGTK keeps cookies in memory and
    only persists them if you hand its cookie manager a file --
    `set_persistent_storage()` -- which nothing did. Every launch started signed
    out of everything, and the saved-logins vault existed to paper over a gap
    that was really this one.
  * **Everything else went to a junk directory.** The default
    WebsiteDataManager derives its paths from `g_get_prgname()`, and under
    `python3 -c` that is literally `-c`: local storage was landing in
    `~/.local/share/-c/localstorage`. Different launcher, different directory,
    so a site's storage came and went depending on how the browser was started.

Both are fixed the same way: build one WebsiteDataManager with explicit base
directories and hand it to the context, instead of taking the default context
and hoping. That has to happen before any WebView exists -- a context's data
manager is fixed at construction -- which is why `make_context()` is the first
thing Browser.__init__ calls.

The disk cache is the other half of the same story, and it is worth more here
than on a fast machine: this browser runs on a spinning-rust-era laptop over
domestic broadband, where not re-fetching a 300KB stylesheet is a visible win.

Nothing in here is on the hot path, so it is all written for legibility.
"""

import os
from pathlib import Path

import gi

gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, WebKit2  # noqa: E402

DATA = Path(GLib.get_user_data_dir()) / "claude-browser"
CACHE = Path(GLib.get_user_cache_dir()) / "claude-browser"

#: Cookie jar. SQLite rather than TEXT: the text backend rewrites the whole file
#: on every change, which on this hardware is a stall you can feel on any page
#: that sets a cookie per request.
COOKIE_JAR = DATA / "cookies.sqlite"

#: What `CB_COOKIES` accepts, and what the browser does by default.
#:
#: NO_THIRD_PARTY is the default rather than ALWAYS because the tracking cookies
#: it drops are, on this machine, also the ones attached to the third-party
#: script loads perf.py already blocks -- so it costs nothing a user notices and
#: keeps the jar small enough to stay fast. ALWAYS is one env var away for the
#: rare site that logs you in through an iframe.
POLICIES = {
    "all": WebKit2.CookieAcceptPolicy.ALWAYS,
    "none": WebKit2.CookieAcceptPolicy.NEVER,
    "nothird": WebKit2.CookieAcceptPolicy.NO_THIRD_PARTY,
}
DEFAULT_POLICY = "nothird"

#: The clearable categories, in the words a person would use for them. Grouped
#: rather than exposed one WebKit constant at a time: "site data" meaning
#: localStorage-but-not-cookies is a distinction only a browser engineer makes.
KINDS = {
    "cache": (WebKit2.WebsiteDataTypes.DISK_CACHE
              | WebKit2.WebsiteDataTypes.MEMORY_CACHE
              | WebKit2.WebsiteDataTypes.DOM_CACHE
              | WebKit2.WebsiteDataTypes.OFFLINE_APPLICATION_CACHE),
    "cookies": WebKit2.WebsiteDataTypes.COOKIES,
    "storage": (WebKit2.WebsiteDataTypes.LOCAL_STORAGE
                | WebKit2.WebsiteDataTypes.SESSION_STORAGE
                | WebKit2.WebsiteDataTypes.INDEXEDDB_DATABASES
                | WebKit2.WebsiteDataTypes.WEBSQL_DATABASES
                | WebKit2.WebsiteDataTypes.SERVICE_WORKER_REGISTRATIONS),
    "all": WebKit2.WebsiteDataTypes.ALL,
}


def policy_name():
    """The configured policy name, always one of POLICIES' keys."""
    name = (os.environ.get("CB_COOKIES") or DEFAULT_POLICY).strip().lower()
    return name if name in POLICIES else DEFAULT_POLICY


def make_context():
    """The one web context this browser uses, with storage that survives a quit.

    Returns `(context, notes)`; the notes are printed at startup so a user who
    wonders where their cookies went can see the answer in the terminal.

    Everything is best-effort. A read-only home directory should cost you your
    cookies, not your browser -- the same rule the history store follows -- so
    every step falls back to the default context rather than raising.
    """
    notes = []
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        manager = WebKit2.WebsiteDataManager(
            base_data_directory=str(DATA),
            base_cache_directory=str(CACHE),
        )
        context = WebKit2.WebContext.new_with_website_data_manager(manager)
        notes.append("data in %s" % DATA)
    except Exception as e:
        # Deprecated in 2.52 and still the only way to bind a data manager to a
        # context; if a later release removes it outright, run on the default.
        print("storage: falling back to the default context (%s)" % e, flush=True)
        context = WebKit2.WebContext.get_default()
        manager = context.get_website_data_manager()

    notes.extend(attach_cookies(manager))

    # ITP keeps a cross-site tracker from carrying state between the sites it is
    # embedded on. It costs a small database and a little bookkeeping per load,
    # which is a fair trade for a browser whose whole premise is that an agent
    # drives the session you are personally signed into.
    if os.environ.get("CB_ITP", "1") != "0" and hasattr(manager, "set_itp_enabled"):
        try:
            manager.set_itp_enabled(True)
            notes.append("tracking prevention on")
        except Exception:
            pass

    return context, notes


_CONTEXT = None


def make_context_once():
    """`make_context()` memoized, with its notes printed the first time.

    A WebContext is process-global in everything that matters -- the cookie jar,
    the disk cache, the `cb:` scheme registration -- and building a second one
    would quietly give some tabs a different jar than others. Memoizing is also
    what lets a test import this module without paying for a second context.
    """
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT, notes = make_context()
        for note in notes:
            print("storage: %s" % note, flush=True)
    return _CONTEXT


def attach_cookies(manager):
    """Give the cookie manager a file to write to, and a policy to apply."""
    notes = []
    try:
        cookies = manager.get_cookie_manager()
    except Exception:
        return notes
    if cookies is None:
        return notes

    name = policy_name()
    try:
        cookies.set_accept_policy(POLICIES[name])
    except Exception:
        pass

    if name == "none":
        # Persisting a jar we refuse to fill would only leave a stale file
        # behind, and the promise of CB_COOKIES=none is that nothing is kept.
        return notes + ["cookies off"]

    try:
        COOKIE_JAR.parent.mkdir(parents=True, exist_ok=True)
        cookies.set_persistent_storage(str(COOKIE_JAR),
                                       WebKit2.CookiePersistentStorage.SQLITE)
        notes.append("cookies persist (%s)" % name)
    except Exception as e:
        notes.append("cookies stay in memory (%s)" % e)
    return notes


# -- clearing ---------------------------------------------------------------

def clear(context, kind, done):
    """Delete one category of website data, for all time.

    `done` is called with a dict, once, on the main loop -- the api_* contract,
    because that is the only caller.
    """
    types = KINDS.get(kind)
    if types is None:
        return done({"ok": False, "error": "unknown kind %r; try %s"
                     % (kind, "/".join(sorted(KINDS)))})
    try:
        manager = context.get_website_data_manager()
    except Exception as e:
        return done({"ok": False, "error": str(e)})

    def finished(src, result):
        try:
            src.clear_finish(result)
        except GLib.Error as e:
            return done({"ok": False, "error": e.message})
        done({"ok": True, "cleared": kind})

    # A zero timespan means "everything", not "nothing": the argument is how far
    # back to go, and GLib.TimeSpan(0) reads as "since the beginning of time".
    manager.clear(types, 0, None, finished)


def facts():
    """Everything about stored data that can be answered without asking WebKit.

    Split out from `summary()` because `cb:data` is rendered synchronously --
    the `cb:` scheme handler has to hand back bytes when it is called -- and the
    only genuinely async part is the cookie domain count. So the page renders
    from this immediately and fills the count in on the next pass.
    """
    return {"policy": policy_name(),
            "data_dir": str(DATA), "cache_dir": str(CACHE),
            "cache_bytes": dir_size(CACHE),
            "cookie_jar_bytes": file_size(COOKIE_JAR),
            "domains": None}


def domains(context, done):
    """How many sites have a cookie. `done` gets an int, or None if unknown.

    A live query rather than the jar's file size: SQLite does not shrink when
    rows are deleted, so a 400KB file can hold nothing at all.
    """
    try:
        cookies = context.get_website_data_manager().get_cookie_manager()
    except Exception:
        cookies = None
    if cookies is None:
        return done(None)

    def got(src, result):
        try:
            done(len(src.get_domains_with_cookies_finish(result) or []))
        except GLib.Error:
            done(None)

    cookies.get_domains_with_cookies(None, got)


def summary(context, done):
    """`facts()` plus the cookie domain count. The `storage` API op."""
    out = dict(facts(), ok=True)
    domains(context, lambda count: done(dict(out, domains=count)))


def file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def dir_size(path):
    """Bytes under `path`, following nothing and forgiving everything.

    os.walk rather than a recursive glob: the disk cache is thousands of small
    files and walk does one readdir per directory instead of a stat storm.
    """
    total = 0
    for root, _dirs, files in os.walk(str(path), onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def human(size):
    """Bytes as a person reads them. Deliberately not `humanize`: one function
    is not worth a dependency on a machine where imports cost measurable time."""
    if size is None:
        return "—"
    if size < 1024:
        return "%d B" % size
    size /= 1024.0
    for unit in ("KB", "MB"):
        if size < 1024:
            return "%.1f %s" % (size, unit)
        size /= 1024.0
    return "%.1f GB" % size
