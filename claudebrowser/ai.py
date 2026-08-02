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

from . import auth

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


# Which credential served the last successful request, for the UI to report.
LAST_CREDENTIAL = None


class NoKey(Exception):
    """No credential available. Raised before any network call is attempted."""


class ApiError(Exception):
    """A request failed in a way retrying did not fix."""

    auth_failure = False


def api_key():
    """Only the API key. Credential selection lives in auth.candidates()."""
    key = auth.api_key()
    if not key:
        raise NoKey(auth._explain(auth.preference()))
    return key


def _open(payload, timeout=600, sleep=time.sleep):
    """POST with bounded retries, trying each credential in turn.

    `sleep` is injectable so tests can exercise the retry path without waiting.
    """
    try:
        options = auth.candidates()
    except auth.NoCredential as e:
        raise NoKey(str(e))

    first = None
    for index, (credential, label) in enumerate(options):
        final = index == len(options) - 1
        try:
            # Only the last candidate gets the retry budget. Spending 7s of
            # backoff on a credential we are about to abandon anyway is pure
            # latency, and it is what made a rate-limited subscription feel
            # like a browser that had frozen.
            return _open_with(payload, credential, label, timeout, sleep,
                              retries=MAX_RETRIES if final else 0)
        except ApiError as e:
            if final:
                # Report the *first* credential's failure, not the last. If the
                # preferred credential is the one the user configured, that is
                # the error they need to read.
                raise first or e
            # Fall through to the next credential on ANY failure, not just an
            # auth rejection. A 429 is the common case -- a subscription that
            # has hit its window is exactly when the API key should take over --
            # and gating this on 401/403 meant a working key was never tried.
            first = first or e
    raise first or ApiError("[error] no credential worked")


def _open_with(payload, credential, label, timeout, sleep, retries=MAX_RETRIES):
    global LAST_CREDENTIAL

    body = json.dumps(payload).encode()
    headers = dict(credential)
    headers["anthropic-version"] = API_VERSION
    headers["content-type"] = "application/json"
    if payload.get("stream"):
        headers["accept"] = "text/event-stream"

    for attempt in range(retries + 1):
        req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(req, timeout=timeout)
            LAST_CREDENTIAL = label
            return response
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:800]
            detail, kind = _describe_error(e.code, raw, label)
            if e.code in RETRY_STATUS and attempt < retries:
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
            error = ApiError(detail)
            error.auth_failure = e.code in (401, 403)
            error.status = e.code
            error.kind = kind
            raise error
        except urllib.error.URLError as e:
            if attempt < retries:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[network error] %s" % (e.reason,))
        except OSError as e:
            if attempt < retries:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[connection error] %s" % (e,))
    raise ApiError("[error] exhausted retries")


def _shadow_hint():
    """Name the file the rejected key came out of.

    "The API key was rejected" is only actionable if you know *which* key that
    was. Point at the exact file so it can be corrected in one step -- and, when
    the key came from an inherited variable instead, say so, because that is the
    case where editing files achieves nothing.
    """
    try:
        source = auth.key_source()
        if source is None:
            return ""
        if source == "your environment":
            return ("\n\nThis key came from an ANTHROPIC_API_KEY in the environment, "
                    "not from the browser's settings file. Put the key in\n    %s\n"
                    "as  ANTHROPIC_API_KEY=sk-ant-...  -- that file wins, and it is "
                    "the only copy the browser can see you change."
                    % auth_config_path())
        return ("\n\nThis key came from %s. Edit that line and try again -- the "
                "change takes effect on the next request, with no restart." % source)
    except Exception:
        pass  # a diagnostic hint must never become the failure it is explaining
    return ""


def auth_config_path():
    from . import envfile

    return envfile.config_path()


def _describe_error(status, raw, label):
    """Turn an API error into something a person can act on.

    The API's own message is often a single unhelpful word -- a rate-limited
    subscription literally returns {"message": "Error"} -- so the status code
    and error type carry the real meaning and we spell it out here. "It failed"
    with no reason is the thing this browser must never do.
    """
    kind = ""
    message = raw
    try:
        parsed = json.loads(raw).get("error", {})
        kind = parsed.get("type", "")
        message = parsed.get("message") or raw
    except Exception:
        pass

    if status == 429:
        if "subscription" in label:
            return ("Your Claude subscription is rate-limited right now.\n\n"
                    "This quota is shared with Claude Code, so a busy session can "
                    "use it up. It refills on its own -- wait a few minutes and try "
                    "again, or put an ANTHROPIC_API_KEY in\n    %s\n"
                    "to bill the API instead." % auth_config_path(), kind)
        return ("Rate limited by the API. Wait a moment and try again.", kind)

    if status in (401, 403):
        if "subscription" in label:
            return ("Your Claude subscription login was rejected (%s).\n\n"
                    "Run /login in Claude Code to refresh it." % (message,), kind)
        return ("The API key was rejected: %s\n\n"
                "Check the value of ANTHROPIC_API_KEY -- keys start with 'sk-ant-' "
                "and can be revoked from the Anthropic console.%s"
                % (message, _shadow_hint()), kind)

    if status == 404:
        return ("The model was not found (%s). This build asks for %s."
                % (message, MODEL), kind)
    if status >= 500:
        return ("Anthropic returned a server error (%s): %s\n"
                "This is usually transient." % (status, message), kind)

    return ("[%s] HTTP %s: %s" % (label, status, message), kind)


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
    # ApiError propagates: the panel turns it into a red card with a "failed"
    # status. Yielding the message as if it were an answer looked like success.
    resp = _open(payload)
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
