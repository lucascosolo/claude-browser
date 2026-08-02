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
  3. Per-frame work. Smooth scrolling and WebGL are pure cost here.

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

    # WebKit's own memory-pressure handling moved between versions; try both
    # homes rather than pin one.
    if hasattr(WebKit2, "MemoryPressureSettings"):
        try:
            settings = WebKit2.MemoryPressureSettings()
            settings.set_memory_limit(512)  # MB before it starts shedding caches
            target = None
            if hasattr(context, "set_memory_pressure_settings"):
                target = context.set_memory_pressure_settings
            elif hasattr(WebKit2.WebsiteDataManager, "set_memory_pressure_settings"):
                target = WebKit2.WebsiteDataManager.set_memory_pressure_settings
            if target:
                target(settings)
                notes.append("memory pressure handler")
        except Exception:
            pass

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
