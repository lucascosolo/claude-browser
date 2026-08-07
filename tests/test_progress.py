"""The load bar's curve. No display needed -- that is why `progress` is GTK-free.

The properties pinned here are the ones the module exists to guarantee, and each
maps to a specific way WebKit's raw estimate looks like slowness: it never moves
backwards, it is visible the instant Enter is pressed, it keeps moving while the
estimate is flat, and it is never full until the load is actually done.
"""

import unittest

from claudebrowser import progress


class Blend(unittest.TestCase):
    def test_the_raw_estimate_is_compressed_into_the_band(self):
        self.assertAlmostEqual(progress.blend(0.0), progress.FLOOR)
        self.assertAlmostEqual(progress.blend(1.0), progress.CEILING)

    def test_a_raw_one_is_not_a_full_bar(self):
        """WebKit emits 1.0 a beat before FINISHED, and sometimes while
        subresources are still arriving. If that painted a full bar, a full bar
        would stop meaning "done"."""
        self.assertLess(progress.blend(1.0), 1.0)

    def test_out_of_range_input_is_clamped(self):
        self.assertAlmostEqual(progress.blend(-5.0), progress.FLOOR)
        self.assertAlmostEqual(progress.blend(17.0), progress.CEILING)


class Easing(unittest.TestCase):
    def test_starting_is_immediately_visible(self):
        """The whole point of the floor: feedback between the keystroke and the
        first byte, when the raw estimate is still flat 0."""
        e = progress.Ease()
        self.assertEqual(e.shown, 0.0)
        e.start()
        self.assertAlmostEqual(e.shown, progress.FLOOR)
        self.assertGreater(e.shown, 0.05, "too small to read as a bar")

    def test_it_never_goes_backwards_when_the_estimate_does(self):
        """The reversal this module was written for: a document that discovers
        more subresources on parse drops WebKit's fraction, and a bar sliding
        left reads as the page un-loading."""
        e = progress.Ease()
        e.start()
        high = e.observe(0.8, dt=0.1)
        after = e.observe(0.2, dt=0.0)      # dt=0 so the creep cannot mask it
        self.assertGreaterEqual(after, high)

    def test_it_keeps_moving_while_the_estimate_is_flat(self):
        """The long stall at a fixed estimate is the other half of the problem."""
        e = progress.Ease()
        e.start()
        seen = [e.observe(0.0, dt=0.1) for _ in range(5)]
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], seen[0], "a flat estimate froze the bar")

    def test_the_creep_can_never_reach_the_ceiling(self):
        """The honesty constraint. A bar below 1.0 has to mean a live load, so
        no amount of waiting may fill it."""
        e = progress.Ease()
        e.start()
        for _ in range(2000):
            e.observe(0.0, dt=1.0)
        self.assertLess(e.shown, progress.CEILING)

    def test_a_raw_estimate_cannot_push_past_the_ceiling_either(self):
        e = progress.Ease()
        e.start()
        for _ in range(50):
            e.observe(1.0, dt=1.0)
        self.assertLessEqual(e.shown, progress.CEILING)
        self.assertLess(e.shown, 1.0)

    def test_only_finish_produces_a_full_bar(self):
        e = progress.Ease()
        e.start()
        e.observe(1.0, dt=5.0)
        self.assertLess(e.shown, 1.0)
        self.assertEqual(e.finish(), 1.0)

    def test_the_creep_is_rate_based_not_per_call(self):
        """A bar that advanced per repaint would race on an idle machine and
        crawl on a busy one -- the opposite of what is wanted, since the busy
        machine is where the wait feels longest. One big step must land in the
        same place as many small ones covering the same time."""
        one = progress.Ease()
        one.start()
        one.observe(0.0, dt=1.0)

        many = progress.Ease()
        many.start()
        for _ in range(10):
            many.observe(0.0, dt=0.1)

        self.assertAlmostEqual(one.shown, many.shown, delta=0.05)

    def test_a_new_load_does_not_inherit_the_last_bar(self):
        """Monotonicity is a promise about one load, not about the tab. A second
        navigation starting at 0.9 would show a nearly-finished load that had
        not begun."""
        e = progress.Ease()
        e.start()
        e.observe(0.9, dt=1.0)
        e.finish()
        e.start()
        self.assertAlmostEqual(e.shown, progress.FLOOR)

    def test_observing_before_a_start_does_nothing(self):
        """A progress tick can arrive for a tab that is not loading -- the
        repaint is shared. It must not put a bar on screen."""
        e = progress.Ease()
        self.assertEqual(e.observe(0.5, dt=1.0), 0.0)

    def test_reset_clears_it(self):
        e = progress.Ease()
        e.start()
        e.observe(0.5, dt=1.0)
        self.assertEqual(e.reset(), 0.0)
        self.assertFalse(e.active)


if __name__ == "__main__":
    unittest.main()
