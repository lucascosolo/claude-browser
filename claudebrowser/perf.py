"""Making WebKit usable on a slow machine.

Written against a Celeron N3060 -- two cores at 1.6GHz, 4GB of RAM, and swap
already in use. Three things dominate on hardware like that, in order:

  1. Third-party JavaScript. On a modern news or docs page the ad and analytics
     bundles routinely outweigh the actual content several times over, and on a
     1.6GHz core that is the difference between "loads" and "hangs". Blocking it
     is worth more than every other tuning knob combined.
  2. Process count. WebKit's default is one web process per view, so a fourth
     tab means a fourth few-hundred-megabyte process. On a box that is already
     swapping, that is what makes everything -- not just the browser -- drag.
  3. Per-frame work. Smooth scrolling, WebGL and CSS animation are pure cost
     here -- see `tune_gtk`.

Those are all things done to a response after it arrives. `load_url` is the one
lever pointed the other way: `Save-Data` asks the server for a cheaper response
in the first place.

Everything is probed with hasattr before use: this file has to survive a
WebKitGTK upgrade deprecating something out from under it, and a browser that
refuses to start because a tuning call vanished is a worse outcome than a
browser that is merely slower.
"""

import os
from pathlib import Path

import gi

gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, WebKit2  # noqa: E402

CACHE = Path(GLib.get_user_cache_dir()) / "claude-browser"

# The heavy hitters, not an exhaustive list. A full EasyList clone would cost
# more to compile and match than it saves on a CPU this slow; these are the
# domains that actually show up in the critical path of ordinary pages.
BLOCKED = [
    # advertising
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "amazon-adsystem.com", "adnxs.com", "rubiconproject.com",
    "pubmatic.com", "openx.net", "criteo.com", "criteo.net", "taboola.com",
    "outbrain.com", "sharethrough.com", "adform.net", "casalemedia.com",
    "smartadserver.com", "teads.tv", "indexww.com", "33across.com", "3lift.com",
    "media.net", "adroll.com", "bidswitch.net", "yieldmo.com", "sonobi.com",
    # analytics and tag managers
    "google-analytics.com", "googletagmanager.com", "googletagservices.com",
    "scorecardresearch.com", "quantserve.com", "chartbeat.com", "chartbeat.net",
    "newrelic.com", "nr-data.net", "segment.com", "segment.io", "amplitude.com",
    "mixpanel.com", "heap.io", "heapanalytics.com", "fullstory.com",
    "mouseflow.com", "hotjar.com", "hotjar.io", "clarity.ms", "luckyorange.com",
    "crazyegg.com", "inspectlet.com", "matomo.cloud", "parsely.com",
    # social widgets and pixels
    "connect.facebook.net", "facebook.net", "ads-twitter.com", "analytics.twitter.com",
    "static.ads-twitter.com", "licdn.com", "snap.licdn.com", "bat.bing.com",
    "pinimg.com/ct", "reddit.com/pixel", "tiktok.com/i18n/pixel", "analytics.tiktok.com",
    # tracking, consent walls and session replay
    "onetrust.com", "cookielaw.org", "trustarc.com", "quantcast.com", "usercentrics.eu",
    "branch.io", "appsflyer.com", "adjust.com", "kochava.com", "braze.com",
    "optimizely.com", "dynamicyield.com", "sail-horizon.com", "krxd.net",
    "demdex.net", "omtrdc.net", "everesttech.net", "adobedtm.com", "2o7.net",
]


def _rules():
    """Safari-style content-blocker JSON, which is what WebKit compiles.

    Third-party only: the same domain loaded first-party is usually the site
    itself (a news site serving its own analytics path), and blocking that
    breaks pages for no gain.
    """
    import json

    rules = []
    for domain in BLOCKED:
        escaped = domain.replace(".", r"\.").replace("/", r"/")
        rules.append({
            "trigger": {
                "url-filter": r"^https?://([^/]*\.)?%s" % escaped,
                "load-type": ["third-party"],
            },
            "action": {"type": "block"},
        })
    return json.dumps(rules)


