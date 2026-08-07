"""Keeping the browser from taking the machine down with it.

This file exists because of one incident. An agent was told to research
something, opened five tabs as fast as the API would take them, and every one
of them started parsing and running JavaScript at the same moment on a two-core
laptop with 3.8GB of RAM and most of a gigabyte already in swap. The machine did
not crash -- it thrashed, for twenty minutes, until it was power-cycled.

Nothing about that was a bug in any single place. Each individual step was
reasonable; there was simply nothing anywhere that said *no*. So this module is
the thing that says no, and it does it in four ways:

  1. **Admission control.** A tab open or a navigation asks permission first.
     Under memory or CPU pressure the answer is "wait", and past a hard floor it
     is "no" with a reason the agent can read and act on.
  2. **One heavy load at a time.** Five concurrent page loads on two cores are
     slower *in total* than five sequential ones, and they are the difference
     between a slow browser and an unusable computer, because their peak memory
     coincides. The gate in browser.py serializes them.
  3. **Discarding idle tabs.** A background tab holding a parsed DOM is memory
     the machine would rather have. Below a threshold the least recently used
     ones are dropped back to a URL and a title and reloaded on return -- the
     same trade every other browser makes, just at a much lower threshold.
  4. **Staying out of the foreground's way.** WebKit's content processes are
     reniced, so when the machine *is* busy it is the page that stutters and not
     the window manager. This is what stops "slow" from becoming "frozen": a
     desktop that can still repaint is a desktop you can still recover from.

Everything here is deliberately GTK-free and reads only `/proc`, so the policy
is testable without a display, which matters because the policy is the part that
can be wrong. The decisions are pure functions of a `Snapshot`; the only impure
things are the two readers at the top and `renice_children`.

**Thresholds are fractions of the machine, not constants.** The numbers were
tuned on a 4GB box, but a hardcoded "420MB free" is either paranoid or useless
on any other machine, and this browser is meant to be portable to whatever slow
laptop someone has.
"""

import os
import time

MEMINFO = "/proc/meminfo"
LOADAVG = "/proc/loadavg"

#: Pressure levels, in order. `OK` means proceed; `TIGHT` means proceed but shed
#: what we can and slow the agent down; `CRITICAL` means refuse new work.
OK, TIGHT, CRITICAL = "ok", "tight", "critical"
LEVELS = (OK, TIGHT, CRITICAL)

#: Memory floors, as a fraction of total RAM, with an absolute floor underneath
#: so a large machine does not wait until it has 4GB free to care.
TIGHT_FRACTION = 0.14
CRITICAL_FRACTION = 0.07
TIGHT_FLOOR_MB = 380
CRITICAL_FLOOR_MB = 190

#: Swapping *activity*, in pages read back from disk per second.
#:
#: This began as a test of how full swap was, which ran for about an hour before
#: the machine it was written for disproved it: a laptop with a few days uptime
#: sits at 70-80% swap occupancy permanently, because pages that were evicted
#: last Tuesday and never touched again still count. Reading that as pressure
#: made the browser discard every background tab it had, over and over, on a
#: machine that was perfectly healthy.
#:
#: Occupancy is a high-water mark; the rate is the live signal. Pages coming
#: *back in* is what thrashing is -- the machine paging out once and settling is
#: normal and costs nothing. At 4KB a page, 200/s is about 1MB/s of fault-in
#: traffic and 2000/s is the range where a spinning disk stops keeping up, which
#: is where "slow" turns into "frozen".
SWAP_RATE_TIGHT = 200.0
SWAP_RATE_CRITICAL = 2000.0

#: Where the cumulative page-in/page-out counters live.
VMSTAT = "/proc/vmstat"

#: How long a tab must have been untouched before it is eligible for discard.
#: The guard is correct without this and unusable with it missing -- see the
#: note in `pick_victims`.
MIN_IDLE_S = 90.0

#: Load average per core. These are higher than a systems-monitoring rule of
#: thumb would put them, on purpose: the machine this runs on idles around ten
#: with the user's own editors and agents on it, and a threshold tuned for a
#: quiet server would report permanent emergency. CPU pressure never refuses
#: anything (see `admit`), so being generous here costs nothing but a little
#: less serialization.
LOAD_TIGHT = 2.5
LOAD_CRITICAL = 5.0

