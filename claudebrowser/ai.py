"""Anthropic Messages API over the standard library.

No SDK, on purpose: this browser is meant to run on a machine with no pip, and
the whole point is that `./cb` works against a bare Debian install. The tradeoff
is that we own the SSE parsing and the retry policy below.

One request path (`_open`) with retries, and everything else built on it.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

MODEL = "claude-opus-5"
MAX_TOKENS = 64000

# Worth retrying: rate limits, overload, and transient gateway failures. A 400
# or 401 is a bug or a bad key and will fail identically forever.
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
MAX_RETRIES = 3

SYSTEM = (
    "You are answering questions about a web page the user is currently reading, "
    "inside a minimal browser built for developers. You are given the page's URL, "
    "title, and extracted text. Answer from that text. If the answer is not in the "
    "page, say so plainly rather than guessing. Be concise: lead with the answer, "
    "then supporting detail only if it changes what the reader would do next. "
    "Quote exact strings when the user asks about specific values, versions, or code."
)

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


class NoKey(Exception):
    """No credential available. Raised before any network call is attempted."""


class ApiError(Exception):
    """A request failed in a way retrying did not fix."""


def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NoKey(
            "Set ANTHROPIC_API_KEY to use Claude features. "
            "Everything else in the browser works without it."
        )
    return key


def _open(payload, timeout=600, sleep=time.sleep):
    """POST with bounded retries. Returns the open response for the caller to read.

    `sleep` is injectable so tests can exercise the retry path without waiting.
    """
    body = json.dumps(payload).encode()
    headers = {
        "x-api-key": api_key(),
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    if payload.get("stream"):
        headers["accept"] = "text/event-stream"

    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            try:
                detail = json.loads(detail)["error"]["message"]
            except Exception:
                pass
            if e.code in RETRY_STATUS and attempt < MAX_RETRIES:
                # Honour Retry-After when the server sends one; otherwise back
                # off exponentially with jitter so several panels retrying at
                # once do not sync up into a thundering herd.
                after = e.headers.get("retry-after") if e.headers else None
                try:
                    delay = float(after)
                except (TypeError, ValueError):
                    delay = (2 ** attempt) + random.random()
                sleep(min(delay, 30))
                continue
            raise ApiError("[api error %s] %s" % (e.code, detail))
        except urllib.error.URLError as e:
            if attempt < MAX_RETRIES:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[network error] %s" % (e.reason,))
        except OSError as e:
            if attempt < MAX_RETRIES:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[connection error] %s" % (e,))
    raise ApiError("[error] exhausted retries")


# -- one-shot text features -------------------------------------------------

def _page_block(page, limit=120_000):
    body = page.get("text") or ""
    if len(body) > limit:
        body = body[:limit] + "\n\n[page truncated for length]"
    return "<page url=%r title=%r>\n%s\n</page>" % (
        page.get("url", ""), page.get("title", ""), body)


def _stream(system, prompt):
    """Shared streaming path. Yields text; never raises past NoKey."""
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = _open(payload)
    except ApiError as e:
        yield str(e)
        return
    try:
        with resp:
            for text in _sse_text(resp):
                yield text
    except (urllib.error.URLError, OSError) as e:
        # The stream can die mid-flight; the user keeps whatever arrived.
        yield "\n[stream interrupted: %s]" % (e,)


def ask(question, page, page_chars=120_000):
    """Answer a question about one page."""
    return _stream(SYSTEM, _page_block(page, page_chars) + "\n\n" + question)


def summarize(page):
    """TL;DR for one page. Invoked by a button, never on load -- a request per
    page view would be both slow and expensive."""
    return _stream(TLDR_SYSTEM, _page_block(page) + "\n\nSummarize this page.")


def synthesize(pages, question=None):
    """Read every open tab together and answer across them."""
    if not pages:
        def empty():
            yield "No readable pages are open."
        return empty()

    # Budget the whole request rather than each page, so twelve tabs do not
    # produce twelve full-length documents.
    per_page = max(4_000, 100_000 // max(len(pages), 1))
    body = "\n\n".join(
        "<page number=%d url=%r title=%r>\n%s\n</page>"
        % (i + 1, p.get("url", ""), p.get("title", ""), (p.get("text") or "")[:per_page])
        for i, p in enumerate(pages)
    )
    return _stream(SYNTHESIS_SYSTEM, body + "\n\n" + (question or "Synthesize these pages."))


# -- tool use ---------------------------------------------------------------

def tool_turn(messages, tools, system, max_tokens=16000):
    """One non-streaming turn that may request tools.

    Non-streaming on purpose: the agent loop needs the whole message -- every
    tool_use block, and on Claude Opus 5 the thinking blocks that must be echoed
    back unchanged -- before it can act. Streaming would buy nothing because
    nothing renders until the turn is complete.
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


# -- SSE --------------------------------------------------------------------

def _sse_text(resp):
    """Pull text deltas out of the SSE stream.

    Only three event shapes matter: text deltas, the stop reason, and an error
    frame. Thinking blocks arrive with empty text by default and are skipped by
    the block-type guard.
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
            stop = event.get("delta", {}).get("stop_reason")
            # A refusal is an HTTP 200 with no usable content -- without this the
            # panel just sits empty and looks like a hang.
            if stop == "refusal":
                yield "\n[the model declined to answer this request]"
            elif stop == "max_tokens":
                yield "\n[truncated: hit the output limit]"
        elif kind == "error":
            yield "\n[stream error] %s" % (
                event.get("error", {}).get("message", "unknown"),
            )
