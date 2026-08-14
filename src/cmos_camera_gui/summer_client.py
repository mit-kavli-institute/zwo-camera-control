"""
SummerClient — the WSP-facing client (SummerCameraGuiHandoff §2.1).

Exposes the pirtcam ``CameraClient`` method names and semantics over this
GUI's JSON-WebSocket transport, so the WSP summer camera daemon can be a
near-copy of ``spring_camera_daemon.py``:

    from cmos_camera_gui.summer_client import SummerClient

    cam = SummerClient(host="localhost", port=8765)
    cam.connect()
    cam.get_status()                  # {"status": "success", "data": {...}}
    cam.set_exposure(7.5)             # float SECONDS, echoed exactly
    cam.set_save_path("~/data/images/20260814/summer")
    cam.capture_frames(filename="img_0001", nframes=1, object="M42",
                       observer="wsp", headers=[["FIELDID", 42, "card"]],
                       wait_for_completion=False)
    # ... poll get_status() until data["is_capturing"] is False ...
    cam.set_tec_temperature(-5.0)
    cam.set_tec_enabled(True)

All replies are ``{"status": "success"|"error", "message": ...}`` dicts
(get_status adds ``"data"``); methods never raise on camera-side failure —
they report it, per the daemon's error convention. Transport errors do
raise, which the daemon treats as a dead connection and reconnects.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from websockets.sync.client import connect as _ws_connect


class SummerClient:
    """pirtcam-compatible client over the GUI's WebSocket protocol."""

    def __init__(self, host: str = "localhost", port: int = 8765,
                 timeout: float = 5.0):
        self._url = f"ws://{host}:{port}"
        self._timeout = timeout
        self._ws = None

    # -- lifecycle -------------------------------------------------------

    def connect(self):
        if self._ws is None:
            self._ws = _ws_connect(self._url, open_timeout=self._timeout)
        return self

    def close(self):
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    # -- transport -------------------------------------------------------

    def _send(self, cmd: Dict[str, Any],
              timeout: Optional[float] = None) -> Dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("client is not connected")
        self._ws.send(json.dumps(cmd))
        reply = json.loads(self._ws.recv(timeout=timeout or self._timeout))
        # Normalize legacy error shape to the WSP convention.
        if isinstance(reply, dict) and "status" not in reply:
            if "error" in reply:
                return {"status": "error", "message": str(reply["error"])}
            return {"status": "success", "message": "", **reply}
        return reply

    # -- WSP contract methods (§2.1) -------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Cached status snapshot; fast, never blocks on hardware."""
        return self._send({"cmd": "get_status"})

    def set_exposure(self, exposure: float) -> Dict[str, Any]:
        """Request exposure time in float SECONDS (echoed exactly)."""
        return self._send({"cmd": "set_exposure",
                           "exposure": float(exposure)})

    def set_save_path(self, path: str) -> Dict[str, Any]:
        """Set the output directory (created if missing; ~ ok)."""
        return self._send({"cmd": "set_save_path", "path": str(path)})

    def capture_frames(
        self,
        filename: str,
        nframes: int = 1,
        object: Optional[str] = None,       # noqa: A002 (contract name)
        observer: Optional[str] = None,
        headers: Optional[List] = None,
        wait_for_completion: bool = False,
        debug: bool = False,
    ) -> Dict[str, Any]:
        """Start a capture; returns the ACK immediately (never blocks).

        The file appears at ``<save_path>/<filename>.fits`` — a single 2D
        image for nframes=1, a single cube file for nframes>1. Completion
        is detected by polling ``get_status()`` for ``is_capturing`` False.
        (``wait_for_completion``/``debug`` accepted for signature
        compatibility; waiting is always the caller's job.)
        """
        return self._send({
            "cmd": "capture",
            "filename": str(filename),
            "nframes": int(nframes),
            "object": object,
            "observer": observer,
            "headers": headers,
        })

    def set_tec_enabled(self, enabled: bool) -> Dict[str, Any]:
        return self._send({"cmd": "set_tec_enabled",
                           "enabled": bool(enabled)})

    def set_tec_temperature(self, temperature: float) -> Dict[str, Any]:
        return self._send({"cmd": "set_tec_temperature",
                           "temperature": float(temperature)})
