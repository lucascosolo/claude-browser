"""Per-site declutter rules: hide a site's noise, keep the part you came for.

Kept free of any GTK import, like `reader.py`, `urls.py` and `tabnames.py`, so
the parts worth testing -- which URL matches which rule, and the shape of the
injected snippet -- can be exercised without a display.

Four decisions are worth spelling out, because each of them looks like an
arbitrary choice and each encodes something that was actually tried.

*The rules hide with CSS; they never remove nodes.* This is the same instinct
as reader mode's overlay, taken further. YouTube is a Polymer app that
re-renders constantly and recycles the nodes in its feed as you scroll, so a
removed element comes back a second later and a removed *container* takes the
app's virtual list with it. A stylesheet has neither problem: it costs one
style recalculation, it applies to elements that do not exist yet, and turning
it off is removing one `<style>`.

*One stylesheet per site, not one per page.* YouTube navigates between home,
watch and a playlist without ever reloading the document, so a rule chosen from
`location.pathname` at injection time is wrong the moment the user clicks a
video. The selectors carry the scoping instead -- `ytd-rich-section-renderer`
only exists on a feed, `#related` only on a watch page -- so a single sheet
covers every page of the site and survives navigation between them. It also
means the injected script never has to watch the URL.

*The rules are conservative about anything that is also a control.* It is
tempting to hide `ytd-popup-container`, since that is where the promos live --
but it is also where "Save to Watch Later" lives, and a declutter mode that
quietly breaks the one action this browser's user opens YouTube to perform is
worse than the clutter. Menus, dialogs and the masthead's search box stay.

*Hover previews are hidden for the CPU, not the noise.* An inline preview
starts decoding video because the pointer passed over a thumbnail. On the two
cores this browser is written for that is not a small thing, and it is the same
reasoning as everything in `perf.py`.
"""

from urllib.parse import urlsplit

from . import envfile, extract

#: The switch. Read from the settings file on every call rather than captured
#: at import, for the same reason `perf.light_enabled` is: the settings page
#: writes that file, and a value read once would leave the browser decluttering
#: pages the user has just asked it to leave alone until the next restart.
RULES_ENV = "CB_SITERULES"


def enabled(raw=None, path=None):
    """Is the declutter layer on? Default yes.

    On by default because the user's complaint was that YouTube is *unusable*,
    not that it is occasionally noisy -- a mode you have to switch on for every
    page is one you are switching on every time. Anything unrecognised means
    on, matching `CB_LIGHT` and `CB_BLOCK`: a typo must not silently remove a
    default nobody asked to lose.
    """
    if raw is None:
        raw = envfile.setting(RULES_ENV, "", path=path)
    return (raw or "").strip().lower() not in ("0", "off", "false", "no")

#: The id of the injected style element. One per document; its presence is the
#: whole of the on/off state, so nothing has to be tracked on the Python side.
STYLE_ID = "cb-siterules-style"


class Rule:
    """One site's declutter sheet.

    `hosts` are matched as suffixes against the URL's hostname with any leading
    `www.` removed, so `youtube.com` covers `m.youtube.com` and
    `music.youtube.com` without three entries.
    """

    __slots__ = ("name", "hosts", "summary", "css")

    def __init__(self, name, hosts, summary, css):
        self.name = name
        self.hosts = tuple(hosts)
        self.summary = summary
        self.css = css

    def matches(self, host):
        return any(host == h or host.endswith("." + h) for h in self.hosts)


def host_of(url):
    """The hostname a rule is matched against: lowercased, no `www.`, no port.

    Returns "" for anything without one -- `cb:` pages, `about:blank`, a bare
    path -- which no rule can match, which is the intent.
    """
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        # urlsplit raises on a malformed IPv6 literal rather than returning
        # nothing. A URL we cannot parse has no rule, same as one with no host.
        return ""
    return host[4:] if host.startswith("www.") else host


def for_url(url):
    """The rule covering this URL, or None."""
    host = host_of(url)
    if not host:
        return None
    for rule in RULES:
        if rule.matches(host):
            return rule
    return None


def toggle(url):
    """JavaScript that turns the rule for `url` on if it is off, and off if on.

    Evaluates to a JSON string, like every snippet in `extract.py`. Returns None
    when no rule covers the URL, so the caller can say so rather than injecting
    an empty stylesheet and reporting success.
    """
    rule = for_url(url)
    if rule is None:
        return None
    return "(function(){var CSS=%s,NAME=%s,ID=%s;%s})()" % (
        extract._js_str(rule.css), extract._js_str(rule.name),
        extract._js_str(STYLE_ID), _SCRIPT)


