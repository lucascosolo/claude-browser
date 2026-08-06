"""Turning a YouTube watch link into a player URL.

GTK-free and offline. The 153 finding this module exists for is recorded in
its docstring; what is testable here is the URL arithmetic, and that is where
the sharp edges are -- an id is pasted straight into a URL, so a loose pattern
would be a way to smuggle another parameter onto the player.
"""

import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import youtube  # noqa: E402

VID = "dQw4w9WgXcQ"
OTHER = "6KIyo166b6M"


def params(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


class Identifying(unittest.TestCase):
    def test_the_spellings_that_reach_a_browser(self):
        for url in ("https://www.youtube.com/watch?v=" + VID,
                    "https://youtube.com/watch?v=%s&list=WL&index=3" % VID,
                    "https://m.youtube.com/watch?v=" + VID,
                    "https://youtu.be/" + VID,
                    "https://youtu.be/%s?t=42" % VID,
                    "https://www.youtube.com/shorts/" + VID,
                    "https://www.youtube.com/live/" + VID,
                    "https://www.youtube-nocookie.com/embed/" + VID):
            self.assertEqual(youtube.video_id(url), VID, url)

    def test_pages_that_are_not_a_single_video(self):
        for url in ("https://www.youtube.com/",
                    "https://www.youtube.com/feed/subscriptions",
                    "https://www.youtube.com/playlist?list=WL",
                    "https://www.youtube.com/@someone",
                    "https://www.youtube.com/results?search_query=cats",
                    "https://www.youtube.com/watch?list=WL"):
            self.assertEqual(youtube.video_id(url), "", url)

    def test_another_site_is_never_a_youtube_url(self):
        # The host list is the guard: without it a page could hand this a
        # `evil.test/watch?v=` and have the browser navigate somewhere else.
        for url in ("https://notyoutube.com/watch?v=" + VID,
                    "https://youtube.com.evil.test/watch?v=" + VID,
                    "https://music.youtube.com/watch?v=" + VID):
            self.assertEqual(youtube.video_id(url), "", url)

    def test_an_id_of_the_wrong_shape_is_not_an_id(self):
        for bad in ("short", VID + "toolong", "abc/../../x", "a b c d e f g h"):
            self.assertEqual(
                youtube.video_id("https://www.youtube.com/watch?v=" + bad), "")

    def test_junk_does_not_raise(self):
        for url in ("", None, "not a url", "http://[", "cb:home"):
            self.assertEqual(youtube.video_id(url), "")

    def test_a_timestamp_is_kept(self):
        self.assertEqual(youtube.start_at("https://youtu.be/%s?t=90" % VID), 90)
        self.assertEqual(youtube.start_at("https://youtu.be/%s?t=90s" % VID), 90)
        self.assertEqual(
            youtube.start_at("https://www.youtube.com/watch?v=%s&start=12" % VID), 12)
        self.assertEqual(youtube.start_at("https://youtu.be/" + VID), 0)
        self.assertEqual(youtube.start_at("https://youtu.be/%s?t=1h" % VID), 0)


class Building(unittest.TestCase):
    def test_the_player_url(self):
        url = youtube.embed_url(VID)
        self.assertTrue(url.startswith(
            "https://www.youtube-nocookie.com/embed/" + VID + "?"))
        self.assertEqual(params(url)["autoplay"], "1")
        self.assertEqual(params(url)["rel"], "0")

    def test_the_rest_of_the_queue_rides_in_playlist(self):
        """The whole reason background listening costs nothing: the advance
        happens inside the player, so no page of ours waits on an ended event."""
        url = youtube.embed_url(VID, queue=[OTHER, VID, "AAAAAAAAAAA"])
        # The current video is not repeated in its own follow-on list.
        self.assertEqual(params(url)["playlist"], "%s,AAAAAAAAAAA" % OTHER)

    def test_the_playlist_is_capped(self):
        # It goes in a URL; a hundred ids is not a URL anyone should build.
        ids = ["%011d" % i for i in range(200)]
        url = youtube.embed_url(VID, queue=ids)
        self.assertEqual(len(params(url)["playlist"].split(",")),
                         youtube.MAX_PLAYLIST)

    def test_a_bad_id_builds_nothing(self):
        self.assertEqual(youtube.embed_url("nope"), "")
        self.assertEqual(youtube.embed_url(""), "")

    def test_ids_in_the_queue_are_checked_too(self):
        url = youtube.embed_url(VID, queue=["ok", "&autoplay=0", OTHER])
        self.assertEqual(params(url)["playlist"], OTHER)

    def test_a_start_time_is_carried_through(self):
        self.assertEqual(params(youtube.embed_url(VID, start=42))["start"], "42")


class Redirecting(unittest.TestCase):
    def test_a_watch_link_becomes_the_player(self):
        url = youtube.redirect("https://www.youtube.com/watch?v=" + VID)
        self.assertIn("/embed/" + VID, url)

    def test_a_player_url_is_left_alone(self):
        """Rewriting an /embed/ URL again would strip the playlist a previous
        rewrite just attached, which is how auto-advance would stop after one
        video."""
        player = youtube.embed_url(VID, queue=[OTHER])
        self.assertEqual(youtube.redirect(player), "")

    def test_everything_that_is_not_a_video_is_left_alone(self):
        for url in ("https://www.youtube.com/", "https://example.com/",
                    "https://www.youtube.com/playlist?list=WL"):
            self.assertEqual(youtube.redirect(url), "")

    def test_the_switch_turns_it_off(self):
        watch = "https://www.youtube.com/watch?v=" + VID
        self.assertEqual(youtube.redirect(watch, raw="off"), "")
        self.assertNotEqual(youtube.redirect(watch, raw="1"), "")

    def test_a_typo_leaves_the_feature_on(self):
        """Same rule as siterules.enabled: only a word that unambiguously means
        off turns it off, so a mistyped value does not silently restore the
        two-minute page."""
        self.assertTrue(youtube.enabled("yes please"))
        self.assertFalse(youtube.enabled("0"))


class Requesting(unittest.TestCase):
    def test_a_referrer_is_sent(self):
        """The player answers a request with no http(s) referrer with error
        153 and no video. This header is the fix, so its absence is a bug with
        a test rather than a line someone tidies away."""
        self.assertEqual(youtube.headers()["Referer"], youtube.REFERRER)
        self.assertTrue(youtube.REFERRER.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
