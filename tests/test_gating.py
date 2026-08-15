"""RecordGate: in-flight / transition-frame rejection (core.gating)."""

from cmos_camera_gui.core.gating import RecordGate


def test_steady_stream_rejects_in_flight_frame():
    g = RecordGate(t_arm=100.0, exposure_s=1.0, expected_cadence_s=1.36)
    assert not g.accept(100.3, 1.36)   # arrived too soon after arm
    assert g.accept(101.66, 1.36)      # fresh frame
    assert g.opened and not g.gave_up
    assert g.accept(102.0, 0.01)       # gate open: everything passes


def test_long_to_short_rejects_old_cadence_straggler():
    g = RecordGate(t_arm=100.0, exposure_s=0.05, expected_cadence_s=0.125)
    assert not g.accept(102.2, 2.2)
    assert g.accept(102.325, 0.125)


def test_short_to_long_rejects_transition_frames():
    g = RecordGate(t_arm=100.0, exposure_s=1.0, expected_cadence_s=1.075)
    assert not g.accept(100.05, 0.05)
    assert not g.accept(100.10, 0.05)
    assert g.accept(101.175, 1.075)


def test_failsafe_never_deadlocks():
    g = RecordGate(t_arm=100.0, exposure_s=1.0, expected_cadence_s=1.1,
                   max_discard=3)
    assert not g.accept(100.0, 0.01)
    assert not g.accept(100.1, 0.01)
    assert not g.accept(100.2, 0.01)
    assert g.accept(100.3, 0.01)
    assert g.gave_up
