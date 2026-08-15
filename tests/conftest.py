"""Shared fixtures: one QApplication for the whole session, no hardware.

All tests here run without any camera or vendor SDK present.
"""

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.fixture(scope="session")
def qapp():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def controller(qapp):
    """A CameraController with no SDKs loaded."""
    from cmos_camera_gui.core.controller import CameraController
    c = CameraController(sdk_path=False)
    yield c


@pytest.fixture
def run_save(qapp):
    """Drive a recorder save (worker thread + Qt bridge) to completion."""
    from PyQt5.QtCore import QEventLoop, QTimer

    def _run(fn, *args, timeout_ms=15000, **kw):
        loop = QEventLoop()
        msgs = []

        def done(m):
            msgs.append(m)
            loop.quit()

        fn(*args, on_done=done, **kw)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec_()
        assert msgs, "save never completed"
        return msgs[0]

    return _run
