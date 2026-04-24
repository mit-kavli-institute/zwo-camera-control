"""
QHY42 camera wrapper used by the GUI.

This module intentionally mirrors a subset of the ASICamera interface so the
existing GUI code can reuse the same control/settings/recording flow.
"""

from __future__ import annotations

from ctypes import byref, c_double, c_uint8, c_uint16, c_uint32, c_void_p, create_string_buffer
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional

import numpy as np

try:
    from qcam.qCam import Qcam
except ImportError:
    Qcam = None


def qcam_available() -> bool:
    return Qcam is not None


def enumerate_qhy_cameras(dll_path: Optional[str] = None) -> List[dict]:
    """Return [{"index": i, "name": str, "id": bytes}, ...] for attached QHY cameras.

    Calls InitQHYCCDResource + ScanQHYCCD + GetQHYCCDId without opening a
    handle. Safe to call repeatedly: the QHY SDK's InitQHYCCDResource is
    idempotent in practice.

    Returns an empty list if qcam is not installed or no cameras are found.
    Raises QHYError only on unexpected SDK failure.
    """
    if Qcam is None:
        return []
    qcam = Qcam(dll_path) if dll_path else Qcam()
    qcam.so.InitQHYCCDResource()
    n = int(qcam.so.ScanQHYCCD())
    cams: List[dict] = []
    for i in range(max(0, n)):
        buf = create_string_buffer(qcam.STR_BUFFER_SIZE)
        qcam.so.GetQHYCCDId(i, buf)
        raw = buf.value
        cams.append({
            "index": i,
            "name": raw.decode(errors="replace"),
            "id": raw,
        })
    return cams


class QHYError(RuntimeError):
    """Raised when QHY SDK calls fail."""


QHYCCD_SUCCESS = 0


class QHYImgType(IntEnum):
    RAW8 = 0
    RAW16 = 2


class QHYCtrl(IntEnum):
    GAIN = 1000
    EXPOSURE = 1001
    OFFSET = 1002
    TEMPERATURE = 1003
    COOLER_ON = 1004
    TARGET_TEMP = 1005


@dataclass
class QHYCameraInfo:
    name: str
    camera_id: bytes
    max_width: int
    max_height: int
    bit_depth: int
    is_color: bool = False
    is_cooler: bool = True
    is_usb3: bool = True


def _caps(min_value: int, max_value: int, default: int, ctrl_type: int, writable: bool = True, desc: str = "") -> dict:
    return {
        "MinValue": int(min_value),
        "MaxValue": int(max_value),
        "DefaultValue": int(default),
        "IsAutoSupported": False,
        "IsWritable": bool(writable),
        "ControlType": int(ctrl_type),
        "Description": desc,
    }


