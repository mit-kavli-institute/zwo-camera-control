"""
TEC settle / stall detection (vendor-agnostic).

"Settled" is threshold + dwell: the sensor temperature must stay within
``tolerance_c`` of the setpoint continuously for ``dwell_s`` seconds.
Hysteresis prevents flapping at the tolerance boundary. Slope is *not*
used for the settled decision (it would be fragile across GUI restarts —
and both vendors' TECs genuinely restart regulation when the camera is
reopened, so paying one dwell period after a restart is honest).

Stall detection covers the bad-PID case where the TEC parks short of the
setpoint and would otherwise sit in TEC_SETTLING forever: cooler power
saturated + temperature flat + still outside tolerance for
``stall_timeout_s``  =>  ``stalled`` is flagged (telemetry only; the state
machine stays in TEC_SETTLING and the operator/telescope decides).

Per-vendor tuning lives with the vendor profile (Phase 2); the defaults
here are deliberately loose enough for the QHY42's coarse TEC.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class TecConfig:
    tolerance_c: float = 1.0        # |temp - setpoint| for "settled"
    dwell_s: float = 30.0           # time continuously in-band before settled
    hysteresis_c: float = 0.5       # unsettle only beyond tolerance + this
    stall_timeout_s: float = 300.0  # saturated+flat+out-of-band this long => stalled
    stall_power_pct: float = 95.0   # cooler power considered saturated
    stall_flat_band_c: float = 0.5  # temp spread over the window considered flat


class TecSettleMonitor:
    """Feed with periodic samples via update(); read .settled / .stalled."""

    def __init__(self, config: TecConfig | None = None):
        self.config = config or TecConfig()
        self.reset()

    def reset(self):
        self.settled = False
        self.stalled = False
        self._setpoint = None
        self._in_band_since = None
        # (t, temp, power) samples kept over the stall window
        self._history: deque = deque()

    def update(self, now: float, temp_c: float, setpoint_c: float,
               power_pct: float) -> None:
        cfg = self.config

        # A setpoint change restarts the whole decision.
        if self._setpoint is None or setpoint_c != self._setpoint:
            self.reset()
            self._setpoint = setpoint_c

        err = abs(temp_c - setpoint_c)

        # --- settled: threshold + dwell, with hysteresis ---
        if self.settled:
            if err > cfg.tolerance_c + cfg.hysteresis_c:
                self.settled = False
                self._in_band_since = None
        else:
            if err <= cfg.tolerance_c:
                if self._in_band_since is None:
                    self._in_band_since = now
                elif now - self._in_band_since >= cfg.dwell_s:
                    self.settled = True
            else:
                self._in_band_since = None

        # --- stall: saturated + flat + out of band for the whole window ---
        self._history.append((now, temp_c, power_pct))
        while self._history and now - self._history[0][0] > cfg.stall_timeout_s:
            self._history.popleft()

        if self.settled:
            self.stalled = False
            return

        window = now - self._history[0][0] if self._history else 0.0
        if window >= cfg.stall_timeout_s * 0.95:
            temps = [t for _, t, _ in self._history]
            powers = [p for _, _, p in self._history]
            flat = (max(temps) - min(temps)) <= cfg.stall_flat_band_c
            saturated = min(powers) >= cfg.stall_power_pct
            self.stalled = flat and saturated and err > cfg.tolerance_c
        else:
            self.stalled = False
