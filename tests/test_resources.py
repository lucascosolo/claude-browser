"""The resource policy, which is the part of the guard that can be wrong.

resources.py is deliberately GTK-free so every decision in it can be tested from
a plain number rather than from a laptop that happens to be under load. What is
asserted here is the behaviour the module exists for:

  * a healthy machine is never slowed down by the guard;
  * a machine in the state that caused the original freeze -- little memory
    free, swap filling -- refuses new work, while a merely busy one does not;
  * the tabs chosen for discard are the ones nobody would miss, and never the
    ones a user would;
  * pressure passes and the guard gets out of the way again.
"""

import os
import tempfile
import unittest

from claudebrowser import resources


def snap(available=2000, total=3800, swap_used=0, swap_total=1544,
         load=0.2, cores=2, swap_rate=0.0):
    return resources.Snapshot(available_mb=available, total_mb=total,
                              swap_used_mb=swap_used, swap_total_mb=swap_total,
                              load=load, cores=cores, at=0.0, swap_rate=swap_rate)


#: The machine as it was during the incident this module was written for.
FREEZING = snap(available=90, swap_used=1400, load=22.0, swap_rate=6000.0)


class TestLevels(unittest.TestCase):
    def test_a_healthy_machine_is_ok(self):
        self.assertEqual(snap().level(), resources.OK)

    def test_the_freeze_state_is_critical(self):
        self.assertEqual(FREEZING.memory_level(), resources.CRITICAL)
        self.assertEqual(FREEZING.cpu_level(), resources.CRITICAL)
        self.assertEqual(FREEZING.level(), resources.CRITICAL)

    def test_low_memory_alone_is_enough(self):
        self.assertEqual(snap(available=200).level(), resources.CRITICAL)
        self.assertEqual(snap(available=450).level(), resources.TIGHT)

    def test_active_swapping_counts_even_with_memory_free(self):
        """A machine paging steadily is in trouble whatever MemAvailable says --
        the slide into thrash can happen with a comfortable free number all the
        way down."""
        easy = snap(available=2000, swap_rate=0.0)
        paging = snap(available=2000, swap_rate=400.0)
        self.assertEqual(easy.memory_level(), resources.OK)
        self.assertEqual(paging.memory_level(), resources.TIGHT)
        self.assertEqual(snap(available=2000, swap_rate=9000.0).memory_level(),
                         resources.CRITICAL)

    def test_a_full_swap_that_is_not_moving_is_not_pressure(self):
        """The bug this replaced: a laptop with a few days uptime sits at 70-80%
        swap occupancy permanently, because pages evicted last week and never
        touched again still count. Reading that as pressure made the browser
        discard every background tab it had, forever, on a healthy machine."""
        settled = snap(available=1500, swap_used=1200, swap_total=1544,
                       swap_rate=0.0)
        self.assertGreater(settled.swap_fraction, 0.75)
        self.assertEqual(settled.memory_level(), resources.OK)

    def test_load_is_per_core(self):
        """Load 12 is comfortable on 16 cores and hopeless on 2. The threshold
        has to divide, or the guard is wrong on every machine but one."""
        self.assertEqual(snap(load=6.0, cores=2).cpu_level(), resources.TIGHT)
        self.assertEqual(snap(load=12.0, cores=2).cpu_level(), resources.CRITICAL)
        self.assertEqual(snap(load=12.0, cores=16).cpu_level(), resources.OK)

    def test_thresholds_scale_with_the_machine(self):
        small = snap(total=2000)
        large = snap(total=32000)
        self.assertLess(small.thresholds()[0], large.thresholds()[0])
        # ...but never below the absolute floor, or a tiny machine would need
        # to be down to a few MB before the guard cared.
        self.assertGreaterEqual(snap(total=512).thresholds()[0],
                                resources.TIGHT_FLOOR_MB)

    def test_an_unreadable_machine_reads_as_ok(self):
        """No /proc means no policy. Refusing to browse because a number could
        not be read would be a worse failure than the one being prevented."""
        blank = resources.Snapshot()
        self.assertEqual(blank.level(), resources.OK)

    def test_worst_wins(self):
        self.assertEqual(
            resources.worst(resources.OK, resources.CRITICAL, resources.TIGHT),
            resources.CRITICAL)

    def test_reason_names_the_constraint_that_bit(self):
        self.assertIn("MB free", snap(available=200).reason())
        self.assertIn("load", snap(load=6.0).reason())
        self.assertEqual(snap().reason(), "ok")


