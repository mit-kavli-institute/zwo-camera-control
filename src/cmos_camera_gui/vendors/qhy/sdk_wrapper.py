"""
QHYCCD SDK ctypes wrapper.

Ported (near-verbatim) from the validated qcam_gps_live wrapper described
in HANDOFF.md -- full function signatures, ControlID enum, burst-mode
bindings, reusable frame buffer, and the GPS row-0 parser. Only the DLL
search (`PlatformSDK._find_library_path`) is adapted to this repo:
  1. QHYCCD_SDK_DLL environment variable (full path to the library)
  2. <repo root>/sdk/** (recursive)
  3. system search (ctypes.util.find_library / PATH)
"""

import ctypes
import ctypes.util
import platform
import os
from ctypes import (
    c_uint8, c_uint16, c_uint32,
    c_char_p, c_double,
    c_bool, c_void_p, POINTER, byref, cast,
    create_string_buffer, Structure
)
from enum import IntEnum


# ---------------------------------------------------------------------------
# Stream mode constants (from qhyccdcamdef.h)
# ---------------------------------------------------------------------------
SINGLE_MODE = 0
LIVE_MODE = 1


# ---------------------------------------------------------------------------
# Read mode constants
# ---------------------------------------------------------------------------
READ_MODE_NORMAL = 0


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------
class QHYCCDError(IntEnum):
    QHYCCD_SUCCESS = 0
    QHYCCD_READ_DIRECTLY = 0x2001
    QHYCCD_DELAY_200MS = 0x2000


# Transfer types
QHYCCD_PCIE = 9
QHYCCD_WINPCAP = 8
QHYCCD_QGIGAE = 7
QHYCCD_USBSYNC = 6
QHYCCD_USBASYNC = 5
QHYCCD_COLOR = 4
QHYCCD_MONO = 3
QHYCCD_COOL = 2
QHYCCD_NOTCOOL = 1


# ---------------------------------------------------------------------------
# Bayer pattern (for color camera detection)
# ---------------------------------------------------------------------------
class BayerPattern(IntEnum):
    BAYER_GB = 1
    BAYER_GR = 2
    BAYER_BG = 3
    BAYER_RG = 4


