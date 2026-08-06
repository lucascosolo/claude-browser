"""What a YouTube URL means, and how to turn a watch link into a player.

GTK-free, like `watchlater.py`, and split from it on purpose: that module knows
what the *queue page contains*, this one knows what a *URL is*. The two facts
change for different reasons -- YouTube reshapes its bootstrap JSON far more
often than it moves `/watch?v=`.

The point of the module is one measured problem. Opening
`youtube.com/watch?v=...` on this laptop downloads and hydrates the whole
Polymer app: the player, plus a sidebar of suggestions with a thumbnail each,
plus comments, plus the shelves under them. That took over two minutes here,
and afterwards every script evaluated against the tab timed out. The player
itself is a fraction of that, and YouTube publishes it separately at
`/embed/<id>` -- no suggestions, no comments, no thumbnails but the one.

**The embed refuses to play without a third-party http(s) referrer, and that is
the whole reason this module attaches headers.** A top-level navigation typed
into the omnibox carries no `Referer` at all, and an `<iframe>` on a `cb:` page
carries nothing WebKit will spell as an origin; both come back as the player's
*error 153*, "Video player configuration error". The surprise is what happens
when you supply the obvious value: a `Referer` of `https://www.youtube.com/`
gives *152-4*, "this video is unavailable", because the player is checking that
somebody else embedded it. The table above `REFERRER` records all four
measurements. `youtube-nocookie.com` is the host because it is the same player
without the cookie the ordinary one sets on arrival.

That also answers a question worth writing down before someone re-derives it:
this cannot be a `cb:` page with the player inside it. A `cb:` document is not
an http origin, so anything embedded in it hits the same 153. Our own chrome
around the player would need the page served over loopback HTTP, which is a
second server and a second origin to defend, and the browser already has a
list-of-titles surface -- `cb:queue`.
"""

import re
import urllib.parse

from . import envfile

#: The switch. On by default: the whole feature is a page that loads in a
#: second instead of two minutes, and the escape hatch is one setting away.
EMBED_ENV = "CB_YT_EMBED"

#: The player, on the host that does not set a cookie until something is
#: played. Same player, same parameters.
EMBED_HOST = "www.youtube-nocookie.com"

#: What to send as `Referer`, and the value is not arbitrary -- all four cases
#: below were run against the live player, in this engine, with everything else
#: held equal:
#:
#:   no Referer at all        -> error 153, "player configuration error"
#:   https://www.youtube.com/ -> error 152-4, "this video is unavailable"
#:   http://127.0.0.1:8791/   -> plays
#:   https://example.com/     -> plays
#:
#: So the player wants *a third party* to have embedded it, and rejects a
#: request claiming to come from YouTube itself -- which is the opposite of the
#: obvious guess and the reason this constant has a table above it.
#:
#: `.localhost` is reserved by RFC 6761 and can never belong to anyone, so this
#: names the embedder honestly without borrowing a real site's identity: a
#: referrer of `example.com` works just as well and would be a lie told to a
#: server about who is asking.
REFERRER = "https://claude-browser.localhost/"

#: An id is eleven characters of an unreserved alphabet. Pinned exactly,
#: because everything downstream pastes it into a URL: a loose pattern here is
#: a way to smuggle another query parameter onto the player.
ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Hosts whose `/watch` this rewrites. `music.youtube.com` is deliberately
#: absent -- its player is a different app and its URLs mean something else.
HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com",
         "youtu.be", "www.youtu.be", "youtube-nocookie.com",
         "www.youtube-nocookie.com")

#: How many ids may ride in a `playlist=` parameter. The player takes a
#: comma-separated list and this is the cheapest possible auto-advance -- no
#: postMessage, no JS API, no page of ours in the loop. Capped because the list
#: goes in a URL: 50 ids is about 600 characters, which every layer here is
#: comfortable with, and a Watch Later queue of 100 would not be.
MAX_PLAYLIST = 50

