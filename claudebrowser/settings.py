"""Every setting the browser has, described once.

A preference lives in exactly one place -- the settings file in `envfile.py` --
and until this table existed the only way to change most of them was to open
that file in an editor. This is what makes them editable from `cb:settings`:
for each key, what to call it in prose, which values the code that reads it
will actually accept, and -- the part a settings page usually gets wrong --
when a change to it starts mattering.

Three rules shaped it.

**The accepted values come from the consumer, not from the name.** A value this
table lets through has to be one the reader of that variable understands, and
several of them do not degrade gracefully: `CB_MAX_TABS` is `int()`-ed at
import time and `CB_PORT` is `int()`-ed before the window is built, so a
non-numeric one does not mean "fall back to the default", it means the browser
does not start. A settings page that can write those is a page that can brick
the next launch, which is the one thing it must not be able to do.

**`effect` is per setting and written from what the code does.** Some values
are re-read on every use (`CB_PERSONA`, `CB_SCRUB`), some were read once before
the first tab existed (`CB_GPU`, `CB_MEM_LIMIT`), and some are read by `cbctl`
and the MCP server in a different process entirely (`CB_URL`, `CB_AUTOSTART`).
A single "restart to apply" banner would be wrong in both directions at once.

**GTK-free, like `personas.py`,** so the whole table can be validated and
rendered in a test. That is also why the handful of facts it shares with
`perf.py` and `storage.py` -- the cookie policy names, how each boolean is
spelled -- are restated here rather than imported: importing either one pulls
in WebKit, and then none of this could be tested without a display. Each such
restatement names the function it mirrors, so a change over there has a string
to grep for.

The prose comes from `envfile.TEMPLATE` wherever the template already had a
line for the key; the rest is taken from the comment above the code that reads
it. Nothing here is a fresh description of behaviour written from the name.
"""

import os

from . import envfile, personas, vpn

#: Section headings, in the order the page shows them, with the one line that
#: says what the section is for.
SECTIONS = (
    ("Appearance", "How the browser looks, and where it starts."),
    ("Privacy", "What is kept, what is sent, and what is stripped before "
                "Claude sees it."),
    ("Performance", "Written for a two-core laptop. Every one of these trades "
                    "something for speed."),
    ("Claude", "The panel, the agent, and which credential they use."),
    ("Control API", "The loopback HTTP server agents drive this browser "
                    "through, and how cbctl and the MCP server find it."),
)

# -- how each boolean is spelled --------------------------------------------
#
# Not one rule but three, because the code being mirrored does not agree with
# itself. Written out rather than unified: this table's job is to report what
# the browser will do with a value, and "CB_ITP=off leaves tracking prevention
# on" is a true and surprising fact about storage.make_context that a tidier
# reader here would hide.


def _off_words(raw):
    """perf.blocking_enabled / perf.light_enabled / scrub.enabled /
    siterules.enabled: on unless a word that unambiguously means off. A typo
    keeps the default."""
    return (raw or "").strip().lower() not in ("0", "off", "false", "no")


def _client_off_words(raw):
    """client.autostart: a shorter off list than the browser's own knobs."""
    return str(raw or "").strip().lower() not in ("0", "false", "no")


def _only_zero_is_off(raw):
    """storage.make_context reads CB_ITP with a literal `!= "0"`."""
    return (raw or "").strip() != "0"


def _only_one_is_on(raw):
    """perf.tune_view reads CB_WEBGL with a literal `== "1"`."""
    return (raw or "").strip() == "1"


def _only_on_words(raw):
    """ai.private_ai_enabled / storage.private_downloads_enabled: off unless a
    word that unambiguously means on. The inverse list from `_off_words`, and
    the inversion is the point -- for these two a typo has to fall back to the
    private behaviour, not out of it."""
    return (raw or "").strip().lower() in ("1", "on", "true", "yes")


