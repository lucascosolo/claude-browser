"""VPN Mode's policy half: the URL, the state machine, and the exit check.

GTK-free, like `vpn.py` itself, and network-free -- the probe is exercised
through an injected opener, so the whole file runs in milliseconds and says
nothing about whether the developer happened to have a proxy up.

The assertions worth reading are the refusals. VPN Mode's one real promise is
that it does not quietly stop working, so most of what is pinned here is that a
failure stays a failure: `failed` has no path back to `off` except a person,
`route()` refuses rather than answering "go direct", and the pool key changes
the instant the mode does.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import ai, pages, style, vpn  # noqa: E402

PROXY = "http://cb:s3cr3t@100.91.16.6:8888"


class ParseTest(unittest.TestCase):
    def test_a_full_url_round_trips(self):
        proxy = vpn.parse_proxy(PROXY)
        self.assertEqual(
            (proxy.scheme, proxy.host, proxy.port, proxy.username, proxy.password),
            ("http", "100.91.16.6", 8888, "cb", "s3cr3t"))
        self.assertEqual(proxy.uri(), PROXY)
        self.assertEqual(proxy.endpoint, "100.91.16.6:8888")

    def test_surrounding_whitespace_is_not_a_syntax_error(self):
        self.assertEqual(vpn.parse_proxy("  %s\n" % PROXY).uri(), PROXY)

    def test_a_missing_port_gets_the_scheme_default(self):
        self.assertEqual(vpn.parse_proxy("http://box").port, 8080)
        self.assertEqual(vpn.parse_proxy("socks5://box").port, 1080)

    def test_credentials_are_optional(self):
        proxy = vpn.parse_proxy("http://10.0.0.5:3128")
        self.assertEqual(proxy.username, "")
        self.assertEqual(proxy.connect_headers(), {})
        self.assertEqual(proxy.uri(), "http://10.0.0.5:3128")

    def test_a_percent_encoded_password_survives_both_directions(self):
        """The URI goes to WebKit encoded and the CONNECT header wants it raw.
        Decoding once, at parse time, is what stops the two disagreeing."""
        proxy = vpn.parse_proxy("http://u:p%40ss%2Fword@box:8888")
        self.assertEqual(proxy.password, "p@ss/word")
        self.assertEqual(proxy.uri(), "http://u:p%40ss%2Fword@box:8888")
        self.assertEqual(vpn.parse_proxy(proxy.uri()).password, "p@ss/word")

    def test_the_three_schemes_parse_and_nothing_else_does(self):
        for scheme in vpn.SCHEMES:
            self.assertEqual(vpn.parse_proxy("%s://box:9" % scheme).scheme, scheme)
        for bad in ("ftp://box:9", "ssh://box:22", "gopher://box"):
            with self.assertRaises(ValueError, msg=bad):
                vpn.parse_proxy(bad)

    def test_only_http_can_be_tunnelled_through(self):
        """WebKit would take all three. The exit check and the Anthropic tunnel
        are an HTTP CONNECT, and that is what decides what may be engaged."""
        self.assertTrue(vpn.parse_proxy("http://box:8888").tunnels)
        self.assertFalse(vpn.parse_proxy("https://box:8888").tunnels)
        self.assertFalse(vpn.parse_proxy("socks5://box:1080").tunnels)

    def test_nothing_shaped_wrong_is_accepted(self):
        for bad in ("", "   ", None,
                    "10.0.0.1:8888",            # no scheme: could be a typo'd one
                    "http://",                  # no host
                    "http://:8888",             # no host, with a port
                    "http://box:notaport",
                    "http://box:99999",
                    "http://box:8888/proxy.pac",  # an endpoint, not a proxy
                    "http://box:8888?a=1",
                    "http://:pass@box:8888"):     # a password with nobody to use it
            with self.assertRaises(ValueError, msg=repr(bad)):
                vpn.parse_proxy(bad)

    def test_every_refusal_says_something_a_person_can_act_on(self):
        for bad in ("", "10.0.0.1:8888", "ftp://box", "http://box:8888/pac"):
            try:
                vpn.parse_proxy(bad)
            except ValueError as e:
                self.assertGreater(len(str(e)), 25, bad)


class RedactionTest(unittest.TestCase):
    def setUp(self):
        self.proxy = vpn.parse_proxy(PROXY)

    def test_the_displayed_form_has_the_password_out_of_it(self):
        self.assertEqual(self.proxy.safe(), "http://cb:***@100.91.16.6:8888")
        self.assertNotIn("s3cr3t", self.proxy.safe())

    def test_repr_is_the_safe_form(self):
        """A Proxy in a traceback, a log line or a debugger is not how the
        password gets out."""
        self.assertNotIn("s3cr3t", repr(self.proxy))

    def test_a_message_built_from_the_uri_is_scrubbed(self):
        message = "could not connect to %s" % self.proxy.uri()
        self.assertNotIn("s3cr3t", self.proxy.redact(message))
        self.assertIn("***", self.proxy.redact(message))

    def test_the_encoded_spelling_is_scrubbed_too(self):
        proxy = vpn.parse_proxy("http://u:p%40ss@box:8888")
        for form in ("p@ss", "p%40ss"):
            self.assertNotIn(form, proxy.redact("failed: %s" % form))

    def test_a_proxy_with_no_password_redacts_nothing(self):
        proxy = vpn.parse_proxy("http://box:8888")
        self.assertEqual(proxy.redact("plain text"), "plain text")

    def test_a_snapshot_never_carries_the_password(self):
        state = vpn.State()
        state.engage(self.proxy)
        self.assertNotIn("s3cr3t", repr(state.snapshot()))
        self.assertEqual(state.snapshot()["proxy"], self.proxy.safe())


class IgnoreHostsTest(unittest.TestCase):
    def test_loopback_never_goes_through_the_proxy(self):
        """The browser's own control API and everything it serves itself are on
        loopback; sending them round the world would be absurd, and there is
        nothing to hide from a socket that never leaves the machine."""
        self.assertIn("localhost", vpn.IGNORE_HOSTS)
        self.assertIn("127.0.0.0/8", vpn.IGNORE_HOSTS)
        self.assertIn("::1", vpn.IGNORE_HOSTS)

    def test_private_ranges_are_deliberately_not_bypassed(self):
        """The one exception is loopback. A LAN address is still a request the
        user made, and an exception for it would be a hole shaped exactly like
        the thing this mode exists to close."""
        for leak in ("10.0.0.0/8", "192.168.0.0/16", "172.16.0.0/12",
                     "169.254.0.0/16"):
            self.assertNotIn(leak, vpn.IGNORE_HOSTS)

    def test_cb_pages_need_no_entry(self):
        """cb: is served by the browser's own scheme handler and never becomes
        a network request, so there is nothing for a bypass list to say."""
        self.assertFalse([h for h in vpn.IGNORE_HOSTS if h.startswith("cb")])


class EnabledTest(unittest.TestCase):
    def test_absent_means_off(self):
        for raw in ("", "   ", None):
            self.assertFalse(vpn.enabled(raw or ""))

    def test_the_words_that_mean_off_mean_off(self):
        for raw in ("0", "off", "OFF", "false", "no", " no "):
            self.assertFalse(vpn.enabled(raw), raw)

    def test_anything_else_means_on(self):
        """The inverse of every other default-off knob here, on purpose:
        misreading this one as off means browsing from your own address while
        the browser says otherwise."""
        for raw in ("1", "on", "yes", "true", "onn", "yeah"):
            self.assertTrue(vpn.enabled(raw), raw)

    def test_it_reads_the_file_when_given_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("CB_VPN=on\n")
            self.assertTrue(vpn.enabled(path=path))
            path.write_text("CB_VPN=0\n")
            self.assertFalse(vpn.enabled(path=path))

    def test_configured_reports_the_reason_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("CB_VPN_PROXY=nonsense\n")
            proxy, error = vpn.configured(path=path)
            self.assertIsNone(proxy)
            self.assertIn("scheme", error)
            path.write_text("CB_VPN_PROXY=%s\n" % PROXY)
            proxy, error = vpn.configured(path=path)
            self.assertIsNone(error)
            self.assertEqual(proxy.endpoint, "100.91.16.6:8888")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.state = vpn.State()
        self.proxy = vpn.parse_proxy(PROXY)

    def test_it_starts_off_and_direct(self):
        self.assertEqual(self.state.mode, vpn.OFF)
        self.assertFalse(self.state.engaged)
        self.assertFalse(self.state.blocks_navigation)
        self.assertEqual(self.state.route(), (None, None))
        self.assertEqual(self.state.transport_key(), "direct")

    def test_engaging_is_not_yet_on(self):
        """The distinction the whole design turns on: the proxy is applied, so
        traffic is covered, but nothing has been proven yet."""
        self.state.engage(self.proxy)
        self.assertEqual(self.state.mode, vpn.CONNECTING)
        self.assertTrue(self.state.engaged)
        self.assertFalse(self.state.snapshot()["on"])
        # and traffic still goes through it while the check runs
        self.assertEqual(self.state.route(), (self.proxy, None))

    def test_a_verified_exit_is_what_turns_it_on(self):
        attempt = self.state.engage(self.proxy)
        self.assertTrue(self.state.verified(attempt, "162.35.172.112", "ipify"))
        self.assertEqual(self.state.mode, vpn.ON)
        self.assertEqual(self.state.snapshot()["exit_ip"], "162.35.172.112")
        self.assertEqual(self.state.snapshot()["service"], "ipify")

    def test_a_failed_check_never_becomes_going_direct(self):
        attempt = self.state.engage(self.proxy)
        self.state.fail(attempt, "no route to the proxy")
        self.assertEqual(self.state.mode, vpn.FAILED)
        self.assertTrue(self.state.engaged, "failed is still engaged")
        self.assertTrue(self.state.blocks_navigation)
        proxy, refusal = self.state.route()
        self.assertIsNone(proxy)
        self.assertTrue(refusal)

    def test_a_failure_with_no_proxy_at_all_is_still_a_failure(self):
        """A missing or unparseable address is not "off". The user asked for
        VPN Mode; answering with off would be the browser deciding for them."""
        self.state.refuse("nothing configured")
        self.assertEqual(self.state.mode, vpn.FAILED)
        self.assertTrue(self.state.blocks_navigation)
        self.assertIsNone(self.state.proxy)
        self.assertEqual(self.state.transport_key(), "blocked")

    def test_nothing_but_disengage_reaches_off(self):
        attempt = self.state.engage(self.proxy)
        self.state.fail(attempt, "broken")
        # Every read that could plausibly be a way out, tried:
        self.state.fail(attempt, "still broken")
        self.state.route()
        self.state.snapshot()
        self.assertEqual(self.state.mode, vpn.FAILED)
        self.state.disengage()
        self.assertEqual(self.state.mode, vpn.OFF)
        self.assertIsNone(self.state.proxy)

    def test_a_late_probe_cannot_report_on_a_proxy_that_was_replaced(self):
        stale = self.state.engage(self.proxy)
        other = vpn.parse_proxy("http://box:8888")
        self.state.engage(other)
        self.assertFalse(self.state.verified(stale, "1.2.3.4", "ipify"))
        self.assertEqual(self.state.mode, vpn.CONNECTING)
        self.assertFalse(self.state.fail(stale, "old failure"))
        self.assertEqual(self.state.mode, vpn.CONNECTING)

    def test_a_probe_that_lands_after_the_mode_was_turned_off_is_dropped(self):
        attempt = self.state.engage(self.proxy)
        self.state.disengage()
        self.assertFalse(self.state.verified(attempt, "1.2.3.4"))
        self.assertEqual(self.state.mode, vpn.OFF)

    def test_the_transport_key_moves_with_the_mode(self):
        self.assertEqual(self.state.transport_key(), "direct")
        self.state.engage(self.proxy)
        self.assertEqual(self.state.transport_key(), "vpn:100.91.16.6:8888")
        self.state.disengage()
        self.assertEqual(self.state.transport_key(), "direct")

    def test_a_proxy_that_cannot_be_tunnelled_is_refused_not_sent_direct(self):
        self.state.engage(vpn.parse_proxy("socks5://box:1080"))
        proxy, refusal = self.state.route()
        self.assertIsNone(proxy)
        self.assertIn("http://", refusal)


class ProbeTest(unittest.TestCase):
    """The exit check, with the socket replaced. Reachability is not the thing
    under test here -- what it does with each *answer* is."""

    def setUp(self):
        self.proxy = vpn.parse_proxy(PROXY)

    def opener(self, plan):
        """`plan` maps a host to a body, a status, or an exception to raise."""
        self.asked = []

        def open_tunnel(proxy, host, port, timeout, context=None):
            self.asked.append((proxy.endpoint, host, port))
            answer = plan.get(host, ConnectionRefusedError("nothing there"))
            if isinstance(answer, Exception):
                raise answer
            return FakeTunnel(answer)
        return open_tunnel

    def test_the_first_service_that_answers_wins(self):
        opener = self.opener({"api.ipify.org": "162.35.172.112\n"})
        self.assertEqual(vpn.probe_exit_ip(self.proxy, opener=opener),
                         ("162.35.172.112", "api.ipify.org"))
        self.assertEqual(len(self.asked), 1, "no second service was needed")

    def test_one_service_being_down_is_not_the_tunnel_being_broken(self):
        opener = self.opener({"api.ipify.org": OSError("timed out"),
                              "checkip.amazonaws.com": "162.35.172.112"})
        exit_ip, service = vpn.probe_exit_ip(self.proxy, opener=opener)
        self.assertEqual(exit_ip, "162.35.172.112")
        self.assertEqual(service, "checkip.amazonaws.com")

    def test_every_service_failing_is_reported_with_all_of_them(self):
        with self.assertRaises(vpn.ProbeError) as caught:
            vpn.probe_exit_ip(self.proxy, opener=self.opener({}))
        for url in vpn.ECHOES:
            self.assertIn(url.split("//")[1].rstrip("/"), str(caught.exception))

    def test_the_reported_failure_never_carries_the_password(self):
        boom = OSError("connect to %s failed" % self.proxy.uri())
        opener = self.opener({host: boom for host in
                              ("api.ipify.org", "checkip.amazonaws.com",
                               "icanhazip.com")})
        with self.assertRaises(vpn.ProbeError) as caught:
            vpn.probe_exit_ip(self.proxy, opener=opener)
        self.assertNotIn("s3cr3t", str(caught.exception))

    def test_a_body_that_is_not_an_address_is_a_failure(self):
        """A captive portal, a proxy error page, or an empty reply must not be
        rendered as though it were the address the world sees."""
        for body in ("<html>blocked</html>", "", "   \n", "not.an.ip",
                     "999.1.1.1"):
            opener = self.opener({"api.ipify.org": body,
                                  "checkip.amazonaws.com": body,
                                  "icanhazip.com": body})
            with self.assertRaises(vpn.ProbeError, msg=repr(body)):
                vpn.probe_exit_ip(self.proxy, opener=opener)

    def test_a_rejected_credential_says_so(self):
        opener = self.opener({"api.ipify.org": (407, "denied"),
                              "checkip.amazonaws.com": (407, "denied"),
                              "icanhazip.com": (407, "denied")})
        with self.assertRaises(vpn.ProbeError) as caught:
            vpn.probe_exit_ip(self.proxy, opener=opener)
        self.assertIn("credential", str(caught.exception))

    def test_an_ipv6_answer_is_an_answer(self):
        opener = self.opener({"api.ipify.org": "2606:4700:4700::1111"})
        self.assertEqual(vpn.probe_exit_ip(self.proxy, opener=opener)[0],
                         "2606:4700:4700::1111")

    def test_it_is_asked_through_the_proxy_and_on_443(self):
        vpn.probe_exit_ip(self.proxy, opener=self.opener(
            {"api.ipify.org": "1.2.3.4"}))
        self.assertEqual(self.asked[0], ("100.91.16.6:8888", "api.ipify.org", 443))

    def test_the_tunnel_refuses_a_proxy_it_cannot_speak_to(self):
        for bad in ("socks5://box:1080", "https://box:8888"):
            with self.assertRaises(vpn.ProbeError, msg=bad):
                vpn.open_tunnel(vpn.parse_proxy(bad), "example.com", 443, 5)


class FakeTunnel:
    """An http.client-shaped connection that answers once and is thrown away."""

    def __init__(self, answer):
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        self.response = FakeHttpResponse(status, body)
        self.closed = False

    def request(self, method, path, headers=None):
        self.method, self.path, self.headers = method, path, headers

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class FakeHttpResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self, amt=None):
        return self._body[:amt] if amt else self._body


class ConnectHeaderTest(unittest.TestCase):
    def test_basic_auth_is_built_from_the_decoded_credential(self):
        import base64

        proxy = vpn.parse_proxy(PROXY)
        header = proxy.connect_headers()["Proxy-Authorization"]
        self.assertTrue(header.startswith("Basic "))
        self.assertEqual(base64.b64decode(header[6:]).decode(), "cb:s3cr3t")

    def test_a_proxy_with_no_username_sends_no_header(self):
        self.assertEqual(vpn.parse_proxy("http://box:8888").connect_headers(), {})


class AiTransportTest(unittest.TestCase):
    """`ai.py`'s side of it: which route a request takes, and the one hazard
    that comes with a connection pool the route can change under."""

    def setUp(self):
        self.addCleanup(vpn.STATE.disengage)
        self.proxy = vpn.parse_proxy(PROXY)

    def test_the_route_name_follows_the_mode(self):
        self.assertEqual(ai.transport_route(), "direct")
        vpn.STATE.engage(self.proxy)
        self.assertEqual(ai.transport_route(), "vpn:100.91.16.6:8888")

    def test_a_direct_socket_is_never_reused_for_a_tunnelled_request(self):
        """The hazard a keyed pool exists for. Turning VPN Mode on does not
        close the connection already parked in the pool, and handing that one
        back out would put the next question on the path the user just asked
        the browser to stop using."""
        made = []

        def connect(timeout):
            made.append(FakeSocketish())
            return made[-1]

        pool = ai._Pool(connect)
        direct, _ = pool.take(30)
        pool.give_back(direct)

        vpn.STATE.engage(self.proxy)
        tunnelled, reused = pool.take(30)
        self.assertFalse(reused)
        self.assertIsNot(tunnelled, direct)
        self.assertTrue(direct.closed, "the unusable one is closed, not leaked")
        self.assertEqual(tunnelled._cb_route, "vpn:100.91.16.6:8888")

    def test_a_tunnelled_socket_is_never_reused_after_the_mode_is_off(self):
        made = []
        pool = ai._Pool(lambda timeout: made.append(FakeSocketish()) or made[-1])
        vpn.STATE.engage(self.proxy)
        tunnelled, _ = pool.take(30)
        pool.give_back(tunnelled)
        vpn.STATE.disengage()
        again, reused = pool.take(30)
        self.assertFalse(reused)
        self.assertIsNot(again, tunnelled)

    def test_a_socket_is_still_reused_when_nothing_changed(self):
        """The keying must not have cost the pool its whole reason to exist."""
        made = []
        pool = ai._Pool(lambda timeout: made.append(FakeSocketish()) or made[-1])
        first, _ = pool.take(30)
        pool.give_back(first)
        again, reused = pool.take(30)
        self.assertIs(again, first)
        self.assertTrue(reused)
        self.assertEqual(len(made), 1)

    def test_a_failed_mode_refuses_the_request_rather_than_sending_it_direct(self):
        vpn.STATE.refuse("the proxy is not reachable")
        with self.assertRaises(ai.ApiError) as caught:
            ai._open({"model": "x"}, sleep=lambda _s: None)
        self.assertIn("VPN Mode", str(caught.exception))

    def test_the_socket_factory_itself_refuses_rather_than_going_direct(self):
        """The gate in `_open` makes this unreachable today. It is asserted
        because this is where a socket is *made*: a future caller that skipped
        the gate must not be able to get a direct connection out of it."""
        vpn.STATE.refuse("no proxy")
        with self.assertRaises(ai.ApiError):
            ai._new_connection(5)

    def test_the_refusal_says_what_to_do_about_it(self):
        vpn.STATE.refuse("broken")
        _proxy, refusal = vpn.api_route()
        self.assertIn("turn VPN Mode off", refusal)

    def test_an_engaged_mode_does_not_refuse(self):
        vpn.STATE.engage(self.proxy)
        proxy, refusal = vpn.api_route()
        self.assertIsNone(refusal)
        self.assertIs(proxy, self.proxy)

    def test_resetting_the_transport_closes_what_was_parked(self):
        made = []
        ai._POOL._connect = lambda timeout: made.append(FakeSocketish()) or made[-1]
        self.addCleanup(setattr, ai._POOL, "_connect", ai._new_connection)
        conn, _ = ai._POOL.take(30)
        ai._POOL.give_back(conn)
        ai.reset_transport()
        self.assertTrue(conn.closed)
        self.assertEqual(ai._POOL.idle_count(), 0)


class FakeSocketish:
    """Just enough of a connection for the pool: it can be labelled and closed."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class PageTest(unittest.TestCase):
    """cb:vpn. The page is the only place the honest wording lives, so what is
    checked here is that it is on it -- in every theme."""

    def render(self, state, theme="phosphor"):
        return pages.vpn_page(style.palette(theme), "NONCE", state)

    def state(self, **kw):
        base = vpn.State()
        return dict(base.snapshot(), **kw)

    def test_it_renders_in_every_theme(self):
        for theme in style.THEME_NAMES:
            page = self.render(self.state(), theme)
            self.assertIn("VPN Mode", page)
            self.assertIn("<!doctype html>", page.lower())

    def test_the_exit_address_is_shown_when_there_is_one(self):
        page = self.render(self.state(mode="on", on=True,
                                      exit_ip="162.35.172.112",
                                      service="api.ipify.org"))
        self.assertIn("162.35.172.112", page)
        self.assertIn("api.ipify.org", page)

    def test_it_never_claims_to_be_a_vpn(self):
        page = self.render(self.state(mode="on", on=True, exit_ip="1.2.3.4"))
        for honest in ("It is not a VPN", "best-effort", "WebRTC",
                       "Nothing outside this browser goes through it",
                       "You are not anonymous"):
            self.assertIn(honest, page)

    def test_the_failed_state_says_loads_are_blocked_not_direct(self):
        page = self.render(self.state(mode="failed",
                                      reason="the proxy refused the credential"))
        self.assertIn("blocked", page)
        self.assertIn("does not fall back", page)
        self.assertIn("the proxy refused the credential", page)

    def test_the_proxy_is_shown_redacted_and_only_redacted(self):
        proxy = vpn.parse_proxy(PROXY)
        state = vpn.State()
        state.engage(proxy)
        page = self.render(state.snapshot())
        self.assertIn("100.91.16.6", page)
        self.assertNotIn("s3cr3t", page)

    def test_a_hostile_reason_cannot_reach_the_document_as_markup(self):
        """The reason is built from an exception, and an exception can carry a
        server-controlled hostname."""
        page = self.render(self.state(mode="failed",
                                      reason="<script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_a_working_tunnel_does_not_look_like_an_alert(self):
        """Every state but `off` gets a notice band, and the band has to tell
        them apart: "verified end to end" in the same ink as "failed" is the
        one thing a working tunnel must not look like."""
        good = self.render(self.state(mode="on", on=True, exit_ip="1.2.3.4"))
        self.assertIn('class="notice good"', good)
        bad = self.render(self.state(mode="failed", reason="x"))
        self.assertIn('class="notice bad"', bad)
        self.assertIn(".notice.good", good, "the band needs a rule to match")

    def test_the_off_state_offers_the_way_on_and_nothing_else(self):
        page = self.render(self.state())
        self.assertIn("vpn_on", page)
        self.assertNotIn("vpn_check", page)

    def test_an_engaged_state_offers_the_way_off_and_a_recheck(self):
        page = self.render(self.state(mode="on", on=True, exit_ip="1.2.3.4"))
        self.assertIn("vpn_off", page)
        self.assertIn("vpn_check", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