class TestAdmission(unittest.TestCase):
    def test_a_healthy_machine_goes_straight_through(self):
        verdict, delay, _reason = resources.admit(snap())
        self.assertEqual(verdict, "go")
        self.assertEqual(delay, 0.0)

    def test_pressure_waits_before_it_refuses(self):
        """The usual cause of pressure is the previous page still loading, and
        that clears itself. A first-contact refusal would make the browser
        useless exactly when an agent is working hardest."""
        verdict, delay, _reason = resources.admit(FREEZING, waited=0.0)
        self.assertEqual(verdict, "wait")
        self.assertGreater(delay, 0)

    def test_it_gives_up_eventually(self):
        verdict, _delay, reason = resources.admit(
            FREEZING, waited=resources.MAX_WAIT_S)
        self.assertEqual(verdict, "no")
        self.assertIn("refused", reason)

    def test_tight_memory_pauses_once_and_then_proceeds(self):
        """Tight is not an emergency. One pause lets a shed land; after that the
        machine can still load a page, just not quickly."""
        tight = snap(available=450)
        self.assertEqual(tight.memory_level(), resources.TIGHT)
        self.assertEqual(resources.admit(tight, waited=0.0)[0], "wait")
        self.assertEqual(
            resources.admit(tight, waited=resources.TIGHT_PATIENCE_S)[0], "go")

    def test_a_busy_cpu_never_refuses_anything(self):
        """The regression this guards against: the first version refused on CPU
        pressure too, and the machine it was written for idles at a load average
        of ten with the user's own editors on it -- so it would have refused
        every tab, forever. Load average also counts uninterruptible sleep, so
        during a swap-thrash it is the memory pressure being counted twice."""
        busy = snap(load=40.0, cores=2)
        self.assertEqual(busy.cpu_level(), resources.CRITICAL)
        self.assertEqual(busy.memory_level(), resources.OK)
        for waited in (0.0, resources.MAX_WAIT_S, 999.0):
            self.assertEqual(resources.admit(busy, waited=waited)[0], "go")

    def test_a_developer_laptop_at_rest_is_not_an_emergency(self):
        """Load 10 on two cores with memory to spare: busy, and fine."""
        typical = snap(available=1400, swap_used=400, load=10.0, cores=2)
        self.assertEqual(resources.admit(typical)[0], "go")


