"""
ASI SDK ctypes wrapper.

IntEnum constants, ctypes structures, error handling, ASIDriver (low-level),
and ASICamera (high-level). No third-party dependencies.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict


# -- Error codes ---------------------------------------------------------------

_ERROR_NAMES = {
    0: "SUCCESS", 1: "INVALID_INDEX", 2: "INVALID_ID",
    3: "INVALID_CONTROL_TYPE", 4: "CAMERA_CLOSED",
    5: "CAMERA_REMOVED", 6: "INVALID_PATH",
    7: "INVALID_FILEFORMAT", 8: "INVALID_SIZE",
    9: "INVALID_IMGTYPE", 10: "OUTOF_BOUNDARY",
    11: "TIMEOUT", 12: "INVALID_SEQUENCE",
    13: "BUFFER_TOO_SMALL", 14: "VIDEO_MODE_ACTIVE",
    15: "EXPOSURE_IN_PROGRESS", 16: "GENERAL_ERROR",
    17: "INVALID_MODE",
}

ASI_SUCCESS = 0
ASI_ERROR_TIMEOUT = 11


class ASIError(RuntimeError):
    """Raised when an ASI SDK function returns a non-zero error code."""
    def __init__(self, code: int, func: str = ""):
        name = _ERROR_NAMES.get(code, f"UNKNOWN({code})")
        super().__init__(f"ASI {func} -> {name} (code {code})")
        self.code = code


# -- Enums ---------------------------------------------------------------------

class ImgType(IntEnum):
    RAW8 = 0
    RGB24 = 1
    RAW16 = 2
    Y8 = 3


class Ctrl(IntEnum):
    GAIN = 0
    EXPOSURE = 1
    GAMMA = 2
    WB_R = 3
    WB_B = 4
    OFFSET = 5
    BANDWIDTHOVERLOAD = 6
    OVERCLOCK = 7
    TEMPERATURE = 8
    FLIP = 9
    AUTO_MAX_GAIN = 10
    AUTO_MAX_EXP = 11
    AUTO_TARGET_BRIGHTNESS = 12
    HARDWARE_BIN = 13
    HIGH_SPEED_MODE = 14
    COOLER_POWER_PERC = 15
    TARGET_TEMP = 16
    COOLER_ON = 17
    MONO_BIN = 18
    FAN_ON = 19
    PATTERN_ADJUST = 20
    ANTI_DEW_HEATER = 21


# -- ctypes structures ---------------------------------------------------------

class _CameraInfo(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_char * 64),
        ("CameraID", ctypes.c_int),
        ("MaxHeight", ctypes.c_long),
        ("MaxWidth", ctypes.c_long),
        ("IsColorCam", ctypes.c_int),
        ("BayerPattern", ctypes.c_int),
        ("SupportedBins", ctypes.c_int * 16),
        ("SupportedVideoFormat", ctypes.c_int * 8),
        ("PixelSize", ctypes.c_double),
        ("MechanicalShutter", ctypes.c_int),
        ("ST4Port", ctypes.c_int),
        ("IsCoolerCam", ctypes.c_int),
        ("IsUSB3Host", ctypes.c_int),
        ("IsUSB3Camera", ctypes.c_int),
        ("ElecPerADU", ctypes.c_float),
        ("BitDepth", ctypes.c_int),
        ("IsTriggerCam", ctypes.c_int),
        ("Unused", ctypes.c_char * 16),
    ]


class _ControlCaps(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_char * 64),
        ("Description", ctypes.c_char * 128),
        ("MaxValue", ctypes.c_long),
        ("MinValue", ctypes.c_long),
        ("DefaultValue", ctypes.c_long),
        ("IsAutoSupported", ctypes.c_int),
        ("IsWritable", ctypes.c_int),
        ("ControlType", ctypes.c_int),
        ("Unused", ctypes.c_char * 32),
    ]


# -- Pythonic dataclasses ------------------------------------------------------

@dataclass
class CameraInfo:
    name: str
    camera_id: int
    max_width: int
    max_height: int
    is_color: bool
    pixel_size: float
    is_cooler: bool
    is_usb3: bool
    bit_depth: int
    e_per_adu: float
    supported_bins: list

    @classmethod
    def from_struct(cls, s):
        bins = [s.SupportedBins[i] for i in range(16) if s.SupportedBins[i]]
        return cls(
            name=s.Name.decode("utf-8", errors="replace").strip("\x00"),
            camera_id=s.CameraID,
            max_width=s.MaxWidth,
            max_height=s.MaxHeight,
            is_color=bool(s.IsColorCam),
            pixel_size=s.PixelSize,
            is_cooler=bool(s.IsCoolerCam),
            is_usb3=bool(s.IsUSB3Camera),
            bit_depth=s.BitDepth,
            e_per_adu=s.ElecPerADU,
            supported_bins=bins,
        )


@dataclass
class ControlInfo:
    name: str
    ctrl_type: int
    min_value: int
    max_value: int
    default_value: int
    is_writable: bool
    is_auto_supported: bool
    description: str = ""

    @classmethod
    def from_struct(cls, s):
        return cls(
            name=s.Name.decode("utf-8", errors="replace").strip("\x00"),
            ctrl_type=s.ControlType,
            min_value=s.MinValue,
            max_value=s.MaxValue,
            default_value=s.DefaultValue,
            is_writable=bool(s.IsWritable),
            is_auto_supported=bool(s.IsAutoSupported),
            description=s.Description.decode("utf-8", errors="replace").strip("\x00"),
        )

    def to_caps_dict(self) -> dict:
        """Convert to the dict format expected by camera_config.py."""
        return {
            "MinValue": self.min_value,
            "MaxValue": self.max_value,
            "DefaultValue": self.default_value,
            "IsAutoSupported": self.is_auto_supported,
            "IsWritable": self.is_writable,
            "ControlType": self.ctrl_type,
            "Description": self.description,
        }


# -- ASIDriver (thin ctypes wrapper) ------------------------------------------

class ASIDriver:
    """
    Direct ctypes wrapper around ASICamera2.dll / libASICamera2.so.
    Each public method maps 1:1 to an SDK function with explicit prototypes.
    """

    def __init__(self, lib_path: str):
        if not os.path.isfile(lib_path):
            raise FileNotFoundError(f"SDK library not found: {lib_path}")
        self._lib = ctypes.cdll.LoadLibrary(lib_path)
        self._setup_prototypes()

    def _setup_prototypes(self):
        L = self._lib

        L.ASIGetNumOfConnectedCameras.restype = ctypes.c_int
        L.ASIGetNumOfConnectedCameras.argtypes = []

        L.ASIGetCameraProperty.restype = ctypes.c_int
        L.ASIGetCameraProperty.argtypes = [
            ctypes.POINTER(_CameraInfo), ctypes.c_int
        ]

        L.ASIOpenCamera.restype = ctypes.c_int
        L.ASIOpenCamera.argtypes = [ctypes.c_int]

        L.ASIInitCamera.restype = ctypes.c_int
        L.ASIInitCamera.argtypes = [ctypes.c_int]

        L.ASICloseCamera.restype = ctypes.c_int
        L.ASICloseCamera.argtypes = [ctypes.c_int]

        L.ASIGetNumOfControls.restype = ctypes.c_int
        L.ASIGetNumOfControls.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]

        L.ASIGetControlCaps.restype = ctypes.c_int
        L.ASIGetControlCaps.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.POINTER(_ControlCaps)
        ]

        L.ASISetControlValue.restype = ctypes.c_int
        L.ASISetControlValue.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_int
        ]

        L.ASIGetControlValue.restype = ctypes.c_int
        L.ASIGetControlValue.argtypes = [
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_int)
        ]

        L.ASISetROIFormat.restype = ctypes.c_int
        L.ASISetROIFormat.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int
        ]

        L.ASIGetROIFormat.restype = ctypes.c_int
        L.ASIGetROIFormat.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
        ]

        L.ASISetStartPos.restype = ctypes.c_int
        L.ASISetStartPos.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]

        L.ASIStartVideoCapture.restype = ctypes.c_int
        L.ASIStartVideoCapture.argtypes = [ctypes.c_int]

        L.ASIStopVideoCapture.restype = ctypes.c_int
        L.ASIStopVideoCapture.argtypes = [ctypes.c_int]

        L.ASIGetVideoData.restype = ctypes.c_int
        L.ASIGetVideoData.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_long, ctypes.c_int
        ]

        L.ASIGetDroppedFrames.restype = ctypes.c_int
        L.ASIGetDroppedFrames.argtypes = [
            ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]

    @staticmethod
    def _chk(code: int, func: str = ""):
        if code != ASI_SUCCESS:
            raise ASIError(code, func)

    # -- Enumeration --

    def get_num_cameras(self) -> int:
        return self._lib.ASIGetNumOfConnectedCameras()

    def get_camera_property(self, index: int):
        info = _CameraInfo()
        self._chk(
            self._lib.ASIGetCameraProperty(ctypes.byref(info), index),
            "GetCameraProperty",
        )
        return info

    # -- Lifecycle --

    def open_camera(self, cam_id: int):
        self._chk(self._lib.ASIOpenCamera(cam_id), "OpenCamera")

    def init_camera(self, cam_id: int):
        self._chk(self._lib.ASIInitCamera(cam_id), "InitCamera")

    def close_camera(self, cam_id: int):
        self._chk(self._lib.ASICloseCamera(cam_id), "CloseCamera")

    # -- Controls --

    def get_num_controls(self, cam_id: int) -> int:
        n = ctypes.c_int()
        self._chk(
            self._lib.ASIGetNumOfControls(cam_id, ctypes.byref(n)),
            "GetNumOfControls",
        )
        return n.value

    def get_control_caps(self, cam_id: int, index: int):
        caps = _ControlCaps()
        self._chk(
            self._lib.ASIGetControlCaps(cam_id, index, ctypes.byref(caps)),
            "GetControlCaps",
        )
        return caps

    def get_all_control_caps(self, cam_id: int):
        n = self.get_num_controls(cam_id)
        result = {}
        for i in range(n):
            c = self.get_control_caps(cam_id, i)
            result[c.ControlType] = c
        return result

    def set_control_value(self, cam_id: int, ctrl: int, value: int,
                          auto: bool = False):
        self._chk(
            self._lib.ASISetControlValue(cam_id, ctrl, value, int(auto)),
            "SetControlValue",
        )

    def get_control_value(self, cam_id: int, ctrl: int):
        val = ctypes.c_long()
        is_auto = ctypes.c_int()
        self._chk(
            self._lib.ASIGetControlValue(
                cam_id, ctrl, ctypes.byref(val), ctypes.byref(is_auto)
            ),
            "GetControlValue",
        )
        return val.value, bool(is_auto.value)

    # -- ROI --

    def set_roi_format(self, cam_id: int, width: int, height: int,
                       binning: int, img_type: int):
        self._chk(
            self._lib.ASISetROIFormat(cam_id, width, height, binning, img_type),
            "SetROIFormat",
        )

    def get_roi_format(self, cam_id: int):
        w, h, b, t = (ctypes.c_int() for _ in range(4))
        self._chk(
            self._lib.ASIGetROIFormat(
                cam_id, ctypes.byref(w), ctypes.byref(h),
                ctypes.byref(b), ctypes.byref(t),
            ),
            "GetROIFormat",
        )
        return w.value, h.value, b.value, t.value

    def set_start_pos(self, cam_id: int, x: int, y: int):
        self._chk(self._lib.ASISetStartPos(cam_id, x, y), "SetStartPos")

    # -- Video capture --

    def start_video_capture(self, cam_id: int):
        self._chk(self._lib.ASIStartVideoCapture(cam_id), "StartVideoCapture")

    def stop_video_capture(self, cam_id: int):
        self._chk(self._lib.ASIStopVideoCapture(cam_id), "StopVideoCapture")

    def get_video_data_raw(self, cam_id: int, buf, buf_size: int,
                           wait_ms: int) -> int:
        """Returns raw error code -- caller handles TIMEOUT vs real errors."""
        return self._lib.ASIGetVideoData(cam_id, buf, buf_size, wait_ms)

    def get_dropped_frames(self, cam_id: int) -> int:
        n = ctypes.c_int()
        self._chk(
            self._lib.ASIGetDroppedFrames(cam_id, ctypes.byref(n)),
            "GetDroppedFrames",
        )
        return n.value


# -- ASICamera (high-level) ----------------------------------------------------

class ASICamera:
    """
    Opened + initialised camera. Owns the lifecycle (open -> init -> close).
    """

    def __init__(self, driver: ASIDriver, index: int):
        self.driver = driver
        self._opened = False
        self._video = False

        raw_info = driver.get_camera_property(index)
        self.info = CameraInfo.from_struct(raw_info)
        self.cam_id = self.info.camera_id

        driver.open_camera(self.cam_id)
        driver.init_camera(self.cam_id)
        self._opened = True

        raw_caps = driver.get_all_control_caps(self.cam_id)
        self.controls = {
            ct: ControlInfo.from_struct(caps) for ct, caps in raw_caps.items()
        }

    def get_caps_dict(self) -> dict:
        """Export controls as {name: caps_dict} for camera_config.py."""
        return {
            info.name: info.to_caps_dict()
            for info in self.controls.values()
        }

    def has_ctrl(self, ctrl: int) -> bool:
        return ctrl in self.controls

    def set_ctrl(self, ctrl: int, value: int, auto: bool = False):
        self.driver.set_control_value(self.cam_id, ctrl, value, auto)

    def get_ctrl(self, ctrl: int):
        return self.driver.get_control_value(self.cam_id, ctrl)

    def get_ctrl_value(self, ctrl: int) -> int:
        val, _ = self.get_ctrl(ctrl)
        return val

    def set_roi(self, width: int, height: int, binning: int = 1,
                img_type: int = 2, start_x: int = 0, start_y: int = 0):
        width = (width // 8) * 8
        height = (height // 2) * 2
        self.driver.set_roi_format(
            self.cam_id, width, height, binning, int(img_type)
        )
        self.driver.set_start_pos(self.cam_id, start_x, start_y)

    def get_roi(self):
        return self.driver.get_roi_format(self.cam_id)

    def frame_buffer_size(self) -> int:
        w, h, _bin, img_type = self.get_roi()
        bpp = 2 if img_type == int(ImgType.RAW16) else 1
        if img_type == int(ImgType.RGB24):
            bpp = 3
        return w * h * bpp

    def temperature(self) -> float:
        return self.get_ctrl_value(Ctrl.TEMPERATURE) / 10.0

    def set_cooler(self, on: bool, target_c: int = -10):
        if self.has_ctrl(Ctrl.COOLER_ON):
            self.set_ctrl(Ctrl.COOLER_ON, int(on))
        if self.has_ctrl(Ctrl.TARGET_TEMP):
            self.set_ctrl(Ctrl.TARGET_TEMP, target_c)

    def start_video(self):
        self.driver.start_video_capture(self.cam_id)
        self._video = True

    def stop_video(self):
        if self._video:
            self.driver.stop_video_capture(self.cam_id)
            self._video = False

    def get_dropped(self) -> int:
        return self.driver.get_dropped_frames(self.cam_id)

    def close(self):
        if self._opened:
            self.stop_video()
            self.driver.close_camera(self.cam_id)
            self._opened = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
