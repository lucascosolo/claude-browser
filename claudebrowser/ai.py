"""Anthropic Messages API over the standard library.

No SDK, on purpose: this browser is meant to run on a machine with no pip, and
the whole point is that `./cb` works against a bare Debian install. The tradeoff
is that we own the SSE parsing, the retry policy, and -- since the switch off
urllib -- the connection pool below.

One request path (`_open`) with retries, and everything else built on it.

**Why not urllib.** `urlopen` opens a fresh TLS connection for every call and
throws it away, so a full handshake was being paid for every panel question,
every TL;DR, and every single step of the agent loop -- a round trip that is
pure latency on a conversation that is otherwise one long exchange with one
host. `http.client` lets the socket be kept, at the cost of owning the two
hazards that come with keep-alive: a connection that went stale while idle, and
a response body that was not finished before the socket was handed to someone
else. `_Pool` and `_Response` exist for exactly those two.
"""

import http.client
import json
import os
import random
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request

from . import auth, personas, scrub, vpn

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

_SPLIT = urllib.parse.urlsplit(API_URL)
API_HOST = _SPLIT.hostname
API_PORT = _SPLIT.port or 443
API_PATH = _SPLIT.path

# How long a connection may sit unused before we stop trusting it. Servers and
# the load balancers in front of them close idle keep-alive sockets on their own
# schedule and never tell the client, so this is a guess -- the real defence is
# the stale retry in `_request`, which catches the cases this misses (a laptop
# suspended mid-idle, for one: CLOCK_MONOTONIC does not tick while suspended,
# so a connection that is hours dead can still look seconds young).
IDLE_TIMEOUT = 50.0
# Two is plenty: the agent worker and the UI are the only concurrent callers.
MAX_IDLE = 2

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

#: What a private tab is told when a Claude feature is asked to read it.
#: One sentence for what happened, one for how to change it -- a refusal the
#: user cannot act on reads as a bug.
PRIVATE_REFUSAL = (
    "This tab is private, so its address, title and text are not sent to "
    "Claude. Ask again in an ordinary tab, or set CB_PRIVATE_AI on in "
    "cb:settings to allow it."
)


def private_ai_enabled(raw=None, path=None):
    """May the Claude features read a private tab? No, unless explicitly told.

    Refusing is the default because every other privacy knob here decides what
    is written to *this* machine, and this one decides what leaves it: a
    private tab's URL is the thing most likely to be a magic link or a
    one-time token, and `scrub.py` deliberately never touches a URL. Only the
    words that unambiguously mean "yes" count, so a typo keeps the refusal.

    Read at the moment a feature is handed a tab, so turning it on takes
    effect on the next question rather than the next launch.
    """
    from . import envfile

    if raw is None:
        raw = envfile.setting("CB_PRIVATE_AI", "", path=path)
    return (raw or "").strip().lower() in ("1", "on", "true", "yes")


class NoKey(Exception):
    """No credential available. Raised before any network call is attempted."""


class ApiError(Exception):
    """A request failed in a way retrying did not fix."""

    auth_failure = False
    # Set when the request was fully delivered before the failure, so no layer
    # above may send it again. See `_Delivered`.
    delivered = False


def api_key():
    """Only the API key. Credential selection lives in auth.candidates()."""
    key = auth.api_key()
    if not key:
        raise NoKey(auth._explain(auth.preference()))
    return key


# -- keep-alive transport ---------------------------------------------------

def _shut(conn):
    """Close a connection and never let that be the thing that fails."""
    try:
        conn.close()
    except Exception:
        pass


def _ssl_context():
    global _SSL
    if _SSL is None:
        # Built once: create_default_context() parses the system CA bundle, and
        # doing that per request is most of what we just saved.
        _SSL = ssl.create_default_context()
    return _SSL


_SSL = None


def _proxy_for(host):
    """(host, port) of the HTTPS proxy to tunnel through, or None.

    urllib honoured `https_proxy` for free; going to http.client means honouring
    it by hand or silently breaking every machine behind one.
    """
    try:
        proxy = urllib.request.getproxies().get("https")
        if not proxy or urllib.request.proxy_bypass(host):
            return None
        parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
        if not parts.hostname:
            return None
        return parts.hostname, parts.port or 8080
    except Exception:
        return None  # a proxy we cannot parse is not worth failing the request over


