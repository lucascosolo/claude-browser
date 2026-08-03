"""VPN Mode: this browser's traffic through a proxy the user runs themselves.

Everything that is not a GTK or WebKit call lives here, so the whole of it can
be tested without a display: parsing the proxy URL, deciding which hosts must
never go through it, keeping the password out of anything that is displayed or
logged, the four-state machine, and the one check that actually proves the
tunnel works.

**The name is the user's, the wording is not.** "VPN Mode" is what this is
called because that is what was asked for; every string it puts on screen says
what it really is. It is an HTTP proxy with an exit on a machine the user owns.
It carries this browser's page loads and its Anthropic API calls. It does not
carry other applications, WebRTC or any other UDP media, or system traffic, and
it is best-effort rather than a kill switch -- a compromised page, or a WebKit
subprocess that ignores the setting, can still open a socket of its own. The
honest guarantee is one layer down, in nftables, and this is not that.

Four decisions worth knowing before editing:

**Verification is an external echo, not a local check.** "The proxy accepted a
connection" proves the proxy is running and nothing else: the exit host's
default route, its NAT, and its provider firewall can each be broken on their
own, and every one of those failures still answers a `CONNECT`. So the state
only reaches `on` when an outside service, reached *through* the proxy, reports
back an address -- and that address is shown, because a check whose result the
user cannot see is a check they cannot disbelieve.

**Nothing here ever falls back.** There is no path from `failed` to `off`
except a person turning the mode off. A silent revert to `DEFAULT` or
`NO_PROXY` would leave the browser loading pages from the user's own address
while every indicator still said otherwise, which is worse than not having the
feature: the failure would be invisible exactly when it matters.

**Only an `http://` proxy is engaged.** `parse_proxy` understands `https://`
and `socks5://` too -- a URL the browser refuses should still be a URL it
*read*, so the refusal can say something useful -- but `engage` takes plain
HTTP only. Both the exit-IP check and the Anthropic tunnel are `http.client`
opening a `CONNECT`, which cannot speak SOCKS5 and cannot wrap TLS around a
proxy hop. WebKit itself would happily use either. Handing it one anyway would
mean a mode that cannot be verified and an AI path that has to be refused, and
"on, unverified" is the one state this must never have.

**The proxy URL holds a password**, so it is treated like the API key: read
straight out of the settings file, never copied into the environment (see
`envfile.SECRET_KEYS`), never written by anything inside the browser, and never
rendered. `Proxy.redact` exists because the interesting failures -- a refused
`CONNECT`, a socket error -- carry the address that failed, and the address is
where the password is.
"""

import base64
import http.client
import ipaddress
import threading
import urllib.parse

from . import envfile

#: The two settings this reads. Both live in `settings.py`'s table as well;
#: these constants are what the rest of the browser refers to them by.
SETTING = "CB_VPN"
PROXY_SETTING = "CB_VPN_PROXY"

# -- states ------------------------------------------------------------------
# `connecting` is a real state and not a cosmetic one: the proxy is already
# applied to WebKit by the time it is entered, so traffic in that window is
# tunnelled or it fails -- it is "not yet proven", not "not yet protected".
OFF = "off"
CONNECTING = "connecting"
ON = "on"
FAILED = "failed"

#: What a URL with no port means, per scheme. tinyproxy and squid both default
#: to 8888/3128 rather than to anything standard, so a missing port is a guess
#: either way; these are the conventional ones and the value is shown back to
#: the user on cb:vpn so a wrong guess is visible rather than mysterious.
DEFAULT_PORTS = {"http": 8080, "https": 8080, "socks5": 1080}
SCHEMES = ("http", "https", "socks5")

