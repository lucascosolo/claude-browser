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