def transport_route():
    """A name for the path the next connection will take.

    "direct", or the VPN proxy it will tunnel through. `_Pool` keys its idle
    sockets on this: a connection opened before VPN Mode was turned on is not
    interchangeable with one opened after it, and handing the old one back out
    would put the next question on the path the user has just asked the browser
    to stop using -- with nothing in the answer to say so.
    """
    return vpn.transport_key()


def reset_transport():
    """Drop every pooled connection, because the route changed.

    Called by the window whenever VPN Mode is engaged or turned off. The keying
    above already stops a mismatched socket from being reused; this closes them
    instead of leaving them parked until the idle timeout, which matters in the
    direction that matters -- a live socket to Anthropic from the user's own
    address, sitting open, after they asked for it not to be.
    """
    _POOL.close_all()


def _new_connection(timeout):
    # VPN Mode wins over the environment's https_proxy. It is a deliberate,
    # visible choice made in this window; https_proxy is ambient configuration
    # that may well be the thing the user is trying to get out from behind.
    tunnel, refusal = vpn.api_route()
    if refusal:
        # Unreachable through `_open`, which refuses before a socket is ever
        # asked for. It is here anyway because this function is where a socket
        # is *made*: a future caller that skipped the gate above would otherwise
        # get a direct connection out of it, which is the one answer that must
        # not be available from this code path at all.
        raise ApiError(refusal)
    if tunnel is not None:
        # Same CONNECT mechanism as the branch below -- see vpn.open_tunnel for
        # why the certificate is still checked against api.anthropic.com.
        return vpn.open_tunnel(tunnel, API_HOST, API_PORT, timeout,
                               context=_ssl_context())

    proxy = _proxy_for(API_HOST)
    if proxy:
        # HTTPSConnection sends CONNECT in the clear and *then* wraps TLS, so
        # the certificate is still checked against the real host, not the proxy.
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout,
                                           context=_ssl_context())
        conn.set_tunnel(API_HOST, API_PORT)
        return conn
    return http.client.HTTPSConnection(API_HOST, API_PORT, timeout=timeout,
                                       context=_ssl_context())


