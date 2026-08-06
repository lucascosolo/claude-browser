"""The Watch Later queue, read without loading YouTube's application.

Kept free of any GTK import, like `reader.py`, `urls.py` and `siterules.py`, so
the part worth testing -- which bytes turn into which queue -- runs in the suite
with no display and no network.

This module exists because of a measurement rather than a preference. Opening
`youtube.com/playlist?list=WL` in a real tab on the two-core laptop this browser
is written for took over 150 seconds, and once it had finished *every* injected
script against that tab timed out at 45 seconds -- including one that did
nothing but return `location.href`. The same call against `cb:home` in the same
window answered instantly, so nothing was wrong with the browser: YouTube's own
web process could not service a callback. A declutter stylesheet does not help
with that, because it hides elements only after the application has downloaded,
parsed and built them. The only way to make the queue usable here is to not run
the application at all.

So the queue is read the cheap way: one HTTP GET of the playlist page, and the
JSON that YouTube has already embedded in it for its own bootstrap. That is
about a megabyte of HTML in and a list of video ids out, with no DOM, no layout
and no script execution anywhere in the process.

Four things about this are worth stating plainly.

*The official API cannot do this.* `playlistItems.list` has not been able to
read `WL` since Google withdrew programmatic Watch Later access in 2016, so
OAuth, a Cloud project and an API key would buy nothing here. What this module
does instead is read a page the user is signed in to, exactly as their browser
would -- which is also why it is unofficial and why the parse is written to fail
loudly rather than quietly.

*There are two shapes, not one, and which you get depends on the page.* A
public playlist page serves `lockupViewModel`. The signed-in Watch Later page
serves `playlistVideoRenderer` -- the older element, still very much alive, a
hundred of them per page. This was learned the hard way twice over: first by
writing the parser from memory against `playlistVideoRenderer` and getting an
empty queue from a public playlist, then by concluding from that single sample
that the element was extinct and getting an empty queue from the real Watch
Later page. Both are parsed now, and a queue mixing them is read in page order.
The lesson is in the shape of the code: never generalise YouTube's data model
from one page.

*It is written to return nothing rather than something wrong.* A failed parse
carries `shape` -- the renderer names actually present -- because "no items" on
its own does not distinguish an empty queue from a reshape from a page whose
items arrive by continuation, and those are three different bugs.

*Cookies are never read from disk here.* This module takes a `Cookie` header
that someone else obtained; it never opens `cookies.sqlite`. The browser asks
WebKit's own `CookieManager`, on the main loop, for the cookies it already holds
for youtube.com -- the credential store stays owned by the thing that owns it,
and this module stays testable with a string.

*Only the first page is parsed.* YouTube serves roughly a hundred items and a
continuation token for the rest. A hundred queued videos is several days of
background listening, and following continuations means replaying InnerTube's
POST protocol with its own key and context. `truncated` says whether more
existed, so the caller can say so honestly instead of pretending the queue ended.
"""

import http.client
import json
import re
import urllib.parse

from . import vpn

#: YouTube's id for the signed-in user's Watch Later queue. Not a playlist
#: anyone else can see, and not one the Data API will hand over -- see above.
WATCH_LATER = "WL"

#: The bootstrap blob. YouTube writes it as a `var` assignment terminated by
#: `;</script>`; the non-greedy body plus that anchor is what keeps this from
#: swallowing the rest of the document. Matched against the raw HTML, because
#: parsing a megabyte of markup to reach a script we are going to `json.loads`
#: anyway is the expensive way to do the same thing.
INITIAL_DATA = re.compile(r"var\s+ytInitialData\s*=\s*(\{.*?\})\s*;\s*</script>",
                          re.DOTALL)

#: What a video lockup calls itself. Anything else in the item list -- a shelf,
#: a playlist, a "no longer available" placeholder -- is skipped rather than
#: guessed at, so an unplayable id never reaches the player.
VIDEO_LOCKUP = "LOCKUP_CONTENT_TYPE_VIDEO"

#: A browser-shaped request. Sent because YouTube serves a different, lighter
#: and differently-shaped page to clients it does not recognise, and this parser
#: is written against the one a browser gets.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    "Accept-Language": "en-US,en;q=0.9",
}


def playlist_url(list_id=WATCH_LATER):
    """The page whose bootstrap JSON carries the queue."""
    return "https://www.youtube.com/playlist?list=%s" % list_id


