"""
JSON WebSocket command interface for remote scripting.

Runs an asyncio event loop in a daemon thread. Commands are dispatched
to the Qt GUI thread via a signal bridge (thread-safe).

Protocol
--------
All messages are JSON objects with a "cmd" key:

    {"cmd": "status"}
    {"cmd": "set", "exposure_ms": 50.0, "gain": 200, ...}
    {"cmd": "start_stream"}  /  {"cmd": "stop_stream"}
    {"cmd": "record", "n_frames": 100, "path": "cube.fits"}
    {"cmd": "cooler", "on": true, "target": -10}

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

log = logging.getLogger("asi_demo.ws")


class _WsBridge(QObject):
    """Thread-safe bridge: WS thread -> Qt GUI thread via signal."""
    dispatch = pyqtSignal(object)


class WebSocketServer:
    """Manages the asyncio WS server in a background thread."""

    def __init__(self, app, port=8765):
        if not HAS_WEBSOCKETS:
            raise ImportError("pip install websockets")
        self._app = app
        self._port = port
        self._loop = None
        self._thread = None
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

                result = await self._dispatch(cmd)
                await ws.send(json.dumps(result, default=str))

                # For recording, send a second message when FITS save completes
                if cmd.get("cmd") == "record" and "error" not in result:
                    done_event = asyncio.Event()
                    done_result = [None]

                    def on_done(msg):
                        done_result[0] = {
                            "cmd": "record_done", "message": msg
                        }
                        if self._loop:
                            self._loop.call_soon_threadsafe(done_event.set)

                    self._app._ws_record_done_cb = on_done
                    try:
                        await asyncio.wait_for(
                            done_event.wait(), timeout=600
                        )
                        await ws.send(
                            json.dumps(done_result[0], default=str)
                        )
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({
                            "cmd": "record_done",
                            "error": "recording timed out",
                        }))

        except websockets.exceptions.ConnectionClosed:
            log.info("WS client disconnected")

    async def _dispatch(self, cmd):
        """Execute a command on the GUI thread and return the result."""
        result_event = threading.Event()
        result_holder = [{"error": "timeout"}]

        def _on_gui_thread():
            result_holder[0] = self._app.handle_ws_command(cmd)
            result_event.set()

        self._bridge.dispatch.emit(_on_gui_thread)

        while not result_event.wait(timeout=0.05):
            await asyncio.sleep(0.01)

        return result_holder[0]

    def stop(self):
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