class TestDiscarding(unittest.TestCase):
    #: All the fixture's timestamps are far enough in the past to be past
    #: MIN_IDLE_S when `now` is this.
    NOW = 10_000.0

    def tabs(self):
        return [
            {"id": 1, "used": 100, "current": True, "url": "https://a.example"},
            {"id": 2, "used": 10, "url": "https://b.example"},
            {"id": 3, "used": 50, "url": "https://c.example"},
            {"id": 4, "used": 5, "url": "https://d.example", "private": True},
            {"id": 5, "used": 1, "url": "https://e.example", "loading": True},
            {"id": 6, "used": 2, "url": "https://f.example", "discarded": True},
            {"id": 7, "used": 3, "url": ""},
            {"id": 8, "used": 4, "url": "cb:queue", "playing": True},
        ]

    def pick(self, count, tabs=None):
        # `is None`, not `or`: an empty tab list is falsy and is a case worth
        # testing, not a request for the fixture.
        return resources.pick_victims(self.tabs() if tabs is None else tabs,
                                      count, now=self.NOW)

    def test_least_recently_used_first(self):
        self.assertEqual(self.pick(2), [2, 3])

    def test_the_focused_tab_is_never_a_victim(self):
        # Even asking for every tab must not take the one on screen.
        self.assertNotIn(1, self.pick(99))

    def test_a_tab_you_just_left_is_not_idle(self):
        """The regression this guards: on a machine sitting at tight, flipping
        between two tabs would discard each one the moment you left it, so every
        switch became a reload. Correct, and unusable."""
        fresh = [{"id": 1, "used": self.NOW, "current": True, "url": "https://a"},
                 {"id": 2, "used": self.NOW - 3, "url": "https://b"}]
        self.assertEqual(resources.pick_victims(fresh, 9, now=self.NOW), [])
        # ...but once it has genuinely been abandoned, it is fair game.
        stale = [{"id": 1, "used": self.NOW, "current": True, "url": "https://a"},
                 {"id": 2, "used": self.NOW - resources.MIN_IDLE_S - 1,
                  "url": "https://b"}]
        self.assertEqual(resources.pick_victims(stale, 9, now=self.NOW), [2])

    def test_private_tabs_are_never_discarded(self):
        """A private tab is not written down anywhere, so discarding it is not
        a discard -- it is a close, and nobody asked for one."""
        self.assertNotIn(4, self.pick(99))

    def test_loading_and_already_discarded_tabs_are_skipped(self):
        picked = self.pick(99)
        self.assertNotIn(5, picked)   # mid-load: killing it wastes the bytes
        self.assertNotIn(6, picked)   # nothing left to reclaim
        self.assertNotIn(7, picked)   # no URL to come back to

    def test_a_tab_playing_audio_is_never_discarded(self):
        """Background listening is the one job where the tab in use is by
        definition the one you are not looking at. Tab 8 is the oldest thing
        in the fixture bar the exempt ones, so a guard that ignored `playing`
        would take it first."""
        self.assertNotIn(8, self.pick(99))

    def test_playing_stops_mattering_once_the_sound_stops(self):
        """`playing` is read live from the engine, not remembered, so a queue
        that finished is a tab like any other -- otherwise one play would
        exempt a tab for the rest of the session."""
        quiet = [{"id": 1, "used": self.NOW, "current": True, "url": "https://a"},
                 {"id": 8, "used": 4, "url": "cb:queue", "playing": False}]
        self.assertEqual(resources.pick_victims(quiet, 9, now=self.NOW), [8])

    def test_it_is_deterministic(self):
        tabs = [{"id": 3, "used": 1, "url": "u"}, {"id": 2, "used": 1, "url": "u"}]
        self.assertEqual(self.pick(1, tabs), [2])

    def test_nothing_is_discarded_on_a_healthy_machine(self):
        self.assertEqual(resources.discard_count(snap(), 8), 0)

    def test_tight_sheds_one_at_a_time(self):
        """All-at-once is how a browser earns a reputation for losing your
        place. One per poll, and the poll repeats."""
        self.assertEqual(resources.discard_count(snap(available=450), 8), 1)

    def test_critical_sheds_in_bulk(self):
        self.assertEqual(resources.discard_count(FREEZING, 8), 4)
        self.assertEqual(resources.discard_count(FREEZING, 1), 1)

    def test_nothing_to_shed_is_not_an_error(self):
        self.assertEqual(resources.discard_count(FREEZING, 0), 0)
        self.assertEqual(self.pick(5, []), [])
        self.assertEqual(self.pick(0), [])


class TestTabCeiling(unittest.TestCase):
    def test_a_healthy_machine_gets_the_configured_number(self):
        self.assertEqual(resources.tab_ceiling(snap(), 10), 10)

    def test_pressure_lowers_it(self):
        self.assertLess(resources.tab_ceiling(snap(available=450), 10), 10)
        self.assertLess(resources.tab_ceiling(FREEZING, 10),
                        resources.tab_ceiling(snap(available=450), 10))

    def test_it_never_falls_below_a_usable_browser(self):
        """Under three tabs you cannot hold a page, a search result, and the
        thing you are comparing it to."""
        self.assertGreaterEqual(resources.tab_ceiling(FREEZING, 4), 3)
        self.assertGreaterEqual(resources.tab_ceiling(FREEZING, 1), 3)