#: Hosts WebKit must keep reaching directly.
#:
#: Loopback only, and deliberately not RFC1918. Sending the browser's own
#: control API and any locally served page around the world and back would be
#: absurd, and there is nothing to hide from a socket that never leaves the
#: machine. A private LAN address is a different matter: it is still a request
#: the user made, and bypassing the proxy for it would be exactly the kind of
#: quiet exception this mode exists to not have. So a NAS at 192.168.1.10 is
#: unreachable while VPN Mode is on -- it fails, visibly, instead of leaking
#: around the tunnel. cb: needs no entry at all: it is served by the browser's
#: own scheme handler and never becomes a network request in the first place.
IGNORE_HOSTS = ("localhost", "127.0.0.0/8", "::1")

#: Where to ask what the world sees. Three, run in order until one answers,
#: because a single service being down is not the tunnel being broken and must
#: not be reported as such. They are independently operated and all three
#: answer with a bare address in the body, which is the only shape worth
#: parsing without a JSON contract to rely on.
ECHOES = (
    "https://api.ipify.org/",
    "https://checkip.amazonaws.com/",
    "https://icanhazip.com/",
)

#: Seconds for one echo. Short: three of them run in series on a worker thread
#: while the user watches an indicator say "connecting", and a proxy that is
#: not there fails fast enough that the whole sweep is over well inside the
#: control API's own timeout.
PROBE_TIMEOUT = 12

#: Sent to the echo services. Not a browser string: they are being asked a
#: question by the browser's plumbing, not visited.
USER_AGENT = "claude-browser/vpn-check"


class ProbeError(Exception):
    """The exit check did not produce an address. Its message is redacted."""


# -- the proxy ---------------------------------------------------------------