# ---------------------------------------------------------------------------
# CONTROL_ID constants
# ---------------------------------------------------------------------------
class ControlID(IntEnum):
    CONTROL_BRIGHTNESS = 0
    CONTROL_CONTRAST = 1
    CONTROL_WBR = 2
    CONTROL_WBB = 3
    CONTROL_WBG = 4
    CONTROL_GAMMA = 5
    CONTROL_GAIN = 6
    CONTROL_OFFSET = 7
    CONTROL_EXPOSURE = 8
    CONTROL_SPEED = 9
    CONTROL_TRANSFERBIT = 10
    CONTROL_CHANNELS = 11
    CONTROL_USBTRAFFIC = 12
    CONTROL_ROWNOISERE = 13
    CONTROL_CURTEMP = 14
    CONTROL_CURPWM = 15
    CONTROL_MANULPWM = 16
    CONTROL_CFWPORT = 17
    CONTROL_COOLER = 18
    CONTROL_ST4PORT = 19
    CAM_COLOR = 20
    CAM_BIN1X1MODE = 21
    CAM_BIN2X2MODE = 22
    CAM_BIN3X3MODE = 23
    CAM_BIN4X4MODE = 24
    CAM_MECHANICALSHUTTER = 25
    CAM_TRIGER_INTERFACE = 26
    CAM_TECOVERPROTECT_INTERFACE = 27
    CAM_SINGNALCLAMP_INTERFACE = 28
    CAM_FINETONE_INTERFACE = 29
    CAM_SHUTTERMOTORHEATING_INTERFACE = 30
    CAM_CALIBRATEFPN_INTERFACE = 31
    CAM_CHIPTEMPERATURESENSOR_INTERFACE = 32
    CAM_USBREADOUTSLOWEST_INTERFACE = 33
    CAM_8BITS = 34
    CAM_16BITS = 35
    CAM_GPS = 36
    CAM_IGNOREOVERSCAN_INTERFACE = 37
    QHYCCD_3A_AUTOEXPOSURE = 39
    QHYCCD_3A_AUTOFOCUS = 40
    CONTROL_AMPV = 41
    CONTROL_VCAM = 42
    CAM_VIEW_MODE = 43
    CONTROL_CFWSLOTSNUM = 44
    IS_EXPOSING_DONE = 45
    ScreenStretchB = 46
    ScreenStretchW = 47
    CONTROL_DDR = 48
    CAM_LIGHT_PERFORMANCE_MODE = 49
    CAM_QHY5II_GUIDE_MODE = 50
    DDR_BUFFER_CAPACITY = 51
    DDR_BUFFER_READ_THRESHOLD = 52
    DefaultGain = 53
    DefaultOffset = 54
    OutputDataActualBits = 55
    OutputDataAlignment = 56
    CAM_SINGLEFRAMEMODE = 57
    CAM_LIVEVIDEOMODE = 58
    CAM_IS_COLOR = 59
    hasHardwareFrameCounter = 60
    CONTROL_MAX_ID_Error = 61
    CAM_HUMIDITY = 62
    CAM_PRESSURE = 63
    CONTROL_VACUUM_PUMP = 64
    CONTROL_SensorChamberCycle_PUMP = 65
    CAM_32BITS = 66
    CAM_Sensor_ULVO_Status = 67
    CAM_SensorPhaseReTrain = 68
    CAM_InitConfigFromFlash = 69
    CAM_TRIGER_MODE = 70
    CAM_TRIGER_OUT = 71
    CAM_BURST_MODE = 72
    CAM_SPEAKER_LED_ALARM = 73
    CAM_WATCH_DOG_FPGA = 74
    CAM_BIN6X6MODE = 75
    CAM_BIN8X8MODE = 76
    CAM_GlobalSensorGPSLED = 77
    CONTROL_ImgProc = 78
    CONTROL_RemoveRBI = 79
    CONTROL_GlobalReset = 80
    CONTROL_FrameDetect = 81
    CAM_GainDBConversion = 82
    CAM_CurveSystemGain = 83
    CAM_CurveFullWell = 84
    CAM_CurveReadoutNoise = 85
    CAM_UseAverageBinning = 86
    CONTROL_OUTSIDE_PUMP_V2 = 87
    CONTROL_AUTOEXPOSURE = 88
    CONTROL_AUTOEXPTargetBrightness = 89
    CONTROL_AUTOEXPSampleArea = 90
    CONTROL_AUTOEXPexpMaxMS = 91
    CONTROL_AUTOEXPgainMax = 92
    CONTROL_Error_Led = 93
    CONTROL_HEATINGBOARD = 94
    CONTROL_CAA_ROTATOR = 95
    CONTROL_MAX_ID = 96


# ---------------------------------------------------------------------------
# Camera info structure
# ---------------------------------------------------------------------------
class QHYCamMinMaxStepValue(Structure):
    _fields_ = [
        ("name", c_char_p),
        ("min", c_double),
        ("max", c_double),
        ("step", c_double),
    ]


