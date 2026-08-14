"""
Record gate: reject in-flight / transition frames at record start.

Both vendors buffer frames: a record command can otherwise capture frames
exposed *before* the command (measured on the ASI294: the first recorded
frame after an exposure switch arrives on the old-exposure cadence), and
the QHY42 produces 2-3 short/hybrid transition frames after an exposure
switch (HANDOFF §4). The gate enforces the rule "every kept exposure
starts after the record command".

A frame is kept only if ALL of:
  - freshness: it arrived at least one full exposure after arming
    (a frame whose exposure started post-command cannot arrive earlier);
  - not a short transition: inter-arrival delta >= 0.8 x exposure;
  - not an old-cadence straggler: delta <= 1.5 x expected cadence + 0.25 s
    (only when an expected cadence is provided).

Once one frame passes, the gate opens permanently (transitions only occur
at the switch). Failsafe: after max_discard rejections or a hard timeout,
the gate opens and flags `gave_up` so acquisition can never deadlock.
"""

from __future__ import annotations

import logging

log = logging.getLogger("cmoscam.gating")


class RecordGate:
    def __init__(self, t_arm: float, exposure_s: float,
                 expected_cadence_s: float | None = None,
                 max_discard: int = 10, timeout_s: float | None = None):
        self.t_arm = float(t_arm)
        self.exposure_s = float(exposure_s)
        self.expected_cadence_s = expected_cadence_s
        self.max_discard = int(max_discard)
        if timeout_s is None:
            base = expected_cadence_s or exposure_s
            timeout_s = 5.0 * base + 5.0
        self.timeout_s = float(timeout_s)

        self.discarded = 0
        self.opened = False
        self.gave_up = False

    def accept(self, arrival: float, delta: float) -> bool:
        """Called per arriving frame while recording; True = keep."""
        if self.opened:
            return True

        # Failsafes: never deadlock the acquisition.
        if (self.discarded >= self.max_discard
                or arrival - self.t_arm > self.timeout_s):
            log.warning(
                "record gate gave up after %d discards / %.1fs -- "
                "accepting frames unconditionally",
                self.discarded, arrival - self.t_arm,
            )
            self.opened = True
            self.gave_up = True
            return True

        ok = arrival >= self.t_arm + self.exposure_s
        if ok and delta < 0.8 * self.exposure_s:
            ok = False
        if ok and self.expected_cadence_s is not None:
            if delta > 1.5 * self.expected_cadence_s + 0.25:
                ok = False

        if ok:
            self.opened = True
            return True
        self.discarded += 1
        log.debug("gate: discarded frame (arrival=%.3f delta=%.3f)",
                  arrival - self.t_arm, delta)
        return False