def _vpn_truth(raw):
    """vpn.enabled: absent is off, but anything that is not a word meaning off
    is on. The inverse of `_only_on_words` and inverted deliberately -- for this
    one knob a misreading in the "off" direction means browsing from your own
    address while the browser says otherwise, and in the "on" direction it means
    a mode you have to turn back off. Called through a wrapper because
    `vpn.enabled(None)` goes and reads the settings file, which is not what a
    validator handed an empty value should do."""
    return vpn.enabled(raw or "")


def _choice_canon(values, aliases=None):
    """Canonicaliser for an enumerated setting: exact keys plus the spellings
    the consuming code also accepts (perf.tune_view takes CB_GPU=none)."""
    aliases = aliases or {}

    def canon(raw):
        text = (raw or "").strip().lower()
        text = aliases.get(text, text)
        return text if text in values else None
    return canon


def _search_template(value):
    """urls.normalize does `SEARCH % quote(query)`. Anything that raises there
    turns every search into a traceback, so it is tried here instead."""
    try:
        probe = value % "claude-browser-probe"
    except (TypeError, ValueError):
        raise ValueError(
            "a search URL needs exactly one %s where the query goes, and no "
            "other % in it -- write a literal percent as %%")
    if "claude-browser-probe" not in probe:
        raise ValueError("the %s in a search URL is where the query goes; "
                         "this template dropped it")
    # `cb:search?q=%s` is the default, and it is not an http URL. It is
    # allowed by name rather than by letting any scheme through: `cb:` is this
    # browser's own, and a template pointing at `javascript:` or `data:` would
    # turn the omnibox into an execution surface for whatever was typed.
    if probe.lower().startswith("cb:search"):
        return
    if not probe.lower().startswith(("http://", "https://")):
        raise ValueError("a search URL has to start with http:// or https://, "
                         "or be cb:search?q=%s")


def _http_url(value):
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("this has to be an http:// or https:// address")
    host = value.split("//", 1)[1].split("/", 1)[0]
    if not host:
        raise ValueError("this address has no host in it")


def _search_lang(text):
    """`en` or `any`, case-insensitively. `None` for anything else.

    A `choice` setting canonicalises rather than validates -- returning None is
    how `_clean_choice` says "not one of these" -- so this is the whole
    contract. Spelled here rather than imported from `search.py` for the same
    reason every other validator is: this table is written from what the
    consumer does with the value, and it must not depend on a module that
    reaches the network.
    """
    low = (text or "").strip().lower()
    return low if low in ("en", "any") else None


def _playlist_id(value):
    """A YouTube playlist id, or WL.

    Written from what the consumer does with it: `watchlater.playlist_url`
    pastes this straight into a query string, so anything outside the character
    set YouTube uses for ids would either 404 or, worse, smuggle another
    parameter onto the URL.
    """
    text = (value or "").strip()
    if not text:
        return "a playlist id, or WL for Watch Later"
    if not all(c.isalnum() or c in "-_" for c in text):
        return "playlist ids are letters, digits, - and _ only"
    return None


def _start_page(value):
    """CB_HOME is loaded with `raw=True` -- it skips the omnibox's
    search-or-navigate guess -- so it has to already be a location."""
    from . import urls

    if not urls.looks_like_url(value):
        raise ValueError("a start page has to be an address, like "
                         "https://example.com or cb:home")
    if value.lower().startswith(("javascript:", "data:")):
        raise ValueError("a start page that runs script is not something this "
                         "page will set for you")


