"""
Entry point: python -m cmos_camera_gui [--sdk PATH] [--ws-port PORT]

CMOS Control GUI — multi-vendor camera control.
"""

import argparse
import logging
import sys

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
    args = parser.parse_args()

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
    win = MainWindow(controller, ws_port=args.ws_port)
    win.show()
    controller.load_sdk(args.sdk)
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