class _Pool:
    """A tiny free-list of live connections to one host.

    **Why a pool and not one connection per thread.** Calls arrive from the
    agent's worker thread and from the UI, and a streaming response holds its
    connection for as long as the answer takes to arrive -- tens of seconds. A
    single shared connection under a lock would therefore serialize a tool_turn
    behind a panel that is still streaming, which is worse than the handshake we
    set out to remove. Thread-locals would avoid that but leak a socket per
    agent run, because `Browser.run_agent` starts a fresh thread each time. A
    checked-out free list gives exclusive use without either problem: the lock
    is held only for the list operations, never across the network.
    """

    def __init__(self, connect=_new_connection, max_idle=MAX_IDLE,
                 idle_timeout=IDLE_TIMEOUT, clock=time.monotonic, route=None):
        self._connect = connect
        self._max_idle = max_idle
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._route = route or transport_route
        self._idle = []          # (connection, parked_at), oldest first
        self._lock = threading.Lock()

    def take(self, timeout, fresh=False):
        """Check out a connection. Returns (connection, was_reused).

        Every connection handed out here must come back through `give_back` or
        `discard`, or it is a leaked socket.

        A socket is only reusable while the route is the one it was opened on --
        see `transport_route`. The route is stamped on the connection rather
        than on the parked entry because the mode can change while a request is
        in flight, and what matters is the path this socket actually took, not
        the path the browser would take now.
        """
        want = self._route()
        while not fresh:
            with self._lock:
                if not self._idle:
                    break
                conn, parked = self._idle.pop()   # newest first: most likely alive
            if (getattr(conn, "_cb_route", None) == want
                    and self._clock() - parked <= self._idle_timeout):
                return conn, True
            _shut(conn)
        conn = self._connect(timeout)
        try:
            conn._cb_route = want
        except AttributeError:
            pass      # a connection that will not be labelled is never reused
        return conn, False

    def give_back(self, conn):
        """Return a connection whose response was fully consumed."""
        evicted = []
        with self._lock:
            self._idle.append((conn, self._clock()))
            while len(self._idle) > self._max_idle:
                evicted.append(self._idle.pop(0))
        for old, _parked in evicted:
            _shut(old)

    def discard(self, conn):
        _shut(conn)

    def close_all(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for conn, _parked in idle:
            _shut(conn)

    def idle_count(self):
        with self._lock:
            return len(self._idle)


_POOL = _Pool()


class _Response:
    """A response that owns its connection until the body is gone.

    The connection is only recycled if the body was read to the end. A response
    abandoned half-read leaves unsent-for bytes sitting in the socket, and the
    *next* request on that socket parses them as its own reply -- the classic
    keep-alive corruption, and the reason `close()` discards by default and
    recycles only on proof of a clean finish.
    """

    def __init__(self, raw, conn, pool):
        self._raw = raw
        self._conn = conn
        self._pool = pool
        self._drained = False
        self._released = False
        self.status = raw.status
        self.headers = raw.headers

    def read(self, amt=None):
        data = self._raw.read(amt)
        if amt is None:
            self._drained = True
        return data

    def __iter__(self):
        for line in self._raw:
            yield line
        # Only reaching EOF proves the body ended where its framing said it
        # would. A caller that breaks out of this loop leaves _drained False,
        # and the connection is dropped rather than poisoning the next request.
        self._drained = True

    def close(self):
        if self._released:
            return
        self._released = True
        # `will_close` is the server saying "Connection: close", or HTTP/1.0,
        # or a body with no length -- reuse is not ours to decide there.
        reusable = self._drained and not self._raw.will_close
        try:
            self._raw.close()
        except Exception:
            reusable = False
        if reusable:
            self._pool.give_back(self._conn)
        else:
            self._pool.discard(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()
        return False

    def __del__(self):
        # A response nobody closed would otherwise strand its socket forever.
        try:
            self.close()
        except Exception:
            pass


# Ways a kept-alive socket announces that the far end hung up while we were not
# looking. RemoteDisconnected is the common one; BadStatusLine is the same event
# seen a moment later, when the close arrives where a status line should be.
_STALE_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.ImproperConnectionState,   # CannotSendRequest et al
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
)


class _Delivered(OSError, http.client.HTTPException):
    """The request was fully sent, and *then* the connection died.

    This is the phase that must never be retried, at any layer. The whole body
    already reached Anthropic, which may have received, run and **billed** the
    inference before the socket broke -- so a second attempt buys a second paid
    call and no better chance of an answer. The write phase raises the *same*
    exception types (a keep-alive socket closed while idle looks identical), so
    the type cannot carry this distinction and a separate class has to.

    It inherits from both OSError and HTTPException so it still satisfies every
    `except` clause the old flat failure did: any caller that does not know
    about the distinction degrades to the previous behaviour rather than
    letting an unhandled exception escape.
    """

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _is_stale(exc, reused):
    """Did a *reused* connection die before the server could answer?

    `reused` is the whole point. The identical exception on a freshly opened
    socket is a real network failure -- the host is unreachable, TLS was refused
    -- and opening one more socket to hear it again only delays the message the
    user needs. A timeout is excluded for a different reason: the server may
    well still be working on the request, so a second copy is a second bill.

    Phase is *not* checked here: `_request` raises `_Delivered` before it ever
    consults this function, so by construction only pre-send failures reach it.
    """
    if not reused:
        return False
    if isinstance(exc, socket.timeout):
        return False
    return isinstance(exc, _STALE_ERRORS)


def _send(conn, body, headers, pool, sent=None):
    """POST and take the response.

    `sent` is an optional one-element list used as an out-parameter recording
    which phase we reached: it is set True once the request body is fully
    written, so a caller catching an exception can tell a write-phase failure
    (safe to retry) from a getresponse-phase one (not safe -- `_Delivered`).
    """
    conn.request("POST", API_PATH, body=body, headers=headers)
    if sent is not None:
        sent[0] = True
    return _Response(conn.getresponse(), conn, pool)


def _request(body, headers, timeout, pool=None):
    """POST once over a pooled connection, retrying a stale socket exactly once.

    Retrying is only sound here because *nothing of the response has been handed
    to anyone yet* -- the retry sits between sending the request and returning
    the response, so a stream that has already yielded text can never be
    silently restarted -- and because it is scoped to failures that happened
    before the body was fully written, where the server cannot have processed
    anything.
    """
    pool = pool or _POOL
    conn, reused = pool.take(timeout)
    sent = [False]
    try:
        return _send(conn, body, headers, pool, sent)
    except Exception as e:
        pool.discard(conn)
        # Phase first, and before any other judgement: once the body is out,
        # no layer above may try again. Re-raising as _Delivered is what stops
        # _open_with's backoff loop from doing exactly what this guard just
        # prevented one level down.
        if sent[0]:
            raise _Delivered(e) from e
        if not _is_stale(e, reused):
            raise

    conn, _reused = pool.take(timeout, fresh=True)
    sent = [False]
    try:
        return _send(conn, body, headers, pool, sent)
    except Exception as e:
        pool.discard(conn)
        if sent[0]:
            raise _Delivered(e) from e
        raise      # a fresh connection failing is a real failure; no third try


def _error_body(resp):
    """Read and release an error response. Its socket is still perfectly good."""
    try:
        return resp.read().decode("utf-8", "replace")[:800]
    except Exception:
        return ""
    finally:
        resp.close()


def _open(payload, timeout=600, sleep=None):
    """POST with bounded retries, trying each credential in turn.

    `sleep` is injectable so tests can exercise the retry path without waiting.
    It defaults to None rather than to `time.sleep` directly because a default
    argument is bound once, at def time: with `sleep=time.sleep` in the
    signature, the callers that do not pass one (`_stream`, `tool_turn`) held a
    reference no test could ever replace, and the one test that tried spent the
    full backoff budget sleeping for real on every run.
    """
    if sleep is None:
        sleep = time.sleep

    # Before the credential is even chosen, and before any socket exists. VPN
    # Mode covers this request or the request does not happen: sending it
    # direct would leak both the question and the fact that the tunnel the user
    # asked for is not working. The panel renders this as a failed card, which
    # is the honest outcome -- see vpn.State.route for the three cases.
    _proxy, refusal = vpn.api_route()
    if refusal:
        raise ApiError(refusal)

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
            if e.delivered:
                # Falling through to the next credential would re-send a
                # request that may already have been billed on this one. The
                # cheapest correct answer is to stop.
                raise first or e
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
        try:
            response = _request(body, headers, timeout)
        except _Delivered as e:
            # Ordered before the two clauses below, both of which would
            # otherwise catch this: the request reached Anthropic and may have
            # been processed and billed, so the retry budget must not be spent
            # on a duplicate. Fail now, and say why in terms the panel can show.
            error = ApiError(
                "[connection lost] The request reached Anthropic but the reply "
                "never arrived (%s).\n\nIt was not retried automatically, "
                "because the request may already have been processed and "
                "charged for. Ask again if you want another attempt."
                % (e.cause,))
            error.delivered = True
            raise error
        except http.client.HTTPException as e:
            if attempt < retries:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[connection error] %s" % (e,))
        except OSError as e:
            # Everything socket- and DNS-shaped lands here; urllib used to wrap
            # it in URLError, which is itself an OSError, so the message the
            # panel shows is unchanged.
            if attempt < retries:
                sleep(min((2 ** attempt) + random.random(), 30))
                continue
            raise ApiError("[network error] %s" % (getattr(e, "reason", None) or e,))

        if 200 <= response.status < 300:
            LAST_CREDENTIAL = label
            return response

        status = response.status
        after = response.headers.get("retry-after") if response.headers else None
        raw = _error_body(response)
        detail, kind = _describe_error(status, raw, label)
        if status in RETRY_STATUS and attempt < retries:
            # Honour Retry-After when the server sends one; otherwise back
            # off exponentially with jitter so several panels retrying at
            # once do not sync up into a thundering herd.
            try:
                delay = float(after)
            except (TypeError, ValueError):
                delay = (2 ** attempt) + random.random()
            sleep(min(delay, 30))
            continue
        error = ApiError(detail)
        error.auth_failure = status in (401, 403)
        error.status = status
        error.kind = kind
        raise error
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

def _redact(text, tally=None):
    """Run the outbound scrubber over one piece of page text.

    Every path that puts page content into a request goes through here, so
    `CB_SCRUB` has one place to be honoured and the counts have one place to be
    collected. `tally` is a dict the caller owns: it is updated in place because
    the request is built inside a generator the UI does not otherwise get a
    return value from, and the UI has to be able to say what was removed.
    """
    result = scrub.scrub(text)
    if tally is not None:
        for kind, n in result.counts.items():
            tally[kind] = tally.get(kind, 0) + n
    return result.text


def _page_block(page, limit=120_000, tally=None):
    # Truncate first, then scrub: scrubbing the tail we are about to throw away
    # is wasted work on a 400KB page, and a placeholder is never longer than
    # what it replaced so the budget still holds afterwards.
    body = page.get("text") or ""
    if len(body) > limit:
        body = body[:limit] + "\n\n[page truncated for length]"
    # The URL is left alone on purpose: the model is asked to cite it back, and
    # a redacted URL is one the user cannot click. The title is not -- a webmail
    # tab puts the correspondent's address straight into it.
    return "<page url=%r title=%r>\n%s\n</page>" % (
        page.get("url", ""), _redact(page.get("title", ""), tally),
        _redact(body, tally))


def _stream(system, prompt):
    """Shared streaming path. Yields text; never raises past NoKey.

    The active persona is composed in *here*, at the one point Ask, TL;DR and
    Research all pass through, so a new text feature cannot forget it and no
    caller can pass a system prompt the persona replaces: `personas.compose`
    only ever appends to what it is given. The agent loop deliberately does not
    come through here -- `agent.SYSTEM` is instructions for driving a browser
    with tools, not an answering style, and layering "read this adversarially"
    over it would change what the agent *does* rather than how it writes.
    """
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": personas.compose(system),
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
    except (OSError, http.client.HTTPException) as e:
        # The stream can die mid-flight; the user keeps whatever arrived. This
        # is also the one place a half-read body is expected: `with resp` has
        # already run by the time we get here, so the connection was dropped
        # rather than pooled.
        yield "\n[stream interrupted: %s]" % (e,)


def ask(question, page, page_chars=120_000, tally=None):
    """Answer a question about one page.

    `tally` is an optional dict; personal data found in the page is counted into
    it by category before the request is built, so the panel can say what left
    the machine. It is filled by the time this returns -- the prompt is
    assembled here, not lazily inside the generator.
    """
    return _stream(SYSTEM, _page_block(page, page_chars, tally) + "\n\n" + question)


def summarize(page, tally=None):
    """TL;DR for one page. Invoked by a button, never on load -- a request per
    page view would be both slow and expensive."""
    return _stream(TLDR_SYSTEM, _page_block(page, tally=tally) + "\n\nSummarize this page.")


def synthesize(pages, question=None, tally=None):
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
        % (i + 1, p.get("url", ""), _redact(p.get("title", ""), tally),
           _redact((p.get("text") or "")[:per_page], tally))
        for i, p in enumerate(pages)
    )
    return _stream(SYNTHESIS_SYSTEM, body + "\n\n" + (question or "Synthesize these pages."))


