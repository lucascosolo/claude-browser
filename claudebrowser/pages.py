"""The browser's own pages, served from the `cb:` scheme.

`cb:home`, `cb:history`, `cb:bookmarks`, `cb:deck` -- rendered as HTML because
there is already a browser engine here, and a card grid in CSS is both nicer and
less code than the equivalent in GTK widgets. They share the panel's visual
language: everything is a card, colour means something, and the accent is
Claude's coral.

**Site marks instead of favicons.** A grid of favicons means a network request
per tile, on a machine where that is exactly the cost we are avoiding, and it
leaks your history to every site you have ever visited the moment you open a new
tab. Each tile instead gets a letter on a colour derived from a hash of the
hostname -- stable, instant, offline, and enough to recognise a site by shape.

**Messages from these pages are authenticated with a nonce.** The script message
handler is registered on the shared UserContentManager, so *any* page in the
browser can post to it -- a website could otherwise call
`window.webkit.messageHandlers.cbui.postMessage({action:'clear_history'})`. Each
document is rendered with a per-session random token that the handler checks,
which a remote page cannot guess. Navigation itself does not use messages at all:
those are ordinary <a href> links, so middle-click and back work as they should.
"""

import datetime
import html
import json
import time

NAV = (
    ("cb:home", "Home", "M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"),
    ("cb:deck", "Deck", "M3 5h8v6H3zm10 0h8v6h-8zM3 13h8v6H3zm10 0h8v6h-8z"),
    ("cb:bookmarks", "Saved", "M6 3h12v18l-6-4.5L6 21z"),
    ("cb:history", "History", "M12 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7v4l5-5-5-5zM11 8v5l4 2"),
    ("cb:passwords", "Logins",
     "M6 10V7a6 6 0 1 1 12 0v3h1a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9a1 1"
     " 0 0 1 1-1zm2 0h8V7a4 4 0 0 0-8 0z"),
    ("cb:data", "Data",
     "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3zM3 9c0 1.66 4.03 3 9 3"
     "s9-1.34 9-3v4c0 1.66-4.03 3-9 3s-9-1.34-9-3zm0 6c0 1.66 4.03 3 9 3s9-1.34 9-3v4"
     "c0 1.66-4.03 3-9 3s-9-1.34-9-3z"),
)


def _hue(text):
    """Stable colour per host. FNV-1a because it is three lines and spreads
    short strings like hostnames far better than Python's salted hash()."""
    h = 2166136261
    for ch in (text or "?").encode("utf-8", "replace"):
        h = ((h ^ ch) * 16777619) & 0xFFFFFFFF
    return h % 360


def _host(url):
    try:
        rest = url.split("//", 1)[1] if "//" in url else url
        return rest.split("/", 1)[0].split("?", 1)[0] or url
    except (IndexError, AttributeError):
        return url or "?"


def _mark(url):
    host = _host(url)
    label = host[4:] if host.startswith("www.") else host
    letter = next((c for c in label if c.isalnum()), "?").upper()
    return letter, _hue(label)


def _ago(when):
    delta = max(0, int(time.time()) - int(when or 0))
    if delta < 60:
        return "just now"
    for span, unit in ((60, "min"), (3600, "hr"), (86400, "day")):
        if delta < span * (60 if unit == "min" else 24 if unit == "hr" else 7):
            n = delta // span
            return "%d %s%s ago" % (n, unit, "" if n == 1 else "s")
    return time.strftime("%b %d", time.localtime(when)).replace(" 0", " ")


def _day(when, today=None):
    """Day heading: history is far easier to scan grouped than as a flat list.

    Compared as real dates, not tm_yday -- yday - 1 says "not yesterday" every
    January 1st, which is the kind of bug that hides for eleven months. `today`
    is injectable so that case can actually be tested rather than argued about.
    """
    that = datetime.date.fromtimestamp(when)
    today = today or datetime.date.today()
    if that == today:
        return "Today"
    if that == today - datetime.timedelta(days=1):
        return "Yesterday"
    return that.strftime("%A, %B %d").replace(" 0", " ")


def _e(text):
    return html.escape(str(text or ""), quote=True)


def _js(value):
    """A JS literal that is safe *inside a double-quoted HTML attribute*.

    json.dumps alone is not enough here. Its output starts and ends with a
    double quote, and these literals go into onclick="...", so a page title
    containing a quote closed the attribute early and everything after it
    became markup -- a real injection, from a string the visited site controls.
    HTML-escaping afterwards is correct rather than belt-and-braces: the parser
    turns &quot; back into a quote before the JS is ever compiled.
    """
    return html.escape(json.dumps(value), quote=True)


