"""
Entry point: python -m zwo_camera_gui [--sdk PATH] [--ws-port PORT]
"""

import argparse
import logging
import sys

from PyQt5.QtWidgets import QApplication

from .style import DARK_STYLE
from .gui import MainWindow


def main():
    parser = argparse.ArgumentParser(
        description="ZWO ASI streaming demo (PyQt5 + direct ctypes)"
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

    win = MainWindow(sdk_path=args.sdk, ws_port=args.ws_port)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