def apply_css(url):
    """JavaScript that installs the rule and leaves it installed.

    The auto-apply path on page load, kept separate from `toggle` because a
    toggle that fires on every load would turn the mode *off* on the second
    load of a page that already had it.
    """
    rule = for_url(url)
    if rule is None:
        return None
    return "(function(){var CSS=%s,NAME=%s,ID=%s;%s})()" % (
        extract._js_str(rule.css), extract._js_str(rule.name),
        extract._js_str(STYLE_ID), _APPLY)


# -- the sheets --------------------------------------------------------------
#
# Every selector below was read off a live, signed-in page rather than
# remembered, because YouTube's element names are the kind of thing that is
# nearly right from memory and silently matches nothing. `ytd-rich-section-
# renderer` is the wrapper the feed uses for its injected shelves -- Shorts,
# Playables and "Top news" were the three on the page this was written
# against -- and hiding it is what leaves a home page that is only
# `ytd-rich-item-renderer` suggestions, which is the whole request.
_YOUTUBE = """
/* Feed: the injected shelves, not the suggestions. */
ytd-rich-section-renderer,
ytd-rich-shelf-renderer,
ytd-mini-game-card-view-model,
mini-game-card-view-model{display:none!important;}

/* Shorts, wherever they are lockup'd in. */
ytm-shorts-lockup-view-model,
ytm-shorts-lockup-view-model-v2,
ytd-reel-shelf-renderer{display:none!important;}

/* The filter chip bar above the feed. */
ytd-feed-filter-chip-bar-renderer,
#chips-wrapper{display:none!important;}

/* The left rail, both states of it. The masthead stays: it carries search. */
#guide,
tp-yt-app-drawer#guide,
ytd-mini-guide-renderer{display:none!important;}
ytd-page-manager{margin-left:0!important;}
#content.ytd-app{padding-left:0!important;}

/* Watch page: the suggestion rail, comments, and the shelves under a video. */
#related,
#comments,
ytd-comments,
ytd-merch-shelf-renderer,
#donation-shelf,
ytd-clarification-renderer,
#clarify-box{display:none!important;}

/* Watch page ad slots. Named individually rather than by a wildcard so a
   miss is a visible gap rather than a hidden control. */
#masthead-ad,
ytd-banner-promo-renderer,
ytd-statement-banner-renderer,
ytd-companion-slot-renderer,
ytd-action-companion-ad-renderer,
ytd-promoted-sparkles-web-renderer,
ytd-promoted-video-renderer,
ytd-display-ad-renderer,
ytd-in-feed-ad-layout-renderer,
ytd-ad-slot-renderer,
ytd-player-legacy-desktop-watch-ads-renderer{display:none!important;}

/* Hover previews start decoding video on a pointer move. See the module note. */
#video-preview,
ytd-video-preview,
ytd-moving-thumbnail-renderer{display:none!important;}

/* With #related gone the player has the row to itself. Capped rather than
   unbounded: a 100%-wide player on a wide window is a worse watch than a
   large one, and this keeps the same max the theatre mode uses. */
#primary.ytd-watch-flexy{max-width:1280px!important;margin:0 auto!important;}
ytd-watch-flexy[flexy] #secondary.ytd-watch-flexy{display:none!important;}
"""

#: Ordered, and matched first-wins. There is one entry today; the table exists
#: so the second one is an entry rather than an `if`.
RULES = (
    Rule("youtube", ("youtube.com", "youtu.be"),
         "Hides Shorts, the shelves, the sidebar, comments and the suggestion "
         "rail. Keeps search, the feed's suggestions, and every menu.",
         _YOUTUBE),
)


#: The body of the IIFE `toggle` builds. `CSS`, `NAME` and `ID` are in scope.
_SCRIPT = r"""
var live=document.getElementById(ID);
if(live){
  live.remove();
  return JSON.stringify({ok:true,simplified:false,rule:NAME,url:location.href});
}
var style=document.createElement('style');
style.id=ID;
style.textContent=CSS;
(document.head||document.documentElement).appendChild(style);
return JSON.stringify({ok:true,simplified:true,rule:NAME,url:location.href});
"""

#: The body of the IIFE `apply_css` builds. Idempotent on purpose: a load that
#: fires the injection twice must not end with two sheets to remove.
_APPLY = r"""
if(document.getElementById(ID)){
  return JSON.stringify({ok:true,simplified:true,rule:NAME,url:location.href});
}
var style=document.createElement('style');
style.id=ID;
style.textContent=CSS;
(document.head||document.documentElement).appendChild(style);
return JSON.stringify({ok:true,simplified:true,rule:NAME,url:location.href});
"""