def _tile(url, title, sub="", starred=False, nonce=""):
    letter, hue = _mark(url)
    return """
    <a class="tile" href="%(url)s" title="%(full)s">
      <span class="mark" style="--h:%(hue)d">%(letter)s</span>
      <span class="tt">
        <span class="t1">%(title)s</span>
        <span class="t2">%(host)s%(sub)s</span>
      </span>
      <button class="act %(starcls)s" title="%(startip)s"
              onclick="return cbui.star(event, %(jurl)s, %(jtitle)s)">%(star)s</button>
      <button class="act" title="Remove from this list"
              onclick="return cbui.drop(event, %(jurl)s)">&times;</button>
    </a>""" % {
        "url": _e(url),
        "full": _e(url),
        "hue": hue,
        "letter": _e(letter),
        "title": _e(title or _host(url)),
        "host": _e(_host(url)),
        "sub": (" &middot; " + _e(sub)) if sub else "",
        "jurl": _js(url),
        "jtitle": _js(title or ""),
        "star": "&#9733;" if starred else "&#9734;",
        "starcls": "on" if starred else "",
        "startip": "Remove bookmark" if starred else "Bookmark this",
    }


def _empty(icon, line, hint=""):
    return ('<div class="empty"><div class="ei">%s</div><div>%s</div>'
            '<div class="eh">%s</div></div>' % (icon, _e(line), hint))


def shell(title, palette, nonce, active, body, search=None):
    """Every internal page: a slim icon rail, a header, and the content."""
    rail = "".join(
        '<a class="railbtn%(on)s" href="%(url)s" title="%(label)s">'
        '<svg viewBox="0 0 24 24"><path d="%(d)s"/></svg>'
        '<span>%(label)s</span></a>'
        % {"on": " on" if url == active else "", "url": url, "label": label, "d": d}
        for url, label, d in NAV)

    searchbox = ""
    if search is not None:
        searchbox = (
            '<input id="q" class="filter" type="search" placeholder="%s" value="%s" '
            'autocomplete="off">' % (_e(search[0]), _e(search[1])))

    return _DOC % {
        "title": _e(title),
        "css": _CSS % palette,
        "rail": rail,
        "head": _e(title),
        "search": searchbox,
        "body": body,
        "nonce": json.dumps(nonce),
    }


# -- the pages --------------------------------------------------------------

def home(palette, nonce, bookmarks, recent, counts):
    """A start page that is a dashboard, not a blank rectangle."""
    n_history, n_marks = counts

    actions = "".join(
        '<button class="qa" onclick="cbui.send({action:%s})">'
        '<b>%s</b><span>%s</span></button>' % (_js(a), _e(label), _e(sub))
        for a, label, sub in (
            ("claude:tldr", "TL;DR", "Summarize a page"),
            ("claude:agent", "Command", "Let Claude drive"),
            ("claude:research", "Research", "Across all tabs"),
            ("private", "Private tab", "Nothing recorded"),
        ))

    saved = ("".join(_tile(r["url"], r["title"], starred=True, nonce=nonce)
                     for r in bookmarks[:12])
             or _empty("&#9734;", "No bookmarks yet",
                       "Press <kbd>Ctrl</kbd>+<kbd>D</kbd> on a page you want back."))

    seen = ("".join(_tile(r["url"], r["title"], sub=_ago(r["last_visit"]), nonce=nonce)
                    for r in recent[:12])
            or _empty("&#8635;", "Nothing visited yet",
                      "Pages you open will show up here."))

    body = """
      <div class="hero">
        <input id="go" class="bigbar" type="text" autocomplete="off" spellcheck="false"
               placeholder="Search, or type an address">
        <div class="qas">%(actions)s</div>
      </div>
      <section>
        <h2>Bookmarks <em>%(nmarks)s</em><a class="more" href="cb:bookmarks">All</a></h2>
        <div class="grid">%(saved)s</div>
      </section>
      <section>
        <h2>Recent <em>%(nhist)s</em><a class="more" href="cb:history">All</a></h2>
        <div class="grid">%(seen)s</div>
      </section>
    """ % {"actions": actions, "saved": saved, "seen": seen,
           "nmarks": n_marks or "", "nhist": n_history or ""}
    return shell("Home", palette, nonce, "cb:home", body)


def history_page(palette, nonce, rows, query="", marked=()):
    marked = set(marked)
    if not rows:
        body = _empty("&#8635;", "No history" if not query else "Nothing matches “%s”"
                      % html.escape(query), "")
    else:
        chunks, current = [], None
        for row in rows:
            day = _day(row["last_visit"])
            if day != current:
                if current is not None:
                    chunks.append("</div>")
                chunks.append('<h3 class="day">%s</h3><div class="rows">' % _e(day))
                current = day
            chunks.append(_row(row["url"], row["title"],
                               time.strftime("%-I:%M %p", time.localtime(row["last_visit"])),
                               row["url"] in marked, row["visits"]))
        chunks.append("</div>")
        body = "".join(chunks)
    body += ('<div class="footer"><button class="danger" '
             'onclick="cbui.confirmClear()">Clear all history</button></div>')
    return shell("History", palette, nonce, "cb:history", body,
                 search=("Search history…", query))