def headers(cookie=""):
    """Request headers, with the caller's `Cookie` header if there is one.

    An empty cookie is allowed on purpose: a public playlist parses fine
    without one, which is what makes this module testable against a real page
    that is not the user's.
    """
    out = dict(HEADERS)
    if cookie:
        out["Cookie"] = cookie
    return out


def cookie_header(pairs):
    """Build a `Cookie` header from (name, value) pairs.

    Takes pairs rather than a jar so the caller can hand over whatever
    WebKit's CookieManager gave it without this module learning a GLib type.
    """
    return "; ".join("%s=%s" % (name, value) for name, value in pairs if name)


#: A page this size is already generous -- the live Watch Later page measured
#: about a megabyte. The cap exists so a redirect to something enormous cannot
#: pull an unbounded body into memory on a laptop with 700MB free.
MAX_BYTES = 4 * 1024 * 1024

#: Long enough for a slow page on a loaded machine, short enough that a wedged
#: fetch does not sit on the worker thread all afternoon.
TIMEOUT = 30


def fetch(url, cookie="", route=None, timeout=TIMEOUT, opener=None):
    """GET `url` and return `(html, error)` -- exactly one of them truthy.

    **Routed the same way `ai.py` routes its own requests.** When VPN Mode is on
    this goes through the proxy's CONNECT tunnel, and when the mode is engaged
    but broken it refuses. A direct socket here would be precisely the leak the
    mode exists to prevent, and it would be an invisible one: the queue would
    keep working, from the user's home address, while the browser said the
    tunnel was up.

    `route` defaults to asking `vpn` itself; it is a parameter so the tests can
    exercise all three cases without touching global state. `opener` is the same
    injection point `vpn._echo_once` uses.

    Returns an error rather than raising for the same reason `parse` does: this
    is the network, and every failure here is one the page has to render.
    """
    proxy, refusal = vpn.api_route() if route is None else route
    if refusal:
        return "", refusal
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    path = parts.path + (("?" + parts.query) if parts.query else "")
    try:
        if proxy is not None:
            conn = (opener or vpn.open_tunnel)(proxy, host, port, timeout)
        else:
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        try:
            request = dict(headers(cookie))
            request["Host"] = host
            # No keep-alive and no compression: this socket is used once, and
            # asking for gzip would mean carrying a decompressor for one caller.
            request["Connection"] = "close"
            request["Accept-Encoding"] = "identity"
            conn.request("GET", path or "/", headers=request)
            response = conn.getresponse()
            if response.status != 200:
                # A 302 here is the consent or sign-in redirect, which is a
                # different problem from a server error and says so.
                return "", ("YouTube answered HTTP %s%s" % (
                    response.status,
                    " (signed out, or a consent redirect)"
                    if response.status in (301, 302, 303, 307, 308) else ""))
            return response.read(MAX_BYTES).decode("utf-8", "replace"), ""
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except (OSError, http.client.HTTPException, vpn.ProbeError) as e:
        return "", "could not reach YouTube: %s" % e


#: The cookies that mean "there is a Google session here". Checked by name
#: rather than by asking the page, because a signed-out Watch Later page is
#: **HTTP 200 with an empty playlist and no marker on it at all** -- probed, and
#: `responseContext` carries nothing but `webResponseContextExtensionData`. So
#: the reliable signal is local: whether this browser had a session to send.
#: `__Secure-3PSID` is the one that travels on a cross-site request; plain `SID`
#: covers an older jar.
SESSION_COOKIES = ("__Secure-3PSID", "SID", "__Secure-1PSID")


def signed_in(cookie):
    """Does this `Cookie` header carry a Google session?"""
    names = {part.split("=", 1)[0].strip()
             for part in (cookie or "").split(";") if "=" in part}
    return any(name in names for name in SESSION_COOKIES)


def load(list_id=WATCH_LATER, cookie="", route=None, fetcher=None):
    """Fetch and parse in one step: `{"ok", "items", "truncated", "error"}`.

    The one function the browser side calls, so that the ordering -- route,
    fetch, parse -- lives here in a testable module rather than in a callback
    chain in `browser.py`.

    An empty result is attributed to a missing session *only when there really
    was no session cookie*. Guessing the other way round -- assuming signed out
    whenever the queue is empty -- would tell someone with a genuinely empty
    Watch Later to go and sign in to an account they are already signed in to.
    """
    html, error = (fetcher or fetch)(playlist_url(list_id), cookie, route)
    if error:
        return {"ok": False, "error": error, "items": [], "truncated": False}
    result = parse(html)
    if not result["ok"] and not result["items"] and not signed_in(cookie):
        return {"ok": False, "signed_out": True, "items": [],
                "truncated": False,
                "error": "not signed in to YouTube in this browser"}
    return result


