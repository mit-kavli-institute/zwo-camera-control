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
from datetime import datetime

from PyQt5.QtCore import QObject, QThread, QTimer, pyqtSignal

from .. import __version__
from ..camera_config import CameraControlSet, CameraSettings
from ..recorder import save_fits_cube, save_fits_individual, HAS_ASTROPY
from ..vendors import create_vendors
from .gating import RecordGate
from .states import CameraState
from .thermal import TecSettleMonitor

log = logging.getLogger("cmoscam.controller")


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
    readonly_update = pyqtSignal(object)         # {name: raw_value} from worker
    thermal_update = pyqtSignal(float, float)    # temp_C, power_pct
    idle_changed = pyqtSignal()                  # remote-origin idle-mode edits
    error = pyqtSignal(str)

    # Internal: emitted from the capture thread once a record is armed.
    _record_armed = pyqtSignal(int)
    # Internal: emitted from the capture thread when a queued exposure
    # push has reached the hardware (clears SETTING_EXPOSURE).
    _exposure_applied = pyqtSignal()

    def __init__(self, sdk_path=None, parent=None):
        super().__init__(parent)

        self._vendors = create_vendors()   # name -> Vendor instance
        self._vendor = None                # vendor of the connected camera
        self._cam_map = []                 # flat index -> (vendor, local_idx)
        self._camera = None
        self._worker = None
        self._worker_thread = None
        self._streaming = False
        self._applied_exposure_s = None    # exposure last pushed to hardware

        # WSP contract state (SummerCameraGuiHandoff §2): the requested
        # exposure in float seconds, echoed EXACTLY in get_status (the
        # daemon's completion check is a float == test); and a flag for
        # the SETTING_EXPOSURE window while a push is queued to hardware.
        self._requested_exposure_s = None
        self._exposure_settling = False
        # Deployment identity: overrides INSTRUME / status camname
        # ("summer" at the telescope; camera model by default in the lab).
        self.instrument_name = None
        self._record_start_utc = None      # wall-clock UTC at record arm

        self._control_set = None    # CameraControlSet
        self._settings = None       # CameraSettings

        # Canonical acquisition geometry/format (GUI widgets mirror these)
        self.roi = {"x": 0, "y": 0, "w": 0, "h": 0}
        self.img_type = "RAW16"
        self._applied_roi = None  # last (x,y,w,h,img_type) pushed to hardware

        # Canonical record parameters (GUI widgets mirror these)
        self.record_params = {
            "n_frames": 100,
            "directory": os.getcwd(),
            "basename": "capture",
            "mode": "stack",        # "stack" | "individual"
            "combine": "none",      # "none" | "mean" | "median"
            "combine_only": False,  # skip cube/frames, keep combined only
        }

        # Display metadata contributed by the GUI (kept in FITS header)
        self.display_stretch = ""

        # High-speed idle (HANDOFF §1/§4): between grabs the stream runs
        # at idle_exposure_us; a grab switches to the staged Exposure at
        # arm (transitions gated away) and drops back to idle when the
        # capture completes. Restores the T + ~65 ms first-frame floor
        # for long exposures. Off by default: preview then shows real
        # target-exposure frames.
        self.idle_mode = False
        self.idle_exposure_us = 1000

        # Cooler state (canonical; hardware is set via set_cooler)
        self._cooler_on = False
        self._cooler_target = -10.0
        self.tec_monitor = TecSettleMonitor()   # re-tuned per vendor at connect

        # Per-record header extras (from remote record cmd; cleared after)
        self._next_record_obstype = None
        self._next_record_extras = []
        self._next_record_wsp = False   # WSP single-file output semantics

        # One-shot callback for the remote server's record-done follow-up.
        # May be invoked from the FITS writer thread's bridge (GUI thread).
        self._record_done_cb = None

        self._saving = False
        self._record_pending = False   # record armed on capture thread
        self._error_msg = None      # latched -> CameraState.ERROR
        self._state = CameraState.DISCONNECTED

        # Thermal telemetry cache, fed by the capture thread while
        # streaming so nothing else has to call into the SDK.
        self._last_temp = None
        self._last_power = 0.0

        self._record_armed.connect(self._on_record_armed)
        self._exposure_applied.connect(self._on_exposure_applied)

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
        elif self._exposure_settling:
            new = CameraState.SETTING_EXPOSURE
        elif self._cooler_on and not self.tec_monitor.settled:
            new = CameraState.TEC_SETTLING
        else:
            new = CameraState.READY
        if new is not self._state:
            self._state = new
            log.info("state -> %s", new.name)
            self.state_changed.emit(new.name)

    def _recording_active(self) -> bool:
        if self._record_pending:
            return True
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
        return any(v.loaded for v in self._vendors.values())

    def load_sdk(self, path=None) -> bool:
        """Load every vendor SDK that can be found.

        `path` (the --sdk flag / Browse dialog) is offered to each vendor;
        each ignores paths that aren't its library.
        """
        loaded = []
        for v in self._vendors.values():
            if v.loaded:
                loaded.append(v.name)
                continue
            where = v.load(path)
            if where:
                loaded.append(v.name)
                self.status_message.emit(f"{v.name} SDK loaded: {where}")
        if not loaded:
            self.status_message.emit(
                "No camera SDK found -- Browse to ASICamera2.dll, or place "
                "qhyccd.dll under sdk/"
            )
            return False
        self.status_message.emit("SDKs loaded: " + ", ".join(loaded))
        return True

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
        """Enumerate cameras across all loaded vendors (flat indices)."""
        if not self.sdk_loaded:
            raise RuntimeError("SDK not loaded")
        self._cam_map = []
        cams = []
        for v in self._vendors.values():
            if not v.loaded:
                continue
            for c in v.list_cameras():
                idx = len(self._cam_map)
                self._cam_map.append((v, c["index"]))
                cams.append({
                    "index": idx, "name": c["name"], "vendor": v.name,
                })
        return cams

    def capabilities(self) -> dict:
        """Capability flags of the connected camera ({} if none)."""
        if not self._camera:
            return {}
        return self._camera.capabilities()

    def advanced_control_names(self) -> set:
        """Controls the GUI should tuck into the Advanced dialog."""
        if not self._vendor:
            return set()
        return set(self._vendor.profile.advanced_controls)

    def connect_camera(self, index: int):
        if not self.sdk_loaded:
            raise RuntimeError("SDK not loaded")
        if self._camera is not None:
            return
        if not self._cam_map:
            self.list_cameras()
        if not 0 <= index < len(self._cam_map):
            raise RuntimeError(
                f"camera index {index} out of range "
                f"({len(self._cam_map)} camera(s) found)"
            )
        vendor, local_idx = self._cam_map[index]

        self._recompute_state(transient=CameraState.INITIALIZING)
        try:
            self._camera = vendor.open(local_idx)
            self._vendor = vendor
            cam = self._camera

            caps_dict = cam.get_caps_dict()
            self._control_set = CameraControlSet.from_caps_dict(
                cam.info.name, caps_dict,
                default_overrides=vendor.profile.default_overrides,
            )
            self._settings = CameraSettings(self._control_set)
            vendor.profile.post_connect(self._settings)
            log.info("\n%s", self._control_set.describe())

            # Per-vendor TEC tuning
            self.tec_monitor = TecSettleMonitor(vendor.profile.tec_config)

            # Full-frame ROI by default
            self.roi = {
                "x": 0, "y": 0,
                "w": cam.info.max_width, "h": cam.info.max_height,
            }

            self.apply_settings(silent=True)

            self._cooler_on = False
            caps = cam.capabilities()
            if caps.get("has_cooler"):
                spec = self._control_set.get("TargetTemp")
                if spec:
                    self._cooler_target = spec.default_value
                self._thermal_timer.start(2000)

            flags = [vendor.name]
            if caps.get("has_cooler"):
                flags.append("cooled")
            if caps.get("has_gps"):
                flags.append("gps")
            if self._control_set.has_offset():
                flags.append("offset")
            self.status_message.emit(
                f"Connected: {cam.info.name}  |  "
                f"{cam.info.max_width}x{cam.info.max_height}  |  "
                f"{cam.info.bit_depth}-bit  |  "
                + "  ".join(flags)
            )
        except Exception:
            if self._camera is not None:
                try:
                    self._camera.close()
                except Exception:
                    pass
            self._camera = None
            self._vendor = None
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
        self._vendor = None
        self._control_set = None
        self._settings = None
        self._cooler_on = False
        self._error_msg = None
        self._applied_roi = None
        self._applied_exposure_s = None
        self._last_temp = None
        self.tec_monitor.reset()
        self._recompute_state()
        self.connected_changed.emit(False)
        self.status_message.emit("Disconnected")

    # =================================================================
    #  Controls / settings
    # =================================================================

    def stage_control(self, name: str, value) -> bool:
        """Stage a control value and push it to the camera immediately
        (GUI-origin; no control_changed echo).

        Live-apply keeps GUI edits and remote `set` commands equivalent:
        what the controls show is what the hardware runs. Geometry
        (ROI/img_type) is deliberately excluded — it goes through
        apply_settings because mid-stream ROI pushes stall the stream.
        """
        if not self._settings:
            return False
        try:
            self._settings.set(name, value, clamp=True)
        except Exception:
            return False
        self._push_control(name)
        return True

    def _push_control(self, name: str):
        """Push one staged control to hardware without blocking the caller.

        While streaming the capture thread owns the SDK (calls from other
        threads block for up to one exposure), so the push is queued there.
        """
        cam, settings = self._camera, self._settings
        if not cam or not settings:
            return
        if name == "Exposure":
            val = settings.get("Exposure")
            if val:
                self._requested_exposure_s = val / 1e6
        # In high-speed idle, Exposure edits stay staged: the stream keeps
        # idling and the grab arm applies the staged value.
        if name == "Exposure" and self.idle_mode and self._streaming:
            return

        def _do():
            try:
                settings.apply_one(cam, name)
                if name == "Exposure":
                    val = settings.get("Exposure")
                    if val:
                        self._applied_exposure_s = val / 1e6
            except Exception as e:
                log.debug("live-apply %s failed: %s", name, e)
            finally:
                if name == "Exposure":
                    self._exposure_applied.emit()

        if self._streaming and self._worker:
            if name == "Exposure":
                self._exposure_settling = True
                self._recompute_state()
            self._worker.run_async(_do)
        else:
            _do()
            self._exposure_settling = False

    def _on_exposure_applied(self):
        self._exposure_settling = False
        self._recompute_state()

    # -- high-speed idle ----------------------------------------------

    def set_idle_mode(self, enabled: bool, idle_exposure_us=None,
                      notify=True):
        """Toggle high-speed idle; optionally set the idle exposure [us]."""
        self.idle_mode = bool(enabled)
        if idle_exposure_us is not None:
            self.idle_exposure_us = max(1, int(idle_exposure_us))
        if self._streaming and not self._recording_active():
            if self.idle_mode:
                self._push_idle_exposure()
            else:
                self._push_control("Exposure")   # restore staged exposure
        self.status_message.emit(
            f"High-speed idle {'ON' if self.idle_mode else 'OFF'}"
            + (f" ({self.idle_exposure_us} us)" if self.idle_mode else "")
        )
        if notify:
            self.idle_changed.emit()

    def _push_idle_exposure(self):
        """Drop the running stream to the idle exposure (worker thread)."""
        if not (self.idle_mode and self._streaming and self._worker):
            return
        spec = self._control_set.get("Exposure") if self._control_set else None
        if spec is None:
            return
        cam = self._camera
        us = int(self.idle_exposure_us)

        def _do():
            try:
                cam.set_ctrl(spec.control_type, us)
                self._applied_exposure_s = us / 1e6
            except Exception as e:
                log.debug("idle exposure push failed: %s", e)

        self._worker.run_async(_do)

    def set_control(self, name: str, value) -> bool:
        """Stage a control value (remote-origin; echoes to the GUI)."""
        if not self._settings:
            return False
        if not self._settings.set_if_present(name, value, clamp=True):
            return False
        self._push_control(name)
        self.control_changed.emit(name, int(self._settings.get(name)))
        return True

    def readonly_values(self) -> dict:
        """Current hardware values of read-only controls (for display).

        Idle-only: while streaming, consume the readonly_update signal
        instead — direct SDK reads block behind the capture thread.
        """
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

        While streaming the push runs on the capture thread (the SDK
        owner) and this returns [] immediately; errors surface via
        status_message. Idle, it runs synchronously and returns a list
        of (name, exception) pairs.
        """
        if not self._camera or not self._settings:
            raise RuntimeError("no camera connected")
        if self._streaming and self._worker:
            self._worker.run_async(lambda: self._apply_hw(silent))
            return []
        return self._apply_hw(silent)

    def _apply_hw(self, silent=False):
        """The actual hardware push. Call only from the SDK-owning thread."""
        cam = self._camera
        # Only touch the ROI when it actually changed: mid-stream ROI
        # pushes stall the ASI video pipeline for seconds (measured).
        roi_key = (self.roi["x"], self.roi["y"], self.roi["w"], self.roi["h"],
                   self.img_type)
        if roi_key != self._applied_roi:
            cam.apply_roi(
                self.roi["x"], self.roi["y"], self.roi["w"], self.roi["h"],
                self.img_type,
            )
            self._applied_roi = roi_key
        errors = self._settings.apply(cam)
        exp = self._settings.get("Exposure")
        if exp:
            self._applied_exposure_s = exp / 1e6

        # In high-speed idle, a full apply outside a grab must drop the
        # stream back to the idle exposure (this runs on the SDK thread).
        if (self.idle_mode and self._streaming
                and not self._recording_active()):
            spec = self._settings.control_set.get("Exposure")
            if spec is not None:
                try:
                    cam.set_ctrl(spec.control_type,
                                 int(self.idle_exposure_us))
                    self._applied_exposure_s = self.idle_exposure_us / 1e6
                except Exception as e:
                    log.debug("idle exposure re-push failed: %s", e)

        if not silent:
            if errors:
                err_str = ", ".join(f"{n}: {e}" for n, e in errors)
                self.status_message.emit(f"Settings errors: {err_str}")
            else:
                self.status_message.emit(
                    f"Applied -- ROI={self.roi['w']}x{self.roi['h']}"
                    f"+({self.roi['x']},{self.roi['y']})  {self.img_type}"
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
        on = bool(on)
        target = int(self._cooler_target)
        if self._streaming and self._worker:
            self._worker.run_async(
                lambda: cam.set_cooler(on=on, target_c=target)
            )
        else:
            cam.set_cooler(on=on, target_c=target)
        if on != self._cooler_on or target_c is not None:
            self.tec_monitor.reset()
        self._cooler_on = on
        self.status_message.emit(
            f"Cooler {'ON' if on else 'OFF'}, target={self._cooler_target:g} C"
        )
        self._recompute_state()

    def _read_thermal_hw(self):
        """Direct SDK thermal read. Only when the capture thread isn't
        running (it owns the SDK while streaming)."""
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            return False
        try:
            # Keep-alive FIRST: QHY TEC regulation needs it periodically,
            # and its CURPWM readback is only valid right after the call.
            cam.thermal_keepalive()
            t = cam.temperature()
            if t == t:   # not NaN (frozen-readout guard)
                self._last_temp = t
            p = cam.cooler_power()
            if p is not None:
                self._last_power = float(p)
            return t == t
        except Exception:
            return False

    def _poll_thermal(self):
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            return
        if not self._streaming:
            if not self._read_thermal_hw():
                return
        # While streaming, _last_temp/_last_power are fed by the capture
        # thread's stats/readonly signals; just consume the cache.
        if self._last_temp is None:
            return
        if self._cooler_on:
            self.tec_monitor.update(
                time.monotonic(), self._last_temp, self._cooler_target,
                self._last_power,
            )
        self.thermal_update.emit(self._last_temp, self._last_power)
        self._recompute_state()

    def thermal_status(self) -> dict:
        """Thermal snapshot, served purely from the telemetry cache.

        Never touches the SDK (WSP polls status at 1 Hz and must never
        block on hardware); the 2 s thermal timer and the capture thread
        keep the cache fresh.
        """
        d = {
            "cooler_on": self._cooler_on,
            "setpoint": self._cooler_target,
            "tec_locked": bool(self._cooler_on and self.tec_monitor.settled),
            "tec_stalled": bool(self.tec_monitor.stalled),
        }
        if self._last_temp is not None:
            d["temp"] = self._last_temp
            d["cooler_power"] = self._last_power
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

        readonly_ctrls = {}
        if self._control_set:
            readonly_ctrls = {
                s.name: s.control_type
                for s in self._control_set.readonly() if s.control_type >= 0
            }
        self._worker = self._vendor.worker_class(cam, exp_ms, readonly_ctrls)
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.stats_update.connect(self._on_worker_stats)
        self._worker.readonly_update.connect(self._on_worker_readonly)
        self._worker.recording_progress.connect(self.record_progress)
        self._worker.recording_done.connect(self._on_recording_done)
        self._worker.error.connect(self._on_capture_error)

        self._worker_thread.start()
        self._streaming = True
        if self.idle_mode:
            self._push_idle_exposure()
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
        self._record_pending = False
        if was_streaming:
            self.streaming_changed.emit(False)
            self.status_message.emit("Stream stopped")
        self._recompute_state()

    def _on_worker_stats(self, fps, total, dropped, temp):
        if temp == temp:  # not NaN
            self._last_temp = float(temp)
        self.stats_update.emit(fps, total, dropped, temp)

    def _on_worker_readonly(self, ro_vals: dict):
        if "CoolerPowerPerc" in ro_vals:
            self._last_power = float(ro_vals["CoolerPowerPerc"])
        self.readonly_update.emit(ro_vals)

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
        combine = self.record_params["combine"]
        if combine not in ("none", "mean", "median", "sum"):
            raise ValueError(
                f"combine must be none|mean|median|sum, got {combine!r}"
            )
        self.record_params["combine_only"] = bool(
            self.record_params["combine_only"]
        )
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

        self._next_record_obstype = obstype or None
        self._next_record_extras = list(extra_headers or [])

        # Arm on the capture thread (the SDK owner): first push any staged
        # settings, then start the recording — queue order guarantees the
        # record runs with what the controls display, and the GUI thread
        # never blocks on an in-flight exposure.
        cam = self._camera
        worker = self._worker
        self._record_pending = True

        overhead_hint = self._vendor.profile.cadence_overhead_s

        def _arm():
            if not self._record_pending:   # cancelled before arming
                return
            # Cadence estimate from the running stream, taken BEFORE the
            # apply below may change the exposure.
            med = worker.recent_median_delta()
            prev_exp = self._applied_exposure_s
            try:
                self._apply_hw(silent=True)
            except Exception:
                log.exception("apply before record failed")
            exp_s = (self._settings.get("Exposure") or 100_000) / 1e6
            if med is not None and prev_exp is not None:
                overhead = max(0.02, med - prev_exp)
            else:
                overhead = overhead_hint
            gate = RecordGate(
                t_arm=time.perf_counter(),
                exposure_s=exp_s,
                expected_cadence_s=exp_s + overhead,
            )
            w, h, _bin, _img = cam.get_roi()
            dtype = cam.frame_dtype()
            worker.start_recording(n, w, h, dtype, gate=gate)
            self._record_armed.emit(n)

        worker.run_async(_arm)
        self._recompute_state()   # pending -> EXPOSING immediately

    def _on_record_armed(self, n: int):
        self._record_pending = False
        self._record_start_utc = datetime.utcnow()
        self._recompute_state()
        self.record_started.emit(n)
        self.status_message.emit(f"Recording {n} frames...")

    def cancel_record(self):
        self._record_pending = False
        self._next_record_wsp = False
        if self._worker:
            self._worker.cancel_recording()
        self._push_idle_exposure()
        self._next_record_obstype = None
        self._next_record_extras = []
        self._recompute_state()
        self.record_cancelled.emit()
        self.status_message.emit("Recording cancelled")

    def _build_record_metadata(self, cube, elapsed) -> dict:
        cam = self._camera
        actual_fps = cube.shape[0] / elapsed if elapsed > 0 else 0

        meta = {
            "INSTRUME": self.instrument_name or (
                cam.info.name if cam else "unknown"
            ),
            "DETECTOR": (cam.info.name if cam else "unknown",
                         "camera model"),
            "NFRAMES": cube.shape[0],
            "STRMFPS": (round(actual_fps, 3), "measured stream rate [fps]"),
            "ELAPSED": (round(elapsed, 4), "total acquisition time [s]"),
            "DEPTH": self.img_type,
        }

        meta["XBINNING"] = (1, "binning factor, X")
        meta["YBINNING"] = (1, "binning factor, Y")
        meta["SWCREATE"] = (f"cmos-camera-gui {__version__}",
                            "software that created this file")

        # DATE-OBS: GPS time of first kept frame when locked, else host
        # UTC of the record arm; TIMESRC records which. TIMESYS is UTC
        # either way.
        meta["TIMESYS"] = ("UTC", "time system")
        frame_meta = getattr(self._worker, "last_frame_meta", None) or []
        gps0 = frame_meta[0] if frame_meta and frame_meta[0] else {}
        if gps0.get("GPS_LOCK") and gps0.get("DATE-BEG"):
            meta["DATE-OBS"] = (gps0["DATE-BEG"],
                                "UTC of first frame exposure start (GPS)")
            meta["TIMESRC"] = ("gps", "time source for DATE-OBS")
        elif self._record_start_utc is not None:
            meta["DATE-OBS"] = (
                self._record_start_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
                "UTC at record arm (host clock)",
            )
            meta["TIMESRC"] = ("host", "time source for DATE-OBS")
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
        if "EXPTIME" not in meta and self._requested_exposure_s is not None:
            meta["EXPTIME"] = (
                self._requested_exposure_s * 1000.0, "[ms] exposure time"
            )
        if cam:
            # From the cube itself -- a get_roi() SDK call here would block
            # behind the still-streaming capture thread.
            meta["ROI_W"] = int(cube.shape[2])
            meta["ROI_H"] = int(cube.shape[1])
            meta["ROI_X"] = self.roi["x"]
            meta["ROI_Y"] = self.roi["y"]
            # Cached by the capture thread -- a direct read here would
            # block on the in-flight exposure (stream is still running).
            if cam.info.is_cooler and self._last_temp is not None:
                meta["DETTEMP"] = (
                    self._last_temp, "[C] sensor temperature"
                )
            # Vendor-specific run cards (DATASEC, GPS flags, read mode...)
            try:
                for key, val, cmt in cam.run_header_cards(
                        int(cube.shape[2]), int(cube.shape[1])):
                    meta[key] = (val, cmt) if cmt else val
            except Exception:
                log.exception("run_header_cards failed")

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
        # Capture complete: drop straight back to the idle exposure so
        # the camera is re-armed while the FITS save runs.
        self._push_idle_exposure()
        meta = self._build_record_metadata(cube, elapsed)
        # Per-frame vendor metadata (e.g. QHY GPS seq/UTC), if the worker
        # collected any during this record.
        frame_meta = getattr(self._worker, "last_frame_meta", None)
        if frame_meta and not any(frame_meta):
            frame_meta = None

        directory = str(self.record_params["directory"]) or os.getcwd()
        basename = str(self.record_params["basename"]) or "capture"
        stack_mode = self.record_params["mode"] == "stack"
        combine = self.record_params["combine"]
        combine_only = bool(self.record_params["combine_only"])
        wsp_single = self._next_record_wsp
        self._next_record_wsp = False

        self._saving = True
        self._recompute_state()

        def _after_save(msg):
            self._saving = False
            if msg.startswith("FITS save error"):
                # WSP contract: a failed capture must not look complete --
                # is_capturing drops false AND the camera leaves READY.
                self._latch_error(msg)
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
            save_fits_cube(path, cube, meta, _after_save,
                           combine=combine, combine_only=combine_only,
                           timestamps=timestamps, frame_meta=frame_meta,
                           wsp_single=wsp_single)
        else:
            save_fits_individual(
                directory, basename, cube, timestamps, meta, _after_save,
                combine=combine, combine_only=combine_only,
                frame_meta=frame_meta,
            )

    # =================================================================
    #  WSP / SUMMER contract surface (SummerCameraGuiHandoff §2)
    # =================================================================

    def set_exposure_seconds(self, seconds):
        """WSP set_exposure: float SECONDS, stored for exact echo."""
        seconds = float(seconds)
        us = max(1, int(round(seconds * 1e6)))
        if not self.set_control("Exposure", us):
            raise RuntimeError("no camera connected (Exposure unavailable)")
        # Echo the requested float exactly (daemon does a float == check),
        # overriding the µs round-trip value set_control stored.
        self._requested_exposure_s = seconds

    def set_save_path(self, directory):
        d = os.path.expanduser(str(directory))
        os.makedirs(d, exist_ok=True)
        self.set_record_params(directory=d)
        return d

    def set_tec_temperature(self, target_c):
        self._cooler_target = float(target_c)
        self.tec_monitor.reset()
        if self._cooler_on:
            self.set_cooler(True, target_c)
        self._recompute_state()

    def wsp_capture(self, filename, nframes=1, object_name=None,
                    observer=None, headers=None):
        """WSP capture: non-blocking; writes exactly
        <save_path>/<filename>.fits (2D for nframes=1, cube for >1).

        is_capturing is true in status synchronously with this call
        returning (R1); completion is detected by polling.
        """
        if not self.connected:
            raise RuntimeError("no camera connected")
        if not self._streaming:
            self.start_stream()

        extras = []
        if object_name is not None:
            extras.append(["OBJECT", object_name, "target name"])
        extras.append(["OBSERVER", observer or "unknown", None])
        for h in (headers or []):
            if isinstance(h, dict):
                extras.extend([k, v, None] for k, v in h.items())
            else:
                extras.append(list(h))

        self._next_record_wsp = True
        self.start_record(
            extra_headers=extras,
            n_frames=int(nframes),
            basename=str(filename),
            mode="stack",
            combine="none",
            combine_only=False,
        )

    def wsp_status(self) -> dict:
        """The §2.2 get_status data snapshot. Cache-only, JSON-native."""
        cam = self._camera
        is_capturing = bool(self._recording_active() or self._saving)

        cur, tot = 0, 0
        if self._worker is not None:
            with self._worker._rec_lock:
                if self._worker._rec_cube is not None:
                    cur = int(self._worker._rec_idx)
                    tot = int(self._worker._rec_target)
        if tot == 0 and is_capturing:
            tot = int(self.record_params["n_frames"])

        exp_s = self._requested_exposure_s
        if exp_s is None and self._settings:
            v = self._settings.get("Exposure")
            exp_s = (v / 1e6) if v else 0.0
        exp_s = float(exp_s or 0.0)

        cadence = exp_s + (
            self._vendor.profile.cadence_overhead_s if self._vendor else 0.3
        )
        remaining = max(0.0, (tot - cur) * cadence) if is_capturing else 0.0

        data = {
            "camera_state": self._state.name,
            "ready": bool(cam is not None
                          and self._state is CameraState.READY),
            "is_capturing": is_capturing,
            "current_frame": cur,
            "total_frames": tot,
            "capture_time_remaining": float(remaining),
            "exposure": exp_s,
            "tec_temp": float(self._last_temp)
            if self._last_temp is not None else -888,
            "tec_setpoint": float(self._cooler_target),
            "tec_enabled": int(self._cooler_on),
            "tec_locked": int(self._cooler_on and self.tec_monitor.settled),
            "tec_voltage": -888,                    # QHY exposes PWM only
            "tec_power_pct": float(self._last_power),
            "save_path": str(self.record_params["directory"]),
            "case_temp": -888,
            "digpcb_temp": -888,
            "senspcb_temp": -888,
            # extras (forwarded into WSP telemetry by the daemon)
            "camname": self.instrument_name or (
                cam.info.name if cam else None
            ),
            "vendor": self._vendor.name if self._vendor else None,
            "connected": cam is not None,
            "streaming": bool(self._streaming),
            "nframes": int(self.record_params["n_frames"]),
            "idle_mode": bool(self.idle_mode),
        }
        if self._settings:
            for k, v in self._settings.snapshot().items():
                if k != "Exposure":
                    data[k.lower()] = v
        gps = getattr(self._worker, "last_gps", None) if self._worker else None
        if gps:
            data["gps_locked"] = int(bool(gps.get("GPS_LOCK")))
            data["gps_seq"] = gps.get("GPS_SEQ")
        return {"status": "success", "data": data}

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
            "vendor": self._vendor.name if self._vendor else None,
            "camera_state": self._state.name,
        }
        gps = getattr(self._worker, "last_gps", None) if self._worker else None
        if gps:
            result["gps"] = {
                "locked": gps.get("GPS_LOCK"),
                "last_seq": gps.get("GPS_SEQ"),
                "last_utc": gps.get("DATE-BEG"),
            }
        if self._error_msg:
            result["error_message"] = self._error_msg
        if cam:
            result.update(self.thermal_status())
            result["roi"] = dict(self.roi)
            result["img_type"] = self.img_type
            result["idle_mode"] = self.idle_mode
            result["idle_exposure_us"] = self.idle_exposure_us
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
                elif key == "idle_mode":
                    self.set_idle_mode(bool(value))
                elif key == "idle_exposure_us":
                    self.set_idle_mode(self.idle_mode,
                                       idle_exposure_us=value)
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
            if "combine" in cmd:
                params["combine"] = str(cmd["combine"]).lower()
            if "combine_only" in cmd:
                params["combine_only"] = bool(cmd["combine_only"])

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
                "combine": self.record_params["combine"],
                "combine_only": self.record_params["combine_only"],
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

        # ---- WSP / SUMMER contract verbs (SummerCameraGuiHandoff §2) ----
        # Reply convention: {"status": "success"|"error", "message": ...}

        elif action == "get_status":
            try:
                return self.wsp_status()
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action == "set_exposure":
            try:
                self.set_exposure_seconds(cmd["exposure"])
                return {"status": "success",
                        "message": f"exposure set to {cmd['exposure']}s"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action == "set_save_path":
            try:
                d = self.set_save_path(cmd["path"])
                return {"status": "success", "message": f"save path {d}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action == "capture":
            try:
                self.wsp_capture(
                    filename=cmd["filename"],
                    nframes=int(cmd.get("nframes", 1)),
                    object_name=cmd.get("object"),
                    observer=cmd.get("observer"),
                    headers=cmd.get("headers"),
                )
                return {"status": "success", "message": "capture started"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action == "set_tec_enabled":
            try:
                self.set_cooler(on=bool(cmd.get("enabled", False)),
                                target_c=self._cooler_target)
                return {"status": "success",
                        "message": f"TEC enabled={bool(cmd.get('enabled'))}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif action == "set_tec_temperature":
            try:
                self.set_tec_temperature(float(cmd["temperature"]))
                return {"status": "success",
                        "message": f"TEC setpoint {cmd['temperature']}C"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

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
