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

from . import pagetext

NAV = (
    ("cb:home", "Home", "M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"),
    ("cb:deck", "Deck", "M3 5h8v6H3zm10 0h8v6h-8zM3 13h8v6H3zm10 0h8v6h-8z"),
    ("cb:bookmarks", "Saved", "M6 3h12v18l-6-4.5L6 21z"),
    ("cb:history", "History", "M12 3a9 9 0 1 0 9 9h-2a7 7 0 1 1-7-7v4l5-5-5-5zM11 8v5l4 2"),
    ("cb:passwords", "Logins",
     "M6 10V7a6 6 0 1 1 12 0v3h1a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-9a1 1"
     " 0 0 1 1-1zm2 0h8V7a4 4 0 0 0-8 0z"),
    ("cb:playbooks", "Playbooks",
     "M4 6h10M4 12h7M4 18h7M15 12.5l6 3.5-6 3.5z"),
    ("cb:data", "Data",
     "M12 3c4.97 0 9 1.34 9 3s-4.03 3-9 3-9-1.34-9-3 4.03-3 9-3zM3 9c0 1.66 4.03 3 9 3"
     "s9-1.34 9-3v4c0 1.66-4.03 3-9 3s-9-1.34-9-3zm0 6c0 1.66 4.03 3 9 3s9-1.34 9-3v4"
     "c0 1.66-4.03 3-9 3s-9-1.34-9-3z"),
    # Sliders rather than a gear: the rail is 19px of stroke, and a gear's teeth
    # disappear at that size into a grey blob.
    ("cb:settings", "Settings",
     "M3 8h6.5M15 8h6M3 16h9.5M18 16h3"
     "M14.5 8a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0"
     "M17.5 16a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0"),
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

    # The HUD is concatenated before the single %-format pass rather than
    # formatted on its own: a value substituted by % is not rescanned, so its
    # %(accent)s would reach the document as literal text.
    sheet = _CSS + (_HUD_CSS if palette.get("name") == "phosphor" else "")
    return _DOC % {
        "title": _e(title),
        "css": sheet % palette,
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

    # `ai` on the three that start Claude: this page's only agent controls, and
    # the ink that means "Claude" is not the ink that means "focus".
    actions = "".join(
        '<button class="qa%s" onclick="cbui.send({action:%s})">'
        '<b>%s</b><span>%s</span></button>'
        % (" ai" if a.startswith("claude:") else "", _js(a), _e(label), _e(sub))
        for a, label, sub in (
            ("claude:tldr", "TL;DR", "Summarize a page"),
            ("claude:agent", "Command", "Let Claude drive"),
            ("claude:research", "Research", "Across all tabs"),
            ("private", "Private tab", "Nothing written down"),
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


#: What a private tab actually changes, and what it does not. Two lists rather
#: than a paragraph of reassurance: every browser's private mode is misread as
#: anonymity, and the second list is the whole reason this page exists. Each
#: line names a mechanism that can be checked, not a promise.
PRIVATE_KEPT = (
    ("Nothing goes in your history", "or in the page-text cache that "
     "cb:history and recall search."),
    ("Its own session", "a separate cookie jar and cache, held in memory and "
     "dropped when the tab closes."),
    ("Claude is not given this page", "Ask, TL;DR, Research and the Ctrl+G "
     "agent refuse a private tab, and it is left out of the tab list the agent "
     "is shown. CB_PRIVATE_AI turns that off."),
    ("The address bar stays quiet", "no DNS is warmed for what you type, and "
     "no history, bookmark or page-text suggestions are offered."),
    ("Playbooks stop recording", "an operation aimed at a private tab is "
     "refused rather than written to disk."),
    ("Downloads are cancelled", "a downloaded file would outlive the tab. "
     "CB_PRIVATE_DOWNLOADS allows them."),
)

PRIVATE_NOT = (
    ("It is not a VPN", "the sites you visit, your network, your employer and "
     "your ISP see exactly what they always did."),
    ("It hides nothing from the page itself", "if you sign in, that site knows "
     "who you are for as long as the tab is open."),
    ("Saved logins still fill in", "reading the keyring is deliberate; nothing "
     "new is ever saved from a private tab."),
    ("Anything you ask for is kept", "a bookmark you add or a setting you "
     "change here is a normal write to disk."),
    ("Other tabs are unaffected", "this window's ordinary tabs go on recording "
     "history as usual."),
)


def private_page(palette, nonce):
    """The start page of a private tab: what it does, and what it does not.

    A private tab deliberately does not open cb:home -- that page is a
    dashboard of history and bookmarks, which is the one thing this session is
    not supposed to be showing you.
    """
    def rows(items):
        # Paragraphs rather than `.row`s: a row is one ellipsized line built for
        # a URL, and every line here is a sentence that has to be readable in
        # full or it is not guidance.
        return "".join('<p class="note"><b>%s</b> &mdash; %s</p>'
                       % (_e(head), _e(rest)) for head, rest in items)

    body = """
      <section>
        <h2>What this tab does not keep</h2>
        %(kept)s
      </section>
      <section>
        <h2>What it does not do</h2>
        %(not)s
      </section>
      <p class="note">%(note)s</p>
    """ % {"kept": rows(PRIVATE_KEPT), "not": rows(PRIVATE_NOT),
           "note": _e("Everything this tab holds lives in memory. Closing it "
                      "is what discards the session — the browser does not "
                      "wait for you to quit.")}
    return shell("Private tab", palette, nonce, "cb:private", body)


#: What VPN Mode actually carries, and what it does not. Written as two lists
#: for the same reason cb:private is: the second one is the honest half, and a
#: page that only has the first is marketing. "VPN Mode" is the label the user
#: asked for; every line here exists so the label cannot be read as a promise
#: the feature does not keep.
VPN_COVERS = (
    ("Pages this browser loads", "every request WebKit makes for a page, its "
     "subresources and its XHRs, in ordinary and private tabs alike."),
    ("Questions you ask Claude", "the panel, TL;DR, Research and the Ctrl+G "
     "agent reach api.anthropic.com through the same proxy, by an HTTP "
     "CONNECT tunnel."),
    ("The address bar stays quiet", "no name is resolved locally to warm a "
     "connection while the mode is on, because that lookup would leave from "
     "here rather than through the tunnel."),
    ("WebRTC is switched off", "it reads the machine's own addresses and hands "
     "them to the page over UDP, which no TCP tunnel can carry. Off is the "
     "only answer, and it means video calls in this browser will not work."),
)

VPN_NOT = (
    ("It is not a VPN", "it is an HTTP proxy with an exit on a machine you "
     "run. Nothing outside this browser goes through it — not your mail "
     "client, not your package manager, not anything else on this computer."),
    ("It is best-effort, not a kill switch", "a compromised page, or a WebKit "
     "subprocess that ignores the proxy setting, can still open a socket of "
     "its own. Only an operating-system egress rule can actually guarantee "
     "this, and that is not what this is."),
    ("It only carries TCP", "the proxy speaks HTTP and CONNECT. Anything a "
     "page tries over UDP — WebRTC media above all — is not proxied, which is "
     "why WebRTC is turned off instead."),
    ("You are not anonymous", "one exit address, used by one person. It "
     "defeats a site logging your home address; it does not defeat anyone who "
     "can see both ends."),
    ("The exit host sees everything you do", "plaintext HTTP through it is "
     "readable there, and whoever runs that machine could start writing it "
     "down at any time. Trust in it is total."),
    ("Local addresses stop working", "only loopback bypasses the proxy. A "
     "device on your own network fails to load rather than quietly going "
     "around the tunnel."),
)

#: How each state reads on the page, and which tone it takes. `bad` is the
#: failed row: it is the only one that means the browser has stopped loading
#: pages, so it is the only one painted as a warning.
_VPN_STATES = {
    "off": ("Off", "", "Traffic leaves from this machine's own address."),
    # Connecting takes the plain accent band: the proxy is applied and traffic
    # is covered, so it is neither a success to announce nor a problem.
    "connecting": ("Connecting", "",
                   "The proxy is applied. Checking what the outside world "
                   "sees before calling it on."),
    "on": ("On", "good", "Verified end to end: an outside service, asked "
                         "through the proxy, reported the address below."),
    "failed": ("Failed", "bad",
               "New page loads are blocked. VPN Mode does not fall back to "
               "loading them from your own address — turn it off if that is "
               "what you want."),
}


def vpn_page(palette, nonce, state):
    """cb:vpn -- where the mode is, what the world sees, and what it is not.

    The observed exit address is the point of the page. A mode that reports
    "on" from a local check is reporting that a proxy answered, which is not
    the question anybody has; this page only ever shows an address that came
    back from outside, through the tunnel, along with which service said it.
    """
    mode = state.get("mode") or "off"
    label, tone, blurb = _VPN_STATES.get(mode, _VPN_STATES["off"])

    facts = [_fact("State", label)]
    if state.get("exit_ip"):
        facts.append(_fact("Address the world sees", state["exit_ip"]))
        if state.get("service"):
            facts.append(_fact("Confirmed by", state["service"]))
    facts.append(_fact("Proxy", state.get("proxy") or "not configured"))
    facts.append(_fact("Never proxied",
                       ", ".join(state.get("ignore_hosts") or ["—"])))

    # The blurb is a notice for every state but `off`, because in every one of
    # those the browser is doing something to the user's traffic and saying so
    # in body text would be burying it. The tone comes from _VPN_STATES rather
    # than from a branch here, so the three bands stay one table away from each
    # other and a fourth state cannot be added without picking one.
    said = _e(blurb)
    if state.get("reason"):
        said += " " + _e(state["reason"])
    blurb_html = ('<p class="note">%s</p>' % said if mode == "off"
                  else '<div class="notice %s">%s</div>' % (tone, said))

    wanted = "off" if mode != "off" else "on"
    buttons = ('<button class="pbbtn%(cls)s" onclick="return cbui.send('
               '{action:%(act)s})">%(text)s</button>'
               % {"cls": " on" if mode != "off" else "",
                  "act": _js("vpn_" + wanted),
                  "text": _e("Turn VPN Mode off" if mode != "off"
                             else "Turn VPN Mode on")})
    if mode != "off":
        buttons += ('<button class="pbbtn" onclick="return cbui.send('
                    '{action:%s})">Check the exit again</button>'
                    % _js("vpn_check"))

    def rows(items):
        return "".join('<p class="note"><b>%s</b> &mdash; %s</p>'
                       % (_e(head), _e(rest)) for head, rest in items)

    body = """
      <section>
        <h2>Status</h2>
        %(blurb)s
        <div class="rows">%(facts)s
          <div class="row set"><span class="sl"><span class="rt">%(label)s</span></span>
          <span class="sc">%(buttons)s</span></div>
        </div>
      </section>
      <section>
        <h2>What goes through it</h2>
        %(covers)s
      </section>
      <section>
        <h2>What it is not</h2>
        %(not)s
      </section>
      <p class="note">%(config)s</p>
    """ % {
        "blurb": blurb_html,
        "label": _e("VPN Mode"),
        "facts": "".join(facts),
        "buttons": buttons,
        "covers": rows(VPN_COVERS),
        "not": rows(VPN_NOT),
        "config": _e(
            "The proxy address goes in the settings file as "
            "CB_VPN_PROXY=http://user:password@host:port. It holds a password, "
            "so the browser will not write it for you — the same rule the API "
            "key follows. Only http:// works: the exit check and the Claude "
            "API tunnel are both an HTTP CONNECT."),
    }
    return shell("VPN Mode", palette, nonce, "cb:vpn", body)


def _snippet_html(text):
    """Render an FTS5 snippet, with its matched terms marked up.

    Escaped first and marked up second, always in that order: the snippet is
    prose from a visited page, and the only thing separating a real match from
    a page that wrote `<mark>` into its own body is that the control characters
    pagetext.py delimits with cannot survive being typed into HTML.
    """
    return (_e(text)
            .replace(pagetext.HL_OPEN, "<mark>")
            .replace(pagetext.HL_CLOSE, "</mark>"))


def _text_hit(url, title, snippet):
    letter, hue = _mark(url)
    return """
    <a class="row hit" href="%(url)s">
      <span class="mark sm" style="--h:%(hue)d">%(letter)s</span>
      <span class="ht">
        <span class="rt">%(title)s</span>
        <span class="hs">%(snippet)s</span>
      </span>
      <span class="ru">%(host)s</span>
    </a>""" % {
        "url": _e(url), "hue": hue, "letter": _e(letter),
        "title": _e(title or _host(url)), "host": _e(_host(url)),
        "snippet": _snippet_html(snippet),
    }


def history_page(palette, nonce, rows, query="", marked=(), fulltext=()):
    """History, and -- when the page was loaded with a query -- what that query
    matched in the *text* of pages, which titles and URLs alone cannot find."""
    marked = set(marked)
    fulltext = list(fulltext)
    if not rows and fulltext:
        body = ""
    elif not rows:
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
    if fulltext:
        body += ('<h2>Found in page text <em>%d</em></h2><div class="rows ft">%s</div>'
                 % (len(fulltext), "".join(
                     _text_hit(h.get("url") or "", h.get("title") or "",
                               h.get("snippet") or "") for h in fulltext)))
    body += ('<div class="footer"><button class="danger" '
             'onclick="cbui.confirmClear()">Clear all history</button></div>')
    return shell("History", palette, nonce, "cb:history", body,
                 search=("Search history — Enter searches page text", query))


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


def playbooks_page(palette, nonce, books, recording=None, available=True):
    """Saved sequences, and whether one is being recorded right now.

    The recording banner is at the top rather than in the footer because a
    recording that was left running is the failure mode worth catching: every
    operation until it is stopped goes into the file, and the only place that is
    visible is here.
    """
    if not available:
        return shell("Playbooks", palette, nonce, "cb:playbooks", _empty(
            "&#9654;", "Playbooks are unavailable",
            "They are a file beside the browser's database, and that directory "
            "could not be opened."))

    body = _recorder_bar(recording or {})
    if books:
        body += '<div class="rows">%s</div>' % "".join(
            _book_row(b) for b in books)
    else:
        body += _empty("&#9654;", "No playbooks yet",
                       "Start a recording above, drive the browser through the "
                       "sequence, then save it.")
    return shell("Playbooks", palette, nonce, "cb:playbooks", body)


def _recorder_bar(status):
    """Either what is being captured, or the field that starts a capture.

    The note says out loud that hand-driving the browser records nothing: the
    capture point is the control API, so a user who starts a recording here and
    then clicks around by hand would otherwise save an empty playbook and have
    to guess why.
    """
    if status.get("recording"):
        steps = int(status.get("steps") or 0)
        skipped = int(status.get("skipped_secrets") or 0)
        private = int(status.get("skipped_private") or 0)
        return """
    <div class="rec">
      <span class="dot"></span>
      <span class="rt">Recording %(name)s</span>
      <span class="ru">%(steps)d step%(plural)s captured%(skipped)s</span>
      <button class="pbbtn" onclick="return cbui.send({action:'pb_stop'})"
              title="Stop recording and save it under this name">Save</button>
      <button class="pbbtn danger" onclick="return cbui.send({action:'pb_cancel'})"
              title="Stop recording and throw it away">Discard</button>
    </div>
    <p class="note">%(note)s</p>""" % {
            "name": "&ldquo;%s&rdquo;" % _e(status.get("name") or ""),
            "steps": steps,
            "plural": "" if steps == 1 else "s",
            "skipped": ((" &middot; %d credential field%s skipped"
                         % (skipped, "" if skipped == 1 else "s")) if skipped else "")
                       + ((" &middot; %d private-tab step%s refused"
                           % (private, "" if private == 1 else "s"))
                          if private else ""),
            "note": _e(
                "The count is from when this page was drawn; reload to see it "
                "again. Credential fields are never written to the file — the "
                "browser's own autofill supplies those on replay. Operations on "
                "a private tab are refused outright, so a recording made there "
                "stays empty.")
            + ("<br>Nothing is being recorded right now: the tab in front is "
               "private." if status.get("private_now") else ""),
        }

    return """
    <div class="rec off">
      <input id="pbname" class="pbname" type="text" autocomplete="off"
             spellcheck="false" placeholder="Name for a new recording">
      <button class="pbbtn" onclick="return cbui.pbstart(event)">Start recording</button>
    </div>
    <p class="note">%s</p>""" % _e(
        "A recording captures the operations Claude and cbctl perform — opening "
        "pages, clicking, filling fields. Browsing by hand is not captured, so "
        "drive the browser through the sequence the way you want it replayed.")


def _book_row(book):
    name = book.get("name") or ""
    steps = int(book.get("steps") or 0)
    letter = next((c for c in name if c.isalnum()), "?").upper()
    return """
    <div class="row book">
      <span class="mark sm" style="--h:%(hue)d">%(letter)s</span>
      <span class="rt">%(name)s</span>
      <span class="ru">%(what)s</span>
      <span class="rw">%(steps)d step%(plural)s%(when)s</span>
      <button class="pbbtn" onclick="return cbui.pbrun(event, %(jn)s)"
              title="Replay this playbook">Run</button>
      <button class="pbbtn danger" onclick="return cbui.pbdrop(event, %(jn)s)"
              title="Delete this playbook">Delete</button>
    </div>""" % {
        "hue": _hue(name), "letter": _e(letter), "name": _e(name),
        "what": _e(_what_it_does(book.get("ops") or [])),
        "steps": steps, "plural": "" if steps == 1 else "s",
        "when": (" &middot; saved %s" % _e(_ago(book["created"])))
                if book.get("created") else "",
        "jn": _js(name),
    }


def _what_it_does(ops, limit=6):
    """The op sequence as one readable line.

    Only the op names are available here -- `summaries()` deliberately does not
    hand out the parameters, which is where the URLs and selectors are -- so the
    line says the shape of the sequence rather than its targets. Runs are
    collapsed, because "click, click, click" reads as a stutter where
    "click x3" reads as a count.
    """
    groups = []
    for op in ops:
        label = str(op or "?")
        if groups and groups[-1][0] == label:
            groups[-1][1] += 1
        else:
            groups.append([label, 1])
    shown = ["%s%s" % (label, "" if n == 1 else " ×%d" % n)
             for label, n in groups[:limit]]
    if len(groups) > limit:
        shown.append("…")
    return " → ".join(shown) or "nothing to replay"


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


def settings_page(palette, nonce, described, notice=None):
    """Every setting in the browser, editable.

    The page renders what `settings.describe()` hands it and knows nothing else
    about any particular key: a knob added to that table appears here with its
    own control, its own explanation and its own honest note about when it
    lands, with nothing to remember on this side.

    Values are treated as hostile text throughout. Most of them are, in the
    ordinary case, typed by the user -- but the settings file is also editable
    by hand and by anything else running as this user, and a search template of
    `"><script>` reaching this document unescaped would be script running in a
    page that holds the message nonce.
    """
    banner = ""
    if notice:
        banner = ('<div class="notice %s">%s</div>'
                  % ("bad" if notice.get("error") else "",
                     _e(notice.get("error") or notice.get("message") or "")))

    chunks = [banner]
    for section in described.get("sections") or []:
        rows = "".join(_setting_row(item) for item in section.get("settings") or [])
        chunks.append("""
      <section>
        <h2>%(title)s</h2>
        <p class="note">%(note)s</p>
        <div class="rows">%(rows)s</div>
      </section>""" % {"title": _e(section.get("title") or ""),
                       "note": _e(section.get("note") or ""),
                       "rows": rows})

    chunks.append(
        '<p class="note">%s <code>%s</code>. %s</p>'
        % (_e("Every one of these is a line in"), _e(described.get("path") or ""),
           _e("Editing that file by hand does the same thing, and your API key "
              "lives there too — it is deliberately not editable from here.")))
    return shell("Settings", palette, nonce, "cb:settings", "".join(chunks))


def _setting_row(item):
    """One setting: what it is, when a change lands, and the control for it."""
    kind = item.get("kind")
    # A setting the browser refuses to write gets no control at all, rather
    # than a Save button wired to a call that always fails. It still gets a row:
    # its value is part of how the browser is configured, and a reader who
    # cannot see it here goes looking for a knob that does not exist.
    if not item.get("writable", True):
        return _set_readonly(item)

    control = {
        "bool": _set_toggle,
        "choice": _set_select,
        "secret": _set_secret,
    }.get(kind, _set_input)(item)

    # Offered only when there is a line to remove. A "Default" button on a
    # setting that is already the default does nothing, and a button that does
    # nothing is one people stop trusting.
    reset = ""
    if item.get("in_file"):
        reset = ('<button class="pbbtn" title="%s" onclick="return cbui.setreset(event, %s)"'
                 '>Default</button>'
                 % (_e("Remove the line and go back to the browser's default"),
                    _js(item.get("key") or "")))

    return """
    <div class="row set">
      <span class="sl">
        <span class="rt">%(label)s</span>
        <span class="sx">%(explain)s</span>
        <span class="eff" title="%(note)s">%(effect)s</span>%(source)s
      </span>
      <span class="sc">%(control)s%(reset)s</span>
    </div>""" % {
        "label": _e(item.get("label") or item.get("key") or ""),
        "explain": _e(item.get("explain") or ""),
        "effect": _e(item.get("effect") or ""),
        "note": _e(item.get("effect_note") or ""),
        # Named because it changes what "Default" means: a value inherited from
        # the environment survives the line being removed, and a user who does
        # not know that reads the button as broken.
        "source": ('<span class="eff env" title="%s">from your environment</span>'
                   % _e("This browser was launched by a shell that exported it. "
                        "Setting it here writes the settings file, which wins.")
                   ) if item.get("source") == "environment" else "",
        "control": control,
        "reset": reset,
    }


def _set_readonly(item):
    """A setting shown but not offered.

    Says three things and no more: whether it is set, that this page will not
    write it, and why. The value itself is never rendered -- the only setting
    that comes through here is a credential, which is the whole reason it is
    not writable.
    """
    if item.get("kind") == "secret":
        state = "set" if item.get("set") else "not set"
    else:
        state = str(item.get("value") or item.get("default") or "")
    # The "why" line sits inside the label column rather than beside the value:
    # `.row.set` is a two-column flex box that wraps, and a third child would
    # drop onto a line of its own at every width.
    return """
    <div class="row set">
      <span class="sl">
        <span class="rt">%(label)s</span>
        <span class="sx">%(explain)s</span>
        <span class="sx"><b>%(why)s</b></span>
        <span class="eff" title="%(note)s">%(effect)s</span>
      </span>
      <span class="sc"><span class="ru">%(state)s</span></span>
    </div>""" % {
        "label": _e(item.get("label") or item.get("key") or ""),
        "explain": _e(item.get("explain") or ""),
        "effect": _e(item.get("effect") or ""),
        "note": _e(item.get("effect_note") or ""),
        "state": _e(state),
        "why": _e(item.get("unwritable_note") or
                  "This one is edited in the settings file by hand."),
    }


def _set_toggle(item):
    """A boolean. Posts the state it wants rather than a flip, for the reason
    `_light_button` does: a page left open while the value changed elsewhere
    would otherwise toggle against something it is no longer showing."""
    on = bool(item.get("on"))
    return ('<button class="pbbtn%(cls)s" onclick="return cbui.setting(event, %(key)s, %(want)s)"'
            ' title="%(tip)s">%(text)s</button>'
            % {"cls": " on" if on else "",
               "key": _js(item.get("key") or ""),
               "want": _js("0" if on else "1"),
               "tip": _e("Turn it off" if on else "Turn it on"),
               "text": "On" if on else "Off"})


def _set_select(item):
    options = "".join(
        '<option value="%s"%s>%s</option>'
        % (_e(choice.get("value")),
           " selected" if choice.get("value") == item.get("value") else "",
           _e(choice.get("label")))
        for choice in item.get("choices") or [])
    return ('<select class="sin pick" onchange="return cbui.setpick(event, %s)">%s</select>'
            % (_js(item.get("key") or ""), options))


def _set_input(item):
    """A number or a free-text value, with its own Save button.

    Not saved on every keystroke: each save is a rewrite of the user's settings
    file, and a half-typed URL is a value the validator would rightly refuse
    four times per word.
    """
    number = item.get("kind") == "number"
    attrs = ""
    if number:
        for name, key in (("min", "minimum"), ("max", "maximum"), ("step", "step")):
            if item.get(key) is not None:
                attrs += ' %s="%s"' % (name, _e(item.get(key)))
    return ('<input class="sin" type="%(type)s" value="%(value)s"%(attrs)s '
            'data-k="%(dkey)s" autocomplete="off" spellcheck="false" %(unit)s>'
            '<button class="pbbtn" onclick="return cbui.setinput(event, %(key)s)">Save</button>'
            % {"type": "number" if number else "text",
               "value": _e(item.get("value") or ""),
               "attrs": attrs,
               "dkey": _e(item.get("key") or ""),
               "unit": ('title="%s"' % _e(item.get("unit"))) if item.get("unit") else "",
               "key": _js(item.get("key") or "")})


def _set_secret(item):
    """The control token. Its value is never in this document.

    The field starts empty whether or not a token is set -- the placeholder is
    the only thing that says which -- so a new one can be typed without the old
    one ever having been rendered, and "Default" is how it is cleared.
    """
    return ('<input class="sin" type="password" value="" autocomplete="off" '
            'spellcheck="false" data-k="%(dkey)s" placeholder="%(hint)s">'
            '<button class="pbbtn" onclick="return cbui.setinput(event, %(key)s)">Save</button>'
            % {"hint": _e("A token is set — type a new one to replace it"
                          if item.get("set") else "No token — the API is open to "
                          "anything on this machine"),
               "dkey": _e(item.get("key") or ""),
               "key": _js(item.get("key") or "")})


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
  /* The rest of the contract, so this sheet can say "Claude" (--agent) without
     borrowing the chrome's focus colour, and can draw structure (--edge) with
     something other than the load-bearing 1px rule. */
  --edge:%(edge)s; --agent:%(agent)s; --agent-soft:%(agent_soft)s;
  --mono:%(mono)s;
}
* { box-sizing:border-box; }
html,body { margin:0; height:100%%; }
body {
  background:var(--bg); color:var(--text);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",Cantarell,sans-serif;
  display:flex; min-height:100%%;
  /* The static scanline the chrome carries, at 3%% (`08` is the alpha byte).
     `grid` equals `bg` in dark and light, so there this is the surface painted
     over itself and resolves to nothing -- one cached gradient, no branch, and
     and never animated, because a background that repaints forever
     is the one thing this browser exists not to do. Written against the raw token because a hex
     alpha suffix cannot be appended to a var(). */
  background-image:repeating-linear-gradient(
    to bottom, %(grid)s08 0, %(grid)s08 1px, transparent 1px, transparent 3px);
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
/* The three actions that start Claude carry the agent ink, not the chrome
   accent. In dark and light the two tokens are the same coral, so this changes
   nothing there and everything in phosphor -- which is the point of the split:
   "Claude is doing something" must never be re-read as "this has focus". */
.qa.ai b { color:var(--agent); }
.qa.ai:hover { border-color:var(--agent); background:var(--agent-soft); }

section { margin-bottom:30px; }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
     font-weight:700; margin:0 0 11px; display:flex; align-items:center; gap:8px; }
/* --dim, not --line: a count beside a heading is text you are meant to read,
   and --line is a hairline colour that sits near 1:1 against the surface. */
h2 em { font-style:normal; font-weight:500; color:var(--dim); }
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

/* cb:playbooks. The recording banner is loud on purpose: a capture left running
   keeps recording everything the API does, and this page is the only place that
   fact is visible. Its buttons are always shown for the same reason the ones on
   cb:passwords are -- they are what the page is for. */
.rec { display:flex; align-items:center; gap:11px; border-radius:11px;
       padding:11px 12px; margin-bottom:6px;
       border:1px solid var(--accent); background:var(--soft); }
.rec.off { border-color:var(--line); background:var(--card); }
.rec .dot { flex:0 0 auto; width:9px; height:9px; border-radius:50%%;
            background:var(--warn); }
.rec .rt { font-weight:620; max-width:none; }
.pbname { flex:1 1 auto; min-width:0; background:var(--field); color:var(--text);
          border:1px solid var(--line); border-radius:8px; padding:7px 12px;
          font:13px system-ui,sans-serif; }
.pbname:focus { outline:none; border-color:var(--accent); }
.pbbtn { flex:0 0 auto; background:var(--card); color:var(--text); cursor:pointer;
         border:1px solid var(--line); border-radius:7px; padding:4px 11px;
         font:12px system-ui,sans-serif; }
.pbbtn:hover { border-color:var(--accent); }
.pbbtn.danger:hover { border-color:var(--warn); color:var(--warn); }
.pbbtn[disabled] { opacity:.55; cursor:default; }
.rec + .note { margin-top:8px; }

.day { font-size:11.5px; text-transform:uppercase; letter-spacing:.07em;
       color:var(--dim); font-weight:700; margin:22px 0 8px; }
.rows { display:flex; flex-direction:column; gap:4px; }
.row { display:flex; align-items:center; gap:11px; padding:7px 8px 7px 10px; }
.rt { flex:0 1 auto; font-size:13px; white-space:nowrap; overflow:hidden;
      text-overflow:ellipsis; max-width:46%%; }
.ru { flex:1 1 auto; font-size:11.5px; color:var(--dim); white-space:nowrap;
      overflow:hidden; text-overflow:ellipsis; }
.rw { flex:0 0 auto; font-size:11px; color:var(--dim); }

/* full-text hits: two lines, because the snippet is the answer to "why did this
   page match?" and a one-line row would ellipsize exactly that away. */
.row.hit { align-items:flex-start; padding:9px 10px; }
.ht { min-width:0; flex:1 1 auto; display:flex; flex-direction:column; gap:2px; }
.row.hit .rt { max-width:100%%; }
.hs { font-size:12px; color:var(--dim); line-height:1.45;
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
      overflow:hidden; }
.hs mark { background:var(--soft); color:var(--text); border-radius:3px;
           padding:0 2px; }
.row.hit .ru { flex:0 0 auto; max-width:30%%; }

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

/* cb:settings. Two columns that collapse: the explanation is the widest thing
   on the page and the control the smallest, so the row wraps rather than
   ellipsizing away the sentence that says what the knob does. */
.row.set { align-items:flex-start; padding:11px 12px; gap:14px; flex-wrap:wrap; }
.sl { flex:1 1 340px; min-width:0; display:flex; flex-direction:column; gap:3px; }
.row.set .rt { max-width:100%%; font-weight:600; white-space:normal; }
.sx { font-size:11.5px; color:var(--dim); line-height:1.5; }
.sc { flex:0 0 auto; margin-left:auto; display:flex; align-items:center; gap:6px; }
.eff { display:inline-block; margin-top:3px; font-size:9.5px; font-weight:700;
       text-transform:uppercase; letter-spacing:.06em; padding:2px 6px;
       border-radius:5px; background:var(--panel); color:var(--dim);
       align-self:flex-start; }
.eff.env { background:var(--soft); color:var(--accent); margin-left:5px; }
.sin { background:var(--field); color:var(--text); border:1px solid var(--line);
       border-radius:8px; padding:6px 10px; font:13px system-ui,sans-serif;
       min-width:120px; max-width:340px; }
.sin:focus { outline:none; border-color:var(--accent); }
.sin.pick { min-width:180px; }
.pbbtn.on { border-color:var(--accent); color:var(--accent); background:var(--soft); }

/* The one place a refused value is explained. Rendered from the browser, above
   everything, because the control that was refused has already snapped back to
   the stored value and would otherwise be the only clue. */
.notice { border:1px solid var(--accent); background:var(--soft); color:var(--text);
          border-radius:10px; padding:10px 13px; margin:0 0 20px;
          font-size:12.5px; line-height:1.5; }
.notice.bad { border-color:var(--warn); }
/* A notice that is reporting success rather than asking for attention. Only
   cb:vpn uses it, and it exists because "verified end to end" rendered in the
   accent band read as an alert -- the one thing a working tunnel must not
   look like. Border only: the soft fill is what makes the row a band, and a
   green band would be shouting in the other direction. */
.notice.good { border-color:var(--ok); }

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

/* Every transition on this page is a one-shot on interaction -- a border
   lighting on hover, a row fading as it is deleted -- and none of them is
   allowed to survive a stated preference against motion. This is the live
   branch on this machine, not a courtesy: WebKitGTK maps
   `prefers-reduced-motion` onto GTK's `gtk-enable-animations`, and
   perf.tune_gtk turns that off whenever CB_LIGHT is on, which is the default.
   `!important` because the JS in _DOC sets `style.transition` inline when it
   removes a row, and an inline declaration outranks any rule that is not. */
@media (prefers-reduced-motion:reduce) {
  * { transition:none !important; animation:none !important;
      scroll-behavior:auto !important; }
}
"""

# ---------------------------------------------------------------------------
# The HUD layer, appended for the phosphor theme only -- the same arrangement
# style.py uses for the chrome, so the two halves of the browser are read and
# edited the same way, and so dark and light keep exactly the pages they had.
#
# Every rule below is geometry or type. Not one of them introduces an ink: the
# colours are the tokens the base sheet already declared, each already held to
# its contrast floor by tests/test_style.py. What "HUD" means here, in the same
# terms as the chrome:
#
#   * square corners, everywhere, without exception.
#   * bracketed slots instead of boxes. A field is held between two 2px uprights
#     that light on focus, matching the omnibox exactly.
#   * registration marks. Two corner brackets per surface, diagonally opposite;
#     four would just be a second border.
#   * micro-labels: monospaced, uppercase, tracked hard. Applied only to the
#     legends -- headings, badges, counters. The titles, hosts, snippets and
#     explanations under them stay proportional, because they are what the
#     instrument is *showing*, not what it is labelled with.
#   * one glow, on focus. 1px hard edge plus bloom, which is the CRT tell, and
#     it happens on interaction rather than on a timer.
# ---------------------------------------------------------------------------
_HUD_CSS = """
/* A radius is the loudest 2010s-app tell there is; a HUD is drawn with a
   plotter. This is the single largest part of the difference. */
.railbtn, .filter, .bigbar, .qa, .tile, .row, .card, .mark, .act, .pbname,
.pbbtn, .rec, .badge, .lvl, .eff, .sin, .notice, .bar, .bar i, .pw.shown,
.hs mark, kbd, .footer button { border-radius:0; }

/* Structure is drawn in --edge; --line stays the load-bearing rule it is. */
.rail { border-right:1px solid %(edge)s; }
.tile, .row, .card, .rec.off { border-color:%(edge)s; }
.mark { border-color:%(edge)s; }

/* ---- type: legends are engraved, content is not ---- */
.railbtn, h1, h2, .day, .badge, .lvl, .eff, .rw, .pbbtn, .footer button,
.qa b, kbd, .curl { font-family:%(mono)s; }
h1 { text-transform:uppercase; letter-spacing:.16em; font-size:17px;
     font-weight:600; }
/* Brackets around the page name: the cheapest way to say "this is a readout",
   and it needs no extra element in the document. */
h1::before { content:"["; color:%(accent)s; margin-right:7px; }
h1::after { content:"]"; color:%(accent)s; margin-left:7px; }
header { border-bottom:1px solid %(edge)s; padding-bottom:13px; }
h2, .day { letter-spacing:.2em; font-size:11px; }
h2 { border-bottom:1px solid %(edge)s; padding-bottom:7px; }
.badge, .lvl, .eff { letter-spacing:.14em; }
.railbtn { text-transform:uppercase; letter-spacing:.1em; font-size:9.5px; }
.railbtn.on { box-shadow:inset 2px 0 0 %(accent)s; }

/* ---- registration marks ---- */
.tile, .card, .qa, .rec, .notice { position:relative; }
.tile::before, .tile::after, .card::before, .card::after,
.qa::before, .qa::after, .rec::before, .rec::after,
.notice::before, .notice::after {
  content:""; position:absolute; width:6px; height:6px;
  border:1px solid %(edge)s; pointer-events:none;
}
.tile::before, .card::before, .qa::before, .rec::before, .notice::before {
  top:-1px; left:-1px; border-right:none; border-bottom:none;
}
.tile::after, .card::after, .qa::after, .rec::after, .notice::after {
  bottom:-1px; right:-1px; border-left:none; border-top:none;
}
.tile:hover::before, .tile:hover::after,
.card:hover::before, .card:hover::after,
.qa:hover::before, .qa:hover::after { border-color:%(accent)s; }
.card.cur::before, .card.cur::after { border-color:%(accent)s; }
/* The Claude quick actions keep their amber marks even under the hover rule
   above, which would otherwise repaint them cyan the moment the pointer lands
   on the one control on this page that starts the agent. */
.qa.ai::before, .qa.ai::after,
.qa.ai:hover::before, .qa.ai:hover::after { border-color:%(agent)s; }
.qa.ai:hover { box-shadow:0 0 10px -2px %(agent)s66; }

/* ---- fields as bracketed slots, exactly as the omnibox is ---- */
.filter, .bigbar, .pbname, .sin {
  border:1px solid %(line)s;
  border-left:2px solid %(edge)s;
  border-right:2px solid %(edge)s;
  font-family:%(mono)s;
}
.filter:focus, .bigbar:focus, .pbname:focus, .sin:focus {
  border-color:%(line)s;
  border-left-color:%(accent)s; border-right-color:%(accent)s;
  box-shadow:0 0 0 1px %(accent)s73, 0 0 10px %(accent)s4d;
}
.bigbar { padding:15px 18px; }

/* ---- controls read as switch positions: a hairline that lights ---- */
.pbbtn, .footer button, .qa { border-color:%(line)s; }
.pbbtn:hover, .footer button:hover, .tile:hover, .card:hover, .qa:hover {
  border-color:%(accent)s; box-shadow:0 0 10px -2px %(accent)s66;
}
.pbbtn.danger:hover, .footer .danger:hover {
  border-color:%(warn)s; box-shadow:0 0 10px -2px %(warn)s66;
}
.pbbtn.on { box-shadow:0 0 10px -2px %(accent)s66; }
/* The hover lift is the one motion left; a HUD does not float. */
.tile:hover, .card:hover { transform:none; }
.railbtn:hover { background:%(field_focus)s; }

/* ---- readouts ---- */
.bar { border-color:%(edge)s; background:%(field)s; height:9px; }
.rec .dot { border-radius:0; box-shadow:0 0 8px %(warn)s; }
.rec { box-shadow:0 0 14px -4px %(accent)s80; }
.card.cur { box-shadow:0 0 0 1px %(accent)s, 0 0 14px -3px %(accent)s80; }
.card.priv { border-style:dashed; }
kbd { border-bottom-width:1px; border-color:%(edge)s; }
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
    pbstart: function (ev) {
      ev.preventDefault();
      var box = document.getElementById('pbname');
      var name = box ? box.value.trim() : '';
      // Refused here rather than round-tripped: an empty name comes back from
      // clean_name as an error flash, which reads as a bug in the button.
      if (!name) { if (box) box.focus(); return false; }
      return this.send({action: 'pb_start', title: name});
    },
    pbrun: function (ev, name) {
      ev.preventDefault();
      // A replay walks pages one step at a time and can take a while. Disabling
      // the button is what stops a second click starting a second walk over the
      // same tab.
      var b = ev.currentTarget;
      b.disabled = true;
      b.textContent = 'Running…';
      return this.send({action: 'pb_run', title: name});
    },
    pbdrop: function (ev, name) {
      // Armed like the clear buttons, and never a modal dialog -- one inside a
      // WebView blocks this embedder's main loop. Deleting a playbook is not
      // undoable, so it takes two clicks.
      ev.preventDefault();
      var b = ev.currentTarget;
      if (b.dataset.armed) {
        b.disabled = true;
        b.textContent = 'Deleting…';
        return this.send({action: 'pb_delete', title: name});
      }
      b.dataset.armed = '1';
      b.textContent = 'Click again';
      setTimeout(function () {
        delete b.dataset.armed; b.textContent = 'Delete';
      }, 4000);
      return false;
    },
    // cb:settings. Every one of these posts {action, url: key, title: value} --
    // the message shape is fixed at {action, url, title}, so the key travels as
    // the url and the value as the title, the way clear_data carries its kind.
    setting: function (ev, key, value) {
      ev.preventDefault();
      return this.send({action: 'set_setting', url: key, title: value});
    },
    setpick: function (ev, key) {
      return this.send({action: 'set_setting', url: key,
                        title: ev.currentTarget.value});
    },
    setinput: function (ev, key) {
      // The box is found from the element that fired rather than by key, so
      // nothing here has to build a selector out of a settings name.
      ev.preventDefault();
      var el = ev.currentTarget;
      var box = el.tagName === 'INPUT' ? el : el.parentNode.querySelector('input');
      if (!box) return false;
      var value = box.value;
      // A password field is cleared on the way out: the token has already been
      // handed over, and leaving it sitting in the DOM is the thing the field
      // exists to avoid.
      if (box.type === 'password') { box.value = ''; }
      return this.send({action: 'set_setting', url: key, title: value});
    },
    setreset: function (ev, key) {
      ev.preventDefault();
      return this.send({action: 'reset_setting', url: key});
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

  var pbname = document.getElementById('pbname');
  if (pbname) {
    pbname.focus();
    pbname.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { cbui.pbstart(e); }
    });
  }

  // Enter saves the field it was pressed in, which is what anyone who has just
  // typed a URL into a box expects. The Save button beside it stays, because
  // the same box is also changed with the arrow keys of a number spinner.
  document.querySelectorAll('input.sin').forEach(function (box) {
    box.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { cbui.setinput(e, box.dataset.k); }
    });
  });

  // Filtering is local: re-rendering from Python on every keystroke would mean
  // a full page load per character. Enter is the exception -- searching the text
  // of pages is a database query, so it costs one navigation, deliberately.
  var q = document.getElementById('q');
  if (q) {
    var apply = function () {
      var needle = q.value.toLowerCase();
      // .hit rows are server-rendered answers to the submitted query; the
      // needle being typed now is not what matched them, and a substring test
      // against a snippet would hide results the user just asked for.
      document.querySelectorAll('.tile, .row:not(.hit)').forEach(function (el) {
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
    q.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      if ((location.href || '').toLowerCase().indexOf('cb:history') !== 0) return;
      cbui.send({action: 'go',
                 url: 'cb:history' + (q.value.trim()
                        ? '?q=' + encodeURIComponent(q.value.trim()) : '')});
    });
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
