"""Resource modes: how much of the web platform a page is allowed to cost.

GTK-free like `siterules.py`, `urls.py` and `reader.py`, and for the same
reason: which mode a URL resolves to, and what that mode turns off, is the part
worth testing, and none of it needs a display. The half that cannot be tested --
pushing the values into a live `WebKitSettings` -- lives in `perf.py` and is
three lines of loop.

Four modes, three of them on a slider in the toolbar and one that is not.

    normal   the full suite, with the waste removed
    light    heavy media and graphics features off
    potato   HTML and basic JavaScript, and very little else
    scraper  no WebKit at all -- see the note below

The ladder exists because this browser runs on a Celeron N3060 with two cores,
and the honest position is that a modern web app and that machine disagree about
what a page may cost. Rather than pick a winner globally, the user picks per
site, and the sites that need the whole platform get it.

**`normal` is not "everything on".** It is the full *suite* with the waste
removed -- smooth scrolling, autoplaying media, speculative prefetch. Everything
a current web app is entitled to assume still works: JavaScript, service
workers, IndexedDB, WebAssembly, cookies and storage, fetch and streams. That is
deliberate and it is the mode's whole job. A browser that saves a core by
breaking Cloudflare's challenge, or by making a site decide it is a bot, has not
saved anything -- it has stopped being a browser for that site. Anything that
would break those goes in `light` or below, never here.

**`scraper` is not a fourth notch on the slider.** It is the tier where no
WebKit process is involved at all: a plain HTTP fetch and an HTML parse. It is
not offered to the user because "this page, but broken" is not a browsing mode;
it exists for the agent, which frequently wants a page's *text* and has no use
for a rendered one. It is separated from `potato` by exactly one property --
whether JavaScript runs -- and that is not an arbitrary line. A JavaScript
engine is cheap to obtain here (JavaScriptCore is on this machine through
introspection, and so is node); what is expensive is a DOM for scripts to act
on, and a DOM faithful enough that real pages do not throw on their first line
is a browser engine. So the moment a page needs scripting it needs WebKit, and
the moment it does not, it does not need WebKit for anything else either.

**A mode is a floor on compatibility, not a promise about speed.** Nothing here
measures anything. `potato` is not "fast", it is "less is loaded"; whether that
is faster depends entirely on the page. Claiming otherwise would invite exactly
the bug this module is most likely to grow -- someone adding a switch because it
sounds cheap, without checking what stops working.
"""

from urllib.parse import urlsplit

from . import envfile

#: The default mode, when nothing has been chosen for a site and no default has
#: been set. `normal`, not `light`: the cost of guessing too light is a page that
#: silently misbehaves and a user who does not connect it to a setting they never
#: touched, and that is a much worse failure than a page that could have been
#: cheaper. Light and potato are opt-in per site, which is what the slider is.
DEFAULT_MODE = "normal"

#: The setting naming the default for sites with no override of their own.
SETTING = "CB_MODE"

#: The setting holding the per-site overrides, as `host=mode` pairs separated by
#: whitespace or commas. Kept in the settings file rather than in a store of its
#: own because it is small, it is something the user may reasonably want to edit
#: by hand, and `envfile` already refuses to let a page write it.
SITES_SETTING = "CB_MODE_SITES"

NORMAL = "normal"
LIGHT = "light"
POTATO = "potato"
SCRAPER = "scraper"

#: What the slider offers, in order. `scraper` is deliberately absent -- see the
#: module note. Ordered least to most aggressive so a UI can render it as a
#: position rather than needing its own opinion about which way is "more".
SLIDER = (NORMAL, LIGHT, POTATO)

#: Every mode, including the one that is not on the slider.
MODES = (NORMAL, LIGHT, POTATO, SCRAPER)

#: How each reads in the UI. Short enough for a slider label; the sentence
#: underneath is `SUMMARIES`.
LABELS = {
    NORMAL: "Normal",
    LIGHT: "Light",
    POTATO: "Potato",
    SCRAPER: "Scraper",
}