#: How long an admission check is willing to wait for memory to free up before
#: it refuses. Long enough for one page to finish loading and release its peak,
#: short enough that an agent is not left hanging on a machine that is not going
#: to recover.
MAX_WAIT_S = 20.0

#: Tight memory is not an emergency, so it buys one pause and then proceeds. The
#: pause is what lets a shed land before the next page starts allocating.
TIGHT_PATIENCE_S = 4.0

#: How much nicer than the UI the content processes run. 5 is enough to lose
#: every scheduling contest against the compositor and the window manager, and
#: not so much that pages crawl when the machine is otherwise idle -- nice only
#: matters when there is contention.
WEB_PROCESS_NICE = 5

#: Names of the processes WebKit spawns underneath us.
WEB_PROCESSES = ("WebKitWebProcess", "WebKitNetworkProcess", "WebKitGPUProcess")

#: The kernel truncates `comm` to TASK_COMM_LEN-1 = 15 bytes, and *every* name
#: above is longer than that -- /proc/PID/stat says `WebKitWebProces` and
#: `WebKitNetworkPr`, never the full spelling. So testing a comm read out of
#: /proc against the full names matched nothing, and `renice_children` was a
#: silent no-op for as long as it existed: measured on a live browser, all four
#: content processes sat at nice 0 while the desktop lost every scheduling
#: contest to a runaway page, which is the exact symptom the function was
#: written to prevent. Its test passed throughout, because the fixture supplied
#: `WebKitWebProcess` as a comm -- a string the kernel cannot produce. Compare
#: against these truncated prefixes instead, which is what a reader of /proc
#: actually gets. Keep both tuples: WEB_PROCESSES is the documentation of what
#: WebKit spawns, this is the string matching has to use.
COMM_MAX = 15
WEB_PROCESS_COMMS = tuple(name[:COMM_MAX] for name in WEB_PROCESSES)


# -- reading the machine ----------------------------------------------------

