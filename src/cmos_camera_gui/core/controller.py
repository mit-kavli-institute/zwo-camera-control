"""
CameraController — the single command surface for camera operation.

Both the GUI and the WebSocket remote server drive the camera exclusively
through this class, so every capability is available identically from
either. The GUI is a view: it renders controller signals and pushes user
input into controller methods; it never touches the SDK.

Phase-1 note: the controller is a QObject living on the GUI thread and the
capture loop is still the Qt-based ``CaptureWorker``. Phase 2 replaces the
vendor-specific internals with the ``CameraBackend`` interface and removes
Qt from this package; the public API here is designed not to change.

Threading contract
------------------
All controller methods must be called on the thread that created it (the
GUI thread). The WS server marshals onto that thread via its signal
bridge. Signals are emitted from that same thread except where noted.
"""

from __future__ import annotations

import logging
import os
import time

import numpy as np

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..sdk import ASICamera, ASIDriver, ASIError, CameraInfo, Ctrl, ImgType
from ..camera_config import CameraControlSet, CameraSettings
from ..capture import CaptureWorker
from ..recorder import save_fits_cube, save_fits_individual, HAS_ASTROPY
from .states import CameraState
from .thermal import TecConfig, TecSettleMonitor

log = logging.getLogger("cmoscam.controller")

VENDOR = "zwo"  # Phase 2 makes this a per-backend property

_SDK_CANDIDATES = [
    "ASICamera2.dll",
    r"C:\Program Files\ASIStudio\ASICamera2.dll",
    r"C:\Program Files (x86)\ASIStudio\ASICamera2.dll",
    "/usr/lib/libASICamera2.so",
    "/usr/local/lib/libASICamera2.so",
]


