"""
Synchronous Python client for the GUI's WebSocket command server.

Launch the GUI with a port::

    python -m asi_stream_gui --ws-port 8765

Then from any other Python process::

    from asi_stream_gui.client import ASIClient

    with ASIClient("ws://localhost:8765") as cam:
        cam.set(Exposure=50_000, Gain=200)
        cam.start_stream()
        cam.record(20, directory="./captures", basename="demo")
        cam.stop_stream()

The client owns one persistent connection and serializes JSON on/off the wire.
All methods are blocking; `record` waits for the background FITS save to finish
before returning.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Dict, Optional

from websockets.sync.client import connect as _ws_connect


class ASIClientError(RuntimeError):
    """Raised when the GUI reports an error or a record save fails."""


class ASIClient(AbstractContextManager):
    """Blocking client over the GUI's JSON-WebSocket protocol."""

    def __init__(self, url: str = "ws://localhost:8765", timeout: float = 5.0):
        self._url = url
        self._timeout = timeout
        self._ws = None

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> "ASIClient":
        if self._ws is None:
            self._ws = _ws_connect(self._url, open_timeout=self._timeout)
        return self

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def __enter__(self) -> "ASIClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- raw protocol ----------------------------------------------------

    def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Send one command, return one reply, raise on protocol errors."""
        if self._ws is None:
            raise ASIClientError("client is not connected")
        self._ws.send(json.dumps(cmd))
        reply = json.loads(self._ws.recv())
        if isinstance(reply, dict) and "error" in reply:
            raise ASIClientError(reply["error"])
        return reply

    def _recv(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        return json.loads(self._ws.recv(timeout=timeout))

    # -- commands --------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Connection state + full control snapshot."""
        return self._send({"cmd": "status"})

    def list_cameras(self) -> list:
        """Enumerate attached ASI cameras. Returns [{"index": int, "name": str}, ...]."""
        reply = self._send({"cmd": "list_cameras"})
        return reply.get("cameras", [])

    def connect_camera(self, index: int = 0) -> Dict[str, Any]:
        """Open the camera at the given driver index (from `list_cameras`)."""
        reply = self._send({"cmd": "connect_camera", "index": int(index)})
        if not reply.get("ok"):
            raise ASIClientError(reply.get("error", "connect_camera failed"))
        return reply

    def disconnect_camera(self) -> Dict[str, Any]:
        return self._send({"cmd": "disconnect_camera"})

    def set(self, **params: Any) -> Dict[str, Any]:
        """
        Push control values. Keys are control names as reported by `status()`.

        Special keys:
            img_type : "RAW8" | "RAW16"
            roi_w, roi_h, roi_x, roi_y : int

        Exposure is in microseconds (50_000 = 50 ms).
        """
        return self._send({"cmd": "set", **params})

    def start_stream(self) -> Dict[str, Any]:
        return self._send({"cmd": "start_stream"})

    def stop_stream(self) -> Dict[str, Any]:
        return self._send({"cmd": "stop_stream"})

    def record(
        self,
        n_frames: int,
        directory: Optional[str] = None,
        basename: Optional[str] = None,
        mode: str = "stack",
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Capture `n_frames` and block until the FITS save completes.

        Parameters
        ----------
        n_frames : int
        directory : str, optional
            Output directory; created if missing. Defaults to the GUI's current
            setting.
        basename : str, optional
            File basename (no extension). Defaults to the GUI's current setting.
        mode : {"stack", "individual"}
            "stack" -> one cube FITS; "individual" -> one file per frame.

        Returns
        -------
        dict
            The `record_done` message with a `"message"` field describing the
            saved files.

        Raises
        ------
        ASIClientError
            On protocol error, timeout, or save failure.
        """
        if mode not in ("stack", "individual"):
            raise ValueError(f"mode must be 'stack' or 'individual', got {mode!r}")

        cmd: Dict[str, Any] = {"cmd": "record", "n_frames": int(n_frames), "mode": mode}
        if directory is not None:
            cmd["directory"] = directory
        if basename is not None:
            cmd["basename"] = basename

        ack = self._send(cmd)  # immediate ack
        done = self._recv(timeout=timeout)  # record_done from save thread
        if "error" in done:
            raise ASIClientError(done["error"])
        # Surface save failures reported as a message
        msg = done.get("message", "")
        if isinstance(msg, str) and msg.startswith("FITS save error"):
            raise ASIClientError(msg)
        done["_ack"] = ack
        return done

    def capture_frames(
        self,
        n_frames: int,
        directory: Optional[str] = None,
        basename: Optional[str] = None,
        stack: bool = True,
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Record `n_frames`, auto-starting the stream if it isn't already running.

        Convenience wrapper for headless scripts. Leaves the stream running on
        exit so the next call doesn't pay the stream-startup cost and a human
        watching the GUI keeps their live preview. Call `stop_stream()`
        yourself when you're done.
        """
        if not self.status().get("streaming", False):
            self.start_stream()
        return self.record(
            n_frames,
            directory=directory,
            basename=basename,
            mode="stack" if stack else "individual",
            timeout=timeout,
        )

    def cooler(self, on: bool, target: Optional[int] = None) -> Dict[str, Any]:
        """Turn the TEC on/off and optionally set target °C."""
        cmd: Dict[str, Any] = {"cmd": "cooler", "on": bool(on)}
        if target is not None:
            cmd["target"] = int(target)
        return self._send(cmd)
