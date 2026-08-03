"""Omnibox intent: is this a place to go, or a thing to search for?

Kept free of any GTK/WebKit import so it can be tested (and reasoned about)
without a display or the introspection bindings present.
"""

import os
import re
from urllib.parse import quote

SEARCH = os.environ.get("CB_SEARCH", "https://duckduckgo.com/?q=%s")

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_FULL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_DOTTED_HOST = re.compile(r"^[\w-]+(\.[\w-]+)+$")


def looks_like_url(text: str) -> bool:
    """Guessing wrong here is the most irritating thing a browser does, so the
    bar is deliberately high: an explicit scheme, localhost, a bare IP, or a
    dotted hostname with no spaces in the input."""
    text = text.strip()
    if not text or " " in text:
        return False
    # "cb:home" carries a scheme but no "//", so it needs naming explicitly --
    # otherwise the omnibox helpfully web-searches for the phrase "cb:home".
    if _FULL_SCHEME.match(text) or text.startswith(("about:", "file:", "data:", "cb:")):
        return True
    host = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
    if host == "localhost" or _IPV4.match(host):
        return True
    return bool(_DOTTED_HOST.match(host))


def normalize(text: str) -> str:
    """Turn omnibox input into something load_uri() accepts."""
    text = text.strip()
    if not text:
        return "about:blank"
    if looks_like_url(text):
        return text if _SCHEME.match(text) else "https://" + text
    return SEARCH % quote(text, safe="")


# -- DNS preconnect ---------------------------------------------------------

# Schemes that resolve nothing: the browser's own pages, the local filesystem,
# and inline data.
_LOCAL_SCHEMES = ("about:", "file:", "data:", "cb:", "javascript:", "blob:")


def prefetch_host(text: str):
    """The hostname worth warming for this omnibox input, or None.

    Deliberately built on `looks_like_url` rather than a second parser: the
    prefetch must agree with the navigate-vs-search decision the Enter key will
    make, or it leaks a DNS lookup for every half-typed search query -- one
    request per phrase the user was only ever going to google. Literal
    addresses and localhost are skipped too, because there is no name to
    resolve and the lookup would be work for nothing.
    """
    text = text.strip()
    if not text or not looks_like_url(text):
        return None

    lowered = text.lower()
    if lowered.startswith(_LOCAL_SCHEMES):
        return None
    rest = text
    match = _FULL_SCHEME.match(text)
    if match:
        if not lowered.startswith(("http://", "https://")):
            return None
        rest = text[match.end():]

    authority = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):
        return None                      # an IPv6 literal has no name to look up
    host = authority.split(":", 1)[0].strip().lower()

    if not host or host == "localhost" or _IPV4.match(host):
        return None
    if not _DOTTED_HOST.match(host):
        return None
    return host


def omnibox_allows(private: bool) -> dict:
    """What the omnibox may do for the tab currently in front.

    There is one omnibox for the whole window, so "am I typing in a private
    tab?" is a question the entry itself cannot answer -- which is how all
    three of these ran in a private session. Stated once, here, rather than as
    three `if` statements in the changed handler, because they are one policy
    and a fourth helper hung off that signal has to be added to it:

      * `prefetch` -- a DNS lookup leaves the machine for a host you have only
        typed, from the shared non-ephemeral context at that.
      * `history` -- the persistent history and bookmark rows the dropdown
        renders are exactly what a private session is not supposed to surface.
      * `recall` -- worse still: those rows carry snippets of the *body* of
        pages read earlier.
    """
    return {"prefetch": not private, "history": not private, "recall": not private}


class HostWarmer:
    """Calls `warm(host)` at most once per host, for the life of the window.

    A resolver caches the answer anyway, so a second prefetch of the same name
    buys nothing; the set is here so that a host typed one character at a time
    cannot turn into a lookup per keystroke. It is capped because an omnibox
    session can wander through a lot of hostnames and an unbounded set on a
    window that stays open for days is a leak, however slow -- clearing it
    outright is the cheapest bound that keeps the common case a single lookup.
    """

    LIMIT = 512

    def __init__(self, warm, limit: int = LIMIT):
        self._warm = warm
        self._limit = limit
        self._seen = set()

    def consider(self, text: str):
        """Warm the host behind `text` if it is new. Returns it, or None."""
        host = prefetch_host(text)
        if host is None or host in self._seen:
            return None
        if len(self._seen) >= self._limit:
            self._seen.clear()
        self._seen.add(host)
        self._warm(host)
        return host