# ---------------------------------------------------------------------------
# Platform detection & library loading
# ---------------------------------------------------------------------------
class PlatformSDK:
    LIB_NAME_MAP = {
        "Windows": "qhyccd.dll",
        "Linux": "libqhyccd.so",
        "Darwin": "libqhyccd.dylib",
    }

    def __init__(self):
        self.system = platform.system()
        self.lib_name = self.LIB_NAME_MAP.get(self.system)
        self._lib = None
        self._loaded = False
        self._last_error = ""

    def _find_library_path(self):
        # 1. explicit env var
        env = os.environ.get("QHYCCD_SDK_DLL")
        if env and os.path.isfile(env):
            return env

        # 2. <repo root>/sdk/** -- this file lives at
        #    src/cmos_camera_gui/vendors/qhy/sdk_wrapper.py
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
        for base in (os.path.join(repo_root, "sdk"),
                     os.path.join(here, "sdk")):
            if os.path.isdir(base):
                for root, _dirs, files in os.walk(base):
                    if self.lib_name in files:
                        return os.path.join(root, self.lib_name)

        # 3. system search
        return ctypes.util.find_library(self.lib_name) or self.lib_name

    def load(self):
        if self._loaded:
            return True
        try:
            lib_path = self._find_library_path()
            self._lib = ctypes.cdll.LoadLibrary(lib_path)
            self._loaded = True
            self.lib_path = lib_path
            return True
        except OSError as e:
            self._last_error = str(e)
            return False

    @property
    def lib(self):
        if not self._loaded:
            self.load()
        return self._lib

    @property
    def is_loaded(self):
        return self._loaded


_platform = PlatformSDK()