#: What cb:search asks for above the results. Deliberately not SYNTHESIS_SYSTEM:
#: that one is written for pages the user has open and has read, while these are
#: search snippets the user has not seen and Claude has not either -- the model
#: is reading an index, not the web. Saying so is what keeps the answer from
#: being stated with more confidence than a snippet can carry.
SEARCH_SYSTEM = (
    "You are answering above a list of web search results, in a browser. "
    "You are shown only the search engine's snippets, not the pages "
    "themselves, so treat them as second-hand and say when they are thin or "
    "disagree. Answer the query directly in the first sentence. Do not open "
    "with a restatement of the question, a preamble, or 'based on the search "
    "results'. Prefer what several results agree on. If the snippets do not "
    "actually answer the query, say that plainly and say what would -- an "
    "invented answer is worse than none, because the results are right below "
    "you and the user can check. Never cite a result that is not in the list. "
    "Answer in English even when the results are not in English."
)

#: The short answer's ceiling. Enough for two or three sentences; the point of
#: the short answer is that it is read in the time it takes results to arrive.
SEARCH_BRIEF_WORDS = 60

#: How much of each result reaches the model. These three numbers *are* what a
#: search costs: the prompt is sent once for the brief answer and again for the
#: verbose one, and it dwarfs both answers put together. Chosen against real
#: LangSearch responses, whose snippets run a couple of hundred characters and
#: whose summaries run to tens of thousands.
SNIPPET_CHARS = 500
SUMMARY_CHARS = 700
SUMMARY_RESULTS = 4


