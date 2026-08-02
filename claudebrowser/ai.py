"""Anthropic Messages API over the standard library.

No SDK, on purpose: this browser is meant to run on a machine with no pip and
a strict memory budget, and the whole point is that `python3 cb` works against
a bare Debian install. The tradeoff is that we own the SSE parsing below.

Streaming is used for every call -- responses can be long, and a non-streaming
request at a large max_tokens risks an HTTP timeout.
"""

import json
import os
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

MODEL = "claude-opus-5"
MAX_TOKENS = 64000

SYSTEM = (
    "You are answering questions about a web page the user is currently reading, "
    "inside a minimal browser built for developers. You are given the page's URL, "
    "title, and extracted text. Answer from that text. If the answer is not in the "
    "page, say so plainly rather than guessing. Be concise: lead with the answer, "
    "then supporting detail only if it changes what the reader would do next. "
    "Quote exact strings when the user asks about specific values, versions, or code."
)


class NoKey(Exception):
    """No credential available. Raised before any network call is attempted."""


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NoKey(
            "Set ANTHROPIC_API_KEY to use Ask Claude. "
            "Everything else in the browser works without it."
        )
    return key


def ask(question: str, page: dict, *, page_chars: int = 120_000):
    """Yield answer text as it streams back.

    `page` is the dict produced by extract.TEXT. The page body is truncated
    rather than chunked -- a browser side-panel is the wrong place to run a
    multi-request map-reduce, and a visible marker beats silent loss.
    """
    body = (page.get("text") or "")
    if len(body) > page_chars:
        body = body[:page_chars] + "\n\n[page truncated for length]"

    prompt = (
        "<page url=%r title=%r>\n%s\n</page>\n\n%s"
        % (page.get("url", ""), page.get("title", ""), body, question)
    )

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        yield "[api error %s] %s" % (e.code, detail)
        return
    except urllib.error.URLError as e:
        yield "[network error] %s" % (e.reason,)
        return

    with resp:
        for text in _sse_text(resp):
            yield text


TLDR_SYSTEM = (
    "Summarize the web page the user gives you. Lead with one sentence saying what "
    "the page IS. Then at most five bullets of the substance a reader actually needs "
    "-- specifics, numbers, names, versions, conclusions -- not a description of the "
    "page's structure. Skip navigation, boilerplate and calls to action. If the page "
    "is mostly empty or is an error, say that in one line instead of padding."
)

SYNTHESIS_SYSTEM = (
    "You are given the text of several web pages the user has open at once. "
    "Synthesize across them rather than summarizing each in turn: what do they agree "
    "on, where do they conflict, and what follows from reading them together. "
    "If they are directly comparable, lead with a compact markdown table. Cite pages "
    "by their number and title. If a page is irrelevant to the others, say so briefly "
    "rather than forcing it into the comparison."
)


def summarize(page):
    """TL;DR for one page. Deliberately invoked by a button, never on load --
    a request per page view would be both slow and expensive."""
    return _stream(TLDR_SYSTEM, _page_block(page) + "\n\nSummarize this page.")


def synthesize(pages, question=None):
    """Read every open tab together and answer across them."""
    body = "\n\n".join(
        "<page number=%d url=%r title=%r>\n%s\n</page>"
        % (i + 1, p.get("url", ""), p.get("title", ""), (p.get("text") or "")[:40_000])
        for i, p in enumerate(pages)
    )
    ask_for = question or "Synthesize these pages."
    return _stream(SYNTHESIS_SYSTEM, body + "\n\n" + ask_for)


def _page_block(page, limit=120_000):
    body = page.get("text") or ""
    if len(body) > limit:
        body = body[:limit] + "\n\n[page truncated for length]"
    return "<page url=%r title=%r>\n%s\n</page>" % (
        page.get("url", ""), page.get("title", ""), body)


def _stream(system, prompt):
    """Shared streaming path for the one-shot text features."""
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = _open(payload)
    except NoKey:
        raise
    except _ApiError as e:
        yield str(e)
        return
    with resp:
        for text in _sse_text(resp):
            yield text


class _ApiError(Exception):
    pass


def _open(payload, timeout=600):
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise _ApiError("[api error %s] %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise _ApiError("[network error] %s" % (e.reason,))


def tool_turn(messages, tools, system, max_tokens=16000):
    """One non-streaming turn that may request tools.

    Non-streaming on purpose: the agent loop needs the whole message -- every
    tool_use block and, on Claude Opus 5, the thinking blocks that have to be
    echoed back unchanged on the next turn -- before it can act. Streaming would
    buy nothing here because nothing is rendered until the turn is complete.
    """
    payload = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "tools": tools,
        "messages": messages,
    }
    with _open(payload) as resp:
        return json.loads(resp.read())


def _sse_text(resp):
    """Pull text deltas out of the SSE stream.

    Only three event shapes matter to us: text deltas, the stop reason, and an
    error frame. Thinking blocks arrive with empty text by default and are
    skipped by the block-index guard below.
    """
    current_is_text = False
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk:
            continue
        try:
            event = json.loads(chunk)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "content_block_start":
            current_is_text = event.get("content_block", {}).get("type") == "text"
        elif kind == "content_block_delta" and current_is_text:
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                yield delta.get("text", "")
        elif kind == "content_block_stop":
            current_is_text = False
        elif kind == "message_delta":
            # A refusal is an HTTP 200 with no usable content -- if we do not
            # say so here, the panel just sits empty and looks like a hang.
            if event.get("delta", {}).get("stop_reason") == "refusal":
                yield "\n[the model declined to answer this request]"
        elif kind == "error":
            yield "\n[stream error] %s" % (
                event.get("error", {}).get("message", "unknown"),
            )