SUMMARIES = {
    NORMAL: "The full web platform, with the waste removed. Use this when a "
            "site has to work.",
    LIGHT: "Heavy graphics and media off. Most sites are unaffected; web apps "
           "that draw their own canvas are.",
    POTATO: "HTML and basic JavaScript. Images, media and web fonts do not "
            "load. Expect things to look wrong.",
    SCRAPER: "No browser engine at all — the page is fetched and parsed as "
             "text. Used by Claude, never by a tab.",
}

#: WebKitSettings properties, per mode, as *names* rather than as calls, so this
#: module stays free of any WebKit import and the table can be read as a table.
#: `perf.apply_mode` walks it with a `hasattr` guard, so a property this build
#: does not have is skipped rather than raising.
#:
#: Only what each mode *changes* is listed. A mode does not restate the ones
#: above it: `settings_for` composes them in ladder order, so a value appearing
#: in `light` is also in effect for `potato`, and adding a switch to `light`
#: cannot be forgotten in `potato`. That composition is the reason these are
#: three small dicts and not three complete ones that would drift apart.
_SETTINGS = {
    # Waste only. Nothing here is a capability a page can feature-detect and
    # nothing here is visible to a bot check.
    NORMAL: {
        # The animation competes for the same two cores that are laying the
        # page out, and it is decoration.
        "enable-smooth-scrolling": False,
        # Defaults to False in WebKitGTK 2.52, which is exactly why it is pinned:
        # a default is not a guarantee. An autoplaying video is not a nuisance on
        # this hardware, it is the entire machine.
        "media-playback-requires-user-gesture": True,
        # Builds a codec capability table nothing in this browser reads.
        "enable-media-capabilities": False,
        # No camera or microphone, and it saves a process.
        "enable-media-stream": False,
    },
    # Heavy graphics and audio. A site that draws with WebGL or synthesises
    # audio will notice; an ordinary page will not.
    LIGHT: {
        "enable-webgl": False,
        "enable-webaudio": False,
        "enable-accelerated-2d-canvas": False,
        "enable-encrypted-media": False,
    },
    # Everything that is not markup and script.
    #
    # `enable-javascript` is emphatically NOT here. Potato is "HTML and basic
    # JavaScript" -- turning script off is `scraper`, which is a different tier
    # with a different implementation, and collapsing the two would leave the
    # slider with a position that silently breaks every site that renders
    # client-side while claiming to be a browsing mode.
    POTATO: {
        "auto-load-images": False,
        "enable-media": False,
        "enable-mediasource": False,
        # The back/forward cache trades memory for a fast return. On the machine
        # this mode is for, memory is the thing in short supply.
        "enable-page-cache": False,
    },
}

#: Whether a mode still gets the full storage and worker platform. Every mode
#: does, and the entry exists so that the next person to want a saving here has
#: to change a `True` to a `False` on purpose and see this comment.
#:
#: Service workers, IndexedDB, WebAssembly and local storage are not switched
#: off by any mode, including potato. They are what a current web app is built
#: on; a site missing them does not degrade, it fails, and it frequently fails by
#: deciding the browser is automated. Nothing about them is per-frame cost --
#: they are idle until a page uses them -- so switching them off is paying in
#: compatibility for a saving that does not exist.
KEEPS_APP_PLATFORM = {mode: True for mode in MODES}


def normalize(name):
    """A mode name, or `DEFAULT_MODE` for anything unrecognised.

    Never raises, on the same reasoning as `style.resolve`: a typo in the
    settings file must not stop the browser from starting, and a mode is a
    preference rather than an assertion. `scraper` is accepted here even though
    it is not on the slider -- this is the parser, not the policy about who may
    ask for what.
    """
    cleaned = (name or "").strip().lower()
    return cleaned if cleaned in MODES else DEFAULT_MODE


def host_of(url):
    """The host a mode is chosen for, lowercased and without `www.`.

    `www.` is stripped so that choosing a mode on `www.example.com` covers
    `example.com`. Anything without a host -- `cb:` pages, `about:blank`, a bare
    search term that never became a URL -- has no site to have a preference, and
    returns "".
    """
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_sites(raw):
    """`{host: mode}` from the settings string.

    Tolerant on purpose: this is a value a person edits in a text file, and an
    unparseable entry should cost that entry rather than the whole map. An
    unrecognised mode name resolves through `normalize`, so `example.com=ptato`
    is `example.com=normal` -- the safe direction, since the failure mode of
    guessing normal is a page that works.
    """
    out = {}
    for chunk in (raw or "").replace(",", " ").split():
        host, sep, mode = chunk.partition("=")
        if not sep:
            continue
        host = host.strip().lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if host:
            out[host] = normalize(mode)
    return out


