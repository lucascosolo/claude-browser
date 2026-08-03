"""The agent's pacing knob and the cursor scripts it paces.

Everything here is pure Python: the delay arithmetic and the generated JS
source. The choreography itself needs a display and a page, so it is not
faked here -- only the text that drives it is asserted.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudebrowser import agent, extract  # noqa: E402


class PaceScale(unittest.TestCase):
    def test_unset_is_on(self):
        self.assertEqual(agent.pace_scale(""), 1.0)
        self.assertEqual(agent.pace_scale("   "), 1.0)

    def test_words_for_on_and_off(self):
        for raw in ("1", "on", "yes", "true", "ON", " true "):
            self.assertEqual(agent.pace_scale(raw), 1.0, raw)
        for raw in ("0", "off", "no", "false", "OFF", " 0 "):
            self.assertEqual(agent.pace_scale(raw), 0.0, raw)

    def test_multiplier(self):
        self.assertEqual(agent.pace_scale("2"), 2.0)
        self.assertAlmostEqual(agent.pace_scale("0.5"), 0.5)

    def test_clamped(self):
        self.assertEqual(agent.pace_scale("1000"), agent.PACE_MAX)
        self.assertEqual(agent.pace_scale("-3"), 0.0)

    def test_garbage_falls_back_to_on(self):
        # A typo must not silently switch the visual feedback off.
        for raw in ("fast", "1.2.3", "nan"):
            self.assertEqual(agent.pace_scale(raw), 1.0, raw)

    def test_reads_the_environment(self):
        import os

        old = os.environ.get(agent.PACE_ENV)
        os.environ[agent.PACE_ENV] = "off"
        try:
            self.assertEqual(agent.pace_scale(), 0.0)
        finally:
            if old is None:
                os.environ.pop(agent.PACE_ENV, None)
            else:
                os.environ[agent.PACE_ENV] = old


class Delays(unittest.TestCase):
    def make(self, pace):
        return agent.Agent(call=lambda *a, **k: {}, emit=lambda t: None, pace=pace)

    def test_scaled(self):
        self.assertAlmostEqual(self.make(2).delay_for(0.1), 0.2)
        self.assertAlmostEqual(self.make(1).delay_for(agent.STEP_S), agent.STEP_S)

    def test_off_means_no_wait(self):
        runner = self.make(0)
        self.assertEqual(runner.delay_for(agent.HOVER_S), 0.0)
        runner._pause(agent.HOVER_S)     # returns immediately; would hang if not

    def test_never_negative(self):
        self.assertEqual(self.make(1).delay_for(-5), 0.0)

    def test_budget_per_acting_step_stays_under_a_second(self):
        runner = self.make(1)
        total = (runner.delay_for(agent.STEP_S) + runner.delay_for(agent.HOVER_S)
                 + runner.delay_for(agent.ACT_S))
        self.assertLess(total, 1.0)
        self.assertGreater(total, 0.3)   # visible, not instant

    def test_hover_covers_the_page_side_animation(self):
        # The pause must outlast the scroll settle plus the cursor's travel, or
        # the click fires while the cursor is still on its way.
        self.assertGreaterEqual(
            agent.HOVER_S * 1000,
            extract.SCROLL_SETTLE_MS + extract.TRAVEL_MS)


class CursorScripts(unittest.TestCase):
    def test_point_escapes_its_selector(self):
        js = extract.point("</script><img onerror=alert(1)>")
        self.assertNotIn("</script>", js)
        self.assertIn("\\u003c", js)

    def test_point_measures_after_the_scroll(self):
        js = extract.point("#x")
        self.assertIn("__cbScrollTo", js)
        # The cursor placement is inside a timeout, not at call time.
        self.assertIn("setTimeout(function(){window.__cbCursorAt(e,false);},%d)"
                      % extract.SCROLL_SETTLE_MS, js)

    def test_point_reports_a_miss(self):
        self.assertIn("no match", extract.point("#gone"))

    def test_click_and_fill_press_the_cursor(self):
        self.assertIn("window.__cbCursorAt(e,true)", extract.click("#a"))
        self.assertIn("window.__cbCursorAt(e,true)", extract.fill("#a", "v"))

    def test_cursor_is_inert_and_unreadable(self):
        self.assertIn("pointer-events:none", extract.HALO)
        # aria-hidden keeps it out of TEXT/MARKDOWN, which drop hidden nodes.
        self.assertIn("aria-hidden", extract.HALO)
        self.assertIn("data-cb-cursor", extract.HALO)

    def test_cursor_does_not_stack_duplicates(self):
        # One guarded definition, and the element is looked up before creation.
        self.assertIn("if (window.__cbHalo) return;", extract.HALO)
        self.assertIn("document.querySelector('[data-cb-cursor]')", extract.HALO)

    def test_timings_are_substituted(self):
        self.assertNotIn("__TRAVEL__", extract.HALO)
        self.assertNotIn("__PRESS__", extract.HALO)
        self.assertIn("transform %dms" % extract.TRAVEL_MS, extract.HALO)
        self.assertIn("}, %d)" % extract.PRESS_MS, extract.HALO)

    def test_every_acting_script_carries_the_cursor_definitions(self):
        for js in (extract.point("#a"), extract.click("#a"), extract.fill("#a", "b")):
            self.assertIn("__cbCursorEl", js)


if __name__ == "__main__":
    unittest.main()