class Setting:
    """One knob: what it is called, what it accepts, and when it lands."""

    __slots__ = ("key", "section", "label", "explain", "kind", "default",
                 "effect", "effect_note", "choices", "canon", "minimum",
                 "maximum", "step", "truth", "check", "allow_empty", "unit",
                 "writable", "unwritable_note")

    def __init__(self, key, section, label, explain, kind, default, effect,
                 effect_note, choices=(), canon=None, minimum=None,
                 maximum=None, step=1, truth=None, check=None,
                 allow_empty=False, unit="", writable=True,
                 unwritable_note=""):
        self.key = key
        self.section = section
        self.label = label
        self.explain = explain
        self.kind = kind              # bool, choice, number, text, secret
        self.default = default        # what the code does with no line at all
        self.effect = effect          # the badge: three or four words
        self.effect_note = effect_note
        self.choices = list(choices)
        self.canon = canon
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.truth = truth or _off_words
        self.check = check
        self.allow_empty = allow_empty    # empty box means "remove the line"
        self.unit = unit
        # A setting the browser reports but refuses to write. There is one, and
        # it is a credential: see CB_VPN_PROXY below and envfile.SECRET_KEYS.
        # The refusal lives here rather than only in envfile so the page can
        # render the row without a control at all, instead of offering a Save
        # button that always fails.
        self.writable = writable
        self.unwritable_note = unwritable_note

    # -- validation ---------------------------------------------------------

    def clean(self, raw):
        """The string to store, or None meaning "remove the line".

        Raises ValueError with a sentence a person can act on. Every path into
        the settings file goes through here -- the page, the HTTP op and cbctl
        all call `apply` -- so there is one place a bad value is stopped.
        """
        if isinstance(raw, bool):
            raw = "1" if raw else "0"
        text = "" if raw is None else str(raw).strip()
        if "\n" in text or "\r" in text:
            raise ValueError("a setting is one line")

        if self.kind == "bool":
            return self._clean_bool(text)
        if self.kind == "choice":
            return self._clean_choice(text)
        if self.kind == "number":
            return self._clean_number(text)
        return self._clean_text(text)

    def _clean_bool(self, text):
        low = text.lower()
        if low in ("1", "on", "true", "yes"):
            return "1"
        if low in ("0", "off", "false", "no"):
            return "0"
        raise ValueError("%s is on or off, not %r" % (self.label, text))

    def _clean_choice(self, text):
        value = self.canon(text) if self.canon else None
        if value is None:
            raise ValueError("%s has to be one of %s"
                             % (self.label,
                                ", ".join(v or "(unset)" for v, _l in self.choices)))
        return value

    def _clean_number(self, text):
        if not text and self.allow_empty:
            return None
        whole = float(self.step).is_integer()
        try:
            number = int(text) if whole else float(text)
        except (TypeError, ValueError):
            raise ValueError("%s is a number%s, not %r"
                             % (self.label, " (whole)" if whole else "", text))
        if number != number:                       # NaN compares unequal
            raise ValueError("%s is a number, not %r" % (self.label, text))
        if self.minimum is not None and number < self.minimum:
            raise ValueError("%s cannot be below %s" % (self.label, self.minimum))
        if self.maximum is not None and number > self.maximum:
            raise ValueError("%s cannot be above %s" % (self.label, self.maximum))
        return "%d" % number if whole else "%g" % number

    def _clean_text(self, text):
        if not text:
            if self.allow_empty:
                return None
            raise ValueError("%s cannot be empty -- use Default to clear it"
                             % self.label)
        if self.check:
            self.check(text)
        return text


