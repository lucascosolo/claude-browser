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
import os
import time

from . import ai, extract

MAX_STEPS = 14
PAGE_CHARS = 15_000     # per read_page result fed back to the model
RESULT_CHARS = 20_000   # hard ceiling on any single tool result
TOTAL_RESULT_CHARS = 120_000  # ceiling across the whole run
REPEAT_LIMIT = 3        # identical tool calls before we call it a loop

# -- pacing ------------------------------------------------------------------
#
# An unpaced step lands in a few milliseconds: the page jumps and the answer
# appears, with nothing in between to watch. These pauses buy the choreography
# in extract.py time to be seen -- the cursor travelling to a link, dipping on
# the click -- at a cost of well under a second per acting step.
#
# They are slept on the agent's worker thread, never on the GTK main loop.
# Agent.run() only ever runs under the thread Browser.run_agent starts (the
# same rule that makes Browser.call_sync safe), so a sleep here stalls this
# loop and nothing else; sleeping on the main loop would freeze the window.
PACE_ENV = "CB_PACE"
PACE_MAX = 5.0          # a typo like CB_PACE=1000 should not hang the run
STEP_S = 0.10                                                  # between steps
HOVER_S = (extract.SCROLL_SETTLE_MS + extract.TRAVEL_MS) / 1000.0  # point -> act
ACT_S = 0.15                                                   # after acting


def pace_scale(raw=None):
    """Read CB_PACE into a multiplier on every pause. Unset means 1.0 (on).

    Anything unparseable is treated as the default rather than as zero: the
    knob exists to slow the agent down or turn the show off deliberately, and a
    typo should not silently remove the visual feedback the user asked for.
    """
    if raw is None:
        raw = os.environ.get(PACE_ENV, "")
    raw = (raw or "").strip().lower()
    if raw in ("", "1", "on", "yes", "true"):
        return 1.0
    if raw in ("0", "off", "no", "false"):
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    if value != value:      # NaN compares unequal to itself
        return 1.0
    return max(0.0, min(PACE_MAX, value))

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

    def __init__(self, call, emit, pace=None):
        self.call = call
        self.emit = emit
        self.cancelled = False
        self.spent = 0          # characters of tool output fed back so far
        self.seen = {}          # (tool, args) -> count, for loop detection
        self.pace = pace_scale() if pace is None else float(pace)

    def cancel(self):
        self.cancelled = True

    def delay_for(self, seconds):
        """Seconds this run should actually pause for a nominal `seconds`."""
        return max(0.0, seconds * self.pace)

    def _pause(self, seconds):
        # Cancelled runs skip the theatre: the user asked it to stop, so the
        # only thing left to be visible about is stopping.
        delay = self.delay_for(seconds)
        if delay > 0 and not self.cancelled:
            time.sleep(delay)

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
            # Hover first, then click: two evals with a pause between them,
            # because a page cannot tell us when its smooth scroll and the
            # cursor's transition have finished.
            aim = self._eval(extract.point(args["selector"]))
            if isinstance(aim, dict) and aim.get("ok"):
                self._pause(HOVER_S)
            out = self._eval(extract.click(args["selector"]))
            self._pause(ACT_S)
            # A click often starts a navigation; give it a moment so the next
            # read_page sees the new page rather than the old one.
            self.call("api_wait", None, timeout=60)
            return out
        if name == "type_text":
            aim = self._eval(extract.point(args["selector"]))
            if isinstance(aim, dict) and aim.get("ok"):
                self._pause(HOVER_S)
            out = self._eval(extract.fill(args["selector"], args["value"]))
            self._pause(ACT_S)
            return out
        return {"error": "unknown tool %r" % name}

    # -- the loop -----------------------------------------------------------

    def run(self, goal):
        goal = (goal or "").strip()
        if not goal:
            return self.emit("Give me a goal to work on.\n")
        messages = [{"role": "user", "content": goal}]

        for step in range(MAX_STEPS):
            if self.cancelled:
                self.emit("\n[stopped]\n")
                return
            try:
                response = ai.tool_turn(messages, TOOLS, SYSTEM)
            except ai.NoKey as e:
                return self.emit(str(e) + "\n")
            except ai.ApiError as e:
                return self.emit("\n%s\n" % e)
            except Exception as e:
                return self.emit("\n[error] %r\n" % (e,))

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

            stop = response.get("stop_reason")
            if stop == "refusal":
                return self.emit("\n[the model declined this request]\n")
            if stop == "max_tokens":
                # A truncated turn can carry a half-written tool_use block, so
                # continuing would send a malformed request. Stop cleanly.
                return self.emit("\n[stopped: response hit the output limit]\n")
            if stop != "tool_use":
                return

            results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                if self.cancelled:
                    self.emit("\n[stopped]\n")
                    return
                name = block.get("name")
                args = block.get("input") or {}
                if not block.get("id"):
                    continue  # malformed block; nothing to answer
                self.emit("  → %s\n" % _describe(name, args))
                # A beat between the line appearing and the page moving, so the
                # two read as cause and effect rather than one event.
                self._pause(STEP_S)

                # Loop detection. Without it a model that keeps re-reading the
                # same page burns the step budget and the user's money making
                # no progress, and the only visible symptom is a stalled panel.
                signature = (name, json.dumps(args, sort_keys=True))
                self.seen[signature] = self.seen.get(signature, 0) + 1
                if self.seen[signature] > REPEAT_LIMIT:
                    output = {"error": "repeated this exact call %d times; "
                                       "try a different approach or stop"
                                       % self.seen[signature]}
                elif self.spent >= TOTAL_RESULT_CHARS:
                    output = {"error": "output budget for this run is exhausted; "
                                       "answer with what you have"}
                else:
                    try:
                        output = self.dispatch(name, args)
                    except Exception as e:
                        output = {"error": repr(e)}

                encoded = json.dumps(output, ensure_ascii=False)[:RESULT_CHARS]
                self.spent += len(encoded)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": encoded,
                })
            if not results:
                return self.emit("\n[stopped: no usable tool calls in that turn]\n")
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
