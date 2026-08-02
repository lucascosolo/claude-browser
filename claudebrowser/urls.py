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
