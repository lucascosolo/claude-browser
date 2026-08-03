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

#: Longest standing summary kept for a discarded tab. Long enough to say what
#: the page was about, short enough to sit in a tooltip and to be repeated once
#: per tab in a `tabs` listing an agent reads.
SUMMARY_CHARS = 180

#: A line shorter than this is furniture -- a nav item, a byline, a cookie
#: banner button -- not the start of the prose. Picking the first *paragraph*
#: sized line is what keeps the lead extract from reading "Skip to content".
MIN_LEAD_CHARS = 40

DISCARD_NOTE = "Discarded to free memory — click to reload"


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


def clamp(text, limit=SUMMARY_CHARS):
    """Collapse whitespace and cut to `limit` characters at a word boundary.

    The ellipsis is counted *inside* the limit: a caller sizing a tooltip means
    the whole string it will have to render, not the string plus one more
    character. The word boundary is only used when it does not throw away more
    than half of what was asked for -- a single very long token would otherwise
    clamp to nothing at all.
    """
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:max(0, limit)]
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-—") + "…"


def lead_extract(text, limit=SUMMARY_CHARS):
    """The opening prose of a page, as a one-line standing summary.

    Deliberately not a model call. This is produced on the discard path, which
    runs *because* the machine is short of memory; a paid network round trip
    there, for a tab the user may never come back to, would be the wrong answer
    on exactly the machine that can least afford it. A lead extract is free and
    honest about being the start of the page rather than a reading of all of it.

    Returns "" when there is nothing cached, which is the ordinary case for a
    page that never finished loading or was never recordable.
    """
    if not text:
        return ""
    for line in text.splitlines():
        line = " ".join(line.split())
        if len(line) >= MIN_LEAD_CHARS:
            return clamp(line, limit)
    # No paragraph-length line anywhere: a link list, a stub, or text extracted
    # as one short line per element. Its words still say more than nothing, so
    # run them together rather than reporting an empty summary.
    return clamp(text, limit)


def tab_tooltip(url, discarded=False, summary=""):
    """The tooltip for one tab: where it is, what it held, and why it is dim."""
    lines = [url or ""]
    if discarded:
        summary = " ".join((summary or "").split())
        if summary:
            lines.append(summary)
        lines.append(DISCARD_NOTE)
    return "\n".join(line for line in lines if line)


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
