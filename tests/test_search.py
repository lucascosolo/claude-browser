"""The web-search client: request shape, response parsing, and refusals.

GTK-free and offline. The response fixture below mirrors LangSearch's real
envelope -- `code`, then `data.webPages.value` -- which was read off the live
API rather than from memory.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import search  # noqa: E402


def envelope(rows, code=200):
    return {"code": code, "data": {"_type": "SearchResponse",
                                   "webPages": {"value": rows}}}


def row(url="https://example.com/a", name="A title", snippet="A snippet",
        summary="A summary", date="2026-01-01"):
    return {"id": "1", "name": name, "url": url, "displayUrl": url,
            "snippet": snippet, "summary": summary, "datePublished": date}


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConn:
    def __init__(self, response):
        self.response = response
        self.sent = None
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.sent = (method, path, body, headers or {})

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Parsing(unittest.TestCase):
    def test_the_real_envelope_is_understood(self):
        result = search.parse(envelope([row(), row(url="https://b.test/")]))
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(len(result["results"]), 2)
        first = result["results"][0]
        self.assertEqual(first.title, "A title")
        self.assertEqual(first.url, "https://example.com/a")
        self.assertEqual(first.snippet, "A snippet")
        self.assertEqual(first.date, "2026-01-01")

    def test_a_result_with_no_url_is_dropped(self):
        # Nowhere to go is not a result, and it would render as a dead row.
        result = search.parse(envelope([row(url=""), row()]))
        self.assertEqual(len(result["results"]), 1)

    def test_a_missing_title_falls_back_to_the_url(self):
        result = search.parse(envelope([row(name="")]))
        self.assertEqual(result["results"][0].title, "https://example.com/a")

    def test_an_api_level_error_code_beats_http_200(self):
        # LangSearch reports its own failures in `code` alongside HTTP 200.
        payload = dict(envelope([], code=401), msg="invalid key")
        result = search.parse(payload)
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertIn("invalid key", result["error"])

    def test_a_reshaped_response_is_reported_not_returned_empty(self):
        result = search.parse({"code": 200, "data": {"nothing": "here"}})
        self.assertFalse(result["ok"])
        self.assertIn("changed shape", result["error"])

    def test_junk_does_not_raise(self):
        for payload in (None, [], "text", 7, {}):
            self.assertFalse(search.parse(payload)["ok"])

    def test_an_unknown_provider_is_refused_rather_than_guessed(self):
        self.assertIn("unknown search provider",
                      search.parse(envelope([]), provider="nope")["error"])

    def test_rows_that_are_not_objects_are_skipped(self):
        result = search.parse(envelope(["oops", None, row()]))
        self.assertEqual(len(result["results"]), 1)


class Requesting(unittest.TestCase):
    def call(self, status=200, body=None, **kw):
        payload = body if body is not None else json.dumps(envelope([row()]))
        conn = FakeConn(FakeResponse(status, payload.encode()))
        result = search.fetch("cats", "KEY", route=("PROXY", None),
                              opener=lambda *a: conn, **kw)
        return conn, result

    def test_the_key_goes_in_an_authorization_header(self):
        conn, (payload, error) = self.call()
        self.assertEqual(error, "")
        _method, _path, _body, headers = conn.sent
        self.assertEqual(headers["Authorization"], "Bearer KEY")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_it_posts_the_documented_endpoint_and_body(self):
        conn, _ = self.call()
        method, path, body, _headers = conn.sent
        self.assertEqual((method, path), ("POST", "/v1/web-search"))
        self.assertEqual(json.loads(body)["query"], "cats")
        self.assertEqual(json.loads(body)["count"], search.COUNT)

    def test_a_rejected_key_says_so(self):
        _conn, (payload, error) = self.call(status=401)
        self.assertIsNone(payload)
        self.assertIn("key was rejected", error)

    def test_a_non_json_body_is_an_error_not_a_crash(self):
        _conn, (payload, error) = self.call(body="<html>nope</html>")
        self.assertIsNone(payload)
        self.assertIn("did not return JSON", error)

    def test_the_socket_is_closed_even_on_failure(self):
        conn, _ = self.call(status=500)
        self.assertTrue(conn.closed)

    def test_a_missing_key_never_reaches_the_network(self):
        def opener(*a):
            raise AssertionError("called the API with no key")

        payload, error = search.fetch("cats", "", route=("PROXY", None),
                                      opener=opener)
        self.assertIsNone(payload)
        self.assertIn("no search API key", error)

    def test_an_empty_query_never_reaches_the_network(self):
        def opener(*a):
            raise AssertionError("searched for nothing")

        payload, error = search.fetch("   ", "KEY", route=("PROXY", None),
                                      opener=opener)
        self.assertIn("nothing to search for", error)


class Routing(unittest.TestCase):
    """A query is the most revealing thing this browser sends anywhere."""

    def test_a_refused_vpn_route_does_not_search(self):
        def opener(*a):
            raise AssertionError("searched despite a VPN refusal")

        payload, error = search.fetch("cats", "KEY",
                                      route=(None, "VPN Mode is not up"),
                                      opener=opener)
        self.assertIsNone(payload)
        self.assertEqual(error, "VPN Mode is not up")

    def test_a_proxy_route_tunnels_to_the_api_host(self):
        seen = {}
        conn = FakeConn(FakeResponse(200, json.dumps(envelope([row()])).encode()))

        def opener(proxy, host, port, timeout):
            seen.update(proxy=proxy, host=host, port=port)
            return conn

        search.fetch("cats", "KEY", route=("PROXY", None), opener=opener)
        self.assertEqual(seen, {"proxy": "PROXY",
                                "host": "api.langsearch.com", "port": 443})

    def test_a_network_failure_is_a_message(self):
        def opener(*a):
            raise OSError("dns went away")

        payload, error = search.fetch("cats", "KEY", route=("PROXY", None),
                                      opener=opener)
        self.assertIn("could not reach the search API", error)


class EndToEnd(unittest.TestCase):
    def test_search_fetches_then_parses(self):
        result = search.search(
            "cats", key="KEY",
            fetcher=lambda *a: (envelope([row(), row(url="https://b/")]), ""))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 2)

    def test_a_fetch_error_stops_before_the_parse(self):
        result = search.search("cats", key="KEY",
                               fetcher=lambda *a: (None, "refused"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "refused")
        self.assertEqual(result["results"], [])


class OmniboxTemplate(unittest.TestCase):
    """cb:search has to be droppable into CB_SEARCH unchanged."""

    def test_the_template_has_exactly_one_substitution(self):
        self.assertEqual(search.TEMPLATE.count("%s"), 1)

    def test_a_query_survives_the_round_trip(self):
        from urllib.parse import quote
        for text in ("cats", "a b", "c++ & rust", "100% sure", "a/b?c=d",
                     "héllo wörld", "quote\"s"):
            url = search.TEMPLATE % quote(text, safe="")
            self.assertEqual(search.query_of(url.partition("?")[2]), text)

    def test_a_missing_q_is_empty_not_an_error(self):
        for raw in ("", "x=1", "qq=2", None):
            self.assertEqual(search.query_of(raw), "")


class Secrecy(unittest.TestCase):
    def test_the_key_is_a_secret_the_browser_cannot_write(self):
        from claudebrowser import envfile
        self.assertIn(search.KEY_ENV, envfile.SECRET_KEYS)
        with self.assertRaises(ValueError):
            envfile.put(search.KEY_ENV, "sk-whatever")


if __name__ == "__main__":
    unittest.main()
