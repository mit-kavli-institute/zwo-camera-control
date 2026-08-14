"""
Main application window — a *view* over ``core.controller.CameraController``.

All camera operation (SDK, connection, settings, streaming, recording,
cooler, remote commands) lives in the controller; this module only renders
controller signals and pushes user input into controller methods. Nothing
here may import vendor SDK modules.

Camera controls are built dynamically from what each camera actually
reports, so it works correctly with the ASI294MM Pro, ASI662MM, and
ASI990MM Pro (which has no offset and has independent frame rate control).
"""

import logging
import math
import os
import queue
import time

from PyQt5.QtCore import Qt, QTimer, pyqtSlot
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSlider, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from .camera_config import ControlKind, ControlSpec, FLIP_LABELS
from .core.controller import CameraController
from .stretch import STRETCH_FUNCS
from .recorder import HAS_ASTROPY
from .widgets import HistogramWidget, ImageDisplay

log = logging.getLogger("cmoscam.gui")

# Maximum slider range before we switch to a spinbox.
_SLIDER_MAX_RANGE = 4096

# Exposure unit choices: (label, µs-per-unit)
_EXP_UNITS = [("µs", 1), ("ms", 1_000), ("s", 1_000_000)]

# camera_state -> stats-bar chip color
_STATE_COLORS = {
    "DISCONNECTED": "#555",
    "INITIALIZING": "#ffaa00",
    "TEC_SETTLING": "#00aaff",
    "READY": "#00e87a",
    "EXPOSING": "#ff4444",
    "SAVING": "#ffaa00",
    "ERROR": "#ff2222",
}


# =====================================================================
#  Dynamic control widget
# =====================================================================

