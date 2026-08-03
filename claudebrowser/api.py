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
       "URLs and titles.",
       call=lambda c, a: ("api_tabs", ()), tab=False),

    # Not an MCP tool: raising a window is a launcher's job, and an agent that
    # can yank the user's focus mid-task is a nuisance rather than a feature.
    Op("present", "/present", "POST", "Raise the browser window to the front.",
       call=lambda c, a: ("api_present", ()), tab=False, mcp=False),

    Op("open", "/open", "POST", "Open a URL in a new tab and wait for it to "
       "finish loading.",
       params=[Param("url", required=True, help="URL or search term."),
               Param("background", "boolean", "Open without focusing it.",
                     cli="opt", default=False)],
       call=lambda c, a: ("api_open", (a["url"], _truthy(a.get("background"), False),
                                       _truthy(a.get("wait")))),
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
       call=_js("TEXT")),

    Op("markdown", "/markdown", "GET", "Read the page as markdown, preserving "
       "headings, links and code blocks.",
       call=_js("MARKDOWN")),

    Op("links", "/links", "GET", "List every link on the page as absolute URLs "
       "with their labels.",
       call=_js("LINKS")),

    Op("html", "/html", "GET", "Get the page's full outer HTML. Prefer text unless "
       "you need the markup.",
       call=_js("HTML")),

    # Reader mode is a *display* change, not a read: it answers with a summary
    # of what it found, not the article. An agent that wants the prose still
    # calls text or markdown, which read the overlay like any other DOM.
    Op("reader", "/reader", "POST", "Toggle reader mode on the tab: strip the "
       "page to its article and re-render it for reading. Reports the resulting "
       "state.",
       params=[Param("font", "integer", "Body text size in px (12-34).", cli="opt"),
               Param("width", "integer", "Line measure in px (360-1100).", cli="opt")],
       call=lambda c, a: ("api_reader", (_tab(a), a.get("font"), a.get("width")))),

    Op("find", "/find", "GET", "Search the rendered page text for a regex and "
       "return matches with context.",
       params=[Param("q", required=True, help="Regex to search for.")],
       call=_js_call("find", "q")),

    Op("click", "/click", "POST", "Click the first element matching a CSS selector.",
       params=[Param("selector", required=True)],
       call=_js_call("click", "selector")),

    Op("fill", "/fill", "POST", "Set an input's value and dispatch input/change so "
       "frameworks notice.",
       params=[Param("selector", required=True), Param("value", required=True)],
       call=_js_call("fill", "selector", "value")),

    Op("eval", "/eval", "POST", "Evaluate JavaScript in the page and return its value.",
       params=[Param("js", required=True, help="JavaScript to evaluate.")],
       call=lambda c, a: ("api_eval", (_tab(a), a["js"]))),

    Op("console", "/console", "GET", "Read console output and uncaught errors "
       "captured on the page. Call this when debugging why a page misbehaves.",
       params=[Param("pattern", help="Regex filter over message text.", cli="opt")],
       call=lambda c, a: ("api_console", (_tab(a), a.get("pattern")))),

    # Cookies and caches. Reading is an MCP tool; clearing is not -- signing the
    # user out of every site they use is not a step an agent should be able to
    # take in pursuit of some other goal. `cbctl clear cookies` is one command
    # away for a person who means it.
    Op("storage", "/storage", "GET", "Report the cookie policy, how many domains "
       "have cookies, and how much disk the cache is using.",
       call=lambda c, a: ("api_storage", ()), tab=False),

    Op("clear", "/clear", "POST", "Delete stored browsing data.",
       params=[Param("kind", help="cache, cookies, storage, or all.",
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
]

BY_NAME = {op.name: op for op in OPS}
BY_ROUTE = {op.route: op for op in OPS}

#: cbctl exposes `shot` and `go` as friendlier names for two operations whose
#: API names read badly at a shell prompt.
CLI_ALIASES = {"shot": "screenshot", "go": "navigate"}


def mcp_tools():
    return [op.mcp_tool() for op in OPS if op.mcp]
