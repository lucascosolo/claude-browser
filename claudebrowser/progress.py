"""How the load bar moves, which is not how the load actually goes.

WebKit's `estimated-load-progress` is an honest number and a bad animation. Three
things about it read as slowness even when the page is loading at its usual speed:

  * It starts at 0 and often *stays* there through DNS, the TCP handshake and TLS
    -- the exact window where the user has just pressed Enter and is waiting to
    find out whether anything happened at all. An empty bar is indistinguishable
    from a browser that ignored the keystroke.
  * It goes **backwards**. The estimate is a fraction of the resources known so
    far, so a document that discovers twenty more subresources on parse drops the
    denominator's answer. A bar sliding left reads as the page un-loading.
  * It arrives in lumps -- long flat stretches punctuated by jumps -- because it
    only moves when a resource completes, not with time.

So this module owns the *displayed* fraction, and the rules are: never go
backwards, always be visibly somewhere the instant a load starts, always appear to
be moving, and never claim to be finished until the load says it is. The page takes
exactly as long as it always did; what changes is that the wait stops looking like
a stall.

The honesty constraint is the last rule, and it is the one worth defending: the
creep asymptotes toward `CEILING` and cannot reach it, so a bar near the end is
always a load still running. Only `finish()` produces 1.0. A bar that filled
completely and then sat there would be a lie the user learns to distrust within a
day, and after that the bar is worth nothing at all.

GTK-free on purpose, so the curve is unit-testable without a display -- `dt` is
always passed in rather than read from a clock.
"""

import math

#: What pressing Enter puts on screen immediately. Small enough to still read as
#: "just started", large enough to be visible as a bar rather than a hairline --
#: below about 0.05 the GTK trough rounds it away to nothing on a narrow window
#: and the whole point is lost.
FLOOR = 0.09

#: The creep's asymptote. Never reached, and never exceeded by the creep, so any
#: fraction below 1.0 means a load that has not finished. Left with real headroom
#: under 1.0 because the gap is what makes the final snap legible as an event.
CEILING = 0.92

#: How fast the creep closes the remaining distance, per second, as a proportion.
#: 0.55 empties a little over half the remaining gap each second, which looks like
#: deliberate motion without ever appearing to race the actual load.
CREEP_RATE = 0.55

#: Below this, a change in WebKit's estimate is noise rather than news, and
#: repainting on it costs a frame on two cores for no visible difference.
EPSILON = 0.004


def blend(raw, floor=FLOOR, ceiling=CEILING):
    """Map WebKit's 0..1 estimate onto the band the bar actually uses.

    The estimate is compressed rather than passed through so that its own 1.0 --
    which WebKit emits a beat before `FINISHED` and sometimes for a document whose
    subresources are still arriving -- lands at `ceiling` and not at a full bar.
    Reaching 1.0 is `finish()`'s job alone, and keeping the two apart is what makes
    a full bar mean something.
    """
    raw = max(0.0, min(1.0, raw))
    return floor + (ceiling - floor) * raw


class Ease:
    """The displayed fraction for one tab's load, advanced by hand.

    Deliberately not a timer of its own. It is stepped from the repaint the
    browser already coalesces to ~10/s, so an idle tab costs nothing and a
    loading one costs no extra wakeups -- on this hardware a per-tab animation
    timer is a real cost, and it would be paid by every background tab too.
    """

    def __init__(self, floor=FLOOR, ceiling=CEILING, rate=CREEP_RATE):
        self.floor = floor
        self.ceiling = ceiling
        self.rate = rate
        self.shown = 0.0
        self.active = False

    def start(self):
        """A load began: jump straight to the floor.

        The jump is the feature. It is the only feedback between the keystroke and
        the first byte, and it is why the browser feels like it reacted instantly
        even though nothing has come back from the network yet.

        Resets `shown` rather than keeping it, because monotonicity is a promise
        about one load and not about the tab: a second navigation that inherited a
        bar at 0.9 would show a nearly-finished load that had not started.
        """
        self.shown = self.floor
        self.active = True

    def observe(self, raw, dt):
        """Advance to wherever WebKit's estimate and the creep agree is furthest.

        `max` of the two is what keeps this monotonic against an estimate that
        drops: a raw value that went backwards simply loses to what is already on
        screen, and nothing has to detect the reversal or special-case it.
        """
        if not self.active:
            return self.shown
        if dt > 0:
            # Asymptotic, so it slows as it approaches the ceiling and cannot
            # cross it however long the load takes. Expressed as a proportion of
            # the *remaining* distance rather than a fixed step per tick, which is
            # what makes it independent of how often it happens to be called --
            # a bar that moved per repaint would race on a fast machine and crawl
            # on a busy one, which is exactly backwards.
            gap = self.ceiling - self.shown
            if gap > 0:
                # `1 - exp(-rate*dt)`, not `rate*dt`. The exponential is the only
                # form that *composes*: two steps of dt land exactly where one
                # step of 2*dt does, so the curve is identical whether the repaint
                # ran twice or twenty times in that second. A linear step fails
                # both ways -- it depends on the call count, and with a large dt
                # it closes the whole gap and sits on the ceiling, which breaks the
                # one promise the ceiling makes. The suite pins both.
                self.shown += gap * (1.0 - math.exp(-self.rate * dt))
        self.shown = max(self.shown, min(self.ceiling, blend(raw, self.floor,
                                                             self.ceiling)))
        return self.shown

    def finish(self):
        """The load is genuinely done. The only path to a full bar."""
        self.active = False
        self.shown = 1.0
        return self.shown

    def reset(self):
        """Back to nothing, for a tab that is not loading at all."""
        self.shown = 0.0
        self.active = False
        return self.shown