SETTINGS = (
    # -- Appearance ---------------------------------------------------------
    Setting(
        "CB_THEME", "Appearance", "Theme",
        "dark, light or phosphor; \"system\" follows the desktop instead.",
        "choice", "",
        "Applies now",
        "The window and the browser's own pages re-colour immediately. The "
        "Claude panel is a document that is already loaded, so it keeps its "
        "colours until the browser restarts.",
        # "Follow the desktop" can only ever mean dark or light -- a desktop has
        # no way to ask for phosphor, so that one has to be picked by name.
        choices=(("", "Phosphor (HUD) \u2014 default"), ("phosphor", "Phosphor (HUD)"),
                 ("dark", "Dark"), ("light", "Light"),
                 ("system", "Follow the desktop")),
        canon=_choice_canon(("", "phosphor", "dark", "light", "system"))),
    Setting(
        "CB_LIGHT", "Appearance", "Ask sites for a lighter page",
        "Sends Save-Data: on with each page this browser loads, and asks for "
        "reduced motion. Servers that honour it send smaller images and fewer "
        "fonts.",
        "bool", "1",
        "Next page load",
        "The header follows on the next page load. Reduced motion cannot: "
        "WebKit reads it once, before the first tab exists, so that half stays "
        "as it was until the browser restarts."),
    Setting(
        "CB_HOME", "Appearance", "Start page",
        "Where a new tab and the home button go.",
        "text", "cb:home",
        "After a restart",
        "Read once when the browser module is imported, so tabs opened in this "
        "session still go to the old one.",
        check=_start_page),
    Setting(
        "CB_SEARCH", "Appearance", "Search engine",
        "Where the omnibox sends anything that is not an address. %s is the "
        "query. The default is this browser's own cb:search, which puts a "
        "short Claude answer above real results; any https:// template "
        "works too.",
        "text", "cb:search?q=%s",
        "After a restart",
        "urls.py reads the template once at import, so this session keeps "
        "searching with the previous one.",
        check=_search_template),
    Setting(
        "CB_SEARCH_LANG", "Appearance", "Search result language",
        "en keeps only results written in Latin script; any keeps everything. "
        "The search API takes no language parameter -- language, lang, market, "
        "mkt and setLang were all sent to it and changed nothing -- so this is "
        "filtered here, after the results arrive.",
        "choice", "en",
        "Next search",
        "Read on every search, so the next one already obeys it.",
        choices=(("en", "Latin script only"), ("any", "Any language")),
        canon=_search_lang),
    Setting(
        "CB_YT_EMBED", "Appearance", "Open YouTube videos in the bare player",
        "A youtube.com/watch link loads the whole YouTube application -- the "
        "player, a sidebar of suggestions with a thumbnail each, comments and "
        "the shelves under them, which took over two minutes on this machine. "
        "With this on, a video link goes straight to YouTube's own /embed/ "
        "player instead: the same video, none of the rest. Home, search and "
        "channel pages are untouched.",
        "bool", "1",
        "Next link",
        "The rewrite happens as a navigation is decided, so the next video "
        "link already obeys it.",
        truth=_off_words),
    Setting(
        "CB_QUEUE_LIST", "Appearance", "Background queue playlist",
        "Which playlist cb:queue plays. WL is your Watch Later queue; any "
        "other playlist id works too, which is what makes moving off WL a "
        "setting change rather than a code change.",
        "text", "WL",
        "Next refresh",
        "cb:queue reads it when it fetches, so Refresh on the page is enough.",
        check=_playlist_id),
    Setting(
        "CB_SITERULES", "Appearance", "Declutter noisy sites",
        "Hide the noise on sites this browser has a rule for -- YouTube's "
        "Shorts, shelves, sidebar, comments and ad slots. Hides with CSS and "
        "never removes anything that is also a control, so menus, dialogs and "
        "Save to Watch Later keep working.",
        "bool", "1",
        "Next page load",
        "The sheet is injected as a page commits, so pages already open keep "
        "whatever they were loaded with until they are reloaded.",
        truth=_off_words),

    # -- Privacy ------------------------------------------------------------
    Setting(
        "CB_BLOCK", "Privacy", "Block ads and trackers",
        "Ad/tracker blocking. On by default -- it is the single biggest win on "
        "slow hardware; turn it off if a site misbehaves.",
        "bool", "1",
        "After a restart",
        "The blocklist is compiled and attached to the web context once, at "
        "startup."),
    Setting(
        "CB_COOKIES", "Privacy", "Cookie policy",
        "Third-party cookies are rejected by default: the ones that drops are "
        "mostly attached to script loads the blocker already refuses. Accept "
        "all for a site that signs you in through an iframe.",
        "choice", "nothird",
        "After a restart",
        "The policy is handed to the cookie manager when the web context is "
        "built, before the first tab.",
        # storage.POLICIES, restated: importing storage.py pulls in WebKit.
        choices=(("nothird", "Reject third-party"), ("all", "Accept all"),
                 ("none", "Reject all")),
        canon=_choice_canon(("nothird", "all", "none"))),
    Setting(
        "CB_ITP", "Privacy", "Tracking prevention",
        "Keeps a tracker embedded on several sites from carrying state between "
        "them. Costs a small database and a little bookkeeping per load.",
        "bool", "1",
        "After a restart",
        "Turned on once on the website data manager, when the context is built.",
        truth=_only_zero_is_off),
    Setting(
        "CB_SCRUB", "Privacy", "Strip personal data from page text",
        "Replaces emails, phone numbers, card numbers, IBANs, SSNs and account "
        "numbers with typed placeholders before page text is sent to Claude.",
        "bool", "1",
        "Next question",
        "Read fresh every time a page is prepared for a question, so the next "
        "one you ask uses the new setting."),
    Setting(
        "CB_PRIVATE_AI", "Privacy", "Let Claude read private tabs",
        "Off by default: Ask, TL;DR, Research and the Ctrl+G agent refuse a "
        "private tab rather than send its address, title and text to "
        "Anthropic. Turn it on if you want those features in a private tab "
        "anyway.",
        "bool", "0",
        "Next question",
        "Read at the moment a Claude feature is asked to read a tab, so the "
        "next thing you ask uses the new setting.",
        truth=_only_on_words),
    Setting(
        "CB_PRIVATE_DOWNLOADS", "Privacy", "Allow downloads from private tabs",
        "Off by default: a download is a permanent file named by the remote "
        "server, which is the one thing a private tab promises not to leave "
        "behind. Downloads from ordinary tabs are unaffected.",
        "bool", "0",
        "Next download",
        "Read when a download starts, so a change applies to the next one "
        "without a restart.",
        truth=_only_on_words),
    Setting(
        "CB_VPN", "Privacy", "VPN Mode",
        "Sends this browser's page loads and its Claude API calls through the "
        "proxy below, so sites see that machine's address instead of yours. It "
        "is a proxy, not a VPN: other applications, WebRTC and system traffic "
        "are untouched. cb:vpn has the state, the address the world sees, and "
        "the full list of what it does not cover.",
        "bool", "0",
        "Applies now",
        "The proxy is handed to the web context and to every private tab's own "
        "session immediately, and the browser then checks what the outside "
        "world sees before it will call itself on. If that check fails the "
        "mode goes to failed and stays there — it never quietly goes back to "
        "loading pages from your own address.",
        truth=_vpn_truth),
    Setting(
        "CB_VPN_PROXY", "Privacy", "VPN proxy address",
        "Where VPN Mode sends traffic, as http://user:password@host:port. Only "
        "http:// works: the exit check and the Claude API tunnel are both an "
        "HTTP CONNECT, which cannot speak socks5 or wrap TLS around a proxy hop.",
        "secret", "",
        "Next time VPN Mode is turned on",
        "Read from the settings file each time the mode is engaged, so "
        "correcting it and toggling VPN Mode off and on is enough.",
        allow_empty=True,
        # The one setting on this page the browser will not write. The address
        # embeds the proxy's password -- WebKit's NetworkProxySettings takes
        # credentials no other way -- so it is a credential wearing a URL's
        # clothes, and every argument that keeps ANTHROPIC_API_KEY out of the
        # settings controls applies to it unchanged. envfile.SECRET_KEYS refuses
        # it one layer down as well; this is the layer that can explain why.
        writable=False,
        unwritable_note="This address holds a password, so the browser will "
                        "not write it. Put it in the settings file by hand, "
                        "the way the API key goes in."),

    # -- Performance --------------------------------------------------------
    Setting(
        "CB_GPU", "Performance", "Hardware acceleration",
        "Force software rendering (off) or GPU compositing (on). Compositing "
        "helps scrolling even on weak Intel parts, and is the first thing to "
        "blame when rendering misbehaves.",
        "choice", "",
        "After a restart",
        "Applied to each WebView's settings as it is created; existing tabs "
        "keep the policy they were built with.",
        choices=(("", "Let WebKit decide"), ("on", "Always composite"),
                 ("off", "Software rendering")),
        canon=_choice_canon(("", "on", "off"),
                            {"1": "on", "always": "on",
                             "0": "off", "none": "off"})),
    Setting(
        "CB_WEBGL", "Performance", "WebGL",
        "Off by default: integrated graphics will run it slowly while competing "
        "with page layout for the same thermal budget. Turn it on for a site "
        "that actually needs it.",
        "bool", "0",
        "After a restart",
        "Set on each WebView as it is created.",
        truth=_only_one_is_on),
    Setting(
        "CB_MEM_LIMIT", "Performance", "Memory ceiling per web process",
        "Megabytes a web process may reach before WebKit starts shedding "
        "caches and then whole documents. Tabs share one process here, so this "
        "is the ceiling for the whole page set.",
        "number", "512",
        "After a restart",
        "Installed on WebKit's memory pressure handler once, at startup.",
        minimum=64, maximum=8192, unit="MB"),
    Setting(
        "CB_MAX_TABS", "Performance", "Tab limit for agents",
        "The most tabs an agent may have open at once. You are never held to "
        "it -- Ctrl+T always works -- and a machine under pressure lowers it "
        "further on its own.",
        "number", "10",
        "After a restart",
        "Read into a module constant when the browser is imported.",
        minimum=1, maximum=200),

    # -- Claude -------------------------------------------------------------
    Setting(
        "CB_PERSONA", "Claude", "Panel persona",
        "How the Claude panel answers. A persona adds a paragraph to the "
        "browser's own instructions; it never replaces them.",
        "choice", personas.DEFAULT,
        "Next question",
        "Read from the settings file on every request, and the panel's own "
        "selector is kept in step.",
        choices=tuple(personas.choices()),
        canon=personas.normalize),
    Setting(
        "CB_AUTH", "Claude", "Credential",
        "auto (default: API key, then a Claude Code subscription token), api, "
        "subscription.",
        "choice", "auto",
        "Next request",
        "Read when a request is about to be made, so the next question uses it.",
        choices=(("auto", "API key, then subscription"),
                 ("api", "API key only"),
                 ("subscription", "Claude Code subscription only")),
        canon=_choice_canon(("auto", "api", "subscription"))),
    Setting(
        "CB_PACE", "Claude", "Agent pacing",
        "A multiplier on the pauses that make the agent's clicks visible. 1 is "
        "normal, 0 turns the choreography off, higher is slower.",
        "number", "1",
        "Next agent step",
        "Read from the environment at every pause, and writing a setting "
        "updates this process's environment, so a run in progress slows down "
        "or speeds up as it goes.",
        # agent.PACE_MAX: past this a typo becomes a hang rather than a pause.
        minimum=0, maximum=5, step=0.1),

    # -- Control API --------------------------------------------------------
    Setting(
        "CB_PORT", "Control API", "Control port",
        "The loopback port the agent control API listens on.",
        "number", "8765",
        "cbctl now, browser on restart",
        "cbctl and the MCP server read it when they run, so they will look "
        "here from now on. This window keeps listening on the port it bound at "
        "startup until it is restarted -- change both at once or they lose "
        "each other.",
        # Below 1024 needs root to bind, and a control API that fails to start
        # takes every agent tool down with it. Not a value this page will write.
        minimum=1024, maximum=65535),
    Setting(
        "CB_TOKEN", "Control API", "Control token",
        "A bearer token required on every control request. Optional -- the API "
        "is loopback-only either way -- and worth setting on a machine other "
        "people can log into.",
        "secret", "",
        "cbctl now, browser on restart",
        "cbctl and the MCP server will start sending it immediately. This "
        "window keeps requiring whatever it was started with.",
        allow_empty=True),
    Setting(
        "CB_URL", "Control API", "Control address",
        "Where cbctl and the MCP server look for a running browser. Leave it "
        "unset and they use 127.0.0.1 with the port above.",
        "text", "",
        "Next cbctl run",
        "Read by each client process as it starts; nothing in this window uses "
        "it.",
        allow_empty=True, check=_http_url),
    Setting(
        "CB_AUTOSTART", "Control API", "Start the browser on demand",
        "Lets the MCP server launch the browser when a tool call arrives and "
        "nothing is running, instead of answering with an error.",
        "bool", "1",
        "Next MCP call",
        "Read by the MCP server when it needs the browser; nothing in this "
        "window uses it.",
        truth=_client_off_words),
)

