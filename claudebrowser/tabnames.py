"""Naming tabs so the strip tells them apart.

Kept free of any GTK import, like urls.py, so the rule can be tested without a
display -- and it needs testing, because the interesting cases are all about
*collisions* between tabs rather than any one tab.

The problem this solves twice over:

  * A GtkLabel with ellipsize set and no `width_chars` reports a minimum width of
    the ellipsis alone, so GtkNotebook shrank every tab to a single "...". That
    is fixed in browser.py where the label is built.
  * Even at a readable width, a raw page title is often not a name. Three GitHub
    pages all say "GitHub"; an RFC says nothing at all and leaves the URL, which
    ellipsizes down to "https://...". So: title when there is one, host when
    there is not, and a disambiguating suffix only for tabs that would otherwise
    read the same.
"""

from urllib.parse import urlparse

INTERNAL = {"home": "Start", "deck": "Deck", "bookmarks": "Bookmarks",
            "history": "History"}

MAX_SUFFIX = 20


def host_of(url):
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def last_segment(url):
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return ""
    segments = [p for p in path.split("/") if p]
    return segments[-1][:MAX_SUFFIX] if segments else ""


def base_name(url, title="", loading=False):
    """The best name for one tab, ignoring every other tab."""
    url = url or ""
    if url.lower().startswith("cb:"):
        return INTERNAL.get(url[3:].strip("/").lower() or "home", "Claude Browser")

    title = (title or "").strip()
    # A title that is just the URL again is not a title. Some sites set one, and
    # it is the case that ellipsizes to "https://..." and tells you nothing.
    if title and not title.lower().startswith(("http://", "https://")):
        return title

    host = host_of(url)
    if host:
        return host
    if loading:
        return "Loading…"
    return "New tab"


def label_tabs(tabs):
    """Name a whole strip at once. `tabs` is a sequence of (url, title, loading).

    Returns one label per tab, in order. Tabs whose base names collide get the
    smallest suffix that separates them -- the host when the hosts differ, the
    last path segment when they do not.
    """
    entries = [(t[0] or "", t[1] if len(t) > 1 else "",
                bool(t[2]) if len(t) > 2 else False) for t in tabs]
    names = [base_name(*entry) for entry in entries]

    groups = {}
    for index, name in enumerate(names):
        groups.setdefault(name, []).append(index)

    out = []
    for index, name in enumerate(names):
        group = groups[name]
        if len(group) > 1:
            url = entries[index][0]
            hosts = {host_of(entries[i][0]) for i in group}
            suffix = host_of(url) if len(hosts) > 1 else last_segment(url)
            if suffix and suffix.lower() != name.lower():
                name = "%s · %s" % (name, suffix)
        out.append(name)
    return out