def blocking_enabled():
    """On by default -- it is the single biggest win on slow hardware. Set
    CB_BLOCK=0 to turn it off for a session when a site misbehaves."""
    return os.environ.get("CB_BLOCK", "1").lower() not in ("0", "off", "false", "no")


# -- asking the server for a cheaper page -----------------------------------
#
# Blocking third-party scripts is us refusing bytes after the server has already
# decided to send them. The other half is telling the server up front that we
# would rather have less, which is what the `Save-Data` client hint is for:
# Cloudflare's Polish, Google's transcoders, Akamai and a number of CMSes read
# it and answer with smaller images, fewer web fonts and sometimes a lighter
# template. It is the only lever in this file that reduces work on the *network*
# rather than after it.
#
# Two hints were considered and deliberately left out:
#
#   * `Device-Memory` is a high-entropy client hint. Servers are supposed to ask
#     for it with `Accept-CH` before it is sent, almost nothing acts on it for
#     the HTML document, and sending it unsolicited on every navigation adds a
#     stable fingerprinting bit about this machine to every site. That is a bad
#     trade in a browser that turns on ITP and rejects third-party cookies by
#     default.
#   * `Downlink` would have to be a number we do not have. There is no bandwidth
#     estimate anywhere in this process, and inventing one is asking the server
#     to adapt to a measurement we made up.
#
# `Save-Data` is different on both counts: it is a low-entropy hint defined to
# be sent unsolicited, and it carries a preference rather than a measurement.
LIGHT_ENV = "CB_LIGHT"

#: What goes out on a navigation when light mode is on. A dict rather than a
#: literal so the assembly can be tested without a display -- the signal wiring
#: around it cannot be.
HINTS = {"Save-Data": "on"}


def light_enabled(raw=None):
    """Is light mode on? Default yes.

    On by default for the same reason the ad blocker is: this browser exists for
    a two-core laptop, and a hint that asks for fewer bytes costs a page nothing
    it can notice. `CB_LIGHT=0` turns it off for a session when a site serves a
    degraded page you did not want.

    Anything unrecognised means on, matching CB_BLOCK: a typo should not
    silently remove a default the user never asked to lose.
    """
    if raw is None:
        raw = os.environ.get(LIGHT_ENV, "")
    return (raw or "").strip().lower() not in ("0", "off", "false", "no")


def hint_headers():
    """The client hints to attach to a navigation, or `{}` when light is off."""
    return dict(HINTS) if light_enabled() else {}


def make_request(url):
    """A `WebKitURIRequest` for `url` carrying the client hints, or None.

    None means "nothing to add, use load_uri" -- either light mode is off or
    this WebKitGTK cannot give us the header list.

    This is a per-navigation request rather than a per-resource one, and that is
    a limit of the installed API, not a choice. Modifying an outgoing request in
    WebKitGTK 4.1 needs `WebKitWebPage::send-request`, which lives in the *web
    process extension* API -- a C shared library, which this project cannot have.
    The UI process only gets `WebKitWebResource::sent-request`, past tense: it
    reports a request that has already gone out and has no return value. So the
    hint rides on loads the browser itself starts (the omnibox, the API, the
    home page, a restored tab), which is where a lighter *document* is decided,
    and subresources fetched by the page do not carry it.
    """
    if not light_enabled():
        return None
    try:
        request = WebKit2.URIRequest.new(url)
        headers = request.get_http_headers()
    except Exception:
        return None
    if headers is None:
        return None
    for name, value in HINTS.items():
        headers.replace(name, value)
    return request


