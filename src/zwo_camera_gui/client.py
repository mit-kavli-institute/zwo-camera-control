"""
Synchronous Python client for the GUI's WebSocket command server.

Works identically against either camera backend (ZWO ASI or QHY42). The
backend is reported by ``status()`` and can be switched at runtime with
``set_backend()``.

Launch the GUI with a port::

    python -m zwo_camera_gui --ws-port 8765

Then from any other Python process::

    from zwo_camera_gui.client import CameraClient

    with CameraClient("ws://localhost:8765") as cam:
        cam.set_backend("qhy")          # optional -- picks ASI or QHY
        cam.connect_camera(0)
        cam.set(Exposure=50_000, Gain=30)
        cam.start_stream()
        cam.record(20, directory="./captures", basename="demo")
        cam.stop_stream()

``ASIClient`` is kept as an alias of ``CameraClient`` for backward compat.

The client owns one persistent connection and serializes JSON on/off the wire.
All methods are blocking; `record` waits for the background FITS save to finish
before returning.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from typing import Any, Dict, Iterable, List, Optional, Union

from websockets.sync.client import connect as _ws_connect


class CameraClientError(RuntimeError):
    """Raised when the GUI reports an error or a record save fails."""


# Backward-compat alias.
ASIClientError = CameraClientError


# Accepted shapes for `extra_headers`:
#   dict: {"KEY": value} or {"KEY": (value, "comment")}
#   iterable of:
#     - (key, value)
#     - (key, value, comment)
#     - astropy.io.fits.Card (or anything with .keyword/.value/.comment)
HeaderLike = Union[
    Dict[str, Any],
    Iterable[Any],
]


def _normalize_headers(headers: HeaderLike) -> List[List[Any]]:
    """Normalize flexible header input into [[key, value, comment|None], ...]
    — a JSON-safe form the WS server can hand straight to astropy."""
    if headers is None:
        return []

    out: List[List[Any]] = []

    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(v, tuple) and len(v) == 2:
                out.append([str(k), v[0], v[1]])
            else:
                out.append([str(k), v, None])
        return out

    for item in headers:
        # Duck-type astropy Card
        if hasattr(item, "keyword") and hasattr(item, "value"):
            cmt = getattr(item, "comment", "") or None
            out.append([str(item.keyword), item.value, cmt])
            continue
        if isinstance(item, (tuple, list)):
            if len(item) == 2:
                out.append([str(item[0]), item[1], None])
            elif len(item) == 3:
                out.append([str(item[0]), item[1], item[2]])
            else:
                raise ValueError(
                    f"header tuple must be (key, val) or (key, val, comment), "
                    f"got {item!r}"
                )
            continue
        raise TypeError(f"unrecognized header item: {item!r}")
    return out


class CameraClient(AbstractContextManager):
    """Blocking client over the GUI's JSON-WebSocket protocol.

    Backend-neutral: works against either the ASI or QHY backend. Use
    ``backend()`` to query and ``set_backend()`` to switch at runtime.
    """

    def __init__(self, url: str = "ws://localhost:8765", timeout: float = 5.0):
        self._url = url
        self._timeout = timeout
        self._ws = None

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> "CameraClient":
        if self._ws is None:
            self._ws = _ws_connect(self._url, open_timeout=self._timeout)
        return self

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def __enter__(self) -> "CameraClient":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- raw protocol ----------------------------------------------------

    def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        """Send one command, return one reply, raise on protocol errors."""
        if self._ws is None:
            raise CameraClientError("client is not connected")
        self._ws.send(json.dumps(cmd))
        reply = json.loads(self._ws.recv())
        if isinstance(reply, dict) and "error" in reply:
            raise CameraClientError(reply["error"])
        return reply

    def _recv(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        return json.loads(self._ws.recv(timeout=timeout))

    # -- commands --------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Connection state + full control snapshot.

        Response includes ``"backend": "asi" | "qhy"`` so a client can tell
        which camera type the server is currently driving.
        """
        return self._send({"cmd": "status"})

    def backend(self) -> str:
        """Return the current camera backend ('asi' or 'qhy')."""
        reply = self._send({"cmd": "backend"})
        return reply.get("backend", "")

    def set_backend(self, backend: str) -> Dict[str, Any]:
        """Switch the active camera backend ('asi' or 'qhy').

        Disconnects the current camera (if any) before switching. Idempotent
        when called with the already-active backend.
        """
        b = str(backend).lower()
        if b not in ("asi", "qhy"):
            raise ValueError(f"backend must be 'asi' or 'qhy', got {backend!r}")
        reply = self._send({"cmd": "backend", "backend": b})
        if not reply.get("ok", True):
            raise CameraClientError(reply.get("error", "set_backend failed"))
        return reply

    def list_cameras(self) -> list:
        """Enumerate attached cameras for the current backend.

        Returns ``[{"index": int, "name": str}, ...]`` -- same shape for ASI
        and QHY backends.
        """
        reply = self._send({"cmd": "list_cameras"})
        return reply.get("cameras", [])

    def connect_camera(self, index: int = 0) -> Dict[str, Any]:
        """Open the camera at the given driver index (from `list_cameras`)."""
        reply = self._send({"cmd": "connect_camera", "index": int(index)})
        if not reply.get("ok"):
            raise CameraClientError(reply.get("error", "connect_camera failed"))
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
        obstype: Optional[str] = None,
        extra_headers: Optional[HeaderLike] = None,
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
        obstype : str, optional
            Value for the OBSTYPE header keyword, e.g. "LIGHT", "DARK", "FLAT".
        extra_headers : dict | iterable, optional
            Additional FITS header entries. Accepts:
              - dict: ``{"KEY": value}`` or ``{"KEY": (value, "comment")}``
              - iterable of ``(key, value)`` / ``(key, value, comment)``
              - iterable of ``astropy.io.fits.Card`` objects
            Applied *after* the auto-filled metadata, so these win on collision.

        Returns
        -------
        dict
            The `record_done` message with a `"message"` field describing the
            saved files.

        Raises
        ------
        CameraClientError
            On protocol error, timeout, or save failure.
        """
        if mode not in ("stack", "individual"):
            raise ValueError(f"mode must be 'stack' or 'individual', got {mode!r}")

        cmd: Dict[str, Any] = {"cmd": "record", "n_frames": int(n_frames), "mode": mode}
        if directory is not None:
            cmd["directory"] = directory
        if basename is not None:
            cmd["basename"] = basename
        if obstype is not None:
            cmd["obstype"] = str(obstype)
        if extra_headers is not None:
            cmd["extra_headers"] = _normalize_headers(extra_headers)

        ack = self._send(cmd)  # immediate ack
        done = self._recv(timeout=timeout)  # record_done from save thread
        if "error" in done:
            raise CameraClientError(done["error"])
        # Surface save failures reported as a message
        msg = done.get("message", "")
        if isinstance(msg, str) and msg.startswith("FITS save error"):
            raise CameraClientError(msg)
        done["_ack"] = ack
        return done

    def capture_frames(
        self,
        n_frames: int,
        directory: Optional[str] = None,
        basename: Optional[str] = None,
        stack: bool = True,
        obstype: Optional[str] = None,
        extra_headers: Optional[HeaderLike] = None,
        timeout: float = 600.0,
    ) -> Dict[str, Any]:
        """
        Record `n_frames`, auto-starting the stream if it isn't already running.

        Convenience wrapper for headless scripts. Leaves the stream running on
        exit so the next call doesn't pay the stream-startup cost and a human
        watching the GUI keeps their live preview. Call `stop_stream()`
        yourself when you're done.

        See `record()` for `obstype` and `extra_headers`.
        """
        if not self.status().get("streaming", False):
            self.start_stream()
        return self.record(
            n_frames,
            directory=directory,
            basename=basename,
            mode="stack" if stack else "individual",
            obstype=obstype,
            extra_headers=extra_headers,
            timeout=timeout,
        )

    def cooler(self, on: bool, target: Optional[int] = None) -> Dict[str, Any]:
        """Turn the TEC on/off and optionally set target °C."""
        cmd: Dict[str, Any] = {"cmd": "cooler", "on": bool(on)}
        if target is not None:
            cmd["target"] = int(target)
        return self._send(cmd)


# Backward-compat alias. New code should prefer ``CameraClient``.
ASIClient = CameraClient
