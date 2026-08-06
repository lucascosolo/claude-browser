"""The Watch Later parser, against a real page's shape.

`fixtures/playlist_page.json` is three item lockups taken verbatim from a live
playlist page, wrapped in the real nesting and with the tracking noise pruned.
It is a fixture rather than a hand-written dict on purpose: the failure this
suite exists to catch is a parser written from a *remembered* shape, and a
hand-written fixture would encode the same memory twice and pass.
"""

import json
import os
import unittest

from claudebrowser import watchlater

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "playlist_page.json")


def page(data, tail="</script>"):
    """Wrap parsed data back up the way YouTube ships it."""
    return "<!doctype html><html><body><script>var ytInitialData = %s;%s" % (
        json.dumps(data), tail)


class Duration(unittest.TestCase):
    def test_clock_forms(self):
        self.assertEqual(watchlater.duration_seconds("16:09"), 969)
        self.assertEqual(watchlater.duration_seconds("1:02:03"), 3723)
        self.assertEqual(watchlater.duration_seconds("0:30"), 30)

    def test_not_a_clock_is_none_not_zero(self):
        # None and 0 mean different things to a player deciding what to do when
        # an item ends, so a live stream must not read as a zero-length video.
        for text in ("", None, "LIVE", "Premieres 8/6/26", "12", "1:2:3:4",
                     "ab:cd"):
            self.assertIsNone(watchlater.duration_seconds(text), text)


class ParseRealShape(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE) as fh:
            self.data = json.load(fh)

    def test_parses_every_video_in_the_fixture(self):
        result = watchlater.parse(page(self.data))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["items"]), 3)

    def test_fields_come_off_the_real_paths(self):
        item = watchlater.parse(page(self.data))["items"][0]
        self.assertEqual(item.video_id, "Vh4O04Bpovw")
        self.assertEqual(item.title,
                         "I Explored A $200,000,000 Forgotten Space Colony")
        self.assertEqual(item.channel, "Yes Theory")
        self.assertEqual(item.duration, "16:09")
        self.assertEqual(item.seconds, 969)

    def test_a_public_playlist_page_carries_no_playlistVideoRenderer(self):
        # Which is what makes the pairing test below matter: this page really
        # does only speak lockupViewModel, and the signed-in Watch Later page
        # really does only speak playlistVideoRenderer. Reading one page and
        # concluding the other element is extinct was the original mistake.
        self.assertNotIn("playlistVideoRenderer", json.dumps(self.data))

    def test_non_video_lockups_are_skipped(self):
        data = json.loads(json.dumps(self.data))
        lockups = list(watchlater._walk(data, "lockupViewModel"))
        lockups[0]["contentType"] = "LOCKUP_CONTENT_TYPE_PLAYLIST"
        result = watchlater.parse(page(data))
        self.assertEqual(len(result["items"]), 2)

    def test_a_repeated_video_is_queued_once(self):
        data = json.loads(json.dumps(self.data))
        lockups = list(watchlater._walk(data, "lockupViewModel"))
        lockups[1]["contentId"] = lockups[0]["contentId"]
        result = watchlater.parse(page(data))
        self.assertEqual(len(result["items"]), 2)

    def test_a_lockup_with_no_id_is_not_queued(self):
        data = json.loads(json.dumps(self.data))
        list(watchlater._walk(data, "lockupViewModel"))[0].pop("contentId")
        self.assertEqual(len(watchlater.parse(page(data))["items"]), 2)

    def test_survives_the_item_list_moving(self):
        # The leaf is what carries meaning; the eleven levels above it have all
        # been renamed before. Re-nest the same lockups somewhere else entirely.
        lockups = [{"lockupViewModel": lv}
                   for lv in watchlater._walk(self.data, "lockupViewModel")]
        moved = {"contents": {"somethingElseRenderer": {"stuff": lockups}}}
        result = watchlater.parse(page(moved))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["items"]), 3)