class QHY42Camera:
    """Minimal QHY42 wrapper compatible with GUI expectations."""

    def __init__(self, dll_path: str | None = None, camera_index: int = 0):
        if Qcam is None:
            raise QHYError("qcam package not installed. Install the QHY Python wrapper first.")

        self.dll_path = dll_path
        self.camera_index = camera_index
        self._qcam = Qcam(dll_path) if dll_path else Qcam()

        self.handle = None
        self.cam_id = None
        self.info = None

        self._image_width = c_uint32(0)
        self._image_height = c_uint32(0)
        self._bits_per_pixel = c_uint32(16)
        self._channels = c_uint32(1)
        self._img_type = int(QHYImgType.RAW16)
        self._buffer = None
        self._video = False

        self._ctrl_values: Dict[int, int] = {
            int(QHYCtrl.GAIN): 30,
            int(QHYCtrl.EXPOSURE): 200_000,
            int(QHYCtrl.OFFSET): 40,
            int(QHYCtrl.COOLER_ON): 0,
            int(QHYCtrl.TARGET_TEMP): 0,
        }

    # -- discovery/lifecycle -------------------------------------------------

    def open(self):
        self._qcam.so.InitQHYCCDResource()
        n = int(self._qcam.so.ScanQHYCCD())
        if n <= 0:
            raise QHYError("No QHY cameras found")
        if self.camera_index >= n:
            raise QHYError(f"Camera index {self.camera_index} out of range (found {n})")

        cam_id_buf = create_string_buffer(self._qcam.STR_BUFFER_SIZE)
        self._qcam.so.GetQHYCCDId(self.camera_index, cam_id_buf)
        self.cam_id = cam_id_buf.value

        raw_handle = self._qcam.so.OpenQHYCCD(self.cam_id)
        self.handle = c_void_p(raw_handle)
        if not self.handle:
            raise QHYError("Failed to open QHY camera")

        # Read mode/stream mode must be set before init on many QHY models.
        self._qcam.so.SetQHYCCDReadMode(self.handle, 1)   # std
        self._qcam.so.SetQHYCCDStreamMode(self.handle, 1) # live stream

        ret = self._qcam.so.InitQHYCCD(self.handle)
        if ret != QHYCCD_SUCCESS:
            raise QHYError(f"InitQHYCCD failed with code {ret}")

        self._qcam.so.SetQHYCCDBitsMode(self.handle, c_uint32(16))

        chip_w = c_double()
        chip_h = c_double()
        pixel_w = c_double()
        pixel_h = c_double()
        self._qcam.so.GetQHYCCDChipInfo(
            self.handle,
            byref(chip_w),
            byref(chip_h),
            byref(self._image_width),
            byref(self._image_height),
            byref(pixel_w),
            byref(pixel_h),
            byref(self._bits_per_pixel),
        )

        self.info = QHYCameraInfo(
            name=self.cam_id.decode(errors="replace"),
            camera_id=self.cam_id,
            max_width=int(self._image_width.value),
            max_height=int(self._image_height.value),
            bit_depth=int(self._bits_per_pixel.value),
        )

        self.set_roi(self.info.max_width, self.info.max_height, 1, int(QHYImgType.RAW16), 0, 0)

        self.set_ctrl(int(QHYCtrl.EXPOSURE), self._ctrl_values[int(QHYCtrl.EXPOSURE)])
        self.set_ctrl(int(QHYCtrl.GAIN), self._ctrl_values[int(QHYCtrl.GAIN)])
        self.set_ctrl(int(QHYCtrl.OFFSET), self._ctrl_values[int(QHYCtrl.OFFSET)])

    def close(self):
        try:
            self.stop_video()
        except Exception:
            pass
        if self.handle:
            self._qcam.so.CloseQHYCCD(self.handle)
            self.handle = None

    # -- controls ------------------------------------------------------------

    def get_caps_dict(self) -> dict:
        return {
            "Gain": _caps(0, 100, 30, int(QHYCtrl.GAIN)),
            "Exposure": _caps(1, 3_600_000_000, 200_000, int(QHYCtrl.EXPOSURE), desc="Exposure in microseconds"),
            "Offset": _caps(0, 1000, 40, int(QHYCtrl.OFFSET)),
            "Temperature": _caps(-500, 500, 0, int(QHYCtrl.TEMPERATURE), writable=False),
            "CoolerOn": _caps(0, 1, 0, int(QHYCtrl.COOLER_ON)),
            "TargetTemp": _caps(-40, 20, 0, int(QHYCtrl.TARGET_TEMP)),
        }

    def has_ctrl(self, ctrl: int) -> bool:
        return ctrl in self._ctrl_values or ctrl == int(QHYCtrl.TEMPERATURE)

    def set_ctrl(self, ctrl: int, value: int, auto: bool = False):
        del auto
        if not self.handle:
            raise QHYError("Camera not opened")

        if ctrl == int(QHYCtrl.EXPOSURE):
            self._qcam.so.SetQHYCCDParam(self.handle, self._qcam.CONTROL_EXPOSURE, c_double(int(value)))
        elif ctrl == int(QHYCtrl.GAIN):
            self._qcam.so.SetQHYCCDParam(self.handle, self._qcam.CONTROL_GAIN, c_double(int(value)))
        elif ctrl == int(QHYCtrl.OFFSET):
            self._qcam.so.SetQHYCCDParam(self.handle, self._qcam.CONTROL_OFFSET, c_double(int(value)))
        elif ctrl == int(QHYCtrl.COOLER_ON):
            self._qcam.so.SetQHYCCDParam(self.handle, self._qcam.CONTROL_COOLER, c_double(1.0 if int(value) else 0.0))
        elif ctrl == int(QHYCtrl.TARGET_TEMP):
            self._qcam.so.SetQHYCCDParam(self.handle, self._qcam.CONTROL_CURTEMP, c_double(float(value)))
        else:
            raise QHYError(f"Unsupported control: {ctrl}")

        self._ctrl_values[int(ctrl)] = int(value)

    def get_ctrl_value(self, ctrl: int) -> int:
        if ctrl == int(QHYCtrl.TEMPERATURE):
            return int(round(self.temperature() * 10.0))
        return int(self._ctrl_values.get(int(ctrl), 0))

    def set_cooler(self, on: bool, target_c: int = -10):
        self.set_ctrl(int(QHYCtrl.COOLER_ON), 1 if on else 0)
        if on:
            self.set_ctrl(int(QHYCtrl.TARGET_TEMP), int(target_c))

    def temperature(self) -> float:
        if not self.handle:
            return float("nan")
        return float(self._qcam.so.GetQHYCCDParam(self.handle, self._qcam.CONTROL_CURTEMP))

    # -- ROI / format --------------------------------------------------------

    def set_roi(self, width: int, height: int, binning: int = 1, img_type: int = int(QHYImgType.RAW16), start_x: int = 0, start_y: int = 0):
        del binning
        if not self.handle:
            raise QHYError("Camera not opened")

        max_w = int(self.info.max_width)
        max_h = int(self.info.max_height)
        width = max(8, min(max_w, int(width)))
        height = max(2, min(max_h, int(height)))
        start_x = max(0, min(max_w - 8, int(start_x)))
        start_y = max(0, min(max_h - 2, int(start_y)))

        self._qcam.so.SetQHYCCDBitsMode(self.handle, c_uint32(16 if int(img_type) == int(QHYImgType.RAW16) else 8))
        self._qcam.so.SetQHYCCDResolution(
            self.handle,
            c_uint32(start_x),
            c_uint32(start_y),
            c_uint32(width),
            c_uint32(height),
        )

        self._image_width = c_uint32(width)
        self._image_height = c_uint32(height)
        self._img_type = int(img_type)

        mem_len = int(self._qcam.so.GetQHYCCDMemLength(self.handle))
        if self._img_type == int(QHYImgType.RAW16):
            self._buffer = (c_uint16 * (mem_len // 2))()
        else:
            self._buffer = (c_uint8 * mem_len)()

    def get_roi(self):
        return (
            int(self._image_width.value),
            int(self._image_height.value),
            1,
            int(self._img_type),
        )

    # -- live video ----------------------------------------------------------

    def start_video(self):
        if not self.handle:
            raise QHYError("Camera not opened")
        if not self._video:
            ret = self._qcam.so.BeginQHYCCDLive(self.handle)
            if ret != QHYCCD_SUCCESS:
                raise QHYError(f"BeginQHYCCDLive failed with code {ret}")
            self._video = True

    def stop_video(self):
        if self.handle and self._video:
            self._qcam.so.StopQHYCCDLive(self.handle)
            self._video = False

    def get_live_frame(self):
        if not self.handle or self._buffer is None:
            return False, None

        ret = self._qcam.so.GetQHYCCDLiveFrame(
            self.handle,
            byref(self._image_width),
            byref(self._image_height),
            byref(self._bits_per_pixel),
            byref(self._channels),
            byref(self._buffer),
        )
        if ret != QHYCCD_SUCCESS:
            return False, None

        arr = np.ctypeslib.as_array(self._buffer)
        size = int(self._image_width.value) * int(self._image_height.value)
        frame = arr[:size].reshape((int(self._image_height.value), int(self._image_width.value))).copy()
        return True, frame

    def get_dropped(self) -> int:
        return 0