class Proxy:
    """One parsed proxy URL, and everything derived from it.

    Username and password are held decoded. `uri()` re-encodes them, because
    that string goes to WebKit as a URI; `connect_headers()` needs them raw for
    Basic auth. Doing the decode once here is what stops the two halves from
    disagreeing about a password containing a `%`.
    """

    __slots__ = ("scheme", "host", "port", "username", "password")

    def __init__(self, scheme, host, port, username="", password=""):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.username = username or ""
        self.password = password or ""

    # -- projections --------------------------------------------------------

    def uri(self):
        """The URL WebKit is given, credentials included.

        `NetworkProxySettings.new()` takes them embedded -- there is no separate
        credential argument in the 4.1 API -- so this string is a secret and
        must not be printed. Use `safe()` for anything a person reads.
        """
        auth = ""
        if self.username:
            auth = urllib.parse.quote(self.username, safe="")
            if self.password:
                auth += ":" + urllib.parse.quote(self.password, safe="")
            auth += "@"
        return "%s://%s%s:%d" % (self.scheme, auth, self.host, self.port)

    def safe(self):
        """The same URL with the password replaced. For every display and log."""
        auth = ("%s:***@" % self.username) if self.username else ""
        return "%s://%s%s:%d" % (self.scheme, auth, self.host, self.port)

    @property
    def endpoint(self):
        return "%s:%d" % (self.host, self.port)

    @property
    def tunnels(self):
        """Can `http.client` open a CONNECT tunnel through this one?

        Plain HTTP only. A `socks5://` proxy speaks a different protocol
        entirely, and an `https://` one wants TLS to the proxy *before* the
        CONNECT, which http.client cannot do -- it wraps TLS around the
        tunnelled socket, not around the hop to the proxy.
        """
        return self.scheme == "http"

    def connect_headers(self):
        """Headers for the CONNECT itself. Empty when the proxy wants no auth."""
        if not self.username:
            return {}
        raw = ("%s:%s" % (self.username, self.password)).encode("utf-8")
        return {"Proxy-Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def redact(self, text):
        """Strip this proxy's password out of arbitrary text.

        Applied to every message that is built from an exception. A socket error
        carries the address it was given, and for an authenticated proxy that
        address is the credential.
        """
        text = "" if text is None else str(text)
        if not self.password:
            return text
        for form in (self.password, urllib.parse.quote(self.password, safe="")):
            if form:
                text = text.replace(form, "***")
        return text

    def __eq__(self, other):
        return (isinstance(other, Proxy)
                and (self.scheme, self.host, self.port, self.username,
                     self.password)
                == (other.scheme, other.host, other.port, other.username,
                    other.password))

    def __repr__(self):
        # Deliberately the redacted form: a Proxy reaching a traceback, a log
        # line or a debugger prompt must not be how the password gets out.
        return "<Proxy %s>" % self.safe()


def parse_proxy(raw):
    """A `Proxy` from what the user wrote, or a ValueError they can act on.

    Strict about the scheme on purpose. Guessing `http://` for a bare
    `10.0.0.1:8888` would be the kind of helpfulness that hides a typo of
    `htp://`, and every message here has to be readable by someone who is
    looking at cb:vpn wondering why it will not turn on.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError(
            "VPN Mode needs a proxy address. Put a line like\n"
            "    CB_VPN_PROXY=http://user:password@10.0.0.1:8888\n"
            "in the settings file -- it holds a password, so the browser will "
            "not write it for you.")
    if "://" not in text:
        raise ValueError("a proxy address starts with http://, https:// or "
                         "socks5:// -- %r has no scheme" % text)

    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as e:
        raise ValueError("that is not a usable proxy address (%s)" % e)

    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEMES:
        raise ValueError("a proxy has to be http://, https:// or socks5://, "
                         "not %r" % scheme)

    # .hostname and .port both parse lazily and both can raise on a malformed
    # authority, so they are read inside the guard rather than after it.
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as e:
        raise ValueError("that proxy address has no usable host and port (%s)" % e)

    if not host:
        raise ValueError("that proxy address has no host in it")
    if parts.path.strip("/") or parts.query or parts.fragment:
        # A path means someone pasted an endpoint URL rather than a proxy
        # address, and WebKit would silently ignore everything after the port.
        raise ValueError("a proxy address is a host and a port, with no path "
                         "after it")
    if port is None:
        port = DEFAULT_PORTS[scheme]

    username = urllib.parse.unquote(parts.username or "")
    password = urllib.parse.unquote(parts.password or "")
    if password and not username:
        raise ValueError("that proxy address has a password but no username")
    return Proxy(scheme, host, int(port), username, password)


# -- reading the settings ----------------------------------------------------

def enabled(raw=None, path=None):
    """Is VPN Mode asked for?

    Absent means off -- nobody has chosen. Anything else that is not one of the
    words meaning "off" means on, which is the inverse of how `CB_PRIVATE_AI`
    treats a typo and inverted for the same reason: the question is which way a
    misreading fails. Reading `CB_VPN=onn` as off would leave the browser
    loading pages from the user's own address while they believed otherwise.
    Reading it as on costs, at worst, a mode that has to be turned back off.
    """
    if raw is None:
        raw = envfile.setting(SETTING, "", path=path)
    text = (raw or "").strip().lower()
    if not text:
        return False
    return text not in ("0", "off", "false", "no")


def configured(path=None):
    """`(proxy, error)` from the settings file. Exactly one of them is None."""
    try:
        return parse_proxy(envfile.setting(PROXY_SETTING, "", path=path)), None
    except ValueError as e:
        return None, str(e)


# -- the state machine -------------------------------------------------------

class State:
    """Where VPN Mode is, and how it got there.

    Lock-guarded because it is read from two places that are not the same
    thread: the GTK main loop, which owns every transition, and `ai.py`, which
    asks on whatever thread a request happens to be on -- the agent's worker,
    usually.

    `attempt` is what makes a slow probe safe. Every `engage` bumps it, and a
    result carrying an older number is dropped rather than applied: without it,
    a check started before the user turned the mode off and on again would come
    back and report `on` about a proxy that is no longer the one in use.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.mode = OFF
        self.reason = ""
        self.proxy = None
        self.exit_ip = None
        self.service = ""
        self.attempt = 0

    # -- transitions --------------------------------------------------------

    def engage(self, proxy):
        """Enter `connecting` against `proxy`. Returns the attempt token."""
        with self._lock:
            self.attempt += 1
            self.mode = CONNECTING
            self.proxy = proxy
            self.reason = ""
            self.exit_ip = None
            self.service = ""
            return self.attempt

    def verified(self, attempt, exit_ip, service=""):
        """A probe came back with an address. True if it was still wanted."""
        with self._lock:
            if attempt != self.attempt or self.mode == OFF:
                return False
            self.mode = ON
            self.reason = ""
            self.exit_ip = exit_ip
            self.service = service
            return True

    def fail(self, attempt, reason):
        """A probe, or an apply, did not work. True if it was still wanted.

        There is no transition out of here except `engage` or `disengage`. That
        is the whole guarantee: nothing in this class can decide on its own that
        going direct would be better than staying broken.
        """
        with self._lock:
            if attempt != self.attempt or self.mode == OFF:
                return False
            self.mode = FAILED
            self.reason = reason or "the exit check did not succeed"
            self.exit_ip = None
            self.service = ""
            return True

    def refuse(self, reason):
        """Fail without ever having had a proxy: the configuration is unusable.

        Still `failed` and not `off`, which is the whole point. The user asked
        for VPN Mode; a missing or unparseable proxy address means it is not
        happening, and answering that with `off` would be the browser deciding
        on their behalf that going direct is close enough.
        """
        with self._lock:
            self.attempt += 1
            self.mode = FAILED
            self.proxy = None
            self.reason = reason
            self.exit_ip = None
            self.service = ""

    def disengage(self):
        """Turn the mode off. The one deliberate way back to `off`.

        Called only from a person asking for it -- the menu, the shortcut, the
        `vpn off` op. No failure path reaches this.
        """
        with self._lock:
            self.attempt += 1
            self.mode = OFF
            self.proxy = None
            self.reason = ""
            self.exit_ip = None
            self.service = ""

    # -- reads --------------------------------------------------------------

    @property
    def engaged(self):
        """Is the proxy applied to WebKit? True for every state but `off`,
        including `failed` -- which is the point of `failed` existing."""
        return self.mode != OFF

    @property
    def blocks_navigation(self):
        """Should a new page load be refused with an explanation?

        Only in `failed`. In `connecting` the proxy is already applied, so a
        load either goes through the tunnel or does not happen; refusing it
        would buy nothing and would make turning the mode on feel like the
        browser had seized up for the length of the check.
        """
        return self.mode == FAILED

    def snapshot(self):
        """A JSON-serializable copy, with nothing secret in it."""
        with self._lock:
            return {
                "mode": self.mode,
                "on": self.mode == ON,
                "reason": self.reason,
                "proxy": self.proxy.safe() if self.proxy else "",
                "exit_ip": self.exit_ip,
                "service": self.service,
                "ignore_hosts": list(IGNORE_HOSTS),
            }

    def route(self):
        """`(proxy, refusal)` for an outbound API request. One is always None.

        The three answers, in the order they matter:

        * `failed` -- refuse. The user asked for their address to be hidden and
          the browser cannot do it; sending the question direct would leak both
          the request and the fact that the mode is not working.
        * engaged with a proxy this build cannot tunnel through -- refuse, for
          the same reason. `engage` will not get here, but a refusal is the
          right answer if it ever does.
        * anything else -- go, direct or tunnelled as the mode says.
        """
        with self._lock:
            mode, proxy = self.mode, self.proxy
        if mode == OFF:
            return None, None
        if mode == FAILED:
            return None, (
                "[VPN Mode failed] The browser is not sending anything while "
                "VPN Mode cannot reach its proxy, including this question. "
                "Fix the proxy or turn VPN Mode off in the menu — it will not "
                "quietly send it from your own address instead.")
        if proxy is None or not proxy.tunnels:
            return None, (
                "[VPN Mode] This build tunnels the Anthropic API through an "
                "http:// proxy only, so the question was not sent rather than "
                "sent around the tunnel.")
        return proxy, None

    def transport_key(self):
        """A name for the path the next connection takes.

        `ai.py`'s pool keys its idle sockets on this. A connection opened
        before the mode changed is not interchangeable with one opened after
        it, and reusing one would put a question on the path the user has just
        asked the browser to stop using -- with nothing in the answer to say so.
        """
        with self._lock:
            if self.mode == OFF:
                return "direct"
            if self.proxy is None:
                # Engaged with nothing to engage through. No request gets this
                # far -- `route` refuses first -- but the key still has to be
                # something a direct socket can never match.
                return "blocked"
            return "vpn:%s" % self.proxy.endpoint


#: The one instance. Module-level rather than owned by the Browser because
#: `ai.py` has to consult it on a worker thread and has no window to ask.
STATE = State()


def snapshot():
    return STATE.snapshot()


def transport_key():
    return STATE.transport_key()


def api_route():
    """`(proxy, refusal)` for `ai.py`. See `State.route`."""
    return STATE.route()


# -- the tunnel ---------------------------------------------------------------

def open_tunnel(proxy, host, port, timeout, context=None):
    """An HTTPS connection to `(host, port)` whose socket is a CONNECT tunnel.

    `http.client` sends the CONNECT in the clear and *then* wraps TLS around the
    tunnelled socket, so the certificate is still validated against `host` and
    not against the proxy. The proxy learns the hostname and nothing else, which
    is the same thing it learns from any HTTPS page this browser loads.

    The connection is returned unconnected: http.client dials on the first
    request, which is what lets the caller own the timing and the failure.
    """
    if not proxy.tunnels:
        raise ProbeError("this build can only tunnel through an http:// proxy, "
                         "not %s://" % proxy.scheme)
    conn = http.client.HTTPSConnection(proxy.host, proxy.port, timeout=timeout,
                                       context=context)
    conn.set_tunnel(host, port, headers=proxy.connect_headers())
    return conn


def _as_ip(body):
    """The address out of an echo's reply, or a ProbeError.

    Parsed with `ipaddress` rather than a regex, so a captive portal's HTML, a
    proxy's own error page, or an empty body is a failure and not a string that
    gets displayed as if it were an address.
    """
    first = (body or "").strip().splitlines()
    token = first[0].strip() if first else ""
    try:
        return str(ipaddress.ip_address(token))
    except ValueError:
        if not token:
            raise ProbeError("answered with nothing")
        shown = token[:60] + ("…" if len(token) > 60 else "")
        raise ProbeError("answered with %r, which is not an address" % shown)


def _echo_once(proxy, url, timeout, opener=None):
    parts = urllib.parse.urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    path = parts.path or "/"
    conn = (opener or open_tunnel)(proxy, host, port, timeout)
    try:
        conn.request("GET", path, headers={
            "Host": host,
            "User-Agent": USER_AGENT,
            "Accept": "text/plain",
            # No keep-alive: this socket is used once and thrown away, and
            # leaving it open would hold a tunnel through the proxy for nothing.
            "Connection": "close",
        })
        response = conn.getresponse()
        body = response.read(2048).decode("utf-8", "replace")
        if response.status != 200:
            # 407 is the interesting one: the tunnel reached the proxy and the
            # credential was wrong, which is a different fix from "unreachable".
            raise ProbeError("HTTP %s from the proxy or the echo service%s"
                             % (response.status,
                                " (the proxy rejected the credential)"
                                if response.status == 407 else ""))
        return _as_ip(body)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def probe_exit_ip(proxy, echoes=ECHOES, timeout=PROBE_TIMEOUT, opener=None):
    """What the outside world sees, asked through `proxy`.

    Never call this on the GTK main loop: it is up to three TLS handshakes over
    a network hop. `Browser` runs it on a worker thread and brings the answer
    back through `GLib.idle_add`.

    Returns `(exit_ip, service)`. Raises `ProbeError` naming what each service
    said, with the password stripped out of every one of those messages.
    """
    problems = []
    for url in echoes:
        host = urllib.parse.urlsplit(url).hostname or url
        try:
            return _echo_once(proxy, url, timeout, opener), host
        except Exception as e:
            problems.append("%s: %s" % (host, proxy.redact(str(e) or type(e).__name__)))
    raise ProbeError("; ".join(problems) or "no echo service was tried")