def bookmarks_page(palette, nonce, rows, query=""):
    if not rows:
        body = _empty("&#9734;",
                      "No bookmarks" if not query else "Nothing matches “%s”"
                      % html.escape(query),
                      "Press <kbd>Ctrl</kbd>+<kbd>D</kbd> to save the page you are on.")
    else:
        body = '<div class="grid wide">%s</div>' % "".join(
            _tile(r["url"], r["title"], sub=_ago(r["added"]), starred=True, nonce=nonce)
            for r in rows)
    return shell("Bookmarks", palette, nonce, "cb:bookmarks", body,
                 search=("Search bookmarks…", query))


def passwords_page(palette, nonce, rows, never=(), available=True):
    """Saved logins. Secrets are not in this HTML until one is asked for.

    The page renders usernames and origins only; a password reaches the document
    when the eye is clicked and not before. That keeps the common case -- a page
    left open on another workspace -- from being a plaintext credential dump.
    """
    if not available:
        return shell("Logins", palette, nonce, "cb:passwords", _empty(
            "&#128274;", "The system keyring is unavailable",
            "Logins are stored in the freedesktop Secret Service. On this desktop "
            "that is <code>gnome-keyring</code> — start it and reopen the browser."))

    if rows:
        body = '<div class="rows">%s</div>' % "".join(
            _login_row(i, r["origin"], r["username"]) for i, r in enumerate(rows))
    else:
        body = _empty("&#128274;", "No saved logins",
                      "Sign in to a site and the browser will offer to save it.")

    if never:
        body += ('<h2>Never ask on these sites <em>%d</em></h2>'
                 '<div class="rows">%s</div>'
                 % (len(never), "".join(_never_row(o) for o in never)))
    return shell("Logins", palette, nonce, "cb:passwords", body)


def _login_row(index, origin, username):
    letter, hue = _mark(origin)
    return """
    <div class="row login">
      <span class="mark sm" style="--h:%(hue)d">%(letter)s</span>
      <span class="rt">%(user)s</span>
      <span class="ru">%(origin)s</span>
      <span class="rw"><code class="pw" data-k="%(i)d">&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;</code></span>
      <button class="act" onclick="return cbui.pwshow(event, %(jo)s, %(ju)s, %(i)d)"
              title="Show this password">&#128065;</button>
      <button class="act" onclick="return cbui.pwdrop(event, %(jo)s, %(ju)s)"
              title="Delete this saved login">&times;</button>
    </div>""" % {
        "hue": hue, "letter": _e(letter), "i": index,
        "user": _e(username or "(no username)"),
        "origin": _e(origin),
        "jo": _js(origin), "ju": _js(username or ""),
    }


def _never_row(origin):
    letter, hue = _mark(origin)
    return """
    <div class="row login">
      <span class="mark sm" style="--h:%(hue)d">%(letter)s</span>
      <span class="rt">%(origin)s</span>
      <span class="ru">saving is turned off here</span>
      <span class="rw"></span>
      <button class="act" onclick="return cbui.pwallow(event, %(jo)s)"
              title="Offer to save logins here again">&#8635;</button>
    </div>""" % {
        "hue": hue, "letter": _e(letter), "origin": _e(origin), "jo": _js(origin),
    }


