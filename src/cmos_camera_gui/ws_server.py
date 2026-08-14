"""
JSON WebSocket command interface for remote scripting.

Runs an asyncio event loop in a daemon thread. Commands are dispatched to
the ``CameraController`` (marshalled onto its owning Qt thread via a signal
bridge, so remote and GUI operation share one code path).

Protocol
--------
All messages are JSON objects with a "cmd" key:

    {"cmd": "status"}
    {"cmd": "list_cameras"}
    {"cmd": "connect_camera", "index": 0}
    {"cmd": "disconnect_camera"}
    {"cmd": "set", "Exposure": 50000, "Gain": 200, ...}
    {"cmd": "start_stream"}  /  {"cmd": "stop_stream"}
    {"cmd": "record", "n_frames": 100,
                      "directory": "D:/data",
                      "basename": "capture",
                      "mode": "stack",       # or "individual"
                      "obstype": "LIGHT",    # optional; sets OBSTYPE header
                      "extra_headers": [     # optional; [[key, val, comment|null], ...]
                          ["FILTER", "Halpha", "narrowband"],
                          ["OBJECT", "M42", null]
                      ]}
    {"cmd": "abort"}
    {"cmd": "cooler", "on": true, "target": -10}
    {"cmd": "clear_error"}

The "status" reply includes ``camera_state`` (READY / EXPOSING / ERROR /
TEC_SETTLING / ...) and ``tec_locked`` for telescope-system polling.

For "record", the server sends two messages:
  1. Immediate ack with the final directory/basename/mode.
  2. A {"cmd": "record_done", "message": ...} when the FITS save finishes.

Example client::

    import asyncio, websockets, json
    async def main():
        async with websockets.connect("ws://localhost:8765") as ws:
            await ws.send(json.dumps({"cmd": "status"}))
            print(json.loads(await ws.recv()))
    asyncio.run(main())
"""

import asyncio
import json
import logging
import threading

from PyQt5.QtCore import QObject, pyqtSignal

try:
    import websockets
    import websockets.server
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

log = logging.getLogger("cmoscam.ws")


class _WsBridge(QObject):
    """Thread-safe bridge: WS thread -> controller (Qt) thread via signal."""
    dispatch = pyqtSignal(object)


class WebSocketServer:
    """Manages the asyncio WS server in a background thread."""

    def __init__(self, controller, port=8765):
        if not HAS_WEBSOCKETS:
            raise ImportError("pip install websockets")
        self._controller = controller
        self._port = port
        self._loop = None
        self._thread = None
        # Created on the controller's thread so the signal queues there.
        self._bridge = _WsBridge()
        self._bridge.dispatch.connect(lambda fn: fn())

    def start(self):
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="WS-Server"
        )
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        start = websockets.server.serve(
            self._handler, "0.0.0.0", self._port,
        )
        self._loop.run_until_complete(start)
        log.info("WebSocket server listening on port %d", self._port)
        self._loop.run_forever()

    async def _handler(self, ws, _path=None):
        log.info("WS client connected: %s", ws.remote_address)
        try:
            async for raw in ws:
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"error": "invalid JSON"}))
                    continue

                # For record, register the done-callback BEFORE dispatch so
                # the save-finished notification can never be missed.
                done_event = None
                done_result = [None]
                if cmd.get("cmd") == "record":
                    done_event = asyncio.Event()
                    loop = self._loop

                    def on_done(msg):
                        done_result[0] = {"cmd": "record_done", "message": msg}
                        if loop:
                            loop.call_soon_threadsafe(done_event.set)

                    self._dispatch_sync(
                        lambda: self._controller.set_record_done_callback(
                            on_done
                        )
                    )

                result = await self._dispatch(cmd)
                await ws.send(json.dumps(result, default=str))

                if done_event is not None:
                    if "error" in result:
                        # Record never started; drop the callback.
                        self._dispatch_sync(
                            lambda: self._controller.set_record_done_callback(
                                None
                            )
                        )
                        continue
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=600)
                        await ws.send(json.dumps(done_result[0], default=str))
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({
                            "cmd": "record_done",
                            "error": "recording timed out",
                        }))

        except websockets.exceptions.ConnectionClosed:
            log.info("WS client disconnected")

    def _dispatch_sync(self, fn):
        """Run fn on the controller thread; don't wait for a result."""
        self._bridge.dispatch.emit(fn)

    async def _dispatch(self, cmd):
        """Execute a command on the controller thread and return the result."""
        result_event = threading.Event()
        result_holder = [{"error": "timeout"}]

        def _on_controller_thread():
            try:
                result_holder[0] = self._controller.handle_command(cmd)
            except Exception as e:
                log.exception("command failed: %s", cmd)
                result_holder[0] = {"error": str(e)}
            result_event.set()

        self._bridge.dispatch.emit(_on_controller_thread)

        while not result_event.wait(timeout=0.05):
            await asyncio.sleep(0.01)

        return result_holder[0]

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