class ControlWidget(QWidget):
    """Appropriate PyQt5 widget for one ControlSpec.

    BOOLEAN              -> QCheckBox
    EXPOSURE/FRAME_RATE  -> QSpinBox  (large numeric range)
    INTEGER (small)      -> QSlider + value label
    INTEGER (large)      -> QSpinBox
    TEMPERATURE/READONLY -> QLabel  (updated externally via update_readonly)
    """

    def __init__(self, spec: ControlSpec, on_change=None, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.on_change = on_change
        self._value_lbl = None
        self._input = None  # the slider, spinbox, or checkbox
        self._exp_unit_combo = None  # only set for EXPOSURE controls
        self._raw_exp_us = 0         # tracks raw µs for EXPOSURE controls
        self._build()

    def _build(self):
        s = self.spec
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(1)

        # Header row: label + value display
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)

        unit = ""
        if s.kind == ControlKind.FRAME_RATE:
            unit = " (fps)"

        lbl = QLabel(s.display_name + unit)
        lbl.setStyleSheet("color: #888; font: 9pt 'Courier New';")
        if s.description:
            self.setToolTip(s.description)
        hdr.addWidget(lbl)
        hdr.addStretch()

        if s.kind == ControlKind.BOOLEAN:
            cb = QCheckBox()
            cb.setChecked(bool(s.default_value))
            cb.stateChanged.connect(lambda _: self._fire())
            hdr.addWidget(cb)
            self._input = cb
            layout.addLayout(hdr)
            return

        if s.kind == ControlKind.FLIP:
            combo = QComboBox()
            for label in FLIP_LABELS:
                combo.addItem(label)
            idx = s.default_value if 0 <= s.default_value < len(FLIP_LABELS) else 0
            combo.setCurrentIndex(idx)
            combo.setStyleSheet(
                "color: #00e87a; font: bold 9pt 'Courier New'; "
                "background: #1a1a1a; border: 1px solid #333;"
            )
            combo.currentIndexChanged.connect(lambda _: self._fire())
            hdr.addWidget(combo)
            self._input = combo
            layout.addLayout(hdr)
            return

        if s.kind in (ControlKind.TEMPERATURE, ControlKind.READONLY):
            val = QLabel(s.display_value(s.default_value))
            val.setStyleSheet("color: #555; font: bold 9pt 'Courier New';")
            hdr.addWidget(val)
            self._value_lbl = val
            self._input = val
            layout.addLayout(hdr)
            return

        if s.kind == ControlKind.EXPOSURE:
            default_idx = (
                2 if s.default_value >= 1_000_000 else
                1 if s.default_value >= 1_000 else 0
            )
            unit_combo = QComboBox()
            for uname, _ in _EXP_UNITS:
                unit_combo.addItem(uname)
            unit_combo.setCurrentIndex(default_idx)
            unit_combo.setFixedWidth(48)
            unit_combo.setStyleSheet("color: #888; font: 9pt 'Courier New';")

            exp_spin = QDoubleSpinBox()
            exp_spin.setDecimals(3)
            exp_spin.setStyleSheet(
                "color: #00e87a; font: bold 9pt 'Courier New'; "
                "background: #1a1a1a; border: 1px solid #333;"
            )

            m0 = _EXP_UNITS[default_idx][1]
            exp_spin.setRange(s.min_value / m0, s.max_value / m0)
            self._raw_exp_us = s.default_value
            exp_spin.setValue(s.default_value / m0)

            self._exp_unit_combo = unit_combo

            def _on_unit_changed(idx):
                m = _EXP_UNITS[idx][1]
                exp_spin.blockSignals(True)
                exp_spin.setRange(s.min_value / m, s.max_value / m)
                exp_spin.setValue(self._raw_exp_us / m)
                exp_spin.blockSignals(False)

            def _on_spin_changed():
                m = _EXP_UNITS[self._exp_unit_combo.currentIndex()][1]
                self._raw_exp_us = self.spec.clamp(round(exp_spin.value() * m))
                self._fire()

            unit_combo.currentIndexChanged.connect(_on_unit_changed)
            exp_spin.valueChanged.connect(lambda _: _on_spin_changed())

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            row.addWidget(exp_spin, stretch=1)
            row.addWidget(unit_combo)

            layout.addLayout(hdr)
            layout.addLayout(row)
            self._input = exp_spin
            return

        rng = s.max_value - s.min_value
        use_slider = (
            rng <= _SLIDER_MAX_RANGE
            and s.kind not in (ControlKind.FRAME_RATE,)
        )

        if use_slider:
            # Spinbox in header for typed input, synced bidirectionally with slider
            val_spin = QSpinBox()
            val_spin.setRange(s.min_value, s.max_value)
            val_spin.setValue(s.default_value)
            val_spin.setFixedWidth(70)
            val_spin.setButtonSymbols(QSpinBox.NoButtons)
            val_spin.setStyleSheet(
                "color: #00e87a; font: bold 9pt 'Courier New'; "
                "background: #1a1a1a; border: 1px solid #333;"
            )
            hdr.addWidget(val_spin)
            layout.addLayout(hdr)

            slider = QSlider(Qt.Horizontal)
            slider.setRange(s.min_value, s.max_value)
            slider.setValue(s.default_value)
            slider.valueChanged.connect(val_spin.setValue)
            val_spin.valueChanged.connect(slider.setValue)
            slider.valueChanged.connect(lambda _: self._fire())
            layout.addWidget(slider)
            self._input = slider
        else:
            val = QLabel(s.display_value(s.default_value))
            val.setStyleSheet("color: #00e87a; font: bold 9pt 'Courier New';")
            val.setFixedWidth(90)
            val.setAlignment(Qt.AlignRight)
            hdr.addWidget(val)
            self._value_lbl = val
            layout.addLayout(hdr)

            spin = QSpinBox()
            spin.setRange(s.min_value, s.max_value)
            spin.setValue(s.default_value)
            inc = max(1, rng // 1000)
            spin.setSingleStep(inc)
            spin.valueChanged.connect(lambda _: self._fire())
            layout.addWidget(spin)
            self._input = spin

    def _fire(self):
        if self._value_lbl and not isinstance(self._input, QLabel):
            self._value_lbl.setText(self.spec.display_value(self.get_value()))
        if self.on_change:
            self.on_change(self.spec.name, self.get_value())

    def get_value(self) -> int:
        if isinstance(self._input, QCheckBox):
            return 1 if self._input.isChecked() else 0
        if isinstance(self._input, QDoubleSpinBox):
            return self._raw_exp_us
        if isinstance(self._input, QComboBox):
            return self.spec.clamp(self._input.currentIndex())
        if isinstance(self._input, QSlider):
            return self.spec.clamp(self._input.value())
        if isinstance(self._input, QSpinBox):
            return self.spec.clamp(self._input.value())
        return self.spec.default_value

    def set_value(self, v: int):
        v = self.spec.clamp(v)
        if isinstance(self._input, QCheckBox):
            self._input.setChecked(bool(v))
        elif isinstance(self._input, QDoubleSpinBox):
            self._raw_exp_us = v
            idx = self._exp_unit_combo.currentIndex()
            m = _EXP_UNITS[idx][1]
            self._input.setValue(v / m)
        elif isinstance(self._input, QComboBox):
            self._input.setCurrentIndex(v)
        elif isinstance(self._input, (QSlider, QSpinBox)):
            self._input.setValue(v)

    def update_readonly(self, raw: int):
        if self._value_lbl:
            self._value_lbl.setText(self.spec.display_value(raw))


# =====================================================================
#  Main window
# =====================================================================

class MainWindow(QMainWindow):

    def __init__(self, controller: CameraController, ws_port=0):
        super().__init__()
        self.setWindowTitle("CMOS Control GUI")
        self.setMinimumSize(950, 620)
        self.resize(1200, 750)

        self._c = controller
        self._ctrl_widgets = {}    # name -> ControlWidget
        self._last_raw_frame = None

        self._build_ui()
        self._connect_controller_signals()

        # Display poll timer — drains frame queue, self-rescheduling.
        # Single-shot avoids pileup if stretch takes longer than the interval.
        self._display_interval = 33  # ~30 Hz target
        self._display_timer = QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.timeout.connect(self._poll_frames)
        self._last_hist_time = 0.0  # rate-limit histogram to ~5 Hz

        self._c.display_stretch = self._stretch_combo.currentText()

        # Optional WebSocket server
        self._ws_server = None
        if ws_port > 0:
            self._start_ws_server(ws_port)

    # =====================================================================
    #  Controller signal wiring
    # =====================================================================

    def _connect_controller_signals(self):
        c = self._c
        c.status_message.connect(self._set_status)
        c.state_changed.connect(self._on_state_changed)
        c.connected_changed.connect(self._on_connected_changed)
        c.streaming_changed.connect(self._on_streaming_changed)
        c.control_changed.connect(self._on_remote_control_changed)
        c.roi_changed.connect(self._sync_roi_widgets)
        c.record_params_changed.connect(self._sync_record_widgets)
        c.record_started.connect(self._on_record_started)
        c.record_progress.connect(self._on_rec_progress)
        c.record_finished.connect(self._on_record_finished)
        c.record_cancelled.connect(self._on_record_cancelled)
        c.stats_update.connect(self._on_stats)
        c.thermal_update.connect(self._on_thermal_update)

        self._on_state_changed(c.camera_state.name)

    # =====================================================================
    #  SDK / connection actions
    # =====================================================================

    def _browse_sdk(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ASICamera2.dll or libASICamera2.so", "",
            "SDK library (*.dll *.so *.dylib);;All files (*)",
        )
        if not path:
            return
        self._c.load_sdk(path)

    def _refresh_cameras(self):
        if not self._c.sdk_loaded:
            self._set_status("Load SDK first")
            return
        self._cam_combo.clear()
        try:
            cams = self._c.list_cameras()
        except Exception as e:
            self._set_status(f"Camera scan failed: {e}")
            return
        if not cams:
            self._set_status("No cameras found")
            return
        for cam in cams:
            self._cam_combo.addItem(f"{cam['index']}: {cam['name']}",
                                    cam["index"])
        self._set_status(f"Found {len(cams)} camera(s)")

    def _connect(self):
        if not self._c.sdk_loaded:
            QMessageBox.warning(self, "No SDK", "Load the SDK first.")
            return
        idx = self._cam_combo.currentData()
        if idx is None:
            QMessageBox.warning(
                self, "No camera", "Click Refresh, then select a camera."
            )
            return
        try:
            self._c.connect_camera(idx)
        except Exception as e:
            QMessageBox.critical(self, "Connect Error", str(e))

    def _on_connected_changed(self, connected: bool):
        if connected:
            self._rebuild_controls_panel()
            self._sync_roi_ranges()
            self._sync_roi_widgets()

            cs = self._c.control_set
            self._cooler_group.setVisible(cs.has_cooler())
            if cs.has_cooler():
                spec = cs.get("TargetTemp")
                if spec:
                    self._cooler_temp.setRange(spec.min_value, spec.max_value)
                    self._cooler_temp.setValue(spec.default_value)

            self._connect_btn.setText("Disconnect")
            self._connect_btn.setStyleSheet("background-color: #5a1414;")
            self._connect_btn.clicked.disconnect()
            self._connect_btn.clicked.connect(self._c.disconnect_camera)
        else:
            self._clear_controls_panel()
            self._connect_btn.setText("Connect")
            self._connect_btn.setStyleSheet("background-color: #1a3a1a;")
            self._connect_btn.clicked.disconnect()
            self._connect_btn.clicked.connect(self._connect)
            self._cooler_group.setVisible(False)

    # =====================================================================
    #  Dynamic controls panel
    # =====================================================================

    def _rebuild_controls_panel(self):
        """Clear and rebuild the CAMERA CONTROLS group from the control set."""
        self._clear_controls_panel()
        cs = self._c.control_set
        if not cs:
            return

        layout = self._ctrl_group.layout()

        # Writable controls
        for spec in cs.writable():
            w = ControlWidget(spec, on_change=self._c.stage_control)
            layout.addWidget(w)
            self._ctrl_widgets[spec.name] = w
            # Reflect any value already staged in the controller (e.g.
            # BandWidth override applied at connect, or remote edits).
            val = self._c.settings.get(spec.name)
            if val is not None and val != w.get_value():
                w.set_value(val)

        # Read-only controls
        ro = cs.readonly()
        if ro:
            layout.addWidget(self._sep())
            lbl = QLabel("READ ONLY")
            lbl.setStyleSheet("color: #444; font: 7pt 'Courier New';")
            layout.addWidget(lbl)
            for spec in ro:
                w = ControlWidget(spec)
                layout.addWidget(w)
                self._ctrl_widgets[spec.name] = w

    def _clear_controls_panel(self):
        layout = self._ctrl_group.layout()
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self._ctrl_widgets.clear()

    def _on_remote_control_changed(self, name: str, value: int):
        w = self._ctrl_widgets.get(name)
        if w:
            w.set_value(value)

    # =====================================================================
    #  ROI / settings
    # =====================================================================

    def _sync_roi_ranges(self):
        cam = self._c.camera
        if not cam:
            return
        self._roi_w.setMaximum(cam.info.max_width)
        self._roi_h.setMaximum(cam.info.max_height)
        self._roi_x.setMaximum(cam.info.max_width - 8)
        self._roi_y.setMaximum(cam.info.max_height - 2)

    def _sync_roi_widgets(self):
        """Reflect controller ROI/img_type into the widgets."""
        roi = self._c.roi
        self._roi_w.setValue(roi["w"])
        self._roi_h.setValue(roi["h"])
        self._roi_x.setValue(roi["x"])
        self._roi_y.setValue(roi["y"])
        if self._c.img_type == "RAW16":
            self._raw16_rb.setChecked(True)
        else:
            self._raw8_rb.setChecked(True)

    def _push_roi(self):
        """Push widget ROI/img_type into the controller (GUI-origin)."""
        self._c.set_roi(
            x=self._roi_x.value(), y=self._roi_y.value(),
            w=self._roi_w.value(), h=self._roi_h.value(),
            img_type="RAW16" if self._raw16_rb.isChecked() else "RAW8",
            notify=False,
        )

    def _roi_full_frame(self):
        if self._c.connected:
            self._c.full_frame_roi()

    def _apply_settings(self):
        if not self._c.connected:
            QMessageBox.warning(self, "No camera", "Connect first.")
            return
        self._push_roi()
        try:
            self._c.apply_settings(silent=False)
        except Exception as e:
            QMessageBox.critical(self, "Settings Error", str(e))

    # =====================================================================
    #  Cooler
    # =====================================================================

    def _apply_cooler(self):
        try:
            self._c.set_cooler(
                on=self._cooler_on_cb.isChecked(),
                target_c=self._cooler_temp.value(),
            )
        except Exception as e:
            self._set_status(f"Cooler error: {e}")

    def _on_thermal_update(self, temp: float, power: float):
        self._cooler_readout.setText(
            f"Sensor: {temp:.1f} C   Power: {power:.0f}%"
        )

    # =====================================================================
    #  Streaming
    # =====================================================================

    def _start_stream(self):
        try:
            self._c.start_stream()
        except Exception as e:
            QMessageBox.warning(self, "Stream", str(e))

    def _on_streaming_changed(self, streaming: bool):
        if streaming:
            self._display_timer.start(self._display_interval)
            self._stream_btn.setText("■  Stop Stream")
            self._stream_btn.setStyleSheet("background-color: #5a1414;")
            self._stream_btn.clicked.disconnect()
            self._stream_btn.clicked.connect(self._c.stop_stream)
        else:
            self._display_timer.stop()
            self._stream_btn.setText("▶  Start Stream")
            self._stream_btn.setStyleSheet("background-color: #1a3a1a;")
            try:
                self._stream_btn.clicked.disconnect()
            except TypeError:
                pass
            self._stream_btn.clicked.connect(self._start_stream)

    # =====================================================================
    #  Frame / stats slots
    # =====================================================================

    def _poll_frames(self):
        """
        Called by single-shot QTimer at ~30 Hz.  Drains the frame queue,
        renders only the latest frame.  Re-arms itself after work completes
        so slow stretches can't cause pileup.
        """
        try:
            fq = self._c.frame_queue
            if fq is None:
                return
            frame = None
            # Drain to latest — discard stale frames
            try:
                while True:
                    frame = fq.get_nowait()
            except queue.Empty:
                pass
            if frame is None:
                return

            self._last_raw_frame = frame
            stretch_name = self._stretch_combo.currentText()
            stretch_fn = STRETCH_FUNCS.get(stretch_name, STRETCH_FUNCS["99.5%"])
            disp, z1, z2 = stretch_fn(frame)
            self._display.set_frame(frame, disp)

            # Histogram at ~5 Hz (expensive on large frames)
            now = time.monotonic()
            if now - self._last_hist_time >= 0.2:
                self._last_hist_time = now
                self._histogram.update_data(frame, z1, z2)
        finally:
            # Re-arm for next tick (fires AFTER this work completes)
            if self._c.streaming:
                self._display_timer.start(self._display_interval)

    @pyqtSlot(float, int, int, float)
    def _on_stats(self, fps, total, dropped, temp):
        self._fps_lbl.setText(f"FPS  {fps:.1f}")
        self._frames_lbl.setText(f"Frames  {total}")
        if dropped > 0:
            self._drop_lbl.setText(f"Dropped  {dropped}")
            self._drop_lbl.setStyleSheet(
                "color: #ff6633; font: bold 10pt 'Courier New';"
            )
        else:
            self._drop_lbl.setText("Dropped  0")
            self._drop_lbl.setStyleSheet(
                "color: #3a5a3a; font: bold 10pt 'Courier New';"
            )
        if not math.isnan(temp):
            self._temp_lbl.setText(f"{temp:.1f} C")

        # Update readonly control widgets (temperature, cooler power, etc.)
        for name, val in self._c.readonly_values().items():
            w = self._ctrl_widgets.get(name)
            if w:
                w.update_readonly(val)

    def _on_state_changed(self, name: str):
        color = _STATE_COLORS.get(name, "#aaa")
        self._state_lbl.setText(name)
        self._state_lbl.setStyleSheet(
            f"color: {color}; font: bold 10pt 'Courier New';"
        )

    # =====================================================================
    #  FITS recording
    # =====================================================================

    def _push_record_params(self):
        self._c.set_record_params(
            n_frames=self._nframes_spin.value(),
            directory=self._fits_dir.text().strip() or os.getcwd(),
            basename=self._fits_basename.text().strip() or "capture",
            mode="stack" if self._mode_stack_rb.isChecked() else "individual",
            notify=False,
        )

    def _sync_record_widgets(self):
        p = self._c.record_params
        self._nframes_spin.setValue(int(p["n_frames"]))
        self._fits_dir.setText(str(p["directory"]))
        self._fits_basename.setText(str(p["basename"]))
        if p["mode"] == "stack":
            self._mode_stack_rb.setChecked(True)
        else:
            self._mode_indiv_rb.setChecked(True)

    def _start_record(self):
        self._push_record_params()
        try:
            self._c.start_record()
        except Exception as e:
            QMessageBox.warning(self, "Record", str(e))

    def _on_record_started(self, n: int):
        self._rec_btn.setText("✕  Cancel")
        self._rec_btn.setStyleSheet("background-color: #5a1414;")
        self._rec_btn.clicked.disconnect()
        self._rec_btn.clicked.connect(self._c.cancel_record)
        self._rec_lbl.setText(f"REC  0/{n}")
        self._rec_progress.setValue(0)

    @pyqtSlot(int, int)
    def _on_rec_progress(self, got, target):
        if target > 0:
            self._rec_progress.setValue(int(got / target * 100))
            self._rec_lbl.setText(f"REC  {got}/{target}")

    def _on_record_finished(self, msg: str):
        self._reset_rec_button()
        self._rec_progress.setValue(100)
        self._rec_lbl.setText("DONE")

    def _on_record_cancelled(self):
        self._reset_rec_button()
        self._rec_progress.setValue(0)
        self._rec_lbl.setText("")

    def _reset_rec_button(self):
        self._rec_btn.setText("⬤  Record FITS")
        self._rec_btn.setStyleSheet("background-color: #3a1a2a;")
        try:
            self._rec_btn.clicked.disconnect()
        except TypeError:
            pass
        self._rec_btn.clicked.connect(self._start_record)

    # =====================================================================
    #  Pixel readout
    # =====================================================================

    def _on_pixel_info(self, x, y, val):
        self._pixel_lbl.setText(f"x {x}  y {y}  val {val}")

    def _on_pixel_left(self):
        self._pixel_lbl.setText("x --  y --  val --")

    # =====================================================================
    #  WebSocket server
    # =====================================================================

    def _start_ws_server(self, port):
        try:
            from .ws_server import WebSocketServer, HAS_WEBSOCKETS
            if not HAS_WEBSOCKETS:
                log.warning("websockets not installed -- WS server disabled")
                return
            self._ws_server = WebSocketServer(self._c, port)
            self._ws_server.start()
            self._set_status(f"WebSocket server on port {port}")
        except Exception as e:
            log.error("Failed to start WS server: %s", e)

    # =====================================================================
    #  Status bar
    # =====================================================================

    def _set_status(self, msg):
        self._status_lbl.setText(msg)

    # =====================================================================
    #  UI construction
    # =====================================================================

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # -- Sidebar (scrollable, fixed width) --
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(380)

        sidebar = QWidget()
        sidebar.setStyleSheet("background-color: #111;")
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(10, 10, 10, 10)
        sb.setSpacing(4)
        self._build_sidebar(sb)
        sb.addStretch()
        scroll.setWidget(sidebar)
        root.addWidget(scroll)

        # -- Main area --
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self._build_main(ml)
        root.addWidget(main, stretch=1)

    def _build_sidebar(self, sb):
        # Title
        title = QLabel("CMOS\nCONTROL")
        title.setStyleSheet("color: #00e87a; font: bold 15pt 'Courier New';")
        title.setAlignment(Qt.AlignCenter)
        sb.addWidget(title)
        sb.addWidget(self._sep())

        # == Camera ==
        grp = QGroupBox("CAMERA")
        gl = QVBoxLayout(grp)
        self._cam_combo = QComboBox()
        gl.addWidget(self._cam_combo)
        row = QHBoxLayout()
        btn = QPushButton("Refresh")
        btn.clicked.connect(self._refresh_cameras)
        row.addWidget(btn)
        self._connect_btn = QPushButton("Connect")
        self._connect_btn.setStyleSheet("background-color: #1a3a1a;")
        self._connect_btn.clicked.connect(self._connect)
        row.addWidget(self._connect_btn)
        gl.addLayout(row)
        btn = QPushButton("Browse SDK...")
        btn.clicked.connect(self._browse_sdk)
        gl.addWidget(btn)
        sb.addWidget(grp)

        # == Camera Controls (dynamic — populated at connect time) ==
        self._ctrl_group = QGroupBox("CAMERA CONTROLS")
        self._ctrl_group.setLayout(QVBoxLayout())
        sb.addWidget(self._ctrl_group)

        # == Mode (image format) ==
        grp = QGroupBox("IMAGE FORMAT")
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        self._raw8_rb = QRadioButton("RAW8")
        self._raw16_rb = QRadioButton("RAW16")
        self._raw16_rb.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self._raw8_rb)
        bg.addButton(self._raw16_rb)
        row.addWidget(self._raw8_rb)
        row.addWidget(self._raw16_rb)
        gl.addLayout(row)
        sb.addWidget(grp)

        # == ROI ==
        grp = QGroupBox("ROI")
        gl = QVBoxLayout(grp)
        gl.addLayout(
            self._spin_row("Width", 8, 9576, 4144, 8, 0, "_roi_w", True)
        )
        gl.addLayout(
            self._spin_row("Height", 2, 6388, 2822, 2, 0, "_roi_h", True)
        )
        gl.addLayout(
            self._spin_row("Start X", 0, 9568, 0, 8, 0, "_roi_x", True)
        )
        gl.addLayout(
            self._spin_row("Start Y", 0, 6386, 0, 2, 0, "_roi_y", True)
        )
        btn = QPushButton("Full Frame")
        btn.clicked.connect(self._roi_full_frame)
        gl.addWidget(btn)
        sb.addWidget(grp)

        # Apply
        btn = QPushButton("Apply Settings")
        btn.setStyleSheet("background-color: #1a2a3a;")
        btn.clicked.connect(self._apply_settings)
        sb.addWidget(btn)

        # == Display ==
        grp = QGroupBox("DISPLAY")
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        row.addWidget(QLabel("Stretch"))
        self._stretch_combo = QComboBox()
        for name in STRETCH_FUNCS:
            self._stretch_combo.addItem(name)
        self._stretch_combo.currentTextChanged.connect(
            lambda name: setattr(self._c, "display_stretch", name)
        )
        row.addWidget(self._stretch_combo)
        gl.addLayout(row)
        sb.addWidget(grp)

        # == Stream ==
        grp = QGroupBox("STREAM")
        gl = QVBoxLayout(grp)
        self._stream_btn = QPushButton("▶  Start Stream")
        self._stream_btn.setStyleSheet("background-color: #1a3a1a;")
        self._stream_btn.clicked.connect(self._start_stream)
        gl.addWidget(self._stream_btn)
        sb.addWidget(grp)

        # == FITS Recording ==
        grp = QGroupBox("FITS RECORDING")
        gl = QVBoxLayout(grp)
        gl.addLayout(
            self._spin_row("Frames", 1, 100000, 100, 10, 0, "_nframes_spin", True)
        )

        # Directory picker
        dir_lbl = QLabel("Directory")
        dir_lbl.setStyleSheet("color: #888; font: 9pt 'Courier New';")
        gl.addWidget(dir_lbl)
        row = QHBoxLayout()
        self._fits_dir = QLineEdit(os.getcwd())
        row.addWidget(self._fits_dir)
        btn = QPushButton("...")
        btn.setFixedWidth(30)
        btn.clicked.connect(self._pick_fits_dir)
        row.addWidget(btn)
        gl.addLayout(row)

        # Basename
        name_lbl = QLabel("Filename (no ext.)")
        name_lbl.setStyleSheet("color: #888; font: 9pt 'Courier New';")
        gl.addWidget(name_lbl)
        self._fits_basename = QLineEdit("capture")
        gl.addWidget(self._fits_basename)

        # Mode: stack to cube (default) vs individual files
        mode_row = QHBoxLayout()
        self._mode_stack_rb = QRadioButton("Stack (cube)")
        self._mode_indiv_rb = QRadioButton("Individual")
        self._mode_stack_rb.setChecked(True)
        mode_group = QButtonGroup(self)
        mode_group.addButton(self._mode_stack_rb)
        mode_group.addButton(self._mode_indiv_rb)
        mode_row.addWidget(self._mode_stack_rb)
        mode_row.addWidget(self._mode_indiv_rb)
        gl.addLayout(mode_row)

        self._rec_btn = QPushButton("⬤  Record FITS")
        self._rec_btn.setStyleSheet("background-color: #3a1a2a;")
        self._rec_btn.clicked.connect(self._start_record)
        gl.addWidget(self._rec_btn)

        self._rec_progress = QProgressBar()
        self._rec_progress.setValue(0)
        gl.addWidget(self._rec_progress)

        if not HAS_ASTROPY:
            lbl = QLabel("Warning: pip install astropy for FITS recording")
            lbl.setStyleSheet("color: #ff7733; font: 8pt 'Courier New';")
            lbl.setWordWrap(True)
            gl.addWidget(lbl)
        sb.addWidget(grp)

        # == Cooler ==
        self._cooler_group = QGroupBox("COOLER")
        gl = QVBoxLayout(self._cooler_group)
        self._cooler_on_cb = QCheckBox("Cooler ON")
        gl.addWidget(self._cooler_on_cb)
        gl.addLayout(
            self._spin_row("Target C", -40, 30, -10, 1, 0, "_cooler_temp", True)
        )
        btn = QPushButton("Apply Cooler")
        btn.clicked.connect(self._apply_cooler)
        gl.addWidget(btn)
        self._cooler_readout = QLabel("Sensor: -- C   Power: --%")
        self._cooler_readout.setStyleSheet(
            "color: #00aaff; font: 9pt 'Courier New';"
        )
        gl.addWidget(self._cooler_readout)
        self._cooler_group.setVisible(False)
        sb.addWidget(self._cooler_group)

    def _build_main(self, ml):
        # Stats bar
        bar = QFrame()
        bar.setFixedHeight(30)
        bar.setStyleSheet("background-color: #161616;")
        sl = QHBoxLayout(bar)
        sl.setContentsMargins(10, 0, 10, 0)

        self._state_lbl = self._stat_label("DISCONNECTED", "#555")
        sl.addWidget(self._state_lbl)
        self._fps_lbl = self._stat_label("FPS  --", "#00e87a")
        sl.addWidget(self._fps_lbl)
        self._frames_lbl = self._stat_label("Frames  0", "#aaa")
        sl.addWidget(self._frames_lbl)
        self._drop_lbl = self._stat_label("Dropped  0", "#3a5a3a")
        sl.addWidget(self._drop_lbl)
        self._temp_lbl = self._stat_label("-- C", "#00aaff")
        sl.addWidget(self._temp_lbl)
        sl.addStretch()
        self._rec_lbl = self._stat_label("", "#ff4444")
        sl.addWidget(self._rec_lbl)
        self._pixel_lbl = self._stat_label("x --  y --  val --", "#555")
        sl.addWidget(self._pixel_lbl)
        ml.addWidget(bar)

        # Splitter: display on top, histogram below
        splitter = QSplitter(Qt.Vertical)

        self._display = ImageDisplay()
        self._display.pixel_info.connect(self._on_pixel_info)
        self._display.pixel_left.connect(self._on_pixel_left)
        splitter.addWidget(self._display)

        self._histogram = HistogramWidget()
        splitter.addWidget(self._histogram)

        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 1)
        ml.addWidget(splitter, stretch=1)

        # Status bar
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet(
            "color: #555; font: 8pt 'Courier New'; padding: 2px 8px;"
        )
        ml.addWidget(self._status_lbl)

    # =====================================================================
    #  Widget factories
    # =====================================================================

    def _sep(self):
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setStyleSheet("color: #222;")
        return f

    def _stat_label(self, text, color):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {color}; font: bold 10pt 'Courier New';"
        )
        return lbl

    def _spin_row(self, label, lo, hi, default, step, decimals, attr,
                  as_int=False):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setFixedWidth(95)
        row.addWidget(lbl)
        if as_int:
            spin = QSpinBox()
            spin.setRange(int(lo), int(hi))
            spin.setValue(int(default))
            spin.setSingleStep(int(step))
        else:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setValue(default)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
        spin.setFixedWidth(100)
        row.addWidget(spin)
        setattr(self, attr, spin)
        return row

    def _pick_fits_dir(self):
        current = self._fits_dir.text().strip() or os.getcwd()
        path = QFileDialog.getExistingDirectory(
            self, "Select output directory", current,
        )
        if path:
            self._fits_dir.setText(path)

    # =====================================================================
    #  Cleanup
    # =====================================================================

    def closeEvent(self, event):
        if self._ws_server:
            self._ws_server.stop()
        self._c.shutdown()
        event.accept()