def data_page(palette, nonce, machine, storage_info, human, pagetext_info=None,
              light=None):
    """What the browser is holding: memory, and what is on disk.

    One page for both because they answer the same question -- "why is this
    slow, and what can I do about it" -- and because the buttons that free disk
    are the ones you want in front of you while reading the memory numbers.

    `human` is passed in rather than imported so this module stays free of
    anything that needs WebKit, which is what lets it be rendered in a test.
    """
    used = machine.get("total_mb", 0) - machine.get("available_mb", 0)
    swap_used, swap_total = machine.get("swap_used_mb", 0), machine.get("swap_total_mb", 0)

    body = """
      <section>
        <h2>Memory <em class="lvl %(memlevel)s">%(memlevel)s</em>
            <em class="lvl %(cpulevel)s">cpu %(cpulevel)s</em></h2>
        <div class="rows">
          %(mem)s
          %(swap)s
          %(cpu)s
        </div>
        <p class="note">%(explain)s</p>
      </section>
      <section>
        <h2>Tabs</h2>
        <div class="rows">
          %(tabs)s
        </div>
      </section>
      <section>
        <h2>Lighter pages</h2>
        <div class="rows">
          %(light)s
        </div>
        <p class="note">%(light_note)s</p>
        <div class="footer">
          %(light_button)s
        </div>
      </section>
      <section>
        <h2>Cookies &amp; cache</h2>
        <div class="rows">
          %(store)s
        </div>
        <div class="footer">
          %(buttons)s
        </div>
      </section>
      <section>
        <h2>Page text</h2>
        <div class="rows">
          %(pagetext)s
        </div>
        <p class="note">%(pagetext_note)s</p>
        <div class="footer">
          %(pagetext_button)s
        </div>
      </section>
    """ % {
        # The two levels are shown apart, never combined. They mean genuinely
        # different things here -- memory decides whether a page load is refused,
        # CPU only how many run at once -- and a single "critical" over a Memory
        # heading, driven by a busy CPU on a machine with gigabytes free, is a
        # scarier lie than either fact on its own.
        "memlevel": _e(machine.get("memory", "ok")),
        "cpulevel": _e(machine.get("cpu", "ok")),
        # The caption names what is *available*, not just what is used, because
        # the available number is the one every decision on this page is made
        # from -- and on Linux "in use" reads alarmingly high on a healthy
        # machine, since the kernel keeps as much page cache as it can.
        "mem": _meter("Memory in use", used, machine.get("total_mb", 0),
                      "%s of %s · %s free" % (
                          human(used * 1024 * 1024),
                          human(machine.get("total_mb", 0) * 1024 * 1024),
                          human(machine.get("available_mb", 0) * 1024 * 1024))),
        "swap": _meter("Swap in use", swap_used, swap_total,
                       "%s of %s" % (human(swap_used * 1024 * 1024),
                                     human(swap_total * 1024 * 1024))
                       if swap_total else "no swap configured"),
        "cpu": _meter("CPU load", machine.get("load", 0),
                      max(1, machine.get("cores", 1)),
                      "%.2f across %d core%s" % (machine.get("load", 0),
                                                 machine.get("cores", 1),
                                                 "s" if machine.get("cores", 1) != 1 else "")),
        "explain": _e(
            "Tabs you are not looking at are dropped to a URL when memory gets "
            "tight, and reload when you come back to them. Nothing an agent "
            "asks for starts while the machine is already saturated."),
        "tabs": "".join(_fact(label, value) for label, value in (
            ("Open", machine.get("tabs", 0)),
            ("Freed to save memory", machine.get("discarded", 0)),
            ("Limit for agent-opened tabs", machine.get("tab_ceiling", "—")),
            ("Loading right now", machine.get("loading", 0)),
        )),
        # Two rows rather than one, because after the switch below is used they
        # can disagree: the header follows immediately, the animation setting was
        # read once at startup and cannot. Reporting them together as a single
        # "light mode: on" would make the second one a lie for the rest of the
        # session. `motion` is what tune_gtk was actually told; it falls back to
        # the header state for a caller that does not pass it.
        "light": "".join(_fact(label, value) for label, value in (
            ("Ask sites for a lighter page",
             "on (Save-Data: on)" if (light or {}).get("enabled") else "off"),
            ("Reduced motion",
             "requested at startup" if (light or {}).get(
                 "motion", (light or {}).get("enabled")) else "not requested"),
        )),
        "light_note": _e(
            "Servers that honour Save-Data send smaller images and fewer fonts. "
            "It rides on pages this browser loads for you, not on files a page "
            "fetches for itself — WebKitGTK gives no way to reach those. The "
            "switch takes effect on the next page load and is remembered as "
            "CB_LIGHT in your settings file. Reduced motion is the half that "
            "cannot follow: WebKit reads it once, before the first tab exists, "
            "so it stays as it was until the browser restarts."),
        "light_button": _light_button(bool((light or {}).get("enabled"))),
        "store": "".join(_fact(label, value) for label, value in (
            ("Cookie policy", {"all": "accept all",
                               "none": "reject all",
                               "nothird": "reject third-party"}.get(
                                   storage_info.get("policy"), "—")),
            ("Sites with cookies", storage_info.get("domains")
             if storage_info.get("domains") is not None else "—"),
            ("Cookie jar", human(storage_info.get("cookie_jar_bytes"))),
            ("Disk cache", human(storage_info.get("cache_bytes"))),
            ("Stored in", storage_info.get("data_dir") or "—"),
        )),
        "buttons": "".join(_clear_button(kind, label)
                           for kind, label in (("cache", "Clear cache"),
                                               ("cookies", "Clear cookies"),
                                               ("all", "Clear everything"))),
        # The text of every page read is on disk so `recall` can search it. That
        # makes it the most personal thing this browser stores -- history is a
        # list of URLs, this is the prose -- so it is clearable from the same
        # page as the cookies, rather than only by deleting a file by hand.
        "pagetext": "".join(_fact(label, value) for label, value in (
            ("Pages cached", (pagetext_info or {}).get("pages", "—")),
            ("Distinct articles", (pagetext_info or {}).get("bodies", "—")),
            ("On disk", human((pagetext_info or {}).get("bytes"))),
            ("Full-text search",
             "on" if (pagetext_info or {}).get("search") else "off"),
        )),
        "pagetext_note": _e(
            "The extracted text of pages you have read, kept so recall can "
            "search it. Clearing it is not undoable: the text is gone until "
            "those pages are visited again."),
        "pagetext_button": _clear_button("pagetext", "Clear page text"),
    }
    return shell("Data", palette, nonce, "cb:data", body)


