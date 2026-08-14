"""
Entry point: python -m cmos_camera_gui [--sdk PATH] [--ws-port PORT]

CMOS Control GUI — multi-vendor camera control.
"""

import argparse
import logging
import sys

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from .core.controller import CameraController
from .style import DARK_STYLE
from .gui import MainWindow


def main():
    parser = argparse.ArgumentParser(
        description="CMOS Control GUI (PyQt5 + direct ctypes SDKs)"
    )
    parser.add_argument(
        "--sdk", metavar="PATH",
        help="Path to ASICamera2.dll / libASICamera2.so",
    )
    parser.add_argument(
        "--ws-port", type=int, default=0, metavar="PORT",
        help="Enable WebSocket command server on PORT (e.g. 8765)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="JSON config file (ws_port, instrument, auto_connect, "
             "headless, idle_exposure_us)",
    )
    parser.add_argument(
        "--instrument", metavar="NAME",
        help="Instrument name for FITS INSTRUME / status camname "
             "(e.g. 'summer' at the telescope; default: camera model)",
    )
    parser.add_argument(
        "--auto-connect", metavar="VENDOR", nargs="?", const="any",
        help="Connect the first camera at startup (optionally a specific "
             "vendor: zwo|qhy). Required for unattended service operation.",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run controller + WebSocket server without the GUI window "
             "(service mode; requires --ws-port and --auto-connect)",
    )
    args = parser.parse_args()

    # Config file provides defaults; CLI flags win.
    cfg = {}
    if args.config:
        import json
        with open(args.config) as f:
            cfg = json.load(f)
    ws_port = args.ws_port or int(cfg.get("ws_port", 0))
    instrument = args.instrument or cfg.get("instrument")
    auto_connect = args.auto_connect or cfg.get("auto_connect")
    headless = args.headless or bool(cfg.get("headless"))

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    # Controller first (headless command surface), then the GUI as its view.
    # SDK load is deferred until the window exists so its status message
    # lands in the status bar.
    controller = CameraController(sdk_path=False)
    if instrument:
        controller.instrument_name = str(instrument)
    if "idle_exposure_us" in cfg:
        controller.idle_exposure_us = int(cfg["idle_exposure_us"])

    win = None
    ws_server = None
    if headless:
        if not ws_port:
            parser.error("--headless requires --ws-port (or ws_port in config)")
        from .ws_server import WebSocketServer
        ws_server = WebSocketServer(controller, ws_port)
        ws_server.start()
        logging.info("Headless mode: WS server on port %d", ws_port)
    else:
        win = MainWindow(controller, ws_port=ws_port)
        win.show()

    controller.load_sdk(args.sdk)

    if auto_connect:
        def _try_connect():
            try:
                cams = controller.list_cameras()
                if auto_connect != "any":
                    cams = [c for c in cams
                            if c["vendor"] == str(auto_connect).lower()]
                if cams:
                    controller.connect_camera(cams[0]["index"])
                    logging.info("Auto-connected: %s", cams[0]["name"])
                else:
                    logging.warning("Auto-connect: no matching camera found")
            except Exception as e:
                logging.error("Auto-connect failed: %s", e)

        QTimer.singleShot(500, _try_connect)

    rc = app.exec_()
    if headless and ws_server:
        ws_server.stop()
        controller.shutdown()
    sys.exit(rc)


if __name__ == "__main__":
    main()