class TestReadingProc(unittest.TestCase):
    def test_meminfo_parses_kb_into_mb(self):
        with tempfile.NamedTemporaryFile("w", suffix=".meminfo",
                                         delete=False) as handle:
            handle.write("MemTotal:        3917284 kB\n"
                         "MemAvailable:     869512 kB\n"
                         "SwapTotal:       1581052 kB\n"
                         "SwapFree:         588000 kB\n"
                         "HugePages_Total:       0\n")
            path = handle.name
        try:
            info = resources.meminfo(path)
            self.assertAlmostEqual(info["MemTotal"], 3825.5, places=1)
            self.assertAlmostEqual(info["MemAvailable"], 849.1, places=1)
            self.assertEqual(info["HugePages_Total"], 0)
        finally:
            os.unlink(path)

    def test_a_missing_file_is_empty_not_an_exception(self):
        self.assertEqual(resources.meminfo("/nonexistent/meminfo"), {})
        self.assertEqual(resources.loadavg("/nonexistent/loadavg"), 0.0)

    def test_loadavg_takes_the_one_minute_figure(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("1.75 2.10 1.44 2/431 9182\n")
            path = handle.name
        try:
            self.assertAlmostEqual(resources.loadavg(path), 1.75)
        finally:
            os.unlink(path)

    def test_swapins_reads_the_right_counter(self):
        """pswpin, not pgpgin: the second counts every page read from any block
        device, so it would call a machine that is opening files a machine that
        is thrashing."""
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("pgpgin 99999999\npswpin 41234\npswpout 88\n")
            path = handle.name
        try:
            self.assertEqual(resources.swapins(path), 41234)
        finally:
            os.unlink(path)

    def test_a_vmstat_without_swap_counters_is_none_not_zero(self):
        """None means "unknown", which must not read as "no swapping" -- one is
        a missing signal and the other is a healthy machine."""
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write("nr_free_pages 1234\n")
            path = handle.name
        try:
            self.assertIsNone(resources.swapins(path))
        finally:
            os.unlink(path)
        self.assertIsNone(resources.swapins("/nonexistent/vmstat"))

    def test_the_first_reading_reports_no_rate(self):
        """The counter is cumulative since boot. Turning that into a rate on the
        first sample would call every freshly started browser a thrashing one."""
        resources._LAST_SWAPIN, resources._LAST_RATE = None, 0.0
        self.assertEqual(resources.Snapshot.take().swap_rate, 0.0)

    def test_rapid_sampling_does_not_blind_the_rate(self):
        """The bug this guards: the queue pump takes a snapshot per iteration,
        sometimes twice in the same tenth of a second. Advancing the baseline on
        those left every comparison spanning milliseconds, so the rate read zero
        forever -- blind to thrashing exactly while a tab storm caused it.

        The baseline must survive a too-short interval, and the last known rate
        must be carried forward rather than replaced with a fabricated zero.
        """
        resources._LAST_SWAPIN, resources._LAST_RATE = None, 0.0
        base = 1_000.0
        resources.Snapshot.take(now=base)                       # establish
        first = resources._LAST_SWAPIN
        # A pump-speed resample, well inside RATE_SPAN_S.
        quick = resources.Snapshot.take(now=base + 0.05)
        self.assertEqual(resources._LAST_SWAPIN, first,
                         "a too-short interval must not move the baseline")
        self.assertEqual(quick.swap_rate, 0.0)

        # Pretend the machine paged heavily since the baseline, then sample
        # after a real interval: the rate must appear...
        pages, when = resources._LAST_SWAPIN
        resources._LAST_SWAPIN = (pages - 10_000, when)
        settled = resources.Snapshot.take(now=base + resources.RATE_SPAN_S)
        self.assertGreater(settled.swap_rate, 0.0)

        # ...and still be reported by an immediate resample rather than
        # collapsing to zero the moment something asks twice.
        again = resources.Snapshot.take(now=base + resources.RATE_SPAN_S + 0.05)
        self.assertEqual(again.swap_rate, settled.swap_rate)

    def test_a_real_snapshot_is_self_consistent(self):
        """One check against the actual machine, because the parsing above is
        only right if /proc looks the way the fixture says it does."""
        live = resources.Snapshot.take()
        self.assertGreater(live.total_mb, 0)
        self.assertGreaterEqual(live.available_mb, 0)
        self.assertLessEqual(live.available_mb, live.total_mb)
        self.assertIn(live.level(), resources.LEVELS)
        self.assertIsInstance(live.as_dict()["reason"], str)


class TestRenice(unittest.TestCase):
    """child_pids walks /proc by hand, and the stat format is the sort of thing
    that is easy to parse almost-correctly."""

    def fake_proc(self, entries):
        root = tempfile.mkdtemp()
        for pid, (comm, ppid) in entries.items():
            os.mkdir(os.path.join(root, str(pid)))
            with open(os.path.join(root, str(pid), "stat"), "w") as handle:
                handle.write("%d (%s) S %d 0 0 0 -1 0 0 0\n" % (pid, comm, ppid))
        return root

    def test_it_finds_the_whole_subtree(self):
        root = self.fake_proc({
            100: ("claude-browser", 1),
            101: ("WebKitNetworkProcess", 100),
            102: ("WebKitWebProcess", 101),      # a grandchild, not a child
            200: ("firefox", 1),                 # somebody else's
        })
        found = dict(resources.child_pids(pid=100, root=root))
        self.assertEqual(set(found), {101, 102})

    def test_a_comm_with_spaces_and_parens_does_not_break_it(self):
        """The comm field is parenthesised and may contain anything, which is
        why this splits on the last ')' rather than on whitespace."""
        root = self.fake_proc({
            100: ("claude-browser", 1),
            103: ("WebKitWebProcess (2)", 100),
        })
        found = dict(resources.child_pids(pid=100, root=root))
        self.assertEqual(found[103], "WebKitWebProcess (2)")

    def test_only_webkit_processes_are_reniced(self):
        root = self.fake_proc({
            100: ("claude-browser", 1),
            101: ("WebKitWebProcess", 100),
            102: ("some-helper", 100),
        })
        calls = []

        def setter(pid, nice):
            calls.append((pid, nice))
            return True

        done = resources.renice_children(nice=5, pid=100, root=root, setter=setter)
        self.assertEqual(done, [101])
        self.assertEqual(calls, [(101, 5)])

    def test_the_kernels_truncated_comm_is_what_gets_matched(self):
        """The names /proc really reports, not the ones WebKit was given.

        `comm` is capped at 15 bytes and all three WebKit names are longer, so
        this is the *only* spelling a reader of /proc ever sees. The version of
        this test that supplied the full names passed while the renice matched
        nothing on a live machine -- the fixture was the bug. Every entry here
        is copied from a real /proc/PID/stat.
        """
        root = self.fake_proc({
            100: ("claude-browser", 1),
            101: ("WebKitWebProces", 100),      # WebKitWebProcess, cut at 15
            102: ("WebKitNetworkPr", 100),      # WebKitNetworkProcess
            103: ("WebKitGPUProces", 100),      # WebKitGPUProcess
            104: ("some-helper", 100),
        })
        done = resources.renice_children(nice=5, pid=100, root=root,
                                        setter=lambda _p, _n: True)
        self.assertEqual(sorted(done), [101, 102, 103])

    def test_every_web_process_name_survives_truncation(self):
        """A name added to WEB_PROCESSES must still be matchable.

        Guards the case that made the original bug invisible: a prefix long
        enough to be cut is fine, a prefix that collides with another after the
        cut is not, and either way the comparison has to be done against the
        truncated form.
        """
        for full in resources.WEB_PROCESSES:
            comm = full[:resources.COMM_MAX]
            self.assertTrue(
                any(comm.startswith(known)
                    for known in resources.WEB_PROCESS_COMMS),
                "%r is unmatchable once the kernel truncates it" % full)
        self.assertEqual(len(set(resources.WEB_PROCESS_COMMS)),
                         len(resources.WEB_PROCESSES),
                         "two web-process names collide after truncation")

    def test_a_process_that_exits_mid_walk_is_not_an_error(self):
        root = self.fake_proc({100: ("claude-browser", 1),
                               101: ("WebKitWebProcess", 100)})

        def setter(_pid, _nice):
            raise OSError("no such process")

        self.assertEqual(
            resources.renice_children(pid=100, root=root, setter=setter), [])

    def test_a_missing_proc_is_empty(self):
        self.assertEqual(resources.child_pids(root="/nonexistent"), [])


if __name__ == "__main__":
    unittest.main()