def _light_button(enabled):
    """The on/off switch for the Save-Data hint.

    It posts the state it wants rather than "flip it": a cb:data tab left open
    while the setting was changed elsewhere would otherwise toggle against a
    value it is no longer showing. Unarmed, unlike the clear buttons -- nothing
    is destroyed, and the same click turns it back.
    """
    want = "off" if enabled else "on"
    return ('<button data-want="%s" onclick="return cbui.send('
            '{action:\'set_light\', title:%s})">%s</button>'
            % (want, _js(want), _e("Turn it off" if enabled else "Turn it on")))


def _clear_button(kind, label):
    """One armed delete button. `data-kind` is what the confirm step reads back,
    so the kind travels with the button rather than through a closure."""
    return ('<button class="danger" data-kind="%s" '
            'onclick="return cbui.confirmData(event, %s, %s)">%s</button>'
            % (kind, _js(kind), _js(label), _e(label)))


def _meter(label, value, total, caption):
    """A labelled bar. Percentages are clamped, because a load average can and
    does exceed the core count and a bar wider than its track looks like a bug
    rather than the alarming number it actually is."""
    pct = 0 if not total else max(0.0, min(1.0, float(value) / float(total)))
    tone = "hot" if pct >= 0.86 else ("warm" if pct >= 0.7 else "")
    return """
    <div class="row meter">
      <span class="rt">%(label)s</span>
      <span class="bar"><i class="%(tone)s" style="width:%(pct).1f%%"></i></span>
      <span class="ru">%(caption)s</span>
    </div>""" % {"label": _e(label), "pct": pct * 100, "caption": _e(caption),
                 "tone": tone}


def _fact(label, value):
    return ('<div class="row fact"><span class="rt">%s</span>'
            '<span class="ru">%s</span></div>' % (_e(label), _e(str(value))))


def deck(palette, nonce, tabs):
    """Every open tab as a card.

    A tab strip stops being navigation somewhere around the eighth tab -- the
    labels ellipsize to nothing and you are left clicking by position. The deck
    is the same set laid out where titles are actually readable.
    """
    if not tabs:
        body = _empty("&#9633;", "No tabs open", "")
    else:
        cards = []
        for t in tabs:
            letter, hue = _mark(t.get("url") or "")
            cards.append("""
            <div class="card%(cur)s%(priv)s" onclick="cbui.send({action:'switch',tab:%(id)d})">
              <div class="chead">
                <span class="mark" style="--h:%(hue)d">%(letter)s</span>
                <span class="ct">%(title)s</span>
                <button class="act" title="Close tab"
                        onclick="event.stopPropagation();cbui.send({action:'closetab',tab:%(id)d})"
                        >&times;</button>
              </div>
              <div class="curl">%(url)s</div>
              <div class="badges">%(badges)s</div>
            </div>""" % {
                "id": t["id"],
                "cur": " cur" if t.get("current") else "",
                "priv": " priv" if t.get("private") else "",
                "hue": hue,
                "letter": _e(letter),
                "title": _e(t.get("title") or "New tab"),
                "url": _e(t.get("url") or ""),
                "badges": ("".join(
                    '<span class="badge %s">%s</span>' % (cls, text)
                    for cls, text, on in (
                        ("now", "current", t.get("current")),
                        ("p", "private", t.get("private")),
                        ("l", "loading", t.get("loading")),
                    ) if on)),
            })
        body = '<div class="grid wide">%s</div>' % "".join(cards)
    body += ('<div class="footer"><button onclick="cbui.send({action:\'newtab\'})">'
             'New tab</button> <button onclick="cbui.send({action:\'private\'})">'
             'New private tab</button></div>')
    return shell("Deck", palette, nonce, "cb:deck", body)


def _row(url, title, when, starred, visits):
    letter, hue = _mark(url)
    return """
    <a class="row" href="%(url)s">
      <span class="mark sm" style="--h:%(hue)d">%(letter)s</span>
      <span class="rt">%(title)s</span>
      <span class="ru">%(host)s</span>
      <span class="rw">%(when)s%(visits)s</span>
      <button class="act %(sc)s" onclick="return cbui.star(event, %(ju)s, %(jt)s)"
              title="%(stip)s">%(star)s</button>
      <button class="act" onclick="return cbui.drop(event, %(ju)s)"
              title="Forget this page">&times;</button>
    </a>""" % {
        "url": _e(url), "hue": hue, "letter": _e(letter),
        "title": _e(title or _host(url)), "host": _e(_host(url)),
        "when": _e(when),
        "visits": (" &middot; %d visits" % visits) if visits and visits > 1 else "",
        "ju": _js(url), "jt": _js(title or ""),
        "star": "&#9733;" if starred else "&#9734;",
        "sc": "on" if starred else "",
        "stip": "Remove bookmark" if starred else "Bookmark this",
    }


