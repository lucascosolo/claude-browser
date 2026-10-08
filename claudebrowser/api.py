"""One description of what the browser can be asked to do.

There were four. The HTTP routes in control.py, the subcommands in `cbctl`, the
tool table in `cb-mcp`, and the model-facing tools in agent.py all described the
same operations, and they had already drifted: `cb-mcp` was missing `forward`,
`wait` and `health` outright, so an agent driving the browser over MCP could not
wait for a load it had started.

Everything except agent.py is generated from the table below. agent.py stays
separate on purpose -- its names and descriptions are prompt engineering aimed
at a model choosing a next step ("find_in_page is much cheaper than read_page"),
not an API reference, and collapsing the two would force one wording to serve
two very different readers.

Each Op says, once:

    name        what cbctl calls it, and (as browser_<name>) what MCP calls it
    route       the HTTP path control.py serves
    method      GET or POST
    summary     one line, shown in `cbctl --help` and as the MCP tool description
    params      ordered Param list -- positional on the CLI, JSON schema for MCP
    call        the api_* method on Browser, and how to build its arguments

`tab` is implicit everywhere except /tabs and /health: omitting it means "the tab
the user is looking at", which is what an agent almost always wants.
"""

TAB_HELP = "Tab id; omit for the focused tab."

#: How long the control API waits on an operation that loads a page.
#:
#: Generous, because page loads are queued rather than run in parallel -- see
#: Browser._admit. A request can legitimately spend a minute waiting its turn on
#: a slow machine, and this has to outlast the browser's own queue expiry
#: (QUEUE_TOTAL_S) or the caller gets "timed out" where the browser had a real
#: reason ready to hand back.
LOAD_TIMEOUT = 150


class Param:
    """One argument to an operation.

    `kind` is the JSON-schema type. `cli` is how it reaches cbctl: "arg" for a
    required positional, "optarg" for an optional one, "opt" for a --flag.
    """

    __slots__ = ("name", "kind", "help", "required", "cli", "default")

    def __init__(self, name, kind="string", help="", required=False, cli="arg",
                 default=None):
        self.name = name
        self.kind = kind
        self.help = help
        self.required = required
        self.cli = cli
        self.default = default

    def schema(self):
        block = {"type": self.kind}
        if self.help:
            block["description"] = self.help
        return block


class Op:
    __slots__ = ("name", "route", "method", "summary", "params", "call", "tab",
                 "timeout", "mcp")

    def __init__(self, name, route, method, summary, call, params=(), tab=True,
                 timeout=45, mcp=True):
        self.name = name
        self.route = route
        self.method = method
        self.summary = summary
        self.call = call          # (control, args) -> (browser_method, arg tuple)
        self.params = list(params)
        self.tab = tab            # takes an optional tab id
        self.timeout = timeout
        self.mcp = mcp            # exposed as an MCP tool

    # -- projections onto each surface --------------------------------------

    def schema(self):
        """JSON schema for the MCP tool, and the shape cbctl validates against."""
        props = {p.name: p.schema() for p in self.params}
        if self.tab:
            props["tab"] = {"type": "integer", "description": TAB_HELP}
        return {
            "type": "object",
            "properties": props,
            "required": [p.name for p in self.params if p.required],
        }

    def mcp_tool(self):
        return {"name": "browser_" + self.name,
                "description": self.summary,
                "inputSchema": self.schema()}


def _extract():
    from . import extract

    return extract


def _js(name):
    """An op whose whole implementation is a snippet from extract.py."""
    def build(_control, args):
        from . import extract

        return "api_eval", (_tab(args), getattr(extract, name))
    return build


def _js_call(name, *arg_names):
    """An op that calls extract.<name>(...) with arguments from the request."""
    def build(_control, args):
        from . import extract

        return "api_eval", (_tab(args),
                            getattr(extract, name)(*[args[a] for a in arg_names]))
    return build


def _js_scoped(name, *lead):
    """A read op with an optional `selector`. Without one it is the constant,
    byte for byte, so callers that never pass a selector are unchanged."""
    def build(_control, args):
        from . import extract

        fn = getattr(extract, name)
        return "api_eval", (_tab(args), fn(*[args[a] for a in lead],
                                           args.get("selector") or None))
    return build


