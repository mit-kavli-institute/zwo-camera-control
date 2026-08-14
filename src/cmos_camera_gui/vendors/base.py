"""
Vendor abstraction (Phase-2 pragmatic form).

A Vendor bundles: SDK loading/enumeration, a camera-adapter factory, a
capture-worker class, and a VendorProfile of vendor-specific tuning.

Camera adapters expose a duck-typed surface the controller and workers
rely on (see ZwoCamera / QhyCamera):

    info                     .name .max_width .max_height .is_cooler
                             .is_usb3 .bit_depth
    get_caps_dict()          {name: caps_dict} for core.camera_config
    capabilities()           {"has_cooler","has_gps","img_types",...}
    set_ctrl(id, v) / get_ctrl_value(id)
    apply_roi(x, y, w, h, img_type_str) / get_roi()
    frame_dtype()
    start_video() / stop_video()
    temperature() -> float (NaN when untrustworthy)
    cooler_power() -> float | None
    set_cooler(on, target_c) / thermal_keepalive()
    get_dropped()
    run_header_cards(w, h) -> [(key, value, comment), ...]
    close()

Workers share the CaptureWorker signal surface (stats_update,
readonly_update, recording_progress, recording_done, error), a
frame_queue, run_async(), start_recording(..., gate=), cancel_recording()
and recent_median_delta(). The worker thread is the sole SDK owner while
streaming.
"""

from __future__ import annotations

from ..core.thermal import TecConfig


class VendorProfile:
    """Per-vendor tuning knobs. Subclass per vendor."""

    # Overrides for SDK-reported control defaults ({control_name: value}).
    default_overrides: dict = {}

    # TEC settle/stall detection tuning (core.thermal).
    tec_config = TecConfig()

    # Expected overhead beyond the exposure in the steady frame cadence
    # (readout + transfer), used to seed the record gate when no
    # measurement is available yet.
    cadence_overhead_s: float = 0.3

    def post_connect(self, settings) -> None:
        """Adjust CameraSettings right after connect (e.g. max out USB)."""
