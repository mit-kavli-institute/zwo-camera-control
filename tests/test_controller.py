"""CameraController: FSM, command dispatch, WS verb parity, WSP contract.

All without hardware: no SDKs are loaded (sdk_path=False).
"""

import json

import pytest

from cmos_camera_gui.core.states import CameraState

# Every verb of the native protocol + the WSP contract verbs.
NATIVE_VERBS = ["status", "list_cameras", "connect_camera",
                "disconnect_camera", "set", "start_stream", "stop_stream",
                "record", "abort", "cooler", "clear_error"]
WSP_VERBS = ["get_status", "set_exposure", "set_save_path", "capture",
             "set_tec_enabled", "set_tec_temperature"]


def test_initial_state(controller):
    st = controller.status()
    assert st["camera_state"] == "DISCONNECTED"
    assert st["connected"] is False
    assert st["streaming"] is False
    assert controller.capabilities() == {}


def test_every_verb_dispatches(controller):
    for verb in NATIVE_VERBS + WSP_VERBS:
        r = controller.handle_command({"cmd": verb})
        assert "unknown command" not in str(r.get("error", "")), verb


def test_unknown_verb(controller):
    r = controller.handle_command({"cmd": "bogus"})
    assert "unknown command" in r["error"]


def test_commands_fail_cleanly_without_camera(controller):
    assert "error" in controller.handle_command(
        {"cmd": "record", "n_frames": 5})
    r = controller.handle_command({"cmd": "start_stream"})
    assert r["ok"] is False
    assert controller.handle_command({"cmd": "abort"})["ok"] is True
    assert controller.handle_command({"cmd": "cooler", "on": True})["ok"] is False
    assert controller.handle_command(
        {"cmd": "set_exposure", "exposure": 1.0})["status"] == "error"
    assert controller.handle_command(
        {"cmd": "capture", "filename": "x"})["status"] == "error"


def test_error_latch_and_clear(controller):
    controller._latch_error("test fault")
    assert controller.camera_state is CameraState.ERROR
    assert controller.status()["error_message"] == "test fault"
    r = controller.handle_command({"cmd": "clear_error"})
    assert r["camera_state"] == "DISCONNECTED"


def test_record_param_validation(controller):
    controller.set_record_params(n_frames=42, mode="individual",
                                 combine="mean", combine_only=True)
    assert controller.record_params["n_frames"] == 42
    with pytest.raises(ValueError):
        controller.set_record_params(mode="nope")
    with pytest.raises(ValueError):
        controller.set_record_params(combine="max")


def test_wsp_status_shape_and_json_native(controller):
    reply = controller.handle_command({"cmd": "get_status"})
    assert reply["status"] == "success"
    data = reply["data"]
    required = ["camera_state", "ready", "is_capturing", "current_frame",
                "total_frames", "capture_time_remaining", "exposure",
                "tec_temp", "tec_setpoint", "tec_enabled", "tec_locked",
                "tec_voltage", "save_path", "case_temp", "digpcb_temp",
                "senspcb_temp"]
    missing = [k for k in required if k not in data]
    assert not missing, f"missing WSP status keys: {missing}"
    json.dumps(data)   # JSON-native types only
    assert data["camera_state"] == "DISCONNECTED"
    assert data["ready"] is False
    assert data["is_capturing"] is False
    assert data["tec_voltage"] == -888


def test_wsp_set_save_path(tmp_path, controller):
    target = tmp_path / "night" / "summer"
    r = controller.handle_command(
        {"cmd": "set_save_path", "path": str(target)})
    assert r["status"] == "success"
    assert target.is_dir()
    assert controller.record_params["directory"] == str(target)


def test_wsp_tec_setpoint_without_camera(controller):
    r = controller.handle_command(
        {"cmd": "set_tec_temperature", "temperature": -5.0})
    assert r["status"] == "success"
    assert controller._cooler_target == -5.0


def test_setting_exposure_state_exists():
    assert CameraState.SETTING_EXPOSURE.name == "SETTING_EXPOSURE"


def test_vendor_registry():
    from cmos_camera_gui.vendors import create_vendors
    vs = create_vendors()
    assert set(vs) == {"zwo", "qhy"}
    for v in vs.values():
        assert hasattr(v.profile, "default_overrides")
        assert hasattr(v.profile, "advanced_controls")


def test_client_aliases_and_helpers():
    import cmos_camera_gui.client as cl
    from cmos_camera_gui.summer_client import SummerClient
    assert cl.ASIClient is cl.CameraClient
    assert hasattr(cl.CameraClient, "wait_for_state")
    for m in ("get_status", "set_exposure", "set_save_path",
              "capture_frames", "set_tec_enabled", "set_tec_temperature"):
        assert hasattr(SummerClient, m)
