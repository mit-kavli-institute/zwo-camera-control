"""
QHYCCD vendor: SDK loading/enumeration, camera adapter, profile.

Lifecycle, timings and quirks per HANDOFF.md (validated on a QHY42PRO,
GSENSE400BSI, SDK V20260625_16):
- SetQHYCCDReadMode BEFORE SetQHYCCDStreamMode, both before InitQHYCCD;
- configure into LIVE mode once and never leave it (mode change = re-init);
- exposure/gain changes are legal mid-stream (basis of the grab method);
- TEC needs periodic ControlQHYCCDTemp keep-alive; temperature readout is
  frozen at a bogus value while PWM == 0;
- GPS mode overwrites the first 64 bytes of row 0 with a binary header.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from ..base import VendorProfile
from ...core.thermal import TecConfig
from .sdk_wrapper import (
    QHYCCDSDK, QHYCCDError, ControlID, LIVE_MODE, _platform,
)
from .worker import QhyCaptureWorker

log = logging.getLogger("cmoscam.qhy")


class QhyProfile(VendorProfile):
    read_mode = 1                     # QHY42PRO validated on read mode 1
    default_overrides = {
        "Gain": 10,
        "Offset": 140,
        "UsbTraffic": 30,
        "Exposure": 100_000,          # 100 ms
    }
    # Fallback ranges if GetQHYCCDParamMinMaxStep fails
    fallback_ranges = {
        "Gain": (0, 200),
        "Offset": (0, 255),
        "Exposure": (1, 3_600_000_000),
        "UsbTraffic": (0, 60),
    }
    controls = {
        "Gain": ControlID.CONTROL_GAIN,
        "Offset": ControlID.CONTROL_OFFSET,
        "Exposure": ControlID.CONTROL_EXPOSURE,
        "UsbTraffic": ControlID.CONTROL_USBTRAFFIC,
    }
    control_descriptions = {
        "Gain": "sensor gain",
        "Offset": "ADC offset (pedestal)",
        "Exposure": "exposure time [us]",
        "UsbTraffic": "USB traffic setting (lower = faster). Measured "
                      "2026-08-14: no effect on this readout-limited "
                      "camera (42 ms/frame at every value, 0 drops); "
                      "raise it only if a weak USB path drops frames.",
    }
    advanced_controls = {"UsbTraffic"}
    # QHY TEC is coarse (bad PID): loose tolerance, longer dwell
    tec_config = TecConfig(tolerance_c=1.5, dwell_s=45.0)
    # Measured overhead beyond one exposure in the live-stream cadence
    cadence_overhead_s = 0.075
    tec_setpoint_range = (-45.0, 20.0)


class _QhyInfo:
    """Duck-typed CameraInfo equivalent."""

    def __init__(self, name, w, h, is_cooler, pixel_um):
        self.name = name
        self.max_width = w
        self.max_height = h
        self.is_color = False
        self.is_cooler = is_cooler
        self.is_usb3 = True
        self.bit_depth = 16
        self.pixel_size = pixel_um
        self.e_per_adu = 0.0
        self.supported_bins = [1]


class QhyCamera:
    """Opened + initialised QHY camera in LIVE mode (adapter surface)."""

    vendor = "qhy"

    def __init__(self, sdk: QHYCCDSDK, camera_id: str, profile: QhyProfile):
        self.sdk = sdk
        self.profile = profile
        self.camera_id = camera_id
        self._live = False
        self._lock = threading.Lock()   # serialize SDK calls defensively

        self.handle = sdk.open(camera_id)
        if not self.handle:
            raise RuntimeError(f"OpenQHYCCD failed for {camera_id!r}")

        try:
            self._chk(sdk.set_read_mode(profile.read_mode), "SetReadMode")
            self._chk(sdk.set_stream_mode(LIVE_MODE), "SetStreamMode")
            self._chk(sdk.init(), "InitQHYCCD")          # slow (~2 s)
            self._chk(sdk.set_bits_mode(16), "SetBitsMode")

            area = sdk.get_effective_area()
            if not area:
                raise RuntimeError("GetQHYCCDEffectiveArea failed")
            self._eff = area                              # (x, y, w, h)
            self._roi = area
            self._pending_roi = None
            self._chk(sdk.set_resolution(*area), "SetResolution")

            chip = sdk.get_chip_info()
            pixel_um = chip[4] if chip else 0.0

            self.gps_enabled = False
            if sdk.has_gps():
                if sdk.set_param(ControlID.CAM_GPS, 1) == QHYCCDError.QHYCCD_SUCCESS:
                    self.gps_enabled = True

            # Buffer frames in camera RAM
            sdk.set_param(ControlID.CONTROL_DDR, 1)

            model = sdk.get_camera_model(camera_id) or camera_id
            self.info = _QhyInfo(model, area[2], area[3],
                                 sdk.has_cooler(), pixel_um)
        except Exception:
            sdk.close(self.handle)
            raise

        # Cooler state (also read by the worker for keep-alive)
        self.cooler_on = False
        self.tec_setpoint = 0.0

        # CURPWM readback quirk (measured 2026-08-14, SDK V20260625_16):
        # the register reads 0 except immediately after a
        # ControlQHYCCDTemp call. We read it right after each keep-alive
        # and cache it.
        self._pwm_cache = None        # (raw 0-255, monotonic time)
        self._temp_history = []       # last few CURTEMP reads (frozen detect)

        self._read_mode = profile.read_mode
        self._sdk_version = sdk.get_sdk_version_string()

    @staticmethod
    def _chk(ret, what):
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            raise RuntimeError(f"QHYCCD {what} failed (0x{ret & 0xFFFFFFFF:08X})")

    # -- capabilities / caps ------------------------------------------

    def capabilities(self) -> dict:
        return {
            "has_cooler": bool(self.info.is_cooler),
            "has_gps": bool(self.gps_enabled),
            "img_types": ["RAW16"],
            "roi_mid_stream": False,
        }

    def get_caps_dict(self) -> dict:
        caps = {}
        for name, cid in self.profile.controls.items():
            rng = self.sdk.get_param_min_max_step(cid, self.handle)
            if rng:
                lo, hi = rng[0], rng[1]
            else:
                lo, hi = self.profile.fallback_ranges[name]
            caps[name] = {
                "MinValue": int(lo),
                "MaxValue": int(hi),
                "DefaultValue": int(self.profile.default_overrides.get(name, lo)),
                "IsAutoSupported": False,
                "IsWritable": True,
                "ControlType": int(cid),
                "Description": self.profile.control_descriptions.get(name, ""),
            }
        return caps

    # -- controls -----------------------------------------------------

    def set_ctrl(self, control_type: int, value, auto: bool = False):
        with self._lock:
            ret = self.sdk.set_param(control_type, float(value), self.handle)
        if ret != QHYCCDError.QHYCCD_SUCCESS:
            raise RuntimeError(
                f"SetQHYCCDParam({control_type}) -> 0x{ret & 0xFFFFFFFF:08X}"
            )

    def get_ctrl_value(self, control_type: int) -> int:
        with self._lock:
            v = self.sdk.get_param(control_type, self.handle)
        return int(v) if v is not None else 0

    # -- geometry -----------------------------------------------------

    def apply_roi(self, x, y, w, h, img_type: str):
        if img_type != "RAW16":
            raise ValueError("QHY backend is 16-bit only")
        roi = (int(x), int(y), int(w), int(h))
        if self._live:
            # Mid-stream resolution changes are not validated on this
            # camera; applied at the next stream start instead.
            self._pending_roi = roi
        else:
            with self._lock:
                self._chk(self.sdk.set_resolution(*roi), "SetResolution")
            self._roi = roi
            self._pending_roi = None

    def get_roi(self):
        x, y, w, h = self._roi
        return (w, h, 1, 2)   # img code 2 == 16-bit, matching ZWO RAW16

    def frame_dtype(self):
        return np.uint16

    # -- streaming (called from the worker thread) --------------------

    def start_video(self):
        with self._lock:
            if self._pending_roi:
                self._chk(self.sdk.set_resolution(*self._pending_roi),
                          "SetResolution")
                self._roi = self._pending_roi
                self._pending_roi = None
            self._chk(self.sdk.begin_live(self.handle), "BeginQHYCCDLive")
        self._live = True

    def stop_video(self):
        if self._live:
            with self._lock:
                self.sdk.stop_live(self.handle)
            self._live = False

    def get_live_frame(self):
        with self._lock:
            return self.sdk.get_live_frame(self.handle)

    def get_dropped(self) -> int:
        return 0   # dropped frames are counted by the worker via GPS seq

    # -- thermal ------------------------------------------------------

    def _pwm_after_keepalive_locked(self):
        """Read CURPWM inside the post-ControlQHYCCDTemp validity window
        and cache it. Caller must hold self._lock."""
        pwm = self.sdk.get_param(ControlID.CONTROL_CURPWM, self.handle)
        if pwm:
            self._pwm_cache = (float(pwm), time.monotonic())
        return pwm

    def temperature(self) -> float:
        """Sensor temp [C]; NaN only while the readout is provably frozen.

        With regulation active the readout is live. When the TEC is idle
        the readout can freeze at a bogus value (HANDOFF §7); the frozen
        state is detected by its signature -- several consecutive reads
        EXACTLY identical -- rather than by CURPWM, whose readback is
        itself unreliable (reads 0 except right after ControlQHYCCDTemp).
        """
        with self._lock:
            t = self.sdk.get_param(ControlID.CONTROL_CURTEMP, self.handle)
        if t is None:
            return float("nan")
        t = float(t)
        if self.cooler_on:
            self._temp_history.clear()
            return t
        self._temp_history.append(t)
        del self._temp_history[:-4]
        if (len(self._temp_history) >= 4
                and len(set(self._temp_history)) == 1):
            return float("nan")   # frozen readout
        return t

    def cooler_power(self):
        with self._lock:
            pwm = self.sdk.get_param(ControlID.CONTROL_CURPWM, self.handle)
        if pwm:
            self._pwm_cache = (float(pwm), time.monotonic())
            return float(pwm) / 255.0 * 100.0
        # Raw readback is 0 almost always; fall back to the value cached
        # right after the last keep-alive.
        if self.cooler_on and self._pwm_cache:
            val, when = self._pwm_cache
            if time.monotonic() - when < 20.0:
                return val / 255.0 * 100.0
        return 0.0

    def set_cooler(self, on: bool, target_c: float = 0.0):
        lo, hi = self.profile.tec_setpoint_range
        target_c = max(lo, min(hi, float(target_c)))
        self.cooler_on = bool(on)
        self.tec_setpoint = target_c
        with self._lock:
            if on:
                self.sdk.control_temp(target_c, self.handle)
                self._pwm_after_keepalive_locked()
            else:
                self.sdk.set_param(ControlID.CONTROL_MANULPWM, 0, self.handle)
                self._pwm_cache = None

    def thermal_keepalive(self):
        """Re-issue the TEC setpoint (cheap; keeps regulation engaged)
        and grab CURPWM inside its post-call validity window."""
        if self.cooler_on:
            with self._lock:
                self.sdk.control_temp(self.tec_setpoint, self.handle)
                self._pwm_after_keepalive_locked()

    # -- misc ---------------------------------------------------------

    def has_ctrl(self, control_type: int) -> bool:
        return self.sdk.is_control_available(control_type, self.handle)

    def run_header_cards(self, w, h) -> list:
        cards = [
            ("CAMVENDR", "qhy", "camera vendor"),
            ("READMODE", self._read_mode, "QHYCCD sensor read mode"),
            ("SDKVER", self._sdk_version, "QHYCCD SDK version"),
        ]
        if self.gps_enabled:
            cards += [
                ("GPSROW0", True,
                 "row 1 (FITS) holds binary GPS header, not image"),
                ("DATASEC", f"[1:{w},2:{h}]",
                 "image section excluding GPS header row"),
            ]
        else:
            cards.append(
                ("DATASEC", f"[1:{w},1:{h}]", "image section (all pixels)")
            )
        return cards

    def close(self):
        self.stop_video()
        if self.handle:
            self.sdk.close(self.handle)
            self.handle = None
        # TEC drops out when the camera is closed (hardware behavior).
        self.cooler_on = False


class QhyVendor:
    name = "qhy"
    profile = QhyProfile()
    worker_class = QhyCaptureWorker

    def __init__(self):
        self._sdk = None
        self._ids = []

    @property
    def loaded(self) -> bool:
        return self._sdk is not None

    def load(self, sdk_path=None) -> str | None:
        """Load qhyccd.dll, init the SDK resource, scan once.

        No hotplug: cameras must be connected before this runs; a re-scan
        requires releasing and re-initialising SDK resources (restart).
        """
        try:
            import os
            if sdk_path:
                os.environ["QHYCCD_SDK_DLL"] = sdk_path
            if not _platform.load():
                return None
            sdk = QHYCCDSDK()
            if sdk.init_resource() != QHYCCDError.QHYCCD_SUCCESS:
                return None
            n = sdk.scan()
            self._ids = [sdk.get_camera_id(i) for i in range(n)]
            self._ids = [i for i in self._ids if i]
            self._sdk = sdk
            return getattr(_platform, "lib_path", "qhyccd.dll")
        except Exception as e:
            log.warning("QHY SDK load failed: %s", e)
            return None

    def list_cameras(self) -> list:
        if not self._sdk:
            return []
        cams = []
        for i, cid in enumerate(self._ids):
            model = self._sdk.get_camera_model(cid) or cid
            cams.append({"index": i, "name": model})
        return cams

    def open(self, index: int) -> QhyCamera:
        if not self._sdk:
            raise RuntimeError("QHY SDK not loaded")
        if not 0 <= index < len(self._ids):
            raise RuntimeError(
                f"camera index {index} out of range "
                f"({len(self._ids)} QHY camera(s) found)"
            )
        return QhyCamera(self._sdk, self._ids[index], self.profile)