def load_url(view, url):
    """`view.load_uri(url)` plus the client hints, when there are any.

    Every navigation this browser initiates goes through here so the hint has
    one place to be attached and one place to be turned off. A link the user
    clicks inside a page is WebKit's own load and cannot be reached from here;
    re-issuing it as a `load_request` would drop the `Referer` WebKit sets and
    turn a form POST into a GET, which is a correctness bug traded for a hint.
    """
    request = make_request(url)
    if request is None:
        return view.load_uri(url)
    return view.load_request(request)


def tune_gtk(settings):
    """Ask for the reduced-cost rendering path via the GTK settings WebKit reads.

    `gtk-enable-animations` is the only input WebKitGTK 2.52 has for the CSS
    `prefers-reduced-motion` media feature: there is no WebKitSettings property
    and no runtime feature flag for it (the whole 489-entry feature list has
    nothing matching "reduced" or "motion"), and `prefers-reduced-data` is not
    implemented in this engine at all -- the string does not appear in
    libwebkit2gtk-4.1 next to the other `prefers-*` features it does support.
    The library *does* watch `notify::gtk-enable-animations`, in the same list
    as `gtk-theme-name` and `gtk-application-prefer-dark-theme`, which is the
    UI-process settings block it forwards to the web process.

    Honest caveat, because it cannot be checked here: this is verified by what
    the installed library contains, not by observing a page. The page-side
    effect needs a display to confirm. What is certain either way is the local
    half -- GTK stops animating this browser's own chrome, which is per-frame
    work on the two cores the page is being laid out on.

    Deliberately not done with injected CSS. An `animation: none !important`
    sheet fights the page's own styles, breaks anything waiting on an
    `animationend`, and produces bug reports that read as rendering faults.
    """
    if settings is None or not light_enabled():
        return []
    try:
        settings.set_property("gtk-enable-animations", False)
    except Exception as e:
        return ["reduced motion unavailable (%s)" % e]
    return ["reduced motion requested (animations off)"]


def load_content_filter(manager, on_ready=None):
    """Compile (or reuse) the blocklist and attach it to `manager`.

    Compilation is genuinely slow on this hardware, so the store caches the
    compiled bytecode on disk and later runs just load it. Both paths are async
    -- attaching the filter a moment after the first page starts loading is
    fine, and far better than blocking startup on a compile.
    """
    if not blocking_enabled():
        return

    store_dir = CACHE / "filters"
    store_dir.mkdir(parents=True, exist_ok=True)
    store = WebKit2.UserContentFilterStore.new(str(store_dir))
    identifier = "cb-blocklist-v1"

    def attached(count):
        if on_ready:
            on_ready(count)

    def on_saved(src, result):
        try:
            content_filter = src.save_finish(result)
        except GLib.Error as e:
            return attached("compile failed: %s" % e.message)
        manager.add_filter(content_filter)
        attached(len(BLOCKED))

    def on_loaded(src, result):
        try:
            content_filter = src.load_finish(result)
        except GLib.Error:
            # Not compiled yet (or the cache was cleared) -- build it now.
            store.save(identifier, GLib.Bytes.new(_rules().encode()), None, on_saved)
            return
        manager.add_filter(content_filter)
        attached(len(BLOCKED))

    store.load(identifier, None, on_loaded)