def search_answer(query, results, verbose=False, tally=None):
    """Answer a search query from its own results.

    `results` are dicts with `title`, `url`, `snippet` and optionally
    `summary`. Everything textual goes through `_redact` for the same reason
    page text does -- a query and its snippets can carry personal data, and
    `CB_SCRUB` has to mean the same thing on every path that leaves the
    machine.

    Everything here is shaped by what a search costs. The prompt is the
    expensive half -- it is sent once for the brief answer and, if the user
    presses Expand, once more for the verbose one, while the answers themselves
    are a few hundred tokens between them. So the trimming happens on the way
    *in* (`SNIPPET_CHARS`, `SUMMARY_CHARS`, `SUMMARY_RESULTS`), and the verbose
    answer is generated only when it is asked for rather than produced with the
    brief one and hidden.
    """
    blocks = []
    for i, hit in enumerate(results[:10]):
        text = (hit.get("snippet") or "")[:SNIPPET_CHARS]
        # The summary is where the tokens are: LangSearch returns page-length
        # summaries, so ten of them unabridged is half a megabyte of prompt
        # for a sixty-word answer, on a metered API, on every search. Only the
        # first few results get one, and only their opening -- a summary's
        # first paragraph is what a search result is *for*, and the rest is
        # the page, which the user can open. This is the single biggest lever
        # on what a search costs.
        if i < SUMMARY_RESULTS:
            summary = (hit.get("summary") or "")[:SUMMARY_CHARS]
            if summary and not summary.startswith(text[:80]):
                text = "%s\n%s" % (text, summary)
        blocks.append("<result number=%d url=%r title=%r>\n%s\n</result>"
                      % (i + 1, hit.get("url", ""),
                         _redact(hit.get("title", ""), tally),
                         _redact(text, tally)))
    body = "\n\n".join(blocks) or "(the search returned nothing)"
    if verbose:
        instruction = ("Answer this query thoroughly, in a few short "
                       "paragraphs, noting where the results disagree: %s"
                       % _redact(query, tally))
    else:
        # Two sentences that answer the question beat a summary of what an
        # answer would contain, so the ceiling is a ceiling and not a target:
        # a query with a short true answer gets that whole answer, and only a
        # query that genuinely does not fit gets the gist plus an honest note
        # that there is more behind Expand.
        instruction = (
            "Answer this query in one short paragraph of at most %d words. "
            "If the complete answer fits in that, give the complete answer "
            "and nothing else. If it genuinely does not, give the gist and "
            "end with one short sentence naming what the longer answer adds. "
            "No headings, no bullet lists, and no citations -- the results "
            "are on screen directly below you. Plain prose, with markdown "
            "only for emphasis or a code name. Query: %s"
            % (SEARCH_BRIEF_WORDS, _redact(query, tally)))
    return _stream(SEARCH_SYSTEM, body + "\n\n" + instruction)


# -- tool use ---------------------------------------------------------------

def _scrubbed_messages(messages, tally=None):
    """A copy of `messages` with page content redacted out of tool results.

    The agent's page reads come back as `tool_result` blocks, which is the only
    place in this conversation a web page's text appears -- the goal is the
    user's own words and the assistant blocks (including thinking, which must be
    echoed back byte-identical) are the model's. Rewriting anything else would
    either edit what the user typed or invalidate a thinking signature, so the
    walk is deliberately narrow and copies rather than mutating the caller's
    list, which the agent loop keeps appending to across steps.
    """
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        blocks, changed = [], False
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_result"
                    and isinstance(block.get("content"), str)):
                text = _redact(block["content"], tally)
                if text != block["content"]:
                    changed = True
                    block = dict(block, content=text)
            blocks.append(block)
        out.append(dict(message, content=blocks) if changed else message)
    return out


def tool_turn(messages, tools, system, max_tokens=16000, tally=None):
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
        "messages": _scrubbed_messages(messages, tally),
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