class ParseFailsHonestly(unittest.TestCase):
    def test_no_blob_says_so(self):
        result = watchlater.parse("<html><body>Sign in to continue</body></html>")
        self.assertFalse(result["ok"])
        self.assertIn("signed", result["error"])
        self.assertEqual(result["items"], [])

    def test_empty_and_none_do_not_raise(self):
        for html in ("", None):
            self.assertFalse(watchlater.parse(html)["ok"])

    def test_bad_json_says_so(self):
        html = "<script>var ytInitialData = {not json,};</script>"
        result = watchlater.parse(html)
        self.assertFalse(result["ok"])
        self.assertIn("did not parse", result["error"])

    def test_a_reshape_is_reported_not_returned_empty(self):
        # The exact failure mode that made the first parser useless: a page
        # full of videos, read as an empty queue, with nothing said about it.
        result = watchlater.parse(page({"contents": {"videos": [{"x": 1}]}}))
        self.assertFalse(result["ok"])
        self.assertIn("reshaped", result["error"])

    def test_the_blob_does_not_swallow_the_document(self):
        data = {"contents": []}
        html = page(data) + "<script>var other = {a:1};</script></body></html>"
        match = watchlater.INITIAL_DATA.search(html)
        self.assertEqual(json.loads(match.group(1)), data)


#: The signed-in Watch Later shape. The *key structure* here was read off a
#: live signed-in page via the browser's own diagnostic -- `videoId`, a `title`
#: with `runs`, `shortBylineText.runs`, `lengthText.simpleText`, `lengthSeconds`
#: and `isPlayable` are all really there, in that spelling. Only the values are
#: invented, because the real ones are the user's own queue and do not belong
#: in a repository.
def playlist_video(video_id, title, channel, length="4:20", seconds="260",
                   playable=True):
    return {"playlistVideoRenderer": {
        "videoId": video_id,
        "title": {"runs": [{"text": title}]},
        "shortBylineText": {"runs": [{"text": channel}]},
        "lengthText": {"simpleText": length},
        "lengthSeconds": seconds,
        "isPlayable": playable,
        "index": {"simpleText": "1"},
    }}


class SignedInShape(unittest.TestCase):
    """The shape the real Watch Later page actually serves."""

    def blob(self, *items):
        return {"contents": {"twoColumnBrowseResultsRenderer":
                             {"tabs": [{"tabRenderer": {"content": list(items)}}]}}}

    def test_playlist_video_renderers_are_read(self):
        data = self.blob(playlist_video("aaa", "First", "Chan A"),
                         playlist_video("bbb", "Second", "Chan B"))
        result = watchlater.parse(page(data))
        self.assertTrue(result["ok"], result.get("error"))
        first, second = result["items"]
        self.assertEqual((first.video_id, first.title, first.channel),
                         ("aaa", "First", "Chan A"))
        self.assertEqual(second.video_id, "bbb")

    def test_length_seconds_beats_the_formatted_string(self):
        # The renderer states it outright; re-deriving it from text formatted
        # for a human is the worse of the two sources.
        data = self.blob(playlist_video("aaa", "T", "C", length="1:00",
                                        seconds="99"))
        self.assertEqual(watchlater.parse(page(data))["items"][0].seconds, 99)

    def test_a_bad_length_seconds_falls_back_to_the_clock(self):
        data = self.blob(playlist_video("aaa", "T", "C", length="2:00",
                                        seconds="not a number"))
        self.assertEqual(watchlater.parse(page(data))["items"][0].seconds, 120)

    def test_an_unplayable_video_is_dropped(self):
        # A Watch Later list of any age holds deleted and private videos. One
        # of those in a queue that advances on `ended` stops it dead.
        data = self.blob(playlist_video("aaa", "Gone", "C", playable=False),
                         playlist_video("bbb", "Fine", "C"))
        items = watchlater.parse(page(data))["items"]
        self.assertEqual([i.video_id for i in items], ["bbb"])

    def test_both_shapes_on_one_page_are_read_in_order(self):
        with open(FIXTURE) as fh:
            lockups = list(watchlater._walk(json.load(fh), "lockupViewModel"))
        data = {"contents": [playlist_video("aaa", "Old shape", "C"),
                             {"lockupViewModel": lockups[0]}]}
        items = watchlater.parse(page(data))["items"]
        self.assertEqual([i.video_id for i in items], ["aaa", "Vh4O04Bpovw"])

    def test_runs_and_simple_text_are_both_understood(self):
        self.assertEqual(watchlater._runs_text({"simpleText": "x"}), "x")
        self.assertEqual(
            watchlater._runs_text({"runs": [{"text": "a"}, {"text": "b"}]}),
            "ab")
        self.assertEqual(watchlater._runs_text(None), "")
        self.assertEqual(watchlater._runs_text({}), "")