# ---------------------------------------------------------------------------
# Main SDK wrapper - matches reference demos calling conventions
# ---------------------------------------------------------------------------
class QHYCCDSDK:

    def __init__(self):
        self._lib = None
        self._initialized = False
        self._handle = None
        self._frame_buf = None  # reused across get_*_frame calls

    # ----------------------------------------------------------------
    # Library loading (internal)
    # ----------------------------------------------------------------
    def _ensure_lib(self):
        if self._lib is None:
            self._lib = _platform.lib
            if self._lib is None:
                raise RuntimeError(f"SDK not loaded: {_platform._last_error}")
            self._setup_functions()

    def _setup_functions(self):
        lib = self._lib

        # --- SDK lifecycle ---
        lib.InitQHYCCDResource.restype = c_uint32
        lib.ReleaseQHYCCDResource.restype = c_uint32
        lib.EnableQHYCCDMessage.argtypes = [c_bool]
        lib.EnableQHYCCDMessage.restype = None  # void in C header

        # --- Scanning ---
        lib.ScanQHYCCD.restype = c_uint32
        lib.GetQHYCCDId.argtypes = [c_uint32, c_char_p]
        lib.GetQHYCCDId.restype = c_uint32
        lib.GetQHYCCDModel.argtypes = [c_char_p, c_char_p]
        lib.GetQHYCCDModel.restype = c_uint32

        # --- Open / Close ---
        lib.OpenQHYCCD.argtypes = [c_char_p]
        lib.OpenQHYCCD.restype = c_void_p
        lib.CloseQHYCCD.argtypes = [c_void_p]
        lib.CloseQHYCCD.restype = c_uint32

        # --- Init ---
        lib.InitQHYCCD.argtypes = [c_void_p]
        lib.InitQHYCCD.restype = c_uint32

        # --- Read mode (must be set before stream mode, per demos) ---
        lib.SetQHYCCDReadMode.argtypes = [c_void_p, c_uint32]
        lib.SetQHYCCDReadMode.restype = c_uint32
        lib.GetQHYCCDReadMode.argtypes = [c_void_p, POINTER(c_uint32)]
        lib.GetQHYCCDReadMode.restype = c_uint32
        lib.GetQHYCCDNumberOfReadModes.argtypes = [c_void_p, POINTER(c_uint32)]
        lib.GetQHYCCDNumberOfReadModes.restype = c_uint32
        lib.GetQHYCCDReadModeResolution.argtypes = [c_void_p, c_uint32, POINTER(c_uint32), POINTER(c_uint32)]
        lib.GetQHYCCDReadModeResolution.restype = c_uint32
        lib.GetQHYCCDReadModeName.argtypes = [c_void_p, c_uint32, c_char_p]
        lib.GetQHYCCDReadModeName.restype = c_uint32

        # --- Stream mode ---
        lib.SetQHYCCDStreamMode.argtypes = [c_void_p, c_uint8]
        lib.SetQHYCCDStreamMode.restype = c_uint32

        # --- Resolution / ROI ---
        lib.SetQHYCCDResolution.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32, c_uint32]
        lib.SetQHYCCDResolution.restype = c_uint32

        # --- Binning / bits ---
        lib.SetQHYCCDBinMode.argtypes = [c_void_p, c_uint32, c_uint32]
        lib.SetQHYCCDBinMode.restype = c_uint32
        lib.SetQHYCCDBitsMode.argtypes = [c_void_p, c_uint32]
        lib.SetQHYCCDBitsMode.restype = c_uint32

        # --- Params ---
        lib.SetQHYCCDParam.argtypes = [c_void_p, c_uint32, c_double]
        lib.SetQHYCCDParam.restype = c_uint32
        lib.GetQHYCCDParam.argtypes = [c_void_p, c_uint32]
        lib.GetQHYCCDParam.restype = c_double
        lib.GetQHYCCDParamMinMaxStep.argtypes = [
            c_void_p, c_uint32, POINTER(c_double), POINTER(c_double), POINTER(c_double)
        ]
        lib.GetQHYCCDParamMinMaxStep.restype = c_uint32

        # --- Control check ---
        lib.IsQHYCCDControlAvailable.argtypes = [c_void_p, c_uint32]
        lib.IsQHYCCDControlAvailable.restype = c_uint32

        # --- Memory ---
        lib.GetQHYCCDMemLength.argtypes = [c_void_p]
        lib.GetQHYCCDMemLength.restype = c_uint32

        # --- Single frame ---
        lib.ExpQHYCCDSingleFrame.argtypes = [c_void_p]
        lib.ExpQHYCCDSingleFrame.restype = c_uint32
        lib.GetQHYCCDSingleFrame.argtypes = [
            c_void_p,
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint8)
        ]
        lib.GetQHYCCDSingleFrame.restype = c_uint32
        lib.CancelQHYCCDExposingAndReadout.argtypes = [c_void_p]
        lib.CancelQHYCCDExposingAndReadout.restype = c_uint32

        # --- Live mode ---
        lib.BeginQHYCCDLive.argtypes = [c_void_p]
        lib.BeginQHYCCDLive.restype = c_uint32
        lib.GetQHYCCDLiveFrame.argtypes = [
            c_void_p,
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint8)
        ]
        lib.GetQHYCCDLiveFrame.restype = c_uint32
        lib.StopQHYCCDLive.argtypes = [c_void_p]
        lib.StopQHYCCDLive.restype = c_uint32

        # --- Burst mode (guarded: not exported by older SDK builds) ---
        if hasattr(lib, "EnableQHYCCDBurstMode"):
            lib.EnableQHYCCDBurstMode.argtypes = [c_void_p, c_bool]
            lib.EnableQHYCCDBurstMode.restype = c_uint32
            lib.SetQHYCCDBurstModeStartEnd.argtypes = [c_void_p, c_uint16, c_uint16]
            lib.SetQHYCCDBurstModeStartEnd.restype = c_uint32
            lib.EnableQHYCCDBurstCountFun.argtypes = [c_void_p, c_bool]
            lib.EnableQHYCCDBurstCountFun.restype = c_uint32
            lib.SetQHYCCDBurstIDLE.argtypes = [c_void_p, c_bool]
            lib.SetQHYCCDBurstIDLE.restype = c_uint32
            lib.ReleaseQHYCCDBurstIDLE.argtypes = [c_void_p]
            lib.ReleaseQHYCCDBurstIDLE.restype = c_uint32
            lib.SetQHYCCDBurstModePatchNumber.argtypes = [c_void_p, c_uint32]
            lib.SetQHYCCDBurstModePatchNumber.restype = c_uint32
        if hasattr(lib, "ResetQHYCCDFrameCounter"):
            lib.ResetQHYCCDFrameCounter.argtypes = [c_void_p]
            lib.ResetQHYCCDFrameCounter.restype = c_uint32

        # --- Temperature ---
        lib.ControlQHYCCDTemp.argtypes = [c_void_p, c_double]
        lib.ControlQHYCCDTemp.restype = c_uint32

        # --- Chip info ---
        lib.GetQHYCCDChipInfo.argtypes = [
            c_void_p,
            POINTER(c_double), POINTER(c_double),
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_double), POINTER(c_double),
            POINTER(c_uint32)
        ]
        lib.GetQHYCCDChipInfo.restype = c_uint32
        lib.GetQHYCCDEffectiveArea.argtypes = [
            c_void_p,
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint32), POINTER(c_uint32)
        ]
        lib.GetQHYCCDEffectiveArea.restype = c_uint32
        lib.GetQHYCCDOverScanArea.argtypes = [
            c_void_p,
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint32), POINTER(c_uint32)
        ]
        lib.GetQHYCCDOverScanArea.restype = c_uint32

        # --- Misc ---
        lib.GetQHYCCDSDKVersion.argtypes = [
            POINTER(c_uint32), POINTER(c_uint32),
            POINTER(c_uint32), POINTER(c_uint32)
        ]
        lib.GetQHYCCDSDKVersion.restype = c_uint32
        lib.SetQHYCCDDebayerOnOff.argtypes = [c_void_p, c_bool]
        lib.SetQHYCCDDebayerOnOff.restype = c_uint32
        lib.GetQHYCCDFWVersion.argtypes = [c_void_p, POINTER(c_uint8)]
        lib.GetQHYCCDFWVersion.restype = c_uint32

    # ----------------------------------------------------------------
    # SDK lifecycle
    # ----------------------------------------------------------------
    def init_resource(self):
        self._ensure_lib()
        ret = self._lib.InitQHYCCDResource()
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            self._initialized = True
        return ret

    def release_resource(self):
        ret = 0
        if self._initialized:
            self._ensure_lib()
            ret = self._lib.ReleaseQHYCCDResource()
            self._initialized = False
        return ret

    def enable_message(self, enable=True):
        """Enable SDK debug messages (from cmake demos)."""
        self._ensure_lib()
        self._lib.EnableQHYCCDMessage(c_bool(enable))

    def get_sdk_version(self):
        self._ensure_lib()
        year = c_uint32(0)
        month = c_uint32(0)
        day = c_uint32(0)
        subday = c_uint32(0)
        ret = self._lib.GetQHYCCDSDKVersion(byref(year), byref(month), byref(day), byref(subday))
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return (year.value, month.value, day.value, subday.value)
        return None

    def get_sdk_version_string(self):
        ver = self.get_sdk_version()
        if ver:
            return f"V20{ver[0]}{ver[1]:02d}{ver[2]:02d}_{ver[3]}"
        return "Unknown"

    # ----------------------------------------------------------------
    # Camera discovery
    # ----------------------------------------------------------------
    def scan(self):
        self._ensure_lib()
        return self._lib.ScanQHYCCD()

    def get_camera_id(self, index):
        self._ensure_lib()
        buf = create_string_buffer(256)
        ret = self._lib.GetQHYCCDId(c_uint32(index), buf)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return buf.value.decode("utf-8", errors="replace")
        return None

    def get_camera_model(self, camera_id):
        self._ensure_lib()
        cid = camera_id.encode("utf-8") if isinstance(camera_id, str) else camera_id
        buf = create_string_buffer(128)
        ret = self._lib.GetQHYCCDModel(c_char_p(cid), buf)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return buf.value.decode("utf-8", errors="replace")
        return None

    # ----------------------------------------------------------------
    # Camera open / close / init
    # ----------------------------------------------------------------
    def open(self, camera_id):
        self._ensure_lib()
        cid = camera_id.encode("utf-8") if isinstance(camera_id, str) else camera_id
        h = self._lib.OpenQHYCCD(c_char_p(cid))
        if h:
            self._handle = h
        return h

    def init(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.InitQHYCCD(h)

    def close(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0
        ret = self._lib.CloseQHYCCD(h)
        if h == self._handle:
            self._handle = None
        return ret

    # ----------------------------------------------------------------
    # Read mode (mandatory before stream mode, per demos)
    # ----------------------------------------------------------------
    def set_read_mode(self, mode_index, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDReadMode(h, c_uint32(mode_index))

    def get_read_mode(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        mode = c_uint32(0)
        ret = self._lib.GetQHYCCDReadMode(h, byref(mode))
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return mode.value
        return None

    def get_number_of_read_modes(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0
        num = c_uint32(0)
        ret = self._lib.GetQHYCCDNumberOfReadModes(h, byref(num))
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return num.value
        return 0

    def get_read_mode_name(self, mode_index, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        buf = create_string_buffer(64)
        ret = self._lib.GetQHYCCDReadModeName(h, c_uint32(mode_index), buf)
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return buf.value.decode("utf-8", errors="replace")
        return None

    # ----------------------------------------------------------------
    # Stream mode
    # ----------------------------------------------------------------
    def set_stream_mode(self, mode, handle=None):
        """mode: SINGLE_MODE(0) or LIVE_MODE(1)"""
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDStreamMode(h, c_uint8(mode))

    # ----------------------------------------------------------------
    # Resolution / ROI
    # ----------------------------------------------------------------
    def set_resolution(self, x, y, xsize, ysize, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDResolution(
            h, c_uint32(x), c_uint32(y), c_uint32(xsize), c_uint32(ysize)
        )

    # ----------------------------------------------------------------
    # Binning / bits
    # ----------------------------------------------------------------
    def set_bin_mode(self, wbin, hbin, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDBinMode(h, c_uint32(wbin), c_uint32(hbin))

    def set_bits_mode(self, bits, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDBitsMode(h, c_uint32(bits))

    # ----------------------------------------------------------------
    # Parameter get/set
    # ----------------------------------------------------------------
    def set_param(self, control_id, value, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.SetQHYCCDParam(h, c_uint32(int(control_id)), c_double(value))

    def get_param(self, control_id, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        return self._lib.GetQHYCCDParam(h, c_uint32(int(control_id)))

    def get_param_min_max_step(self, control_id, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        _min = c_double(0)
        _max = c_double(0)
        _step = c_double(0)
        ret = self._lib.GetQHYCCDParamMinMaxStep(
            h, c_uint32(int(control_id)), byref(_min), byref(_max), byref(_step)
        )
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return (_min.value, _max.value, _step.value)
        return None

    # ----------------------------------------------------------------
    # Control availability
    # ----------------------------------------------------------------
    def is_control_available(self, control_id, handle=None):
        """Check if a control is supported. Returns True if available.

        NOTE: For CAM_COLOR (ControlID.CAM_COLOR), this returns a BayerPattern
        value on success, not just a boolean. Use get_color_bayer() for that.
        """
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return False
        ret = self._lib.IsQHYCCDControlAvailable(h, c_uint32(int(control_id)))
        return ret not in (0xFFFFFFFF,)

    # ----------------------------------------------------------------
    # Memory
    # ----------------------------------------------------------------
    def _get_frame_buffer(self, mem_len):
        """Reusable frame buffer; avoids a multi-MB alloc on every poll."""
        if self._frame_buf is None or len(self._frame_buf) != mem_len:
            self._frame_buf = (c_uint8 * mem_len)()
        return self._frame_buf

    def get_mem_length(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0
        return self._lib.GetQHYCCDMemLength(h)

    # ----------------------------------------------------------------
    # Live capture (matches LiveFrameSample demo)
    # ----------------------------------------------------------------
    def begin_live(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.BeginQHYCCDLive(h)

    def get_live_frame(self, handle=None):
        """Returns (width, height, bpp, channels, imgdata_bytes) or None."""
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None

        mem_len = self.get_mem_length(h)
        if mem_len == 0:
            return None
        imgdata = self._get_frame_buffer(mem_len)
        w = c_uint32(0)
        h_val = c_uint32(0)
        bpp = c_uint32(0)
        channels = c_uint32(0)

        ret = self._lib.GetQHYCCDLiveFrame(
            h, byref(w), byref(h_val), byref(bpp), byref(channels),
            cast(imgdata, POINTER(c_uint8))
        )
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            actual = w.value * h_val.value * (bpp.value // 8) * channels.value
            return (w.value, h_val.value, bpp.value, channels.value,
                    ctypes.string_at(imgdata, actual))
        return None

    def stop_live(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.StopQHYCCDLive(h)

    # ----------------------------------------------------------------
    # Temperature
    # ----------------------------------------------------------------
    def control_temp(self, target_temp, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return 0xFFFFFFFF
        return self._lib.ControlQHYCCDTemp(h, c_double(target_temp))

    def get_temp(self, handle=None):
        return self.get_param(ControlID.CONTROL_CURTEMP, handle)

    def get_cooler_pwm(self, handle=None):
        return self.get_param(ControlID.CONTROL_CURPWM, handle)

    # ----------------------------------------------------------------
    # Chip info
    # ----------------------------------------------------------------
    def get_chip_info(self, handle=None):
        """Returns (chipw_mm, chiph_mm, imagew, imageh, pixelw_um, pixelh_um, bpp)."""
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        chipw = c_double(0)
        chiph = c_double(0)
        imagew = c_uint32(0)
        imageh = c_uint32(0)
        pixelw = c_double(0)
        pixelh = c_double(0)
        bpp = c_uint32(0)
        ret = self._lib.GetQHYCCDChipInfo(
            h,
            byref(chipw), byref(chiph),
            byref(imagew), byref(imageh),
            byref(pixelw), byref(pixelh),
            byref(bpp)
        )
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return (chipw.value, chiph.value, imagew.value, imageh.value,
                    pixelw.value, pixelh.value, bpp.value)
        return None

    def get_effective_area(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        startX = c_uint32(0)
        startY = c_uint32(0)
        sizeX = c_uint32(0)
        sizeY = c_uint32(0)
        ret = self._lib.GetQHYCCDEffectiveArea(h, byref(startX), byref(startY), byref(sizeX), byref(sizeY))
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            return (startX.value, startY.value, sizeX.value, sizeY.value)
        return None

    # ----------------------------------------------------------------
    # Firmware
    # ----------------------------------------------------------------
    def get_fw_version(self, handle=None):
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        buf = (c_uint8 * 32)()
        ret = self._lib.GetQHYCCDFWVersion(h, cast(buf, POINTER(c_uint8)))
        if ret == QHYCCDError.QHYCCD_SUCCESS:
            fwv = bytes(buf)
            if fwv[0] >= 0x10:
                return f"20{fwv[0] >> 4}_{fwv[0] & 0x0F}_{fwv[1]}"
            else:
                return f"20{(fwv[0] >> 4) + 0x10}_{fwv[0] & 0x0F}_{fwv[1]}"
        return None

    # ----------------------------------------------------------------
    # Color detection (matches demos: checks CAM_COLOR returns bayer)
    # ----------------------------------------------------------------
    def get_color_bayer(self, handle=None):
        """Return BayerPattern if color, None if mono."""
        self._ensure_lib()
        h = handle or self._handle
        if h is None:
            return None
        ret = self._lib.IsQHYCCDControlAvailable(h, c_uint32(int(ControlID.CAM_COLOR)))
        try:
            return BayerPattern(ret)
        except ValueError:
            return None

    def is_color(self, handle=None):
        return self.get_color_bayer(handle) is not None

    def has_cooler(self, handle=None):
        return self.is_control_available(ControlID.CONTROL_COOLER, handle)

    def has_gps(self, handle=None):
        return self.is_control_available(ControlID.CAM_GPS, handle)

    # ----------------------------------------------------------------
    # Properties
    # ----------------------------------------------------------------
    @property
    def lib(self):
        return self._lib

    @property
    def handle(self):
        return self._handle

    @property
    def initialized(self):
        return self._initialized


# ---------------------------------------------------------------------------
# GPS parser (pure Python reimplementation of ParseGPSFromFrame)
# ---------------------------------------------------------------------------


def _jd_to_date(jd):
    """Convert Julian Date to (year, month, day)."""
    jd += 0.5
    z = int(jd)
    a = z
    if z >= 2299161:
        alpha = int((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - int(alpha / 4)
    b = a + 1524
    c = int((b - 122.1) / 365.25)
    d = int(365.25 * c)
    e = int((b - d) / 30.6001)
    dd = b - d - int(30.6001 * e)
    mm = e - 1 if e < 14 else e - 13
    yy = c - 4716 if mm > 2 else c - 4715
    return (yy, mm, dd)


def _decode_js(js_sec, timezone=0):
    """Decode Julian Seconds to (JD, hour, minute, second)."""
    JD = js_sec / 3600.0 / 24.0 + 2450000.0
    k = js_sec % (3600 * 24)
    h = int(k / 3600)
    k = k % 3600
    m = int(k / 60)
    k = k % 60
    s = int(k)
    JD = JD + 0.5 + ((h + timezone) * 3600.0 + m * 60.0 + s) / 3600.0 / 24.0
    return (JD, h, m, s)


def parse_gps_from_frame(imgdata, w, bpp, channels):
    """Parse GPS data from the first row of image frame data.

    Matches SDK's ParseGPSFromFrame function logic.
    Returns dict with keys:
      seq, lat, lon, locked, year, month, day, hour, minute, second
    or None if no valid GPS data.
    """
    if len(imgdata) < 64:
        return None

    gps_buf = bytearray(64)
    row_bytes = w * channels
    copy_len = min(64, row_bytes)
    gps_buf[:copy_len] = imgdata[:copy_len]

    seq = (gps_buf[0] * 0x1000000 + gps_buf[1] * 0x10000 +
           gps_buf[2] * 0x100 + gps_buf[3])
    lat = (gps_buf[9] * 0x1000000 + gps_buf[10] * 0x10000 +
           gps_buf[11] * 0x100 + gps_buf[12])
    lon = (gps_buf[13] * 0x1000000 + gps_buf[14] * 0x10000 +
           gps_buf[15] * 0x100 + gps_buf[16])
    start_flag = gps_buf[17]
    start_sec = (gps_buf[18] * 0x1000000 + gps_buf[19] * 0x10000 +
                 gps_buf[20] * 0x100 + gps_buf[21])

    locked = (start_flag == 51)

    jd, hour, minute, second = _decode_js(start_sec)
    year, month, day = _jd_to_date(jd)

    return {
        "seq": seq, "lat": lat, "lon": lon, "locked": locked,
        "year": year, "month": month, "day": day,
        "hour": hour, "minute": minute, "second": second,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def error_string(code):
    if code == QHYCCDError.QHYCCD_SUCCESS:
        return "Success"
    error_map = {
        0xFFFFFFFF: "QHYCCD_ERROR (general)",
        0x2000: "QHYCCD_DELAY_200MS",
        0x2001: "QHYCCD_READ_DIRECTLY",
    }
    if code in error_map:
        return error_map[code]
    if code < 0:
        code = code & 0xFFFFFFFF
    return f"Unknown error (0x{code:08X})"


def platform_info():
    return {
        "system": _platform.system,
        "lib_name": _platform.lib_name,
        "loaded": _platform.is_loaded,
    }
