"""Required-header set: controller coverage and save-time validation."""

from datetime import datetime

import numpy as np
from astropy.io import fits

from cmos_camera_gui.recorder import (
    REQUIRED_HEADER_KEYS, _validate_required, save_fits_cube,
)


class _FakeInfo:
    name = "FakeCam"
    max_width = 48
    max_height = 32
    is_cooler = False
    is_usb3 = True
    bit_depth = 16


class _FakeCam:
    info = _FakeInfo()
    serial = "FAKE123"

    def run_header_cards(self, w, h):
        return [
            ("CAMVENDR", "fake", "camera vendor"),
            ("CAMID", self.serial, "camera serial"),
            ("DATASEC", f"[1:{w},1:{h}]", "image section"),
        ]


def test_validate_fills_missing_with_unknown():
    out = _validate_required({"INSTRUME": "x"})
    for k in REQUIRED_HEADER_KEYS:
        assert k in out
    assert out["SWCREATE"] == "UNKNOWN"
    assert out["INSTRUME"] == "x"


def test_validate_passthrough_when_complete():
    full = {k: 1 for k in REQUIRED_HEADER_KEYS}
    assert _validate_required(full) is full   # no copy when complete


def test_controller_metadata_covers_required(controller):
    """Normal operation must produce every required key for real
    (no UNKNOWN fills) — modulo GAIN, which comes from the control
    snapshot of a connected camera."""
    controller.instrument_name = "summer"
    controller._camera = _FakeCam()
    controller._record_start_utc = datetime(2026, 8, 15, 1, 2, 3)
    controller._requested_exposure_s = 0.5   # normally set via set_exposure

    cube = np.zeros((2, 32, 48), dtype=np.uint16)
    meta = controller._build_record_metadata(cube, elapsed=1.0)
    meta["GAIN"] = 10   # supplied by the settings snapshot when connected

    missing = [k for k in REQUIRED_HEADER_KEYS if k not in meta]
    assert not missing, f"controller metadata missing: {missing}"
    assert meta["INSTRUME"] == "summer"
    assert meta["DETECTOR"][0] == "FakeCam"
    assert meta["CAMID"][0] == "FAKE123"
    assert meta["TIMESYS"][0] == "UTC"
    assert meta["TIMESRC"][0] == "host"
    assert meta["DATE-OBS"][0].startswith("2026-08-15T01:02:03")
    assert meta["XBINNING"][0] == 1
    controller._camera = None   # avoid shutdown() touching the fake


def test_saved_file_contains_required_keys(run_save, tmp_path):
    p = str(tmp_path / "req.fits")
    run_save(save_fits_cube, p, np.zeros((1, 8, 8), np.uint16),
             {"INSTRUME": "t"})
    with fits.open(p) as h:
        for k in REQUIRED_HEADER_KEYS:
            assert k in h[0].header, k