BY_KEY = {s.key: s for s in SETTINGS}


def get(key):
    """The Setting, or a ValueError naming the key that does not exist."""
    found = BY_KEY.get(key)
    if found is None:
        raise ValueError("no such setting %r" % (key,))
    return found


def effective(setting, path=None, environ=None):
    """(raw value, where it came from) for one setting.

    The file wins over the environment, which is `envfile.setting`'s rule and
    the reason a value changed here beats a stale one exported by whatever shell
    launched the browser.
    """
    environ = os.environ if environ is None else environ
    stored = envfile.values(path)
    if setting.key in stored:
        return stored[setting.key], "file"
    inherited = environ.get(setting.key)
    if inherited:
        return inherited, "environment"
    return setting.default, "default"


def describe_one(setting, path=None, environ=None):
    """What one setting looks like to the page and to the API.

    A secret reports only whether it is set. `CB_TOKEN` is a credential the
    control API accepts, and a settings page that renders it puts it in the
    DOM of a document any page-side bug can read -- so its value never leaves
    this function.
    """
    raw, source = effective(setting, path, environ)
    block = {
        "key": setting.key,
        "label": setting.label,
        "explain": setting.explain,
        "kind": setting.kind,
        "effect": setting.effect,
        "effect_note": setting.effect_note,
        "source": source,
        "in_file": source == "file",
        "default": setting.default,
        "writable": setting.writable,
        "unwritable_note": setting.unwritable_note,
    }
    if setting.kind == "secret":
        block["set"] = bool(raw)
    elif setting.kind == "bool":
        block["on"] = bool(setting.truth(raw))
    elif setting.kind == "choice":
        block["choices"] = [{"value": v, "label": lb} for v, lb in setting.choices]
        # A hand-edited typo shows as the default, because that is what the
        # code reading it will do with the value.
        block["value"] = (setting.canon(raw) if setting.canon else None)
        if block["value"] is None:
            block["value"] = setting.default
    else:
        block["value"] = raw
        block["minimum"] = setting.minimum
        block["maximum"] = setting.maximum
        block["step"] = setting.step
        block["unit"] = setting.unit
    return block