def format_sites(sites):
    """The settings string for a `{host: mode}` map. Sorted, so a rewrite of the
    file does not reorder lines and produce a spurious change."""
    return " ".join("%s=%s" % (host, sites[host]) for host in sorted(sites))


def _matches(host, chosen):
    """The mode chosen for `host`, honouring parent domains.

    A choice on `example.com` covers `docs.example.com`, because the thing a
    person is expressing is about a site and not about one of its subdomains.
    The most specific match wins, so `docs.example.com=normal` still overrides
    `example.com=potato`.
    """
    if not host:
        return None
    parts = host.split(".")
    for cut in range(len(parts) - 1):
        candidate = ".".join(parts[cut:])
        if candidate in chosen:
            return chosen[candidate]
    return None


def for_url(url, sites=None, default=None, path=None):
    """Which mode this URL runs in.

    Order: the site's own override, then the configured default, then
    `DEFAULT_MODE`. Read from the settings file on every call rather than
    captured at import, for the same reason `siterules.enabled` and
    `perf.light_enabled` are -- the slider writes that file, and a value read
    once would leave the browser using a mode the user has just changed until it
    was restarted.

    A URL with no host gets the default and not a stored choice, which is what
    keeps `cb:` pages out of this: they are rendered by the browser itself and
    have nothing to economise on.
    """
    if sites is None:
        sites = parse_sites(envfile.setting(SITES_SETTING, "", path=path))
    if default is None:
        default = envfile.setting(SETTING, "", path=path)
    chosen = _matches(host_of(url), sites)
    return chosen or normalize(default)


def with_site(url, mode, sites=None, default=None, path=None):
    """The `{host: mode}` map with this URL's site set to `mode`.

    Returns `(sites, host)` so the caller can say which site it changed -- a
    slider moved on `docs.example.com` writes `example.com`, and telling the user
    that is the difference between a control that feels precise and one that
    feels like it did something else.

    Setting a site back to the configured default *removes* the entry rather
    than writing it, on the same reasoning as `envfile.remove`: an entry the user
    did not choose would pin that site against any future change of default.
    """
    if sites is None:
        sites = parse_sites(envfile.setting(SITES_SETTING, "", path=path))
    else:
        sites = dict(sites)
    if default is None:
        default = envfile.setting(SETTING, "", path=path)
    host = host_of(url)
    if not host:
        return sites, ""
    mode = normalize(mode)
    if mode == normalize(default):
        sites.pop(host, None)
    else:
        sites[host] = mode
    return sites, host


def settings_for(mode):
    """The WebKitSettings values this mode wants, composed down the ladder.

    Every mode gets `normal`'s, `potato` gets `light`'s as well, and so on, so a
    switch added to a lighter mode cannot be forgotten in a heavier one. Returns
    a fresh dict each call: the caller is handing it to WebKit and has no reason
    to be careful with it.

    `scraper` returns nothing at all, and that is not an oversight -- there is no
    WebView in that tier for any of these to be set on.
    """
    mode = normalize(mode)
    if mode == SCRAPER:
        return {}
    out = {}
    for rung in (NORMAL, LIGHT, POTATO):
        out.update(_SETTINGS.get(rung, {}))
        if rung == mode:
            break
    return out


def uses_webkit(mode):
    """Does this mode involve a web process at all?

    The one question that decides which half of the browser handles a request,
    so it is a function rather than a comparison written out at each call site.
    """
    return normalize(mode) != SCRAPER


def blocks_images(mode):
    """Whether images are refused. Read by the UI, which says so -- a page with
    no pictures and no explanation reads as broken rather than as chosen."""
    return settings_for(mode).get("auto-load-images") is False


def is_selectable(mode):
    """May a person put a tab in this mode? Everything but `scraper`.

    Enforced where a mode is *set* rather than where it is used, so the agent
    can still read a page in scraper mode without the slider ever offering a
    position that renders nothing.
    """
    return normalize(mode) in SLIDER