class CameraController(QObject):

    # -- signals (the GUI renders these; low-frequency only) --
    status_message = pyqtSignal(str)
    state_changed = pyqtSignal(str)              # CameraState.name
    connected_changed = pyqtSignal(bool)
    streaming_changed = pyqtSignal(bool)
    control_changed = pyqtSignal(str, int)       # remote-origin control edits
    roi_changed = pyqtSignal()                   # remote-origin ROI/img_type edits
    record_params_changed = pyqtSignal()         # remote-origin record param edits
    record_started = pyqtSignal(int)             # n_frames
    record_progress = pyqtSignal(int, int)       # got, target
    record_finished = pyqtSignal(str)            # save-result message
    record_cancelled = pyqtSignal()
    stats_update = pyqtSignal(float, int, int, float)  # fps, total, dropped, temp
    thermal_update = pyqtSignal(float, float)    # temp_C, power_pct
    error = pyqtSignal(str)

    def __init__(self, sdk_path=None, parent=None):
        super().__init__(parent)

        self._driver = None
        self._camera = None
        self._worker = None
        self._worker_thread = None
        self._streaming = False

        self._control_set = None    # CameraControlSet
        self._settings = None       # CameraSettings

        # Canonical acquisition geometry/format (GUI widgets mirror these)
        self.roi = {"x": 0, "y": 0, "w": 0, "h": 0}
        self.img_type = "RAW16"

        # Canonical record parameters (GUI widgets mirror these)
        self.record_params = {
            "n_frames": 100,
            "directory": os.getcwd(),
            "basename": "capture",
            "mode": "stack",        # "stack" | "individual"
        }

        # Display metadata contributed by the GUI (kept in FITS header)
        self.display_stretch = ""

        # Cooler state (canonical; hardware is set via set_cooler)
        self._cooler_on = False
        self._cooler_target = -10.0
        self.tec_monitor = TecSettleMonitor(TecConfig())

        # Per-record header extras (from remote record cmd; cleared after)
        self._next_record_obstype = None
        self._next_record_extras = []

        # One-shot callback for the remote server's record-done follow-up.
        # May be invoked from the FITS writer thread's bridge (GUI thread).
        self._record_done_cb = None

        self._saving = False
        self._error_msg = None      # latched -> CameraState.ERROR
        self._state = CameraState.DISCONNECTED

        # Thermal poll (runs while connected on cooled cameras)
        self._thermal_timer = QTimer(self)
        self._thermal_timer.timeout.connect(self._poll_thermal)

        if sdk_path is not False:
            self.load_sdk(sdk_path)

    # =================================================================
    #  FSM
    # =================================================================

    @property
    def camera_state(self) -> CameraState:
        return self._state

    def _recompute_state(self, transient: CameraState | None = None):
        if transient is not None:
            new = transient
        elif self._error_msg is not None:
            new = CameraState.ERROR
        elif self._camera is None:
            new = CameraState.DISCONNECTED
        elif self._recording_active():
            new = CameraState.EXPOSING
        elif self._saving:
            new = CameraState.SAVING
        elif self._cooler_on and not self.tec_monitor.settled:
            new = CameraState.TEC_SETTLING
        else:
            new = CameraState.READY
        if new is not self._state:
            self._state = new
            log.info("state -> %s", new.name)
            self.state_changed.emit(new.name)

    def _recording_active(self) -> bool:
        if not self._worker:
            return False
        with self._worker._rec_lock:
            return self._worker._rec_cube is not None

    def clear_error(self):
        self._error_msg = None
        self._recompute_state()

    def _latch_error(self, msg: str):
        self._error_msg = msg
        self.error.emit(msg)
        self._recompute_state()

    # =================================================================
    #  SDK
    # =================================================================

    @property
    def sdk_loaded(self) -> bool:
        return self._driver is not None

    def load_sdk(self, path=None) -> bool:
        candidates = ([path] if path else []) + _SDK_CANDIDATES
        for c in candidates:
            if os.path.isfile(c):
                try:
                    self._driver = ASIDriver(c)
                    self.status_message.emit(f"SDK loaded: {c}")
                    return True
                except Exception as e:
                    self.status_message.emit(f"SDK load failed ({c}): {e}")
        self.status_message.emit(
            "SDK not found -- click Browse SDK to locate ASICamera2.dll/.so"
        )
        return False

    # =================================================================
    #  Discovery / connection
    # =================================================================

    @property
    def camera(self):
        return self._camera

    @property
    def connected(self) -> bool:
        return self._camera is not None

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def control_set(self):
        return self._control_set

    @property
    def settings(self):
        return self._settings

    @property
    def frame_queue(self):
        """Bounded display-frame queue of the running capture, or None."""
        return self._worker.frame_queue if self._worker else None

    def list_cameras(self) -> list:
        if not self._driver:
            raise RuntimeError("SDK not loaded")
        cams = []
        for i in range(self._driver.get_num_cameras()):
            info = CameraInfo.from_struct(self._driver.get_camera_property(i))
            cams.append({"index": i, "name": info.name, "vendor": VENDOR})
        return cams

    def connect_camera(self, index: int):
        if not self._driver:
            raise RuntimeError("SDK not loaded")
        if self._camera is not None:
            return
        self._recompute_state(transient=CameraState.INITIALIZING)
        try:
            self._camera = ASICamera(self._driver, index)
            cam = self._camera

            caps_dict = cam.get_caps_dict()
            self._control_set = CameraControlSet.from_caps_dict(
                cam.info.name, caps_dict
            )
            self._settings = CameraSettings(self._control_set)
            log.info("\n%s", self._control_set.describe())

            # Max out USB bandwidth for streaming
            self._settings.set_if_present("BandWidth", 9999, clamp=True)

            # Full-frame ROI by default
            self.roi = {
                "x": 0, "y": 0,
                "w": cam.info.max_width, "h": cam.info.max_height,
            }

            self.apply_settings(silent=True)

            self.tec_monitor.reset()
            self._cooler_on = False
            if self._control_set.has_cooler():
                spec = self._control_set.get("TargetTemp")
                if spec:
                    self._cooler_target = spec.default_value
                self._thermal_timer.start(2000)

            flags = []
            if self._control_set.has_cooler():
                flags.append("cooled")
            if self._control_set.has_frame_rate_control():
                flags.append("indep-fps")
            if self._control_set.has_offset():
                flags.append("offset")
            self.status_message.emit(
                f"Connected: {cam.info.name}  |  "
                f"{cam.info.max_width}x{cam.info.max_height}  |  "
                f"{cam.info.bit_depth}-bit  |  "
                f"USB3={'yes' if cam.info.is_usb3 else 'no'}  |  "
                + "  ".join(flags)
            )
        except Exception:
            self._camera = None
            self._control_set = None
            self._settings = None
            self._recompute_state()
            raise
        self._recompute_state()
        self.connected_changed.emit(True)

    def disconnect_camera(self):
        self.stop_stream()
        self._thermal_timer.stop()
        if self._camera:
            self._camera.close()
            self._camera = None
        self._control_set = None
        self._settings = None
        self._cooler_on = False
        self._error_msg = None
        self.tec_monitor.reset()
        self._recompute_state()
        self.connected_changed.emit(False)
        self.status_message.emit("Disconnected")

    # =================================================================
    #  Controls / settings
    # =================================================================

    def stage_control(self, name: str, value) -> bool:
        """Stage a control value (GUI-origin; no control_changed echo)."""
        if not self._settings:
            return False
        try:
            self._settings.set(name, value, clamp=True)
            return True
        except Exception:
            return False

    def set_control(self, name: str, value) -> bool:
        """Stage a control value (remote-origin; echoes to the GUI)."""
        if not self._settings:
            return False
        if not self._settings.set_if_present(name, value, clamp=True):
            return False
        self.control_changed.emit(name, int(self._settings.get(name)))
        return True

    def readonly_values(self) -> dict:
        """Current hardware values of read-only controls (for display)."""
        vals = {}
        if self._camera and self._control_set:
            for spec in self._control_set.readonly():
                try:
                    vals[spec.name] = self._camera.get_ctrl_value(
                        spec.control_type
                    )
                except Exception:
                    pass
        return vals

    def set_roi(self, x=None, y=None, w=None, h=None, img_type=None,
                notify=True):
        if x is not None:
            self.roi["x"] = int(x)
        if y is not None:
            self.roi["y"] = int(y)
        if w is not None:
            self.roi["w"] = int(w)
        if h is not None:
            self.roi["h"] = int(h)
        if img_type is not None:
            if img_type not in ("RAW8", "RAW16"):
                raise ValueError(f"img_type must be RAW8|RAW16, got {img_type!r}")
            self.img_type = img_type
        if notify:
            self.roi_changed.emit()

    def full_frame_roi(self):
        if self._camera:
            self.set_roi(0, 0, self._camera.info.max_width,
                         self._camera.info.max_height)

    def apply_settings(self, silent=False):
        """Push ROI/format + all staged control values to the camera.

        Returns a list of (name, exception) pairs; empty on full success.
        Raises only if the camera itself rejects the ROI/format.
        """
        cam = self._camera
        if not cam or not self._settings:
            raise RuntimeError("no camera connected")

        img_type = ImgType.RAW16 if self.img_type == "RAW16" else ImgType.RAW8
        cam.set_roi(
            self.roi["w"], self.roi["h"], 1, img_type,
            self.roi["x"], self.roi["y"],
        )
        errors = self._settings.apply(cam)

        if not silent:
            if errors:
                err_str = ", ".join(f"{n}: {e}" for n, e in errors)
                self.status_message.emit(f"Settings errors: {err_str}")
            else:
                w, h, _b, _t = cam.get_roi()
                self.status_message.emit(
                    f"Applied -- ROI={w}x{h}+({self.roi['x']},{self.roi['y']})  "
                    f"{self.img_type}"
                )
        return errors

    # =================================================================
    #  Cooler / thermal
    # =================================================================

    def set_cooler(self, on: bool, target_c=None):
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            raise RuntimeError("camera has no cooler")
        if target_c is not None:
            self._cooler_target = float(target_c)
        cam.set_cooler(on=bool(on), target_c=int(self._cooler_target))
        if bool(on) != self._cooler_on or target_c is not None:
            self.tec_monitor.reset()
        self._cooler_on = bool(on)
        self.status_message.emit(
            f"Cooler {'ON' if on else 'OFF'}, target={self._cooler_target:g} C"
        )
        self._recompute_state()

    def _poll_thermal(self):
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            return
        try:
            temp = cam.temperature()
            power = (
                cam.get_ctrl_value(Ctrl.COOLER_POWER_PERC)
                if cam.has_ctrl(Ctrl.COOLER_POWER_PERC) else 0
            )
        except ASIError:
            return
        if self._cooler_on:
            self.tec_monitor.update(
                time.monotonic(), temp, self._cooler_target, power
            )
        self.thermal_update.emit(temp, float(power))
        self._recompute_state()

    def thermal_status(self) -> dict:
        d = {
            "cooler_on": self._cooler_on,
            "setpoint": self._cooler_target,
            "tec_locked": bool(self._cooler_on and self.tec_monitor.settled),
            "tec_stalled": bool(self.tec_monitor.stalled),
        }
        cam = self._camera
        if cam and cam.info.is_cooler:
            try:
                d["temp"] = cam.temperature()
                if cam.has_ctrl(Ctrl.COOLER_POWER_PERC):
                    d["cooler_power"] = cam.get_ctrl_value(
                        Ctrl.COOLER_POWER_PERC
                    )
            except ASIError:
                pass
        return d

    # =================================================================
    #  Streaming
    # =================================================================

    def start_stream(self):
        cam = self._camera
        if not cam:
            raise RuntimeError("no camera connected")
        if self._streaming:
            return

        exp_us = self._settings.get("Exposure") if self._settings else 100_000
        exp_ms = (exp_us or 100_000) / 1000.0

        self._worker = CaptureWorker(cam, exp_ms)
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.stats_update.connect(self.stats_update)
        self._worker.recording_progress.connect(self.record_progress)
        self._worker.recording_done.connect(self._on_recording_done)
        self._worker.error.connect(self._on_capture_error)

        self._worker_thread.start()
        self._streaming = True
        self.streaming_changed.emit(True)
        self.status_message.emit("Streaming...")

    def stop_stream(self):
        if self._worker:
            self._worker.request_stop()
        if self._worker_thread:
            self._worker_thread.quit()
            self._worker_thread.wait(5000)
        was_streaming = self._streaming
        self._worker = None
        self._worker_thread = None
        self._streaming = False
        if was_streaming:
            self.streaming_changed.emit(False)
            self.status_message.emit("Stream stopped")
        self._recompute_state()

    def _on_capture_error(self, msg: str):
        self._latch_error(f"Capture: {msg}")
        self.status_message.emit(f"Capture: {msg}")

    # =================================================================
    #  Recording
    # =================================================================

    def set_record_params(self, notify=True, **kwargs):
        for k, v in kwargs.items():
            if v is None:
                continue
            if k not in self.record_params:
                raise KeyError(f"unknown record param {k!r}")
            self.record_params[k] = v
        mode = self.record_params["mode"]
        if mode not in ("stack", "individual"):
            raise ValueError(f"mode must be stack|individual, got {mode!r}")
        if notify:
            self.record_params_changed.emit()

    def set_record_done_callback(self, cb):
        """One-shot callback fired with the save-result message (WS server)."""
        self._record_done_cb = cb

    def start_record(self, obstype=None, extra_headers=None, **param_overrides):
        if not HAS_ASTROPY:
            raise RuntimeError("astropy not installed -- pip install astropy")
        if not self._worker or not self._streaming:
            raise RuntimeError("not streaming -- start the stream first")
        if self._recording_active():
            raise RuntimeError("recording already in progress")

        if param_overrides:
            self.set_record_params(**param_overrides)
        n = int(self.record_params["n_frames"])

        cam = self._camera
        w, h, _bin, img_t = cam.get_roi()
        dtype = np.uint16 if img_t == int(ImgType.RAW16) else np.uint8

        self._next_record_obstype = obstype or None
        self._next_record_extras = list(extra_headers or [])

        self._worker.start_recording(n, w, h, dtype)
        self._recompute_state()
        self.record_started.emit(n)
        self.status_message.emit(f"Recording {n} frames...")

    def cancel_record(self):
        if self._worker:
            self._worker.cancel_recording()
        self._next_record_obstype = None
        self._next_record_extras = []
        self._recompute_state()
        self.record_cancelled.emit()
        self.status_message.emit("Recording cancelled")

    def _build_record_metadata(self, cube, elapsed) -> dict:
        cam = self._camera
        actual_fps = cube.shape[0] / elapsed if elapsed > 0 else 0

        meta = {
            "INSTRUME": cam.info.name if cam else "unknown",
            "NFRAMES": cube.shape[0],
            "STRMFPS": (round(actual_fps, 3), "measured stream rate [fps]"),
            "ELAPSED": (round(elapsed, 4), "total acquisition time [s]"),
            "DEPTH": self.img_type,
        }
        if self.display_stretch:
            meta["STRETCH"] = self.display_stretch
        if self._next_record_obstype:
            meta["OBSTYPE"] = self._next_record_obstype

        # All current control values; Exposure separately as EXPTIME (ms).
        if self._settings:
            snap = self._settings.snapshot()
            for k, v in snap.items():
                if k == "Exposure":
                    continue
                meta[k[:8].upper()] = v
            if "Exposure" in snap:
                meta["EXPTIME"] = (
                    snap["Exposure"] / 1000.0, "[ms] exposure time"
                )
        if cam:
            w, h, _b, _t = cam.get_roi()
            meta["ROI_W"] = w
            meta["ROI_H"] = h
            meta["ROI_X"] = self.roi["x"]
            meta["ROI_Y"] = self.roi["y"]
            if cam.info.is_cooler:
                try:
                    meta["DETTEMP"] = (
                        cam.temperature(), "[C] sensor temperature"
                    )
                except ASIError:
                    pass

        # Extras from the remote client, applied last so they win.
        for extra in (self._next_record_extras or []):
            key = extra[0]
            val = extra[1]
            cmt = extra[2] if len(extra) > 2 else None
            meta[key] = (val, cmt) if cmt else val
        self._next_record_obstype = None
        self._next_record_extras = []
        return meta

    def _on_recording_done(self, cube, timestamps, elapsed):
        meta = self._build_record_metadata(cube, elapsed)

        directory = str(self.record_params["directory"]) or os.getcwd()
        basename = str(self.record_params["basename"]) or "capture"
        stack_mode = self.record_params["mode"] == "stack"

        self._saving = True
        self._recompute_state()

        def _after_save(msg):
            self._saving = False
            self.status_message.emit(msg)
            self.record_finished.emit(msg)
            if self._record_done_cb:
                cb, self._record_done_cb = self._record_done_cb, None
                cb(msg)
            self._recompute_state()

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            _after_save(f"FITS save error: cannot create {directory}: {e}")
            return

        if stack_mode:
            path = os.path.join(directory, f"{basename}.fits")
            save_fits_cube(path, cube, meta, _after_save)
        else:
            save_fits_individual(
                directory, basename, cube, timestamps, meta, _after_save
            )

    # =================================================================
    #  Status / remote command dispatch
    # =================================================================

    def status(self) -> dict:
        cam = self._camera
        result = {
            "cmd": "status",
            "connected": cam is not None,
            "streaming": self._streaming,
            "camera": cam.info.name if cam else None,
            "vendor": VENDOR if cam else None,
            "camera_state": self._state.name,
        }
        if self._error_msg:
            result["error_message"] = self._error_msg
        if cam:
            result.update(self.thermal_status())
            result["roi"] = dict(self.roi)
            result["img_type"] = self.img_type
        if self._settings:
            result["controls"] = self._settings.snapshot()
        return result

    def handle_command(self, cmd: dict) -> dict:
        """Remote JSON command dispatch (called on the controller thread)."""
        action = cmd.get("cmd", "")

        if action == "status":
            return self.status()

        elif action == "list_cameras":
            try:
                return {"cmd": "list_cameras", "cameras": self.list_cameras()}
            except Exception as e:
                return {"cmd": "list_cameras", "cameras": [], "error": str(e)}

        elif action == "connect_camera":
            if self._camera is not None:
                return {"cmd": "connect_camera", "ok": True,
                        "camera": self._camera.info.name,
                        "note": "already connected"}
            idx = int(cmd.get("index", 0))
            try:
                known = {c["index"] for c in self.list_cameras()}
                if idx not in known:
                    return {"cmd": "connect_camera", "ok": False,
                            "error": f"no camera with index {idx}"}
                self.connect_camera(idx)
            except Exception as e:
                return {"cmd": "connect_camera", "ok": False, "error": str(e)}
            return {"cmd": "connect_camera", "ok": True,
                    "camera": self._camera.info.name}

        elif action == "disconnect_camera":
            if self._camera is None:
                return {"cmd": "disconnect_camera", "ok": True,
                        "note": "not connected"}
            self.disconnect_camera()
            return {"cmd": "disconnect_camera", "ok": True}

        elif action == "set":
            roi_kwargs = {}
            for key, value in cmd.items():
                if key == "cmd":
                    continue
                if key == "img_type":
                    roi_kwargs["img_type"] = str(value)
                elif key in ("roi_w", "roi_h", "roi_x", "roi_y"):
                    roi_kwargs[key[4:]] = int(value)
                else:
                    self.set_control(key, value)
            if roi_kwargs:
                try:
                    self.set_roi(**roi_kwargs)
                except ValueError as e:
                    return {"cmd": "set", "ok": False, "error": str(e)}
            if self._camera:
                try:
                    self.apply_settings(silent=True)
                except Exception as e:
                    return {"cmd": "set", "ok": False, "error": str(e)}
            return {"cmd": "set", "ok": True}

        elif action == "start_stream":
            try:
                self.start_stream()
            except Exception as e:
                return {"cmd": "start_stream", "ok": False, "error": str(e)}
            return {"cmd": "start_stream", "ok": self._streaming}

        elif action == "stop_stream":
            self.stop_stream()
            return {"cmd": "stop_stream", "ok": True}

        elif action == "record":
            params = {}
            if "n_frames" in cmd:
                params["n_frames"] = int(cmd["n_frames"])
            # Backward-compat: `path` sets directory+basename in one shot.
            if "path" in cmd:
                d, f = os.path.split(str(cmd["path"]))
                if d:
                    params["directory"] = d
                base, _ext = os.path.splitext(f)
                if base:
                    params["basename"] = base
            if "directory" in cmd:
                params["directory"] = str(cmd["directory"])
            if "basename" in cmd:
                params["basename"] = str(cmd["basename"])
            if "mode" in cmd:
                params["mode"] = str(cmd["mode"]).lower()

            try:
                self.start_record(
                    obstype=cmd.get("obstype"),
                    extra_headers=cmd.get("extra_headers"),
                    **params,
                )
            except Exception as e:
                return {"cmd": "record", "error": str(e)}
            return {
                "cmd": "record", "ack": True,
                "n_frames": self.record_params["n_frames"],
                "directory": self.record_params["directory"],
                "basename": self.record_params["basename"],
                "mode": self.record_params["mode"],
            }

        elif action == "abort":
            self.cancel_record()
            return {"cmd": "abort", "ok": True}

        elif action == "cooler":
            try:
                self.set_cooler(
                    on=bool(cmd.get("on", False)),
                    target_c=cmd.get("target"),
                )
            except Exception as e:
                return {"cmd": "cooler", "ok": False, "error": str(e)}
            return {"cmd": "cooler", "ok": True}

        elif action == "clear_error":
            self.clear_error()
            return {"cmd": "clear_error", "ok": True,
                    "camera_state": self._state.name}

        return {"error": f"unknown command: {action}"}

    # =================================================================
    #  Shutdown
    # =================================================================

    def shutdown(self):
        self.stop_stream()
        self._thermal_timer.stop()
        if self._camera:
            self._camera.close()
            self._camera = None