#: Player parameters, and why each one is here.
#:   autoplay   -- the point: the click already said "play this".
#:   rel=0      -- related videos at the end come from this channel only. It
#:                 cannot be turned off entirely any more; this is the least it
#:                 will do.
#:   modestbranding, playsinline, iv_load_policy=3 -- no title bar overlay, no
#:                 fullscreen hijack, no annotation layer.
PARAMS = (("autoplay", "1"), ("rel", "0"), ("modestbranding", "1"),
          ("playsinline", "1"), ("iv_load_policy", "3"))


def enabled(raw=None, path=None):
    """Is watch-link rewriting on? Off only for a word that means off.

    Same rule as `siterules.enabled` and `perf.light_enabled`, so a typo leaves
    the feature on rather than silently restoring the two-minute page.
    """
    if raw is None:
        raw = envfile.setting(EMBED_ENV, "1", path=path)
    return (raw or "").strip().lower() not in ("0", "off", "false", "no")


def _host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def video_id(url):
    """The video id in this URL, or "".

    Handles the four spellings that reach a browser: `/watch?v=`, `youtu.be/`,
    `/shorts/` and an `/embed/` URL that is already a player. Never guesses --
    an id that does not match `ID` exactly is not an id, because the caller is
    about to paste it into a URL.
    """
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host not in HOSTS:
        return ""
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts.path.lstrip("/").split("/", 1)[0]
        return candidate if ID.match(candidate) else ""
    path = parts.path.rstrip("/")
    if path == "/watch":
        for key, value in urllib.parse.parse_qsl(parts.query):
            if key == "v":
                return value if ID.match(value) else ""
        return ""
    for prefix in ("/shorts/", "/embed/", "/live/", "/v/"):
        if path.startswith(prefix):
            candidate = path[len(prefix):].split("/", 1)[0]
            return candidate if ID.match(candidate) else ""
    return ""


def start_at(url):
    """Seconds into the video this URL points at, or 0.

    A link into the middle of a video is a link to *that moment*, and dropping
    it is the kind of quiet loss that makes a rewrite feel broken rather than
    fast. `t=90`, `t=90s` and `start=90` all appear in the wild.
    """
    try:
        query = urllib.parse.urlsplit(url or "").query
    except ValueError:
        return 0
    for key, value in urllib.parse.parse_qsl(query):
        if key not in ("t", "start"):
            continue
        digits = value[:-1] if value.endswith("s") else value
        if digits.isdigit():
            return int(digits)
    return 0


def embed_url(vid, queue=(), start=0):
    """The player URL for `vid`, optionally followed by `queue`.

    `queue` is the ids to play *after* this one. It is passed as the player's
    own `playlist` parameter rather than driven from a page of ours, which is
    what makes background listening cost nothing: the advance happens inside
    the player, so there is no script anywhere waiting to be told a video
    ended.
    """
    if not ID.match(vid or ""):
        return ""
    params = list(PARAMS)
    rest = [v for v in queue if ID.match(v or "") and v != vid][:MAX_PLAYLIST]
    if rest:
        params.append(("playlist", ",".join(rest)))
    if start > 0:
        params.append(("start", str(int(start))))
    return "https://%s/embed/%s?%s" % (EMBED_HOST, vid,
                                       urllib.parse.urlencode(params))


def redirect(url, raw=None, path=None):
    """The player URL this watch link should become, or "".

    Returns "" for everything that is not a single video -- the home feed, a
    channel, a search, a playlist page -- because those are pages the declutter
    sheet handles and a player cannot show.
    """
    if not enabled(raw, path):
        return ""
    vid = video_id(url)
    if not vid:
        return ""
    # An /embed/ URL is already the player. Rewriting it again would strip a
    # playlist that a previous rewrite just attached, which is exactly how
    # auto-advance would stop after the first video.
    if "/embed/" in (url or ""):
        return ""
    return embed_url(vid, start=start_at(url))


def headers():
    """The request headers the player needs. See the note at the top."""
    return {"Referer": REFERRER}
