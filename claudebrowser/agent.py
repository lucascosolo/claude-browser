"""The command bar: give Claude a goal and let it drive the browser.

This is the feature the control API was always for, turned inward -- the same
navigate/read/click primitives an external agent gets over HTTP, handed to a
tool-use loop running inside the browser itself.

Two design choices worth stating:

  * It drives the *visible* window, not a hidden one. You watch each step land
    in the tab you are looking at, on your own logged-in session. That is the
    point of the whole project, and it is also the safety story: nothing happens
    off-screen.
  * Tool results are truncated hard before going back to the model. A page's
    text can be hundreds of KB, and an agent loop that feeds three of those back
    verbatim will blow the context window and the user's budget in four steps.
"""

import json

from . import ai, extract

MAX_STEPS = 14
PAGE_CHARS = 15_000     # per read_page result fed back to the model
RESULT_CHARS = 20_000   # hard ceiling on any single tool result

SYSTEM = """You are driving a real web browser on the user's behalf. The window \
is visible to them and uses their existing logins and cookies.

Work in small steps: look before you act. Read a page before clicking in it, and \
prefer find_in_page or page_links over re-reading a whole page you have already \
seen. Navigate directly to a URL when you know it rather than searching for it.

You are on a slow machine, so every page load costs the user real seconds. Do not \
browse speculatively; each navigation should be one you can justify.

Be careful with side effects. Clicking and typing are available because they are \
often necessary, but do not submit forms, post content, make purchases, or change \
any account state unless the user's goal plainly asks for it. If a goal seems to \
require that, stop and say what you would do instead.

When you have the answer, state it directly. Do not narrate the steps you took \
unless the user asked how you got there."""

TOOLS = [
    {
        "name": "navigate",
        "description": "Navigate the current tab to a URL and wait for it to load. "
                       "Returns the resulting title and URL (which may differ after "
                       "a redirect).",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "read_page",
        "description": "Read the current page as clean text, with navigation, scripts "
                       "and footers stripped. Truncated for length. This is the main "
                       "way to see what a page says.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_in_page",
        "description": "Search the rendered page for a regex and return matches with "
                       "surrounding context. Much cheaper than read_page when you know "
                       "what you are looking for.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "page_links",
        "description": "List the links on the current page as absolute URLs with their "
                       "visible text. Use this to decide where to go next.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "click",
        "description": "Click the first element matching a CSS selector, then wait "
                       "briefly for any navigation it triggers.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "type_text",
        "description": "Type a value into an input or textarea matching a CSS selector. "
                       "Does not submit; click the submit control separately.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
        },
    },
    {
        "name": "open_tab",
        "description": "Open a URL in a new background tab and wait for it to load. "
                       "Use when you need to keep the current page.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "list_tabs",
        "description": "List the open tabs with their ids, URLs and titles.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class Agent:
    """Runs the tool loop. `call` marshals onto the GTK thread and blocks;
    `emit` renders a line of progress for the user."""

    def __init__(self, call, emit):
        self.call = call
        self.emit = emit
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    # -- tool implementations ----------------------------------------------

    def _eval(self, script):
        result = self.call("api_eval", None, script)
        if not result.get("ok"):
            return {"error": result.get("error", "eval failed")}
        return result.get("result")

    def dispatch(self, name, args):
        if name == "navigate":
            r = self.call("api_navigate", None, args["url"], True, timeout=120)
            return {k: r.get(k) for k in ("ok", "url", "title", "error") if k in r}
        if name == "open_tab":
            r = self.call("api_open", args["url"], True, True, timeout=120)
            return {k: r.get(k) for k in ("ok", "id", "url", "title", "error") if k in r}
        if name == "list_tabs":
            return self.call("api_tabs")
        if name == "read_page":
            page = self._eval(extract.TEXT) or {}
            if isinstance(page, dict) and page.get("text"):
                page = dict(page)
                if len(page["text"]) > PAGE_CHARS:
                    page["text"] = page["text"][:PAGE_CHARS] + "\n[truncated]"
            return page
        if name == "find_in_page":
            return self._eval(extract.find(args["pattern"]))
        if name == "page_links":
            links = self._eval(extract.LINKS) or {}
            if isinstance(links, dict) and len(links.get("links", [])) > 120:
                links = dict(links, links=links["links"][:120], truncated=True)
            return links
        if name == "click":
            out = self._eval(extract.click(args["selector"]))
            # A click often starts a navigation; give it a moment so the next
            # read_page sees the new page rather than the old one.
            self.call("api_wait", None, timeout=60)
            return out
        if name == "type_text":
            return self._eval(extract.fill(args["selector"], args["value"]))
        return {"error": "unknown tool %r" % name}

    # -- the loop -----------------------------------------------------------

    def run(self, goal):
        messages = [{"role": "user", "content": goal}]

        for step in range(MAX_STEPS):
            if self.cancelled:
                self.emit("\n[stopped]\n")
                return
            try:
                response = ai.tool_turn(messages, TOOLS, SYSTEM)
            except ai.NoKey as e:
                return self.emit(str(e) + "\n")
            except Exception as e:
                return self.emit("\n[error] %s\n" % e)

            if response.get("type") == "error":
                return self.emit("\n[api error] %s\n"
                                 % response.get("error", {}).get("message", "unknown"))

            content = response.get("content", [])
            # Echoed back verbatim on the next turn -- this carries the thinking
            # blocks Claude Opus 5 requires to be returned unmodified.
            messages.append({"role": "assistant", "content": content})

            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.emit(block["text"].rstrip() + "\n")

            if response.get("stop_reason") == "refusal":
                return self.emit("\n[the model declined this request]\n")
            if response.get("stop_reason") != "tool_use":
                return

            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                if self.cancelled:
                    self.emit("\n[stopped]\n")
                    return
                self.emit("  → %s\n" % _describe(block["name"], block.get("input", {})))
                try:
                    output = self.dispatch(block["name"], block.get("input", {}))
                except Exception as e:
                    output = {"error": repr(e)}
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(output, ensure_ascii=False)[:RESULT_CHARS],
                })
            messages.append({"role": "user", "content": results})

        self.emit("\n[stopped after %d steps]\n" % MAX_STEPS)


def _describe(name, args):
    """A short human-readable line per tool call, for the progress pane."""
    if name in ("navigate", "open_tab"):
        return "%s %s" % (name, args.get("url", ""))
    if name == "click":
        return "click %s" % args.get("selector", "")
    if name == "type_text":
        return "type into %s" % args.get("selector", "")
    if name == "find_in_page":
        return "find %r" % args.get("pattern", "")
    return name