def tune_context(context):
    """Process model and caching. Applied once, to the shared web context."""
    notes = []

    # NOTE: set_process_model(SHARED_SECONDARY_PROCESS) is the obvious call here
    # and it is a trap -- WebKitGTK 2.52 prints "deprecated and has no effect"
    # and carries on with one process per view. The supported way to share a web
    # process is to create each new view *related* to an existing one, which
    # Browser.new_tab does. Nothing to do at the context level.

    if hasattr(context, "set_cache_model"):
        context.set_cache_model(WebKit2.CacheModel.WEB_BROWSER)
        notes.append("browser cache model")

    # WebKit's own memory-pressure handling: the web processes watch their own
    # footprint and shed caches, then whole documents, as they approach a limit.
    # It is the layer below resources.py -- that one decides which *tabs* to
    # give up, this one keeps a single tab from ballooning in the first place.
    #
    # This was previously written as `WebKit2.MemoryPressureSettings()` handed to
    # whichever of two objects had a `set_memory_pressure_settings` attribute,
    # inside a bare `except Exception: pass`. Both halves were wrong, and the
    # except is why nobody noticed: the boxed type needs `.new()`, and the
    # setter is a *static* function on WebsiteDataManager (it configures every
    # web process, not one manager), so fetching it off the class and calling it
    # with the settings object passed the settings as `self`. The handler has
    # never actually been installed. The narrowed except is deliberate -- a
    # future rename should be visible in the startup notes, not swallowed.
    if hasattr(WebKit2, "MemoryPressureSettings"):
        try:
            settings = WebKit2.MemoryPressureSettings.new()
            # MB per web process before it starts shedding. Tabs share one
            # process here, so this is the ceiling for the whole page set, not
            # per tab -- 512 of a 3.8GB machine's RAM, which is roughly where
            # the desktop starts to suffer.
            settings.set_memory_limit(int(os.environ.get("CB_MEM_LIMIT", "512")))
            # Start shedding well before the limit rather than at it. The
            # default thresholds assume there is headroom to recover in; on a
            # machine that is already in swap there is not, and arriving at the
            # ceiling is arriving too late.
            settings.set_conservative_threshold(0.33)
            settings.set_strict_threshold(0.55)
            # Never let WebKit kill its own web process on pressure: with tabs
            # sharing one process, a kill takes every tab down at once. Shedding
            # is the behaviour we want; dying is not.
            settings.set_kill_threshold(0.0)
            settings.set_poll_interval(4.0)
            WebKit2.WebsiteDataManager.set_memory_pressure_settings(settings)
            notes.append("memory pressure handler (%dMB)"
                         % settings.get_memory_limit())
        except (AttributeError, TypeError) as e:
            notes.append("memory pressure handler unavailable (%s)" % e)

    return notes


def tune_view(view):
    """Per-view settings. Everything here is cost we do not need."""
    s = view.get_settings()

    s.set_enable_page_cache(True)          # back/forward without a reload
    s.set_enable_developer_extras(True)    # this is a browser for developers
    s.set_javascript_can_open_windows_automatically(False)

    # Pure per-frame cost on a 2-core machine: the animation runs on the same
    # cores that are trying to lay the page out.
    s.set_enable_smooth_scrolling(False)

    # Braswell integrated graphics will technically run WebGL, slowly, while
    # competing with page layout for the same thermal budget. Opt back in with
    # CB_WEBGL=1 if a site actually needs it.
    if hasattr(s, "set_enable_webgl"):
        s.set_enable_webgl(os.environ.get("CB_WEBGL", "0") == "1")

    if hasattr(s, "set_enable_media_stream"):
        s.set_enable_media_stream(False)   # no camera/mic; saves a process

    # NOTE: set_enable_hyperlink_auditing() is the third API in this codebase
    # that exists, is documented, and does nothing -- 2.52 logs "deprecated and
    # does nothing" once per view. hasattr() is not enough to tell; only running
    # it is. Left out rather than left in looking useful. (The other two:
    # set_process_model, and innerText on a detached clone.)

    # Probing codec support builds a capability table this browser never uses.
    if hasattr(s, "set_enable_media_capabilities"):
        s.set_enable_media_capabilities(False)

    # GPU compositing is a real win for scrolling even on weak Intel parts, but
    # it is also the first thing to blame when rendering misbehaves, so it stays
    # switchable: CB_GPU=off forces software, CB_GPU=on forces compositing.
    gpu = os.environ.get("CB_GPU", "").lower()
    if hasattr(s, "set_hardware_acceleration_policy"):
        if gpu in ("off", "0", "none"):
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.NEVER)
        elif gpu in ("on", "1", "always"):
            s.set_hardware_acceleration_policy(WebKit2.HardwareAccelerationPolicy.ALWAYS)
    return s