def meminfo(path=MEMINFO):
    """`/proc/meminfo` as a dict of MB. Empty if it cannot be read, which is
    how this module degrades on a kernel without procfs: every threshold test
    against a missing value answers OK, so the browser behaves as it did before
    this file existed rather than refusing to work."""
    out = {}
    try:
        with open(path) as handle:
            for line in handle:
                key, _sep, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    try:
                        out[key] = int(parts[0]) / 1024.0
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def loadavg(path=LOADAVG):
    """One-minute load average, or 0.0 if unreadable."""
    try:
        with open(path) as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def swapins(path=VMSTAT):
    """Cumulative pages swapped in since boot, or None if unreadable.

    `pswpin`, not `pgpgin`: the second counts every page read from any block
    device, so it is dominated by ordinary file reads and would call a machine
    that is merely opening files a machine that is thrashing.
    """
    try:
        with open(path) as handle:
            for line in handle:
                if line.startswith("pswpin "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


#: Shortest interval a swap rate is computed over. Below this the arithmetic is
#: dominated by when the samples happened to land rather than by what the
#: machine is doing.
RATE_SPAN_S = 2.0

#: The previous reading and the last rate derived from it, so `Snapshot.take()`
#: can turn the cumulative counter above into a rate. Module state because the
#: counter is global to the machine -- there is nothing per-instance about it --
#: and because `take()` is already the impure end of this module. Snapshots
#: built directly (as the tests build them) pass `swap_rate` in and never touch
#: either of these.
_LAST_SWAPIN = None
_LAST_RATE = 0.0


def cores():
    return max(1, os.cpu_count() or 1)


class Snapshot:
    """One reading of the machine. Cheap enough to take every few seconds; the
    whole thing is two small file reads."""

    __slots__ = ("available_mb", "total_mb", "swap_used_mb", "swap_total_mb",
                 "swap_rate", "load", "cores", "at")

    def __init__(self, available_mb=0.0, total_mb=0.0, swap_used_mb=0.0,
                 swap_total_mb=0.0, load=0.0, cores=1, at=0.0, swap_rate=0.0):
        self.available_mb = available_mb
        self.total_mb = total_mb
        self.swap_used_mb = swap_used_mb
        self.swap_total_mb = swap_total_mb
        self.swap_rate = swap_rate
        self.load = load
        self.cores = cores
        self.at = at

    @classmethod
    def take(cls, now=None):
        global _LAST_SWAPIN, _LAST_RATE

        info = meminfo()
        total = info.get("MemTotal", 0.0)
        swap_total = info.get("SwapTotal", 0.0)
        swap_free = info.get("SwapFree", 0.0)
        now = now if now is not None else time.monotonic()

        # Rate since the previous reading. The first call establishes a baseline
        # and reports zero -- claiming a rate from a since-boot counter would
        # call every freshly started browser a thrashing one.
        #
        # The baseline is only replaced once a full interval has actually
        # elapsed, and the last known rate is carried forward until then. That
        # matters because snapshots are *not* taken on a tidy schedule: the
        # queue pump takes one per iteration, sometimes twice within the same
        # tenth of a second. Advancing the baseline on those would leave every
        # comparison spanning a few milliseconds, and the rate would read zero
        # forever -- the guard would be blind to thrashing precisely while a tab
        # storm was causing it.
        rate = _LAST_RATE
        pages = swapins()
        if pages is not None:
            if _LAST_SWAPIN is None:
                _LAST_SWAPIN, rate = (pages, now), 0.0
            else:
                last_pages, last_at = _LAST_SWAPIN
                span = now - last_at
                if span >= RATE_SPAN_S:
                    rate = max(0, pages - last_pages) / span
                    _LAST_SWAPIN, _LAST_RATE = (pages, now), rate

        return cls(
            swap_rate=rate,
            # MemAvailable is the kernel's own estimate of what a new allocation
            # can actually get, reclaimable page cache included. MemFree is the
            # number people reach for and it is wrong here -- a healthy Linux box
            # has almost no MemFree by design.
            available_mb=info.get("MemAvailable", info.get("MemFree", 0.0)),
            total_mb=total,
            swap_used_mb=max(0.0, swap_total - swap_free),
            swap_total_mb=swap_total,
            load=loadavg(),
            cores=cores(),
            at=now,
        )

    # -- derived facts ------------------------------------------------------

    @property
    def swap_fraction(self):
        """How full swap is. For display only -- deliberately not part of any
        threshold; see the note on SWAP_RATE_TIGHT for why occupancy lies."""
        return (self.swap_used_mb / self.swap_total_mb) if self.swap_total_mb else 0.0

    @property
    def load_per_core(self):
        return self.load / self.cores

    def thresholds(self):
        """(tight_mb, critical_mb) for this machine."""
        if not self.total_mb:
            return TIGHT_FLOOR_MB, CRITICAL_FLOOR_MB
        return (max(TIGHT_FLOOR_MB, self.total_mb * TIGHT_FRACTION),
                max(CRITICAL_FLOOR_MB, self.total_mb * CRITICAL_FRACTION))

    def memory_level(self):
        """Unknown memory reads as OK -- see the note on meminfo().

        Two signals, and they say different things. MemAvailable is how much
        room is left; the swap-in rate is whether the machine is already paying
        to get room back. Either one alone is enough, because the freeze this
        guards against can arrive from either direction -- a single page that
        allocates a gigabyte, or a slow slide into thrash with a comfortable
        MemAvailable the whole way down.
        """
        if not self.total_mb:
            return OK
        tight, critical = self.thresholds()
        if self.available_mb <= critical or self.swap_rate >= SWAP_RATE_CRITICAL:
            return CRITICAL
        if self.available_mb <= tight or self.swap_rate >= SWAP_RATE_TIGHT:
            return TIGHT
        return OK

    def cpu_level(self):
        if not self.load:
            return OK
        if self.load_per_core >= LOAD_CRITICAL:
            return CRITICAL
        if self.load_per_core >= LOAD_TIGHT:
            return TIGHT
        return OK

    def level(self):
        return worst(self.memory_level(), self.cpu_level())

    def reason(self):
        """One line, for the status bar and for the error an agent gets back.

        Says which of the two constraints bit, because "wait" for a busy CPU and
        "wait" for exhausted memory call for different things from the caller.
        """
        bits = []
        if self.memory_level() != OK:
            bits.append("%dMB free" % self.available_mb
                        + (", swapping in %d pages/s" % self.swap_rate
                           if self.swap_rate >= SWAP_RATE_TIGHT else ""))
        if self.cpu_level() != OK:
            bits.append("load %.1f on %d core%s"
                        % (self.load, self.cores, "s" if self.cores != 1 else ""))
        return "; ".join(bits) or "ok"

    def as_dict(self):
        return {"level": self.level(), "reason": self.reason(),
                "available_mb": round(self.available_mb),
                "total_mb": round(self.total_mb),
                "swap_used_mb": round(self.swap_used_mb),
                "swap_total_mb": round(self.swap_total_mb),
                "swap_rate": round(self.swap_rate),
                "load": round(self.load, 2), "cores": self.cores,
                "memory": self.memory_level(), "cpu": self.cpu_level()}


def worst(*levels):
    return max(levels, key=LEVELS.index)


# -- what to do about it ----------------------------------------------------

def admit(snapshot, waited=0.0):
    """May a new page load start now?

    Returns `(verdict, delay, reason)` where verdict is "go", "wait" or "no".

    **Only memory can refuse.** This was written the other way first -- any
    pressure, memory or CPU, could refuse -- and running it on the machine it
    was written for showed why that is wrong: a developer laptop with a couple
    of agents and a Chrome open sits at a load average of ten *all day*, and a
    CPU-driven refusal would have meant a browser that never opened a tab
    again. A saturated CPU makes a page load slow, which the caller can live
    with; exhausted memory makes the machine stop, which is the failure this
    module exists to prevent. So CPU pressure shapes how many loads run at once
    (the caller's concurrency limit) and memory pressure decides whether one may
    start at all.

    Load average is also not a clean CPU signal on Linux: it counts tasks in
    uninterruptible sleep, which is exactly what a machine in swap-thrash is
    full of. Half of what looks like CPU pressure during a freeze is the memory
    pressure being counted twice.
    """
    level = snapshot.memory_level()
    if level == OK:
        return "go", 0.0, ""

    if level == TIGHT:
        # One pause, not an open-ended one: shedding happens during the wait, so
        # a second is usually all it takes -- and if it was not enough, tight is
        # still a machine that can load a page, just not quickly.
        if waited >= TIGHT_PATIENCE_S:
            return "go", 0.0, snapshot.reason()
        return "wait", 1.5, snapshot.reason()

    if waited >= MAX_WAIT_S:
        return "no", 0.0, (
            "refused: the machine is out of memory (%s) and did not recover "
            "while waiting. Close a tab or discard one, then retry."
            % snapshot.reason())
    return "wait", 3.0, snapshot.reason()


def discard_count(snapshot, live_background):
    """How many background tabs to drop right now.

    Proportional rather than all-or-nothing: dropping every background tab the
    first time memory dips is how a browser earns a reputation for losing your
    place. One at a time under TIGHT, half of them under CRITICAL, and the
    caller repeats on the next poll if that was not enough.
    """
    if live_background <= 0:
        return 0
    level = snapshot.memory_level()
    if level == OK:
        return 0
    if level == TIGHT:
        return 1
    return max(1, live_background // 2)


def pick_victims(tabs, count, now=None):
    """Which tabs to discard, least recently used first.

    `tabs` is a list of dicts: `id`, `used` (a monotonic timestamp), and the
    flags below. A tab is exempt when it is:

      * the one on screen -- discarding what the user is looking at is absurd;
      * already discarded -- nothing left to reclaim;
      * loading -- killing a load mid-flight wastes the bandwidth already spent
        and reports as a failure to whoever asked for it;
      * private -- its page is the only copy of that session. A private tab is
        not written to disk *anywhere*, so a discard is not a discard, it is a
        close, and the user did not ask for one;
      * playing audio -- a tab making sound is a tab being used, whatever its
        `used` timestamp says, and that timestamp only moves when the tab is
        *touched*. Background listening is the one job where the tab is by
        definition the one you are not looking at, so without this the guard
        aims squarely at cb:queue and silences the thing it was built for.
        Restoring it is not a reload the user can shrug at either: it is
        silence, then a fresh player, back at the start of the queue;
      * used within the last MIN_IDLE_S. Without this the guard is correct and
        unusable: on a machine that sits at tight, flipping between two tabs
        discards each one the moment you leave it, so every switch is a reload.
        A tab has to have actually been abandoned before it counts as idle.

    Returns ids, oldest first. Ties break on id so the choice is deterministic,
    which is the difference between a testable policy and a coin flip.
    """
    if count <= 0:
        return []
    now = now if now is not None else time.monotonic()
    eligible = [t for t in tabs
                if not t.get("current") and not t.get("discarded")
                and not t.get("loading") and not t.get("private")
                and not t.get("playing") and t.get("url")
                and now - (t.get("used") or 0.0) >= MIN_IDLE_S]
    eligible.sort(key=lambda t: (t.get("used") or 0.0, t.get("id") or 0))
    return [t["id"] for t in eligible[:count]]


def tab_ceiling(snapshot, configured):
    """The most tabs that may be open at once, given the machine.

    A ceiling that ignores the hardware is the wrong ceiling on both ends. The
    configured number is what a healthy machine allows; a machine that is
    already tight gets less, because the tabs it has are evidently costing more
    than they should. Never below 3 -- a browser that cannot hold a page, a
    search result and the thing you are comparing it to is not a browser.
    """
    level = snapshot.memory_level()
    if level == OK:
        return configured
    if level == TIGHT:
        return max(3, int(configured * 0.6))
    return max(3, int(configured * 0.4))


# -- being a good citizen on a busy machine ---------------------------------

def child_pids(pid=None, root="/proc"):
    """PIDs of our direct and indirect children, by walking /proc.

    Every WebKit process is a descendant of ours -- some through the network
    process, which is a child of the UI process but a parent of nothing we
    spawned by name -- so this collects the whole subtree rather than one level.
    """
    pid = pid or os.getpid()
    parents = {}
    names = {}
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(root, entry, "stat")) as handle:
                data = handle.read()
        except OSError:
            continue
        # The comm field is parenthesised and may itself contain spaces or
        # parens, so split on the *last* ')' rather than on whitespace.
        head, _sep, tail = data.rpartition(")")
        open_paren = head.find("(")
        if open_paren < 0 or not tail:
            continue
        names[int(entry)] = head[open_paren + 1:]
        fields = tail.split()
        if len(fields) >= 2:
            try:
                parents[int(entry)] = int(fields[1])
            except ValueError:
                pass

    out = []
    for child, parent in parents.items():
        seen = 0
        walker = parent
        while walker > 1 and seen < 12:      # depth cap: /proc can race and lie
            if walker == pid:
                out.append((child, names.get(child, "")))
                break
            walker = parents.get(walker, 0)
            seen += 1
    return out


def renice_children(nice=WEB_PROCESS_NICE, pid=None, root="/proc", setter=None):
    """Push WebKit's content processes below the UI in the scheduler.

    This is the single most effective thing in this file for the symptom the
    user actually reported. Thrashing is survivable if you can still move the
    mouse and close a tab; what made the machine need a power cycle was that the
    desktop lost every scheduling contest to the page renderers. A niced content
    process cannot do that.

    Only raises niceness, never lowers it: without CAP_SYS_NICE a process cannot
    take a nice value back, so an attempt to "restore" one would fail loudly and
    permanently. Returns the pids actually adjusted.
    """
    setter = setter or _set_nice
    done = []
    for child, name in child_pids(pid=pid, root=root):
        if not any(name.startswith(known) for known in WEB_PROCESS_COMMS):
            continue
        try:
            if setter(child, nice):
                done.append(child)
        except OSError:
            pass       # it exited between the listdir and the setpriority
    return done


def _set_nice(pid, nice):
    current = os.getpriority(os.PRIO_PROCESS, pid)
    if current >= nice:
        return False
    os.setpriority(os.PRIO_PROCESS, pid, nice)
    return True