_CSS = """
:root {
  --bg:%(bg)s; --card:%(bar)s; --panel:%(panel)s; --line:%(line)s; --text:%(text)s;
  --dim:%(dim)s; --accent:%(accent)s; --soft:%(accent_soft)s; --on:%(on_accent)s;
  --ok:%(ok)s; --warn:%(warn)s; --field:%(field)s;
}
* { box-sizing:border-box; }
html,body { margin:0; height:100%%; }
body {
  background:var(--bg); color:var(--text);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",Cantarell,sans-serif;
  display:flex; min-height:100%%;
}
::selection { background:var(--accent); color:var(--on); }

/* icon rail -- navigation that does not eat vertical space */
.rail {
  width:72px; flex:0 0 72px; background:var(--card);
  border-right:1px solid var(--line);
  display:flex; flex-direction:column; align-items:center; gap:4px;
  padding:14px 0; position:sticky; top:0; height:100vh;
}
.railbtn {
  width:56px; padding:9px 0 7px; border-radius:10px; text-decoration:none;
  color:var(--dim); display:flex; flex-direction:column; align-items:center; gap:3px;
  font-size:10.5px; letter-spacing:.02em;
}
.railbtn svg { width:19px; height:19px; fill:none; stroke:currentColor; stroke-width:1.7;
               stroke-linejoin:round; stroke-linecap:round; }
.railbtn:hover { color:var(--text); background:var(--panel); }
.railbtn.on { color:var(--accent); background:var(--soft); }

.main { flex:1; min-width:0; padding:26px 30px 60px; max-width:1180px; }
header { display:flex; align-items:center; gap:14px; margin-bottom:20px; }
header h1 { font-size:20px; font-weight:650; margin:0; letter-spacing:-.01em; }
.filter {
  margin-left:auto; background:var(--field); color:var(--text);
  border:1px solid var(--line); border-radius:9px; padding:7px 12px;
  font:13px system-ui,sans-serif; min-width:230px;
}
.filter:focus { outline:none; border-color:var(--accent); }

/* hero: the start page's one input, which both searches and navigates */
.hero { margin:6px 0 30px; }
.bigbar {
  width:100%%; background:var(--card); color:var(--text);
  border:1px solid var(--line); border-radius:14px;
  padding:16px 20px; font:16px system-ui,sans-serif;
}
.bigbar:focus { outline:none; border-color:var(--accent);
                box-shadow:0 0 0 3px var(--soft); }
.qas { display:flex; gap:9px; margin-top:12px; flex-wrap:wrap; }
.qa {
  flex:1 1 150px; text-align:left; cursor:pointer;
  background:var(--card); border:1px solid var(--line); border-radius:11px;
  padding:10px 13px; color:var(--text); font:inherit;
  display:flex; flex-direction:column; gap:1px;
}
.qa b { font-size:13px; font-weight:620; }
.qa span { font-size:11.5px; color:var(--dim); }
.qa:hover { border-color:var(--accent); background:var(--soft); }

section { margin-bottom:30px; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
     font-weight:700; margin:0 0 11px; display:flex; align-items:center; gap:8px; }
h2 em { font-style:normal; font-weight:500; color:var(--line); }
h2 .more { margin-left:auto; color:var(--accent); text-decoration:none;
           font-size:11px; letter-spacing:.04em; }
h2 .more:hover { text-decoration:underline; }

.grid { display:grid; gap:9px; grid-template-columns:repeat(auto-fill,minmax(232px,1fr)); }
.grid.wide { grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); }

.tile, .row, .card {
  background:var(--card); border:1px solid var(--line); border-radius:11px;
  text-decoration:none; color:var(--text);
  transition:border-color .12s, transform .12s;
}
.tile { display:flex; align-items:center; gap:10px; padding:10px 8px 10px 11px; }
.tile:hover, .card:hover { border-color:var(--accent); transform:translateY(-1px); }
.tt { min-width:0; flex:1; display:flex; flex-direction:column; }
.t1 { font-size:13px; font-weight:550; white-space:nowrap; overflow:hidden;
      text-overflow:ellipsis; }
.t2 { font-size:11px; color:var(--dim); white-space:nowrap; overflow:hidden;
      text-overflow:ellipsis; }

/* site mark: a letter on a hashed hue. No favicon fetch, no leak, no wait. */
.mark {
  flex:0 0 auto; width:30px; height:30px; border-radius:8px;
  display:grid; place-items:center; font-weight:700; font-size:13px;
  background:hsl(var(--h) 46%% 46%% / .17); color:hsl(var(--h) 52%% 44%%);
  border:1px solid hsl(var(--h) 46%% 46%% / .28);
}
.mark.sm { width:24px; height:24px; font-size:11px; border-radius:7px; }

.act {
  flex:0 0 auto; background:none; border:none; cursor:pointer; color:var(--line);
  font-size:15px; line-height:1; padding:4px 5px; border-radius:6px;
}
.tile .act, .row .act { opacity:0; }
.tile:hover .act, .row:hover .act, .act.on { opacity:1; }
.act:hover { background:var(--panel); color:var(--text); }
.act.on { color:var(--accent); }

/* On cb:passwords the buttons are the reason the page exists, so they do not
   hide until hover the way they do on a grid of bookmarks -- a delete you have
   to discover by sweeping the mouse is a delete nobody finds. */
.row.login .act { opacity:.5; }
.row.login:hover .act { opacity:1; }
.rows + h2 { margin-top:26px; }

.day { font-size:11.5px; text-transform:uppercase; letter-spacing:.07em;
       color:var(--dim); font-weight:700; margin:22px 0 8px; }
.rows { display:flex; flex-direction:column; gap:4px; }
.row { display:flex; align-items:center; gap:11px; padding:7px 8px 7px 10px; }
.rt { flex:0 1 auto; font-size:13px; white-space:nowrap; overflow:hidden;
      text-overflow:ellipsis; max-width:46%%; }
.ru { flex:1 1 auto; font-size:11.5px; color:var(--dim); white-space:nowrap;
      overflow:hidden; text-overflow:ellipsis; }
.rw { flex:0 0 auto; font-size:11px; color:var(--dim); }

/* A saved password renders as dots until it is asked for; `.shown` is the only
   state in which a secret is in this document at all. */
.pw { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px;
      letter-spacing:.12em; color:var(--dim); }
.pw.shown { color:var(--text); letter-spacing:0;
            background:var(--soft); padding:1px 6px; border-radius:4px;
            user-select:all; }

.card { padding:12px 13px; cursor:pointer; display:flex; flex-direction:column; gap:7px; }
.card.cur { border-color:var(--accent); box-shadow:0 0 0 2px var(--soft); }
.card.priv { border-style:dashed; }
.chead { display:flex; align-items:center; gap:9px; }
.ct { flex:1; min-width:0; font-size:13px; font-weight:550; white-space:nowrap;
      overflow:hidden; text-overflow:ellipsis; }
.card .act { opacity:.5; }
.card:hover .act { opacity:1; }
.curl { font-size:11px; color:var(--dim); white-space:nowrap; overflow:hidden;
        text-overflow:ellipsis; }
.badges { display:flex; gap:5px; min-height:16px; }
.badge { font-size:9.5px; text-transform:uppercase; letter-spacing:.06em;
         font-weight:700; padding:2px 6px; border-radius:5px;
         background:var(--panel); color:var(--dim); }
.badge.now { background:var(--soft); color:var(--accent); }
.badge.l { color:var(--warn); }

/* cb:data. The meter is a plain div rather than <progress>, which cannot be
   restyled consistently across GTK themes -- and the colour is the whole point:
   the number people act on is "is the bar red", not the megabytes. */
.row.meter .rt, .row.fact .rt { flex:0 0 40%%; max-width:40%%; }
.row.fact .ru { text-align:right; color:var(--text); }
.bar { flex:1 1 auto; height:7px; border-radius:4px; background:var(--panel);
       overflow:hidden; border:1px solid var(--line); }
.bar i { display:block; height:100%%; background:var(--accent); border-radius:4px;
         transition:width 200ms ease; }
.bar i.warm { background:var(--warn); }
.bar i.hot { background:var(--warn); filter:saturate(1.5); }
.row.meter .ru { flex:0 0 auto; text-align:right; min-width:24%%; }
.lvl { text-transform:uppercase; letter-spacing:.06em; font-size:10px;
       font-weight:700; padding:2px 7px; border-radius:5px;
       background:var(--soft); color:var(--accent); font-style:normal; }
.lvl.tight, .lvl.critical { color:var(--warn); }
.note { color:var(--dim); font-size:12px; line-height:1.55; margin:14px 2px 0;
        max-width:62ch; }

.empty { text-align:center; color:var(--dim); padding:44px 16px; grid-column:1/-1; }
.ei { font-size:26px; opacity:.5; margin-bottom:8px; }
.eh { font-size:12px; margin-top:5px; opacity:.85; }
kbd { background:var(--panel); border:1px solid var(--line); border-bottom-width:2px;
      border-radius:5px; padding:1px 5px; font:11px monospace; }

.footer { margin-top:30px; padding-top:16px; border-top:1px solid var(--line);
          display:flex; gap:9px; }
.footer button {
  background:var(--card); color:var(--text); border:1px solid var(--line);
  border-radius:8px; padding:7px 14px; font:13px system-ui,sans-serif; cursor:pointer;
}
.footer button:hover { border-color:var(--accent); }
.footer .danger:hover { border-color:var(--warn); color:var(--warn); }

@media (max-width:640px) {
  .rail { width:54px; flex-basis:54px; }
  .railbtn span { display:none; }
  .main { padding:18px 15px 50px; }
  .rt { max-width:100%%; } .ru { display:none; }
}
"""