def duration_seconds(text):
    """`"16:09"` -> 969. None for anything that is not a clock.

    Live streams carry no badge and upcoming ones carry a date, so this returns
    None rather than 0 -- a zero-length video and a video of unknown length are
    different things to a player deciding what to do when one ends.
    """
    if not text:
        return None
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts) or not 2 <= len(parts) <= 3:
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


class Item:
    """One playable entry in the queue."""

    __slots__ = ("video_id", "title", "channel", "duration", "seconds")

    def __init__(self, video_id, title="", channel="", duration="",
                 seconds=None):
        self.video_id = video_id
        self.title = title
        self.channel = channel
        self.duration = duration
        # `playlistVideoRenderer` carries `lengthSeconds` outright, which beats
        # re-deriving it from a string that was formatted for a human.
        self.seconds = seconds if seconds is not None else duration_seconds(duration)

    def as_dict(self):
        return {"video_id": self.video_id, "title": self.title,
                "channel": self.channel, "duration": self.duration,
                "seconds": self.seconds}

    def __repr__(self):
        return "<Item %s %r>" % (self.video_id, self.title[:40])


def _walk(node, key):
    """Yield every value stored under `key`, at any depth.

    Walking rather than indexing a fixed path is deliberate. The path to the
    item list on a live page today is eleven levels of
    `twoColumnBrowseResultsRenderer / tabs / tabRenderer / content /
    sectionListRenderer / contents / itemSectionRenderer / contents`, and every
    one of those names has been renamed at least once in this product's life.
    The *leaf* is the part that carries meaning, so the leaf is what we look
    for; a reshuffle above it costs nothing.
    """
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key:
                yield value
            yield from _walk(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, key)


def _first(node, key):
    for value in _walk(node, key):
        return value
    return None


def _title_of(lockup):
    meta = _first(lockup.get("metadata"), "lockupMetadataViewModel") or {}
    title = meta.get("title") or {}
    return title.get("content") or ""


def _channel_of(lockup):
    """The first metadata row's first text part.

    That row is the channel name on every video lockup seen; the second is view
    count and age. Read positionally because none of it is labelled -- which is
    also why a miss returns "" instead of picking whichever string is nearest.
    """
    rows = _first(lockup.get("metadata"), "metadataRows") or []
    for row in rows:
        for part in row.get("metadataParts") or []:
            content = (part.get("text") or {}).get("content")
            if content:
                return content
    return ""


def _duration_of(lockup):
    """The badge painted over the thumbnail, e.g. `16:09`.

    `thumbnailBadgeViewModel` also carries the "Now playing" animation badge,
    which has no `text`, so this takes the first badge that actually has one.
    """
    for badge in _walk(lockup.get("contentImage"), "thumbnailBadgeViewModel"):
        text = badge.get("text")
        if text:
            return text
    return ""


#: The two element names an item can arrive under. See the module note: a
#: public playlist page uses the first, the signed-in Watch Later page the
#: second, and assuming either one is "the" shape has now been wrong twice.
ITEM_KEYS = ("lockupViewModel", "playlistVideoRenderer")


def _runs_text(field):
    """`{"runs": [{"text": ...}]}` or `{"simpleText": ...}` -> a string.

    YouTube uses both spellings for the same idea, sometimes within one item,
    so every text field goes through here rather than picking one at each site.
    """
    if not isinstance(field, dict):
        return ""
    if "simpleText" in field:
        return field.get("simpleText") or ""
    return "".join(run.get("text") or "" for run in field.get("runs") or []
                   if isinstance(run, dict))


def _item_from_lockup(node):
    if node.get("contentType") != VIDEO_LOCKUP:
        return None
    return Item(node.get("contentId"), _title_of(node), _channel_of(node),
                _duration_of(node))


def _item_from_playlist_video(node):
    # `isPlayable` is False for a video that has been deleted or made private
    # since it was queued. A Watch Later list of any age has these in it, and
    # an unplayable id in a queue that advances on `ended` is a queue that
    # stops dead partway through with no explanation.
    if node.get("isPlayable") is False:
        return None
    seconds = node.get("lengthSeconds")
    try:
        seconds = int(seconds) if seconds is not None else None
    except (TypeError, ValueError):
        seconds = None
    return Item(node.get("videoId"),
                _runs_text(node.get("title")),
                _runs_text(node.get("shortBylineText")),
                _runs_text(node.get("lengthText")),
                seconds)