def describe(path=None, environ=None):
    """Every setting, grouped, for cb:settings and the `settings` op."""
    sections = []
    for title, note in SECTIONS:
        sections.append({
            "title": title,
            "note": note,
            "settings": [describe_one(s, path, environ)
                         for s in SETTINGS if s.section == title],
        })
    return {"sections": sections,
            "path": str(envfile.config_path() if path is None else path)}


def _writable(setting):
    """Refuse a setting the browser reports but will not write.

    Checked in `apply` and in `reset` alike: "reset to default" on a credential
    is deleting the user's credential from inside the browser, which is not a
    smaller act than writing one. envfile refuses both again underneath, but
    the message it raises names the mechanism; this one names the reason.
    """
    if setting.writable:
        return
    raise ValueError(
        "%s is not editable from inside the browser. %s"
        % (setting.label, setting.unwritable_note
           or "Edit the settings file by hand."))


def apply(key, value, path=None, environ=None):
    """Validate and store one setting. Returns what was written, or None when
    an empty value meant "remove the line"."""
    setting = get(key)
    _writable(setting)
    cleaned = setting.clean(value)
    if cleaned is None:
        envfile.remove(key, path=path, environ=environ)
        return None
    envfile.put(key, cleaned, path=path, environ=environ)
    return cleaned


def reset(key, path=None, environ=None):
    """Drop the line so the built-in default applies again. Returns the default
    that is now in force."""
    setting = get(key)
    _writable(setting)
    envfile.remove(key, path=path, environ=environ)
    return setting.default