_DOC = """<!doctype html>
<meta charset="utf-8">
<title>%(title)s</title>
<style>%(css)s</style>
<nav class="rail">%(rail)s</nav>
<div class="main">
  <header><h1>%(head)s</h1>%(search)s</header>
  %(body)s
</div>
<script>
(function () {
  // Proves to the browser that this message came from one of its own pages.
  // A remote page can call the same handler but cannot guess this value.
  var TOKEN = %(nonce)s;
  var post = (window.webkit && window.webkit.messageHandlers &&
              window.webkit.messageHandlers.cbui);

  window.cbui = {
    send: function (msg) {
      if (!post) return false;
      msg.t = TOKEN;
      post.postMessage(msg);
      return false;
    },
    star: function (ev, url, title) {
      ev.preventDefault(); ev.stopPropagation();
      var btn = ev.currentTarget, on = btn.classList.toggle('on');
      btn.innerHTML = on ? '\\u2605' : '\\u2606';
      btn.title = on ? 'Remove bookmark' : 'Bookmark this';
      return this.send({action: on ? 'bookmark' : 'unbookmark', url: url, title: title});
    },
    drop: function (ev, url) {
      ev.preventDefault(); ev.stopPropagation();
      // Remove it from the page immediately. Waiting for a reload to show the
      // deletion makes a fast action feel broken on a slow machine.
      var el = ev.currentTarget.closest('.tile, .row');
      if (el) { el.style.transition = 'opacity .12s'; el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 120); }
      return this.send({action: 'forget', url: url});
    },
    pwshow: function (ev, origin, username, index) {
      ev.preventDefault(); ev.stopPropagation();
      // The secret is not in this document yet. Ask for it; reveal() writes it
      // in when the browser answers.
      return this.send({action: 'pw_reveal', url: origin, title: username,
                        idx: index});
    },
    reveal: function (index, secret) {
      var el = document.querySelector('.pw[data-k="' + index + '"]');
      if (el) { el.textContent = secret; el.classList.add('shown'); }
    },
    pwdrop: function (ev, origin, username) {
      ev.preventDefault(); ev.stopPropagation();
      var el = ev.currentTarget.closest('.row');
      if (el) { el.style.transition = 'opacity .12s'; el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 120); }
      return this.send({action: 'pw_forget', url: origin, title: username});
    },
    pwallow: function (ev, origin) {
      ev.preventDefault(); ev.stopPropagation();
      var el = ev.currentTarget.closest('.row');
      if (el) { el.style.transition = 'opacity .12s'; el.style.opacity = '0';
                setTimeout(function () { el.remove(); }, 120); }
      return this.send({action: 'pw_allow', url: origin});
    },
    confirmData: function (ev, kind, label) {
      // Same two-click arming as confirmClear, and for the same reason: a
      // window.confirm() inside a WebView blocks this embedder's main loop.
      // Clearing cookies signs the user out of everything, so it gets the
      // same treatment as erasing history.
      ev.preventDefault();
      var b = ev.currentTarget;
      if (b.dataset.armed) {
        b.textContent = 'Clearing…';
        b.disabled = true;
        return this.send({action: 'clear_data', title: kind});
      }
      b.dataset.armed = '1';
      b.textContent = 'Click again to confirm';
      setTimeout(function () {
        delete b.dataset.armed; b.textContent = label;
      }, 4000);
      return false;
    },
    confirmClear: function () {
      // Deliberately not window.confirm(): a modal dialog inside a WebView
      // blocks the whole GTK main loop in this embedder.
      var b = event.currentTarget;
      if (b.dataset.armed) { return this.send({action: 'clear_history'}); }
      b.dataset.armed = '1';
      b.textContent = 'Click again to erase everything';
      b.classList.add('danger');
      setTimeout(function () {
        delete b.dataset.armed; b.textContent = 'Clear all history';
      }, 4000);
      return false;
    }
  };

  var go = document.getElementById('go');
  if (go) {
    go.focus();
    go.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && go.value.trim()) {
        cbui.send({action: 'go', url: go.value.trim()});
      }
    });
  }

  // Filtering is local: re-rendering from Python on every keystroke would mean
  // a full page load per character.
  var q = document.getElementById('q');
  if (q) {
    var apply = function () {
      var needle = q.value.toLowerCase();
      document.querySelectorAll('.tile, .row').forEach(function (el) {
        var hit = el.textContent.toLowerCase().indexOf(needle) !== -1 ||
                  (el.getAttribute('href') || '').toLowerCase().indexOf(needle) !== -1;
        el.style.display = hit ? '' : 'none';
      });
      document.querySelectorAll('.day').forEach(function (h) {
        var group = h.nextElementSibling, any = false;
        if (group) group.querySelectorAll('.row').forEach(function (r) {
          if (r.style.display !== 'none') any = true;
        });
        h.style.display = any ? '' : 'none';
        if (group) group.style.display = any ? '' : 'none';
      });
    };
    q.addEventListener('input', apply);
    if (q.value) apply();
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === '/' && document.activeElement === document.body) {
      var box = document.getElementById('q') || document.getElementById('go');
      if (box) { e.preventDefault(); box.focus(); }
    }
  });
})();
</script>
"""