#: Which builder reads which element.
_BUILDERS = {"lockupViewModel": _item_from_lockup,
             "playlistVideoRenderer": _item_from_playlist_video}


def _iter_items(node):
    """Every item-shaped node, either spelling, in document order."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name in ITEM_KEYS and isinstance(value, dict):
                yield name, value
            yield from _iter_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_items(value)


def items_from_data(data):
    """Every playable video in a parsed `ytInitialData`, in page order."""
    out = []
    seen = set()
    for name, node in _iter_items(data):
        item = _BUILDERS[name](node)
        # No id means not playable; a repeat is the same video appearing in a
        # shelf as well as the list. Neither belongs in a queue played in order.
        if item is None or not item.video_id or item.video_id in seen:
            continue
        seen.add(item.video_id)
        out.append(item)
    return out


def _sample_keys(data, limit=3):
    """Key names of the first item-shaped node, values never included.

    Ground truth for a shape this code has not met before. Keys only: the
    values are the user's own queue, and a diagnostic that prints what someone
    is watching into a log file is a diagnostic that should not exist.
    """
    for name in ("playlistVideoRenderer", "lockupViewModel",
                 "richItemRenderer"):
        node = _first(data, name)
        if isinstance(node, dict):
            shown = {}
            for key, value in list(node.items())[:14]:
                if isinstance(value, dict):
                    shown[key] = sorted(value)[:limit]
                elif isinstance(value, list):
                    shown[key] = ["[]"]
                else:
                    shown[key] = type(value).__name__
            return "%s -> %s" % (name, shown)
    return ""


def shape_summary(data, limit=8):
    """The renderer names present, commonest first: `"a x12, b x3"`.

    Carried on a failed parse rather than kept for a debugger. This module
    reads a page that is reshaped without notice, and "no video lockups" on its
    own does not distinguish a reshape from an empty queue from a page whose
    items arrive by continuation -- which are three different bugs with three
    different fixes.
    """
    counts = {}

    def visit(node):
        if isinstance(node, dict):
            for name, value in node.items():
                if name.endswith(("Renderer", "ViewModel")):
                    counts[name] = counts.get(name, 0) + 1
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(data)
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return ", ".join("%s x%d" % (name, n) for name, n in top) or "nothing"


def logged_out(data):
    """Did YouTube answer as though nobody is signed in?

    `mainAppWebResponseContext.loggedOut` is the page telling us directly, which
    is worth reading because the signed-out Watch Later page is not an error:
    it is HTTP 200 with an empty playlist. Without this the one failure the user
    is most likely to hit -- not signed in to *this* browser -- would be
    reported as "the playlist is empty, or YouTube reshaped its data", which
    sends them to read a parser instead of to a sign-in button.
    """
    for context in _walk(data, "mainAppWebResponseContext"):
        if isinstance(context, dict) and "loggedOut" in context:
            return bool(context["loggedOut"])
    return None


def has_continuation(data):
    """Did YouTube say there were more items than it sent?"""
    return _first(data, "continuationItemRenderer") is not None


def parse(html):
    """Turn a playlist page into a queue.

    Returns `{"ok": True, "items": [...], "truncated": bool}` or
    `{"ok": False, "error": ...}`. Never raises on bad input: this is fed a
    page fetched from the network, and a browser that stops working because a
    site changed shape is worse than one that says it could not read the queue.
    """
    match = INITIAL_DATA.search(html or "")
    if not match:
        # Also what a consent interstitial or a signed-out redirect looks like,
        # so the message says what to check rather than blaming the parser.
        return {"ok": False, "error": "no ytInitialData in the page -- signed "
                                      "out, or YouTube served an interstitial",
                "items": [], "truncated": False}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        return {"ok": False, "error": "ytInitialData did not parse: %s" % e,
                "items": [], "truncated": False}
    items = items_from_data(data)
    if not items:
        if logged_out(data):
            # By far the likeliest failure, and the only one with an action
            # attached, so it is answered before the parser blames itself.
            return {"ok": False, "signed_out": True, "items": [],
                    "truncated": False,
                    "error": "not signed in to YouTube in this browser"}
        # The `playlistVideoRenderer` failure, stated rather than silent.
        return {"ok": False, "error": "no video lockups in the page -- the "
                                      "playlist is empty, or YouTube reshaped "
                                      "its data again",
                "shape": shape_summary(data),
                "sample_keys": _sample_keys(data),
                "items": [], "truncated": has_continuation(data)}
    return {"ok": True, "items": items, "truncated": has_continuation(data)}
