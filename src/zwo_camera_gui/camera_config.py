"""
camera_config.py
================
Type-safe camera configuration layer for ZWO ASI cameras.

Design
------
  ControlSpec       — immutable description of one control's capabilities
                      (range, kind, read-only flag, etc.)

  CameraControlSet  — the full capability description for a connected camera.
                      Immutable once built.

  CameraSettings    — a mutable, validated settings bag.  Applying it to
                      a camera is a single call.

Nothing here imports zwoasi or the ctypes SDK at module level; it only
receives raw caps dicts so it can work with any wrapper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Iterator, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# ControlKind — how a control should be presented and validated
# ---------------------------------------------------------------------------

class ControlKind(Enum):
    BOOLEAN     = auto()  # 0 or 1
    INTEGER     = auto()  # integer in [min, max]
    EXPOSURE    = auto()  # integer µs; display as ms / s
    FRAME_RATE  = auto()  # integer fps limit (absent on CMOS; present on ASI990)
    FLIP        = auto()  # 0=None, 1=Horizontal, 2=Vertical, 3=Both
    TEMPERATURE = auto()  # read-only; raw value is 10× actual °C
    READONLY    = auto()  # generic read-only


# Names that map to a fixed kind regardless of value range.
_KIND_BY_NAME: Dict[str, ControlKind] = {
    "Exposure":          ControlKind.EXPOSURE,
    "FrameRateLimit":    ControlKind.FRAME_RATE,
    "TargetFPS":         ControlKind.FRAME_RATE,
    "FrameRate":         ControlKind.FRAME_RATE,
    "Temperature":       ControlKind.TEMPERATURE,
    "Flip":              ControlKind.FLIP,
    "HighSpeedMode":     ControlKind.BOOLEAN,
    "CoolerOn":          ControlKind.BOOLEAN,
    "FanOn":             ControlKind.BOOLEAN,
    "MonoBin":           ControlKind.BOOLEAN,
    "HardwareBin":       ControlKind.BOOLEAN,
    "AntiDewHeater":     ControlKind.BOOLEAN,
    "PatternAdjust":     ControlKind.BOOLEAN,
}

# Canonical renames applied at caps-parse time. Everything downstream
# (GUI dict keys, WS API keys, FITS headers, snapshots) uses the new name.
_NAME_RENAMES: Dict[str, str] = {
    "Brightness": "Offset",
}

# Display-only labels that differ from the canonical name.
_DISPLAY_NAME_OVERRIDES: Dict[str, str] = {
    "BandWidth": "Turbo USB",
}

# Default values to apply on connect, overriding the SDK's DefaultValue.
# Keys are post-rename canonical names. Clamped to the SDK-reported
# [min, max] at spec-construction time.
_DEFAULT_OVERRIDES: Dict[str, int] = {
    "Gain":      200,
    "BandWidth": 100,
}

FLIP_LABELS = ("None", "Horizontal", "Vertical", "Both")

_FORCE_READONLY: set = {"Temperature"}
_HIDDEN: set = {
    "Overclock",
    # Auto-exposure algorithm controls — only relevant when auto-exp is active
    "AutoMaxGain", "AutoMaxExp", "AutoMaxBrightness",
    "AutoExpMaxGain", "AutoExpMaxExp", "AutoExpTargetBrightness",
    # "Shifts gain and exposure leaving brightness unchanged" — only useful for
    # casual imaging; confusing for astronomy / lab characterization.
    "ExposureGainShift", "ExpGainShift",
}


# ---------------------------------------------------------------------------
# ControlSpec  (immutable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ControlSpec:
    name:          str
    description:   str
    kind:          ControlKind
    min_value:     int
    max_value:     int
    default_value: int
    is_auto:       bool
    is_writable:   bool
    control_type:  int

    @property
    def display_name(self) -> str:
        if self.name in _DISPLAY_NAME_OVERRIDES:
            return _DISPLAY_NAME_OVERRIDES[self.name]
        return re.sub(r'([A-Z])', r' \1', self.name).strip()

    @property
    def is_readonly(self) -> bool:
        return (not self.is_writable) or self.kind in (
            ControlKind.READONLY, ControlKind.TEMPERATURE
        )

    def display_value(self, raw: int) -> str:
        if self.kind == ControlKind.BOOLEAN:
            return "ON" if raw else "OFF"
        if self.kind == ControlKind.FLIP:
            return FLIP_LABELS[raw] if 0 <= raw < len(FLIP_LABELS) else str(raw)
        if self.kind == ControlKind.TEMPERATURE:
            return f"{raw / 10:.1f} °C"
        if self.kind == ControlKind.EXPOSURE:
            if raw < 1_000:
                return f"{raw} µs"
            if raw < 1_000_000:
                return f"{raw / 1_000:.2f} ms"
            return f"{raw / 1_000_000:.2f} s"
        if self.kind == ControlKind.FRAME_RATE:
            return f"{raw} fps" if raw > 0 else "unlimited"
        return str(raw)

    def clamp(self, value: Union[int, float, bool]) -> int:
        return max(self.min_value, min(self.max_value, int(value)))

    def validate(self, value: Union[int, float, bool]) -> Tuple[bool, str]:
        if self.is_readonly:
            return False, f"'{self.name}' is read-only"
        if self.kind == ControlKind.BOOLEAN:
            if int(value) not in (0, 1):
                return False, f"'{self.name}' is boolean (0 or 1), got {value!r}"
            return True, ""
        v = int(value)
        if v < self.min_value or v > self.max_value:
            return False, (
                f"'{self.name}' must be in [{self.min_value}, {self.max_value}], "
                f"got {v}"
            )
        return True, ""

    @classmethod
    def from_caps_dict(cls, name: str, caps: dict) -> "ControlSpec":
        """Build from a caps dict with keys: MinValue, MaxValue,
        DefaultValue, IsAutoSupported, IsWritable, ControlType, Description."""
        is_writable = bool(caps.get("IsWritable", True)) and name not in _FORCE_READONLY

        kind = _KIND_BY_NAME.get(name)
        if kind is None:
            lo = int(caps.get("MinValue", 0))
            hi = int(caps.get("MaxValue", 1))
            if not is_writable:
                kind = ControlKind.READONLY
            elif lo == 0 and hi == 1:
                kind = ControlKind.BOOLEAN
            else:
                kind = ControlKind.INTEGER

        min_value = int(caps.get("MinValue", 0))
        max_value = int(caps.get("MaxValue", 0))
        sdk_default = int(caps.get("DefaultValue", 0))
        # Only apply the override when it fits this camera's reported range.
        # Falling back to the SDK default (rather than clamping) keeps cameras
        # with narrower ranges -- e.g. QHY42 Gain [0, 100] -- from being pinned
        # at their maximum by an override tuned for a different model.
        override = _DEFAULT_OVERRIDES.get(name)
        if override is not None and min_value <= override <= max_value:
            default_value = override
        else:
            default_value = sdk_default

        return cls(
            name          = name,
            description   = caps.get("Description", ""),
            kind          = kind,
            min_value     = min_value,
            max_value     = max_value,
            default_value = default_value,
            is_auto       = bool(caps.get("IsAutoSupported", False)),
            is_writable   = is_writable,
            control_type  = int(caps.get("ControlType", -1)),
        )


# ---------------------------------------------------------------------------
# CameraControlSet  (immutable)
# ---------------------------------------------------------------------------

_DISPLAY_ORDER = [
    "Gain",
    "Exposure",
    "FrameRateLimit", "TargetFPS", "FrameRate",
    "Offset",
    "BandWidth",
    "HighSpeedMode",
    "Flip",
    "Temperature",
    "CoolerOn",
    "TargetTemp",
    "CoolerPowerPerc",
    "FanOn",
    "AntiDewHeater",
    "MonoBin",
    "HardwareBin",
    "AutoMaxGain",
    "AutoMaxExp",
    "AutoMaxBrightness",
    "WbR", "WbB",
    "Gamma",
]
_ORDER_INDEX = {name: i for i, name in enumerate(_DISPLAY_ORDER)}


@dataclass(frozen=True)
class CameraControlSet:
    camera_name: str
    specs: Dict[str, ControlSpec] = field(default_factory=dict)

    def __contains__(self, name: str) -> bool:
        return name in self.specs

    def __getitem__(self, name: str) -> ControlSpec:
        return self.specs[name]

    def get(self, name: str) -> Optional[ControlSpec]:
        return self.specs.get(name)

    def _sorted(self, specs) -> List[ControlSpec]:
        return sorted(specs, key=lambda s: (_ORDER_INDEX.get(s.name, 999), s.name))

    def all(self) -> List[ControlSpec]:
        return self._sorted(self.specs.values())

    def writable(self) -> List[ControlSpec]:
        return self._sorted(s for s in self.specs.values() if not s.is_readonly)

    def readonly(self) -> List[ControlSpec]:
        return self._sorted(s for s in self.specs.values() if s.is_readonly)

    def has_offset(self) -> bool:
        return "Offset" in self.specs

    def has_frame_rate_control(self) -> bool:
        return any(s.kind == ControlKind.FRAME_RATE for s in self.specs.values())

    def has_cooler(self) -> bool:
        return "CoolerOn" in self.specs

    @classmethod
    def from_caps_dict(cls, camera_name: str, raw_caps: dict) -> "CameraControlSet":
        specs = {}
        for sdk_name, caps in raw_caps.items():
            if sdk_name in _HIDDEN:
                continue
            name = _NAME_RENAMES.get(sdk_name, sdk_name)
            specs[name] = ControlSpec.from_caps_dict(name, caps)
        return cls(camera_name=camera_name, specs=specs)

    def describe(self) -> str:
        lines = [f"Camera: {self.camera_name}"]
        lines.append(f"  Writable controls ({len(self.writable())}):")
        for s in self.writable():
            rng = f"[{s.min_value}, {s.max_value}]" if s.kind not in (
                ControlKind.BOOLEAN,) else "[0, 1]"
            auto = " (auto)" if s.is_auto else ""
            lines.append(f"    {s.name:<28} {s.kind.name:<12} {rng}{auto}")
        if self.readonly():
            lines.append(f"  Read-only ({len(self.readonly())}):")
            for s in self.readonly():
                lines.append(f"    {s.name}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CameraSettings  (mutable, validated)
# ---------------------------------------------------------------------------

class CameraSettings:
    def __init__(self, control_set: CameraControlSet):
        self._cs = control_set
        self._values: Dict[str, int] = {
            spec.name: spec.default_value
            for spec in control_set.writable()
        }

    @property
    def control_set(self) -> CameraControlSet:
        return self._cs

    def get(self, name: str) -> Optional[int]:
        return self._values.get(name)

    def get_display(self, name: str) -> str:
        spec = self._cs.get(name)
        val  = self._values.get(name)
        if spec is None or val is None:
            return "—"
        return spec.display_value(val)

    def has(self, name: str) -> bool:
        return name in self._values

    def items(self) -> Iterator[Tuple[str, int]]:
        return iter(self._values.items())

    def snapshot(self) -> Dict[str, int]:
        return dict(self._values)

    def set(self, name: str, value: Union[int, float, bool],
            clamp: bool = False) -> "CameraSettings":
        spec = self._cs.get(name)
        if spec is None:
            raise KeyError(f"Control '{name}' not available on {self._cs.camera_name!r}")
        if spec.is_readonly:
            raise TypeError(f"Control '{name}' is read-only")
        int_val = int(value)
        if clamp:
            int_val = spec.clamp(int_val)
        else:
            ok, msg = spec.validate(int_val)
            if not ok:
                raise ValueError(msg)
        self._values[name] = int_val
        return self

    def set_if_present(self, name: str, value: Union[int, float, bool],
                       clamp: bool = True) -> bool:
        if not self.has(name):
            return False
        self.set(name, value, clamp=clamp)
        return True

    def reset_to_defaults(self) -> "CameraSettings":
        for spec in self._cs.writable():
            self._values[spec.name] = spec.default_value
        return self

    def apply(self, camera) -> List[Tuple[str, Exception]]:
        """Push all values via camera.set_ctrl(control_type, value).
        Works with ASICamera from sdk.py."""
        errors: List[Tuple[str, Exception]] = []
        for name, value in self._values.items():
            spec = self._cs.get(name)
            if spec is None or spec.control_type < 0:
                continue
            try:
                camera.set_ctrl(spec.control_type, value)
            except Exception as exc:
                errors.append((name, exc))
        return errors

    def apply_one(self, camera, name: str) -> None:
        spec = self._cs.get(name)
        if spec is None:
            raise KeyError(f"'{name}' is not in the control set")
        val = self._values.get(name)
        if val is None:
            raise KeyError(f"'{name}' has no current value")
        camera.set_ctrl(spec.control_type, val)