def _tables(_control, args):
    from . import extract

    return "api_eval", (_tab(args), extract.tables(args.get("selector") or None,
                                                   int(args.get("limit") or 200)))


def _tab(args):
    raw = args.get("tab")
    return int(raw) if raw not in (None, "") else None


def _truthy(value, default=True):
    if value is None:
        return default
    return str(value).lower() not in ("0", "false", "no", "")


# -- the table --------------------------------------------------------------
# Ordered as a person would learn them: look, go, read, act.

OPS = [
    Op("health", "/health", "GET", "Check the browser is up.",
       call=None, tab=False, mcp=False),

    Op("tabs", "/tabs", "GET", "List the browser's open tabs with their ids, "
       "URLs and titles. A tab dropped to free memory is marked discarded and "
       "carries a short summary of what it held.",
       call=lambda c, a: ("api_tabs", ()), tab=False),

    # Not an MCP tool: raising a window is a launcher's job, and an agent that
    # can yank the user's focus mid-task is a nuisance rather than a feature.
    Op("present", "/present", "POST", "Raise the browser window to the front.",
       call=lambda c, a: ("api_present", ()), tab=False, mcp=False),

    # Not an MCP tool: an agent restarting the browser it is being driven
    # through ends its own session mid-task. The browser restarts itself when
    # the checkout moves and it is idle (update.py); this is the override for a
    # person at a shell who wants it now.
    Op("restart", "/restart", "POST", "Restart the browser onto the code "
       "currently in its checkout, reopening this session's tabs.",
       call=lambda c, a: ("api_restart", ()), tab=False, mcp=False),

    Op("open", "/open", "POST", "Open a URL in a new tab and wait for it to "
       "finish loading.",
       params=[Param("url", required=True, help="URL or search term."),
               Param("background", "boolean", "Open without focusing it.",
                     cli="opt", default=False),
               # Asking for privacy is possible; giving it up is not. A tab
               # opened from a private one is private whatever this says --
               # see api_open -- so the flag can only ever add the property.
               Param("private", "boolean", "Open it as a private tab: its own "
                     "ephemeral session, nothing written down, and no page "
                     "data sent to Claude.", cli="opt", default=False)],
       call=lambda c, a: ("api_open", (a["url"], _truthy(a.get("background"), False),
                                       _truthy(a.get("wait")),
                                       _truthy(a.get("private"), False))),
       tab=False, timeout=LOAD_TIMEOUT),

    Op("navigate", "/navigate", "POST", "Navigate an existing tab to a URL and "
       "wait for the load.",
       params=[Param("url", required=True)],
       call=lambda c, a: ("api_navigate", (_tab(a), a["url"], _truthy(a.get("wait")))),
       timeout=LOAD_TIMEOUT),

    Op("back", "/back", "POST", "Go back one entry in the tab's history.",
       call=lambda c, a: ("api_history", (_tab(a), -1, _truthy(a.get("wait")))),
       timeout=LOAD_TIMEOUT),

    Op("forward", "/forward", "POST", "Go forward one entry in the tab's history.",
       call=lambda c, a: ("api_history", (_tab(a), 1, _truthy(a.get("wait")))),
       timeout=LOAD_TIMEOUT),

    Op("reload", "/reload", "POST", "Reload the tab and wait for the load.",
       call=lambda c, a: ("api_reload", (_tab(a), _truthy(a.get("wait")))),
       timeout=LOAD_TIMEOUT),

    Op("wait", "/wait", "POST", "Block until the tab's current load finishes.",
       call=lambda c, a: ("api_wait", (_tab(a),)), timeout=120),

    Op("wait-for", "/wait/for", "POST", "Block until a condition holds on the "
       "page: an element is visible (or, with gone, absent), the page text "
       "matches a regex, or the URL matches a regex. Use after a click that "
       "triggers a slow update. Fails with a timeout error if nothing matches.",
       params=[Param("selector", help="CSS selector or @ref that must be visible.",
                     cli="opt"),
               Param("text", help="Regex the page text must match.", cli="opt"),
               Param("url", help="Regex the URL must match.", cli="opt"),
               Param("gone", "boolean", "Wait for the selector to disappear instead.",
                     cli="opt", default=False),
               Param("timeout", "integer", "Seconds to wait (default 20, max 120).",
                     cli="opt")],
       call=lambda c, a: ("api_wait_for", (_tab(a), a.get("selector") or None,
                                           a.get("text") or None, a.get("url") or None,
                                           _truthy(a.get("gone"), False),
                                           a.get("timeout"))),
       timeout=130),

    Op("close", "/close", "POST", "Close a tab.",
       call=lambda c, a: ("api_close", (_tab(a),))),

    # The resource ops. `machine` is the one an agent should reach for after an
    # open is refused: it says whether to wait, discard, or give up, instead of
    # leaving retry-or-not to guesswork.
    Op("machine", "/machine", "GET", "Report free memory, swap and CPU load, the "
       "current tab limit, and how many tabs could be freed. Call this when an "
       "open or navigate is refused for pressure.",
       call=lambda c, a: ("api_machine", ()), tab=False),

    Op("discard", "/discard", "POST", "Free a tab's memory but keep the tab; it "
       "reloads on next use. Use this instead of closing a tab you still want.",
       call=lambda c, a: ("api_discard", (_tab(a),))),

    Op("text", "/text", "GET", "Read the page as clean text, with nav/script/footer "
       "chrome stripped. Use this first when verifying what a page says.",
       params=[Param("selector", help="Limit to this element: CSS selector or @ref.", cli="opt")],
       call=_js_scoped("text")),

    Op("markdown", "/markdown", "GET", "Read the page as markdown, preserving "
       "headings, links and code blocks.",
       params=[Param("selector", help="Limit to this element: CSS selector or @ref.", cli="opt")],
       call=_js_scoped("markdown")),

    Op("links", "/links", "GET", "List every link on the page as absolute URLs "
       "with their labels.",
       params=[Param("selector", help="Limit to this element: CSS selector or @ref.", cli="opt")],
       call=_js_scoped("links")),

    Op("html", "/html", "GET", "Get the page's full outer HTML. Prefer text unless "
       "you need the markup.",
       params=[Param("selector", help="Limit to this element: CSS selector or @ref.", cli="opt")],
       call=_js_scoped("html")),

    Op("tables", "/tables", "GET", "Read the page's tables as structured rows: "
       "each has a caption, headers and rows of cell text. Cells that span "
       "rows or columns are not expanded.",
       params=[Param("selector", help="A table, or an element containing tables: "
                     "CSS selector or @ref. Default: every table.", cli="opt"),
               Param("limit", "integer", "Rows per table (default 200).", cli="opt")],
       call=_tables),

    # Reader mode is a *display* change, not a read: it answers with a summary
    # of what it found, not the article. An agent that wants the prose still
    # calls text or markdown, which read the overlay like any other DOM.
    Op("reader", "/reader", "POST", "Toggle reader mode on the tab: strip the "
       "page to its article and re-render it for reading. Reports the resulting "
       "state.",
       params=[Param("font", "integer", "Body text size in px (12-34).", cli="opt"),
               Param("width", "integer", "Line measure in px (360-1100).", cli="opt")],
       call=lambda c, a: ("api_reader", (_tab(a), a.get("font"), a.get("width")))),

    # Like reader, a *display* change that reports state rather than content.
    # An agent wanting the prose still calls text or markdown, which read the
    # decluttered DOM like any other -- the sheet hides nodes, it never removes
    # them, so nothing an agent can reach disappears because this is on.
    Op("simplify", "/simplify", "POST", "Toggle the declutter sheet for sites "
       "with a rule (YouTube and friends): hide Shorts, shelves, sidebars, "
       "comments and ad slots. Reports the resulting state.",
       call=lambda c, a: ("api_simplify", (_tab(a),))),

    Op("find", "/find", "GET", "Search the rendered page text for a regex and "
       "return matches with context.",
       params=[Param("q", required=True, help="Regex to search for."),
               Param("selector", help="Limit to this element: CSS selector or @ref.", cli="opt")],
       call=_js_scoped("find", "q")),

    Op("snapshot", "/snapshot", "GET", "List the page's interactive elements "
       "(inputs, buttons, links) as a compact, ref-indexed manifest -- use this "
       "instead of `text` before filling a form. Each ref (like @e7) can be "
       "passed to `click`/`fill` in place of a CSS selector.",
       call=lambda c, a: ("api_snapshot", (_tab(a),))),

    Op("click", "/click", "POST", "Click the first element matching a CSS "
       "selector, or a @ref from `snapshot` (e.g. \"@e7\").",
       params=[Param("selector", required=True)],
       call=_js_call("click", "selector")),

    Op("scroll", "/scroll", "POST", "Scroll the page: `to` is top, bottom, a CSS "
       "selector or a @ref (centred in view), or `by` is a signed pixel count. "
       "Answers the scroll position and whether the page bottom is reached. "
       "Give exactly one of to, by.",
       params=[Param("to", help="top, bottom, a CSS selector or @ref.", cli="opt"),
               Param("by", "integer", "Pixels to scroll; negative scrolls up.",
                     cli="opt")],
       call=lambda c, a: ("api_scroll", (_tab(a), a.get("to") or None,
                                         None if a.get("by") in (None, "")
                                         else int(a["by"])))),

    Op("fill", "/fill", "POST", "Set an input's value and dispatch input/change "
       "so frameworks notice. `selector` is a CSS selector or a @ref from "
       "`snapshot`.",
       params=[Param("selector", required=True), Param("value", required=True)],
       call=_js_call("fill", "selector", "value")),

    Op("fill-many", "/fill/many", "POST", "Fill several fields in one call. "
       "`fields` is a JSON object mapping each selector (or @ref from "
       "`snapshot`) to a value -- use \"{profile:KEY}\" as a value to fill "
       "from the stored profile without ever seeing the value yourself.",
       params=[Param("fields", required=True,
                     help="JSON object: {selector_or_ref: value}.")],
       call=lambda c, a: ("api_fill_many", (_tab(a), a["fields"]))),

    Op("press", "/press", "POST", "Press a key or combo (Enter, Escape, Tab, "
       "Shift+Tab, Ctrl+K, ArrowDown, a) on `selector`, else the focused element. "
       "Also applies the key's default action: Enter submits the form or "
       "activates a button/link, Tab moves focus, Escape blurs.",
       params=[Param("key", required=True, help="Key combo, e.g. Enter or Ctrl+K."),
               Param("selector", help="CSS selector or @ref to focus first.",
                     cli="opt")],
       call=lambda c, a: ("api_eval", (_tab(a), _extract().press(
           a["key"], a.get("selector") or None)))),

    Op("type", "/type", "POST", "Type text into `selector`, else the focused "
       "element, as real input (works in contenteditable too). Appends at the "
       "caret; use `fill` to replace a field's value.",
       params=[Param("text", required=True),
               Param("selector", help="CSS selector or @ref to focus first.",
                     cli="opt")],
       call=lambda c, a: ("api_eval", (_tab(a), _extract().type_text(
           a["text"], a.get("selector") or None)))),

    Op("select", "/select", "POST", "Choose an option in a <select> by value or "
       "visible label (a JSON array for <select multiple>), or tick/untick a "
       "checkbox or radio with `checked`. Fires input and change.",
       params=[Param("selector", required=True),
               Param("value", help="Option value or label.", cli="optarg"),
               Param("checked", "boolean", "Checkbox/radio state to set.",
                     cli="opt")],
       call=lambda c, a: ("api_eval", (_tab(a), _extract().select(
           a["selector"], a.get("value"),
           None if a.get("checked") in (None, "") else _truthy(a["checked"]))))),

    Op("hover", "/hover", "POST", "Move the pointer onto an element without "
       "clicking, to open hover menus and tooltips.",
       params=[Param("selector", required=True)],
       call=_js_call("hover", "selector")),

    Op("submit", "/submit", "POST", "Submit a form the way a user would "
       "(validation and submit handlers run). `selector` is the form or a field "
       "in it; default the first form. Waits for the page load it starts.",
       params=[Param("selector", cli="optarg",
                     help="Form or field inside one; default the first form.")],
       call=lambda c, a: ("api_submit", (_tab(a), a.get("selector") or None)),
       timeout=LOAD_TIMEOUT),

    Op("eval", "/eval", "POST", "Evaluate JavaScript in the page and return its value.",
       params=[Param("js", required=True, help="JavaScript to evaluate.")],
       call=lambda c, a: ("api_eval", (_tab(a), a["js"]))),

    Op("console", "/console", "GET", "Read console output and uncaught errors "
       "captured on the page. Call this when debugging why a page misbehaves.",
       params=[Param("pattern", help="Regex filter over message text.", cli="opt")],
       call=lambda c, a: ("api_console", (_tab(a), a.get("pattern")))),

    Op("blocked", "/blocked", "GET", "Check whether the page is showing a "
       "CAPTCHA or anti-bot challenge instead of its real content. Call this "
       "when a click or fill had no visible effect. This never attempts to "
       "solve or bypass a challenge -- it only reports one.",
       call=_js("BLOCKED")),

    # Cookies and caches. Reading is an MCP tool; clearing is not -- signing the
    # user out of every site they use is not a step an agent should be able to
    # take in pursuit of some other goal. `cbctl clear cookies` is one command
    # away for a person who means it.
    Op("storage", "/storage", "GET", "Report the cookie policy, how many domains "
       "have cookies, and how much disk the cache is using.",
       call=lambda c, a: ("api_storage", ()), tab=False),

    Op("clear", "/clear", "POST", "Delete stored browsing data.",
       params=[Param("kind", help="cache, cookies, storage, pagetext, or all.",
                     cli="optarg")],
       call=lambda c, a: ("api_clear", (a.get("kind") or "cache",)),
       tab=False, mcp=False, timeout=90),

    # Search over what has already been read. Not a web search: it only sees
    # pages this browser loaded, which is exactly why it is useful -- "the page
    # about X I had open yesterday" is a question no search engine can answer.
    Op("recall", "/recall", "GET", "Search the full text of pages already visited "
       "in this browser and return ranked matches with snippets. Use this to find "
       "a page seen earlier instead of re-opening tabs to look for it.",
       params=[Param("q", required=True, help="Words to look for."),
               Param("limit", "integer", "Maximum matches (default 10).",
                     cli="opt")],
       call=lambda c, a: ("api_recall", (a["q"], a.get("limit"))),
       tab=False),

    Op("screenshot", "/screenshot", "GET", "Save a PNG of the visible viewport to a "
       "path on disk.",
       params=[Param("path", help="Write here; omit to stream the PNG to stdout.",
                     cli="optarg")],
       call=lambda c, a: ("api_screenshot", (_tab(a), a.get("path"))),
       timeout=60),

    # Playbooks: a saved, ordered list of the operations above. Nothing new is
    # described here -- a playbook's steps are entries from this same table,
    # which is why recording needs no per-op support and replay needs no
    # interpreter. See playbooks.py.
    Op("playbook-record", "/playbook/record", "POST",
       "Start or stop recording the operations you perform into a named "
       "playbook. Credential fields are never captured.",
       params=[Param("action", required=True,
                     help="start, stop, cancel or status."),
               Param("name", cli="optarg",
                     help="Playbook name; required for start.")],
       call=lambda c, a: ("api_playbook_record", (a["action"], a.get("name"))),
       tab=False),

    Op("playbook-list", "/playbook/list", "GET",
       "List saved playbooks with their step counts, and say whether a "
       "recording is in progress.",
       call=lambda c, a: ("api_playbook_list", ()), tab=False),

    # Generous timeout: a playbook is several operations in series, and any of
    # them may be a page load that queues behind others (see Browser._admit).
    Op("playbook-run", "/playbook/run", "POST",
       "Replay a saved playbook against the live browser, one step at a time.",
       params=[Param("name", required=True, help="Playbook to replay.")],
       call=lambda c, a: ("api_playbook_run", (a["name"],)),
       tab=False, timeout=600),

    # Not an MCP tool, for the same reason `clear` is not: deleting something
    # the user recorded is not a step an agent should take in pursuit of some
    # other goal.
    Op("playbook-delete", "/playbook/delete", "POST", "Delete a saved playbook.",
       params=[Param("name", required=True, help="Playbook to delete.")],
       call=lambda c, a: ("api_playbook_delete", (a["name"],)),
       tab=False, mcp=False),

    # Not an MCP tool: the persona is the user's own preference about how the
    # panel answers *them*, it is written to their settings file, and an agent
    # driving the browser has no answer of its own coming out of that panel.
    Op("persona", "/persona", "POST",
       "Report the Claude panel's persona, or switch to another one.",
       params=[Param("name", cli="optarg",
                     help="off, developer, researcher, critic or translator; "
                          "omit to report the current one.")],
       call=lambda c, a: ("api_persona", (a.get("name"),)),
       tab=False, mcp=False),

    # Not an MCP tool, for the same reason `settings` is not, and the reason is
    # sharper here: this op decides whether the browser's traffic -- including
    # the agent's own requests to Anthropic -- leaves from the user's address
    # or from their exit host. An agent that can turn it off can deanonymize
    # the person driving it in pursuit of some unrelated goal, and one that can
    # turn it on can block every load it was asked to make. Neither belongs in
    # a tool list. `cbctl vpn` is one command away for a person who means it.
    #
    # Generous timeout: turning it on ends with an external HTTPS fetch through
    # the proxy, run three services deep before it gives up, and the op does not
    # answer until that verdict is in -- reporting "connecting" and leaving the
    # caller to poll would be a worse API for the one caller it has.
    Op("vpn", "/vpn", "POST",
       "Report VPN Mode, or turn it on or off. On, the browser's page loads and "
       "its Claude API calls go through the configured proxy and the answer "
       "carries the exit address an outside service actually saw.",
       params=[Param("action", cli="optarg",
                     help="on, off, check; omit to report the current state.")],
       call=lambda c, a: ("api_vpn", (a.get("action"),)),
       tab=False, mcp=False, timeout=90),

    # Not an MCP tool, and for a stronger version of the reason `persona` is
    # not: these are the user's own preferences about their browser, several of
    # them decide what is sent to Anthropic and what is stripped first, and one
    # is the token guarding this very API. An agent driving the browser has no
    # business rewriting the rules it is being driven under.
    Op("settings", "/settings", "POST",
       "Report the browser's settings, or change one of them.",
       params=[Param("name", cli="optarg",
                     help="Setting key (CB_THEME, CB_BLOCK, ...); omit to "
                          "report them all."),
               Param("value", cli="optarg", help="New value."),
               Param("reset", "boolean", "Remove the line so the built-in "
                     "default applies again.", cli="opt", default=False)],
       call=lambda c, a: ("api_settings", (a.get("name"), a.get("value"),
                                           _truthy(a.get("reset"), False))),
       tab=False, mcp=False),

    # Not an MCP tool for `profile-set`, and for the same reason `settings`
    # and `persona` aren't: this is the user's own data, and an agent acting
    # toward some other goal has no business rewriting their name or address
    # as a side effect. `profile` itself IS exposed -- an agent needs to read
    # it to fill out forms, which is the entire point of storing it.
    Op("profile", "/profile", "GET",
       "Report the stored personal-info profile (name, address, contact "
       "fields) used to fill out forms.",
       call=lambda c, a: ("api_profile", ()), tab=False),

    Op("profile-set", "/profile/set", "POST",
       "Set or delete one field in the personal-info profile.",
       params=[Param("key", required=True),
               Param("value", cli="optarg",
                     help="New value; omit to delete the field.")],
       call=lambda c, a: ("api_profile_set", (a["key"], a.get("value"))),
       tab=False, mcp=False),

    Op("bookmarks", "/bookmarks", "GET",
       "List saved bookmarks, optionally filtered by a search term.",
       params=[Param("q", cli="opt", help="Filter text; omit to list all.")],
       call=lambda c, a: ("api_bookmarks", (a.get("q"),)), tab=False),

    Op("bookmark-add", "/bookmark/add", "POST",
       "Bookmark a page -- the current tab by default, or a given URL.",
       params=[Param("url", cli="optarg", help="URL to bookmark; omit for "
                     "the tab's own."),
               Param("title", cli="optarg", help="Title to store; omit to "
                     "use the tab's own.")],
       call=lambda c, a: ("api_bookmark_add",
                          (_tab(a), a.get("url"), a.get("title")))),

    # Not an MCP tool, for the same reason `clear` is not: removing something
    # the user saved is not a step an agent should take in pursuit of some
    # other goal.
    Op("bookmark-remove", "/bookmark/remove", "POST", "Remove a bookmark.",
       params=[Param("url", required=True)],
       call=lambda c, a: ("api_bookmark_remove", (a["url"],)),
       tab=False, mcp=False),

    Op("history", "/history", "GET",
       "Search browsing history. A search term is required -- this is a "
       "search, not a full listing, the same posture as `recall`.",
       params=[Param("q", required=True, help="Words to look for."),
               Param("limit", "integer", "Maximum matches (default 50).",
                     cli="opt")],
       call=lambda c, a: ("api_history", (a["q"], a.get("limit"))), tab=False),

    # Not an MCP tool, for the same reason `clear` is not: signing the user
    # out of every site's memory of them is not a step an agent should take
    # in pursuit of some other goal. `cbctl history-clear` is one command
    # away for a person who means it.
    Op("history-clear", "/history/clear", "POST", "Delete all browsing history.",
       call=lambda c, a: ("api_history_clear", ()), tab=False, mcp=False),

    Op("downloads", "/downloads", "GET",
       "List this session's downloads and their status.",
       call=lambda c, a: ("api_downloads", ()), tab=False),

    # Not an MCP tool: this is a user-triggered one-time migration of their
    # own Chrome data, never a step an agent should take toward some other
    # goal, and it is the one op in this table that ever holds a decrypted
    # password -- see chrome_import.py and Browser.api_import_chrome.
    Op("import-chrome", "/import-chrome", "POST",
       "Import bookmarks, history and saved passwords from the local Google "
       "Chrome profile. Existing entries are never overwritten -- this only "
       "fills gaps. Reports counts only, never values.",
       params=[Param("passwords", "boolean", "Import saved passwords.",
                     cli="opt", default=False),
               Param("bookmarks", "boolean", "Import bookmarks.",
                     cli="opt", default=False),
               Param("history", "boolean", "Import browsing history.",
                     cli="opt", default=False)],
       call=lambda c, a: ("api_import_chrome", (_import_chrome_kinds(a),)),
       tab=False, mcp=False, timeout=90),
    Op("save-password", "/passwords/save", "POST",
       "Save one credential straight into the password vault. Not an agent "
       "tool -- the one path for a human to hand this browser a password "
       "without typing it anywhere an agent or a chat transcript can see.",
       params=[Param("origin", "string", "Site origin/URL this login is for.",
                     required=True, cli="arg"),
               Param("username", "string", "Username or email for this login.",
                     required=True, cli="arg"),
               Param("password", "string", "The password value.",
                     required=True, cli="secret")],
       call=lambda c, a: ("api_save_password",
                          (a["origin"], a.get("username") or "", a["password"])),
       tab=False, mcp=False),
]

BY_NAME = {op.name: op for op in OPS}
BY_ROUTE = {op.route: op for op in OPS}

#: cbctl exposes `shot` and `go` as friendlier names for two operations whose
#: API names read badly at a shell prompt.
CLI_ALIASES = {"shot": "screenshot", "go": "navigate"}


def mcp_tools():
    return [op.mcp_tool() for op in OPS if op.mcp]


def _import_chrome_kinds(args):
    """No flag given at all means "all three" -- cbctl import-chrome with no
    arguments is the common case, not an error asking the user to spell out
    every kind."""
    requested = {k for k in ("passwords", "bookmarks", "history")
                 if _truthy(args.get(k), False)}
    return sorted(requested) if requested else ["passwords", "bookmarks", "history"]