class SignedOut(unittest.TestCase):
    """The likeliest failure, and the only one with an action attached."""

    def blob(self, logged_out, contents=None):
        return {"responseContext": {"mainAppWebResponseContext":
                                    {"loggedOut": logged_out}},
                "contents": contents or {}}

    def test_signed_out_and_empty_says_signed_out(self):
        result = watchlater.parse(page(self.blob(True)))
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("signed_out"))
        self.assertIn("not signed in", result["error"])

    def test_signed_in_and_empty_still_blames_the_shape(self):
        result = watchlater.parse(page(self.blob(False)))
        self.assertFalse(result["ok"])
        self.assertFalse(result.get("signed_out", False))
        self.assertIn("reshaped", result["error"])

    def test_a_signed_out_flag_does_not_discard_a_queue_we_did_read(self):
        # Order matters: items win. A public playlist read without cookies is
        # signed out and perfectly readable, and must not be thrown away.
        with open(FIXTURE) as fh:
            data = json.load(fh)
        data["responseContext"] = {"mainAppWebResponseContext":
                                   {"loggedOut": True}}
        result = watchlater.parse(page(data))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 3)

    def test_no_flag_at_all_is_unknown_not_false(self):
        self.assertIsNone(watchlater.logged_out({"contents": {}}))

    def test_session_cookies_are_recognised(self):
        self.assertTrue(watchlater.signed_in("__Secure-3PSID=abc; OTHER=1"))
        self.assertTrue(watchlater.signed_in("SID=abc"))
        self.assertFalse(watchlater.signed_in("VISITOR_INFO1_LIVE=x; YSC=y"))
        self.assertFalse(watchlater.signed_in(""))
        self.assertFalse(watchlater.signed_in(None))

    def test_an_empty_queue_with_no_session_reads_as_signed_out(self):
        # The real shape of this failure, probed: a signed-out Watch Later page
        # is HTTP 200, empty, and carries no marker of its own.
        result = watchlater.load(cookie="",
                                 fetcher=lambda *a: (page({"contents": {}}), ""))
        self.assertTrue(result["signed_out"])
        self.assertIn("not signed in", result["error"])

    def test_an_empty_queue_with_a_session_is_just_empty(self):
        # Telling someone who *is* signed in to go and sign in is the failure
        # this half prevents.
        result = watchlater.load(cookie="__Secure-3PSID=abc",
                                 fetcher=lambda *a: (page({"contents": {}}), ""))
        self.assertFalse(result.get("signed_out", False))

    def test_a_readable_queue_is_never_called_signed_out(self):
        with open(FIXTURE) as fh:
            html = page(json.load(fh))
        result = watchlater.load(cookie="", fetcher=lambda *a: (html, ""))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 3)


class Requests(unittest.TestCase):
    def test_watch_later_url(self):
        self.assertEqual(watchlater.playlist_url(),
                         "https://www.youtube.com/playlist?list=WL")

    def test_cookie_header_is_only_added_when_there_is_one(self):
        self.assertNotIn("Cookie", watchlater.headers())
        self.assertEqual(watchlater.headers("a=1")["Cookie"], "a=1")

    def test_headers_are_not_shared_between_calls(self):
        watchlater.headers("a=1")["Cookie"] = "mutated"
        self.assertNotIn("Cookie", watchlater.HEADERS)

    def test_cookie_header_from_pairs(self):
        self.assertEqual(
            watchlater.cookie_header([("SID", "x"), ("HSID", "y")]),
            "SID=x; HSID=y")

    def test_a_nameless_cookie_is_dropped(self):
        self.assertEqual(watchlater.cookie_header([("", "x"), ("SID", "y")]),
                         "SID=y")


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self, _limit=None):
        return self._body


