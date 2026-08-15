"""
ZWO ASI vendor: SDK loading/enumeration, camera adapter, profile.
"""

from __future__ import annotations

import logging
import os

import numpy as np

from ..base import VendorProfile
from ...core.thermal import TecConfig
from .sdk import ASICamera, ASIDriver, Ctrl, ImgType
from .capture import CaptureWorker

log = logging.getLogger("cmoscam.zwo")

_SDK_CANDIDATES = [
    "ASICamera2.dll",
    r"C:\Program Files\ASIStudio\ASICamera2.dll",
    r"C:\Program Files (x86)\ASIStudio\ASICamera2.dll",
    "/usr/lib/libASICamera2.so",
    "/usr/local/lib/libASICamera2.so",
]


class ZwoProfile(VendorProfile):
    default_overrides = {
        "Gain": 200,
        "BandWidth": 100,
    }
    tec_config = TecConfig(tolerance_c=0.7, dwell_s=30.0)
    cadence_overhead_s = 0.4   # full-frame readout+transfer, measured ASI294
    # USB/readout plumbing: rarely touched, hidden in the Advanced dialog
    advanced_controls = {"BandWidth", "HighSpeedMode"}

    def post_connect(self, settings) -> None:
        # Max out USB bandwidth for streaming
        settings.set_if_present("BandWidth", 9999, clamp=True)


class ZwoCamera(ASICamera):
    """ASICamera + the vendor-neutral adapter surface."""

    vendor = "zwo"

    def capabilities(self) -> dict:
        return {
            "has_cooler": bool(self.info.is_cooler),
            "has_gps": False,
            "img_types": ["RAW8", "RAW16"],
            "roi_mid_stream": True,
        }

    def apply_roi(self, x, y, w, h, img_type: str):
        img = ImgType.RAW16 if img_type == "RAW16" else ImgType.RAW8
        self.set_roi(w, h, 1, img, x, y)

    def frame_dtype(self):
        _w, _h, _b, t = self.get_roi()
        return np.uint16 if t == int(ImgType.RAW16) else np.uint8

    def cooler_power(self):
        if self.has_ctrl(Ctrl.COOLER_POWER_PERC):
            return float(self.get_ctrl_value(Ctrl.COOLER_POWER_PERC))
        return None

    def thermal_keepalive(self):
        pass  # ASI TEC regulates on its own once set

    @property
    def serial(self):
        try:
            return self.driver.get_serial_number(self.cam_id)
        except Exception:
            return None

    def run_header_cards(self, w, h) -> list:
        cards = [
            ("CAMVENDR", "zwo", "camera vendor"),
            ("DATASEC", f"[1:{w},1:{h}]", "image section (all pixels)"),
        ]
        sn = self.serial
        if sn:
            cards.append(("CAMID", sn, "camera serial number"))
        return cards


class ZwoVendor:
    name = "zwo"
    profile = ZwoProfile()
    worker_class = CaptureWorker

    def __init__(self):
        self._driver = None

    @property
    def loaded(self) -> bool:
        return self._driver is not None

    def load(self, sdk_path=None) -> str | None:
        """Try to load the SDK; returns the loaded path or None."""
        candidates = ([sdk_path] if sdk_path else []) + _SDK_CANDIDATES
        for c in candidates:
            if os.path.isfile(c):
                try:
                    self._driver = ASIDriver(c)
                    return c
                except Exception as e:
                    log.warning("ASI SDK load failed (%s): %s", c, e)
        return None

    def list_cameras(self) -> list:
        if not self._driver:
            return []
        from .sdk import CameraInfo
        cams = []
        for i in range(self._driver.get_num_cameras()):
            info = CameraInfo.from_struct(self._driver.get_camera_property(i))
            cams.append({"index": i, "name": info.name})
        return cams

    def open(self, index: int) -> ZwoCamera:
        if not self._driver:
            raise RuntimeError("ZWO SDK not loaded")
        n = self._driver.get_num_cameras()
        if not 0 <= index < n:
            raise RuntimeError(
                f"camera index {index} out of range ({n} camera(s) found)"
            )
        return ZwoCamera(self._driver, index)
