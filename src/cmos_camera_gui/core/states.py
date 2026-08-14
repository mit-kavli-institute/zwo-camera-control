"""
Camera finite state machine.

State names mirror pirt-camera-control (READY / EXPOSING / ERROR /
TEC_SETTLING) so telescope-control code can use the same
``wait_for_state("READY")`` pattern against either application. The extra
states are additive and never replace those three.

Reported to remote clients as ``camera_state`` (the enum ``.name``).
"""

from enum import Enum


class CameraState(Enum):
    DISCONNECTED = "DISCONNECTED"       # no camera open
    INITIALIZING = "INITIALIZING"       # connect/init in progress
    SETTING_EXPOSURE = "SETTING_EXPOSURE"  # exposure change not yet on hardware
    TEC_SETTLING = "TEC_SETTLING"       # idle; cooler regulating toward setpoint
    READY = "READY"                     # idle (streaming or not); safe to take data
    EXPOSING = "EXPOSING"               # record/grab in flight
    SAVING = "SAVING"                   # kept frames flushing to FITS
    ERROR = "ERROR"                     # latched fault; cleared explicitly


# Priority when several conditions hold at once (highest wins).
STATE_PRIORITY = [
    CameraState.ERROR,
    CameraState.EXPOSING,
    CameraState.SAVING,
    CameraState.SETTING_EXPOSURE,
    CameraState.TEC_SETTLING,
    CameraState.READY,
]
