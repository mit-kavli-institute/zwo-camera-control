"""TEC settle/stall monitor (core.thermal)."""

from cmos_camera_gui.core.thermal import TecConfig, TecSettleMonitor

CFG = TecConfig(tolerance_c=1.0, dwell_s=30.0, hysteresis_c=0.5,
                stall_timeout_s=300.0, stall_power_pct=95.0,
                stall_flat_band_c=0.5)


def test_settle_after_ramp_and_dwell():
    m = TecSettleMonitor(CFG)
    t, temp = 0.0, 20.0
    while t < 200.0:
        m.update(t, temp, 0.0, 100.0)
        temp = max(0.3, temp - 0.5)
        t += 2.0
    assert m.settled
    assert not m.stalled


def test_hysteresis_unsettle_and_setpoint_reset():
    m = TecSettleMonitor(CFG)
    t, temp = 0.0, 20.0
    while t < 200.0:
        m.update(t, temp, 0.0, 100.0)
        temp = max(0.3, temp - 0.5)
        t += 2.0
    m.update(t, 2.0, 0.0, 80.0)          # beyond tolerance + hysteresis
    assert not m.settled
    m.update(t + 2, 0.3, -5.0, 100.0)    # setpoint change resets
    assert not m.settled


def test_stall_detection_parked_tec():
    m = TecSettleMonitor(CFG)
    t = 0.0
    while t < 400.0:
        m.update(t, -17.5, -20.0, 100.0)  # parked short of setpoint
        t += 2.0
    assert not m.settled
    assert m.stalled