class FakeConn:
    """Stands in for the tunnelled connection `vpn.open_tunnel` returns."""

    def __init__(self, response):
        self.response = response
        self.sent = None
        self.closed = False

    def request(self, method, path, headers=None):
        self.sent = (method, path, headers or {})

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class Routing(unittest.TestCase):
    """VPN Mode is the reason this fetch is not two lines of urllib."""

    def test_a_refused_route_does_not_fetch(self):
        # The leak this guards: a queue that keeps working, from the user's
        # home address, while the browser says the tunnel is up.
        called = []

        def opener(*a, **k):
            called.append(a)
            raise AssertionError("fetched despite a refusal")

        html, error = watchlater.fetch("https://www.youtube.com/x",
                                       route=(None, "VPN Mode is not up"),
                                       opener=opener)
        self.assertEqual(html, "")
        self.assertEqual(error, "VPN Mode is not up")
        self.assertEqual(called, [])

    def test_a_proxy_route_goes_through_the_tunnel(self):
        conn = FakeConn(FakeResponse(200, b"<html>hi</html>"))
        seen = {}

        def opener(proxy, host, port, timeout):
            seen.update(proxy=proxy, host=host, port=port)
            return conn

        html, error = watchlater.fetch(
            "https://www.youtube.com/playlist?list=WL", cookie="SID=x",
            route=("PROXY", None), opener=opener)
        self.assertEqual(error, "")
        self.assertEqual(html, "<html>hi</html>")
        self.assertEqual(seen, {"proxy": "PROXY", "host": "www.youtube.com",
                                "port": 443})
        method, path, headers = conn.sent
        self.assertEqual((method, path), ("GET", "/playlist?list=WL"))
        self.assertEqual(headers["Cookie"], "SID=x")
        self.assertEqual(headers["Host"], "www.youtube.com")
        self.assertTrue(conn.closed)

    def test_a_redirect_is_reported_as_signed_out(self):
        conn = FakeConn(FakeResponse(302, b""))
        html, error = watchlater.fetch("https://www.youtube.com/x",
                                       route=("PROXY", None),
                                       opener=lambda *a: conn)
        self.assertEqual(html, "")
        self.assertIn("302", error)
        self.assertIn("signed out", error)

    def test_a_network_failure_is_an_error_not_an_exception(self):
        def opener(*a):
            raise OSError("no route to host")

        html, error = watchlater.fetch("https://www.youtube.com/x",
                                       route=("PROXY", None), opener=opener)
        self.assertEqual(html, "")
        self.assertIn("could not reach YouTube", error)

    def test_the_connection_is_closed_even_when_the_response_is_bad(self):
        conn = FakeConn(FakeResponse(500, b""))
        watchlater.fetch("https://www.youtube.com/x", route=("PROXY", None),
                         opener=lambda *a: conn)
        self.assertTrue(conn.closed)


class Load(unittest.TestCase):
    def test_load_parses_what_it_fetched(self):
        with open(FIXTURE) as fh:
            html = page(json.load(fh))
        result = watchlater.load(fetcher=lambda *a: (html, ""))
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 3)

    def test_a_fetch_error_stops_before_the_parse(self):
        result = watchlater.load(fetcher=lambda *a: ("", "refused"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "refused")
        self.assertEqual(result["items"], [])

    def test_load_asks_for_the_watch_later_url_by_default(self):
        seen = []
        watchlater.load(cookie="SID=x",
                        fetcher=lambda url, ck, rt: seen.append((url, ck)) or ("", "x"))
        self.assertEqual(seen, [("https://www.youtube.com/playlist?list=WL",
                                 "SID=x")])


if __name__ == "__main__":
    unittest.main()
