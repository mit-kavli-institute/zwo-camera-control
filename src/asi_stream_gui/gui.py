"""
Main application window.

Sidebar with camera controls, image display with histogram, stats bar,
FITS recording, cooler management, and WebSocket command handling.

Camera controls are built dynamically from what each camera actually
reports, so it works correctly with the ASI294MM Pro, ASI662MM, and
ASI990MM Pro (which has no offset and has independent frame rate control).
"""

import logging
import math
import os
import queue
import time

import numpy as np

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSlot
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QScrollArea, QSlider, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from .sdk import ASICamera, ASIDriver, ASIError, CameraInfo, Ctrl, ImgType
from .camera_config import (
    CameraControlSet, CameraSettings, ControlKind, ControlSpec, FLIP_LABELS,
)
from .stretch import STRETCH_FUNCS
from .capture import CaptureWorker
from .recorder import save_fits_cube, save_fits_individual, HAS_ASTROPY
from .widgets import HistogramWidget, ImageDisplay

log = logging.getLogger("asi_demo.gui")

# Maximum slider range before we switch to a spinbox.
_SLIDER_MAX_RANGE = 4096

# Exposure unit choices: (label, µs-per-unit)
_EXP_UNITS = [("µs", 1), ("ms", 1_000), ("s", 1_000_000)]


# =====================================================================
#  Dynamic control widget (PyQt5 equivalent of tkinter ControlWidget)
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

    def __init__(self, sdk_path=None, ws_port=0):
        super().__init__()
        self.setWindowTitle("ZWO ASI Streaming Demo")
        self.setMinimumSize(950, 620)
        self.resize(1200, 750)

        self._driver = None
        self._camera = None
        self._worker = None
        self._worker_thread = None
        self._last_raw_frame = None
        self._streaming = False

        # Dynamic camera config
        self._control_set = None   # CameraControlSet
        self._settings = None      # CameraSettings
        self._ctrl_widgets = {}    # name -> ControlWidget

        # WebSocket recording callback (set by ws_server during record cmd)
        self._ws_record_done_cb = None

        self._build_ui()
        self._init_sdk(sdk_path)

        # Cooler poll timer
        self._cooler_timer = QTimer(self)
        self._cooler_timer.timeout.connect(self._update_cooler_readout)

        # Display poll timer — drains frame queue, self-rescheduling.
        # Single-shot avoids pileup if stretch takes longer than the interval.
        self._display_interval = 33  # ~30 Hz target
        self._display_timer = QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.timeout.connect(self._poll_frames)
        self._last_hist_time = 0.0  # rate-limit histogram to ~5 Hz

        # Optional WebSocket server
        self._ws_server = None
        if ws_port > 0:
            self._start_ws_server(ws_port)

    # =====================================================================
    #  SDK init
    # =====================================================================

    def _init_sdk(self, path=None):
        candidates = []
        if path:
            candidates.append(path)
        candidates += [
            "ASICamera2.dll",
            r"C:\Program Files\ASIStudio\ASICamera2.dll",
            r"C:\Program Files (x86)\ASIStudio\ASICamera2.dll",
            "/usr/lib/libASICamera2.so",
            "/usr/local/lib/libASICamera2.so",
        ]
        for c in candidates:
            if os.path.isfile(c):
                try:
                    self._driver = ASIDriver(c)
                    self._set_status(f"SDK loaded: {c}")
                    return
                except Exception as e:
                    self._set_status(f"SDK load failed ({c}): {e}")
        self._set_status(
            "SDK not found -- click Browse SDK to locate ASICamera2.dll/.so"
        )

    def _browse_sdk(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ASICamera2.dll or libASICamera2.so", "",
            "SDK library (*.dll *.so *.dylib);;All files (*)",
        )
        if not path:
            return
        try:
            self._driver = ASIDriver(path)
            self._set_status(f"SDK loaded: {path}")
        except Exception as e:
            QMessageBox.critical(self, "SDK Error", str(e))

    # =====================================================================
    #  Camera connection
    # =====================================================================

    def _refresh_cameras(self):
        if not self._driver:
            self._set_status("Load SDK first")
            return
        n = self._driver.get_num_cameras()
        self._cam_combo.clear()
        if n == 0:
            self._set_status("No cameras found")
            return
        for i in range(n):
            info = CameraInfo.from_struct(self._driver.get_camera_property(i))
            self._cam_combo.addItem(f"{i}: {info.name}", i)
        self._set_status(f"Found {n} camera(s)")

    def _connect(self):
        if not self._driver:
            QMessageBox.warning(self, "No SDK", "Load the SDK first.")
            return
        idx = self._cam_combo.currentData()
        if idx is None:
            QMessageBox.warning(
                self, "No camera", "Click Refresh, then select a camera."
            )
            return
        try:
            self._camera = ASICamera(self._driver, idx)
            cam = self._camera

            # Build dynamic control set from what the camera reports
            caps_dict = cam.get_caps_dict()
            self._control_set = CameraControlSet.from_caps_dict(
                cam.info.name, caps_dict
            )
            self._settings = CameraSettings(self._control_set)
            log.info("\n%s", self._control_set.describe())

            # Max out USB bandwidth for streaming
            self._settings.set_if_present("BandWidth", 9999, clamp=True)

            # Build dynamic controls UI and apply defaults
            self._rebuild_controls_panel()
            self._apply_settings(silent=True)

            # Sync ROI limits
            self._roi_w.setMaximum(cam.info.max_width)
            self._roi_h.setMaximum(cam.info.max_height)
            self._roi_x.setMaximum(cam.info.max_width - 8)
            self._roi_y.setMaximum(cam.info.max_height - 2)
            self._roi_w.setValue(cam.info.max_width)
            self._roi_h.setValue(cam.info.max_height)
            self._roi_x.setValue(0)
            self._roi_y.setValue(0)

            # Cooler section
            self._cooler_group.setVisible(self._control_set.has_cooler())
            if self._control_set.has_cooler():
                self._cooler_timer.start(2000)
                spec = self._control_set.get("TargetTemp")
                if spec:
                    self._cooler_temp.setRange(spec.min_value, spec.max_value)
                    self._cooler_temp.setValue(spec.default_value)

            # Status line with capability flags
            flags = []
            if self._control_set.has_cooler():
                flags.append("cooled")
            if self._control_set.has_frame_rate_control():
                flags.append("indep-fps")
            if self._control_set.has_offset():
                flags.append("offset")
            self._set_status(
                f"Connected: {cam.info.name}  |  "
                f"{cam.info.max_width}x{cam.info.max_height}  |  "
                f"{cam.info.bit_depth}-bit  |  "
                f"USB3={'yes' if cam.info.is_usb3 else 'no'}  |  "
                + "  ".join(flags)
            )
            self._connect_btn.setText("Disconnect")
            self._connect_btn.setStyleSheet("background-color: #5a1414;")
            self._connect_btn.clicked.disconnect()
            self._connect_btn.clicked.connect(self._disconnect)

        except (ASIError, Exception) as e:
            self._camera = None
            self._control_set = None
            self._settings = None
            QMessageBox.critical(self, "Connect Error", str(e))

    def _disconnect(self):
        self._stop_stream()
        self._cooler_timer.stop()
        if self._camera:
            self._camera.close()
            self._camera = None
        self._control_set = None
        self._settings = None
        self._clear_controls_panel()
        self._connect_btn.setText("Connect")
        self._connect_btn.setStyleSheet("background-color: #1a3a1a;")
        self._connect_btn.clicked.disconnect()
        self._connect_btn.clicked.connect(self._connect)
        self._cooler_group.setVisible(False)
        self._set_status("Disconnected")

    # =====================================================================
    #  Dynamic controls panel
    # =====================================================================

    def _rebuild_controls_panel(self):
        """Clear and rebuild the CAMERA CONTROLS group from the control set."""
        self._clear_controls_panel()
        if not self._control_set:
            return

        layout = self._ctrl_group.layout()

        # Writable controls
        for spec in self._control_set.writable():
            w = ControlWidget(spec, on_change=self._on_ctrl_changed)
            layout.addWidget(w)
            self._ctrl_widgets[spec.name] = w

        # Read-only controls
        ro = self._control_set.readonly()
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

    def _on_ctrl_changed(self, name: str, value: int):
        if self._settings:
            try:
                self._settings.set(name, value, clamp=True)
            except Exception:
                pass

    # =====================================================================
    #  Settings
    # =====================================================================

    def _apply_settings(self, silent=False):
        cam = self._camera
        if not cam or not self._settings:
            if not silent:
                QMessageBox.warning(self, "No camera", "Connect first.")
            return
        try:
            # Sync all widget values into settings
            for name, w in self._ctrl_widgets.items():
                spec = self._control_set.get(name) if self._control_set else None
                if spec and not spec.is_readonly:
                    try:
                        self._settings.set(name, w.get_value(), clamp=True)
                    except Exception:
                        pass

            # Set ROI / image format
            img_type = (
                ImgType.RAW16 if self._raw16_rb.isChecked() else ImgType.RAW8
            )
            cam.set_roi(
                self._roi_w.value(), self._roi_h.value(),
                1, img_type,
                self._roi_x.value(), self._roi_y.value(),
            )

            # Push all control values to the camera
            errors = self._settings.apply(cam)

            if not silent:
                if errors:
                    err_str = ", ".join(f"{n}: {e}" for n, e in errors)
                    self._set_status(f"Settings errors: {err_str}")
                else:
                    w, h, _b, _t = cam.get_roi()
                    self._set_status(
                        f"Applied -- "
                        f"ROI={w}x{h}+({self._roi_x.value()},{self._roi_y.value()})  "
                        f"{'RAW16' if img_type == ImgType.RAW16 else 'RAW8'}  "
                        f"({len(self._ctrl_widgets)} controls)"
                    )
        except (ASIError, Exception) as e:
            if not silent:
                QMessageBox.critical(self, "Settings Error", str(e))
            else:
                self._set_status(f"Settings error: {e}")

    # =====================================================================
    #  Cooler
    # =====================================================================

    def _apply_cooler(self):
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            return
        try:
            cam.set_cooler(
                on=self._cooler_on_cb.isChecked(),
                target_c=self._cooler_temp.value(),
            )
            state = "ON" if self._cooler_on_cb.isChecked() else "OFF"
            self._set_status(
                f"Cooler {state}, target={self._cooler_temp.value()} C"
            )
        except ASIError as e:
            self._set_status(f"Cooler error: {e}")

    def _update_cooler_readout(self):
        cam = self._camera
        if not cam or not cam.info.is_cooler:
            return
        try:
            temp = cam.temperature()
            power = (
                cam.get_ctrl_value(Ctrl.COOLER_POWER_PERC)
                if cam.has_ctrl(Ctrl.COOLER_POWER_PERC) else 0
            )
            self._cooler_readout.setText(
                f"Sensor: {temp:.1f} C   Power: {power}%"
            )
        except ASIError:
            pass

    # =====================================================================
    #  Streaming
    # =====================================================================

    def _start_stream(self):
        cam = self._camera
        if not cam:
            QMessageBox.warning(self, "No camera", "Connect a camera first.")
            return

        # Get exposure in ms for timeout calculation
        exp_us = self._settings.get("Exposure") if self._settings else 100_000
        exp_ms = (exp_us or 100_000) / 1000.0

        self._worker = CaptureWorker(cam, exp_ms)
        self._worker_thread = QThread()
        self._worker.moveToThread(self._worker_thread)

        # Connect signals — low-frequency only (no frame_ready signal;
        # frames are delivered via worker.frame_queue, polled by QTimer).
        self._worker_thread.started.connect(self._worker.run)
        self._worker.stats_update.connect(self._on_stats)
        self._worker.recording_progress.connect(self._on_rec_progress)
        self._worker.recording_done.connect(self._on_recording_done)
        self._worker.error.connect(
            lambda msg: self._set_status(f"Capture: {msg}")
        )

        self._worker_thread.start()
        self._display_timer.start(self._display_interval)
        self._streaming = True

        self._stream_btn.setText("■  Stop Stream")
        self._stream_btn.setStyleSheet("background-color: #5a1414;")
        self._stream_btn.clicked.disconnect()
        self._stream_btn.clicked.connect(self._stop_stream)
        self._set_status("Streaming...")

    def _stop_stream(self):
        self._display_timer.stop()
        if self._worker:
            self._worker.request_stop()
        if self._worker_thread:
            self._worker_thread.quit()
            self._worker_thread.wait(5000)
        self._worker = None
        self._worker_thread = None
        self._streaming = False

        self._stream_btn.setText("▶  Start Stream")
        self._stream_btn.setStyleSheet("background-color: #1a3a1a;")
        try:
            self._stream_btn.clicked.disconnect()
        except TypeError:
            pass
        self._stream_btn.clicked.connect(self._start_stream)
        self._set_status("Stream stopped")

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
            if not self._worker:
                return
            fq = self._worker.frame_queue
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
            if self._streaming:
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
        if self._camera and self._control_set:
            for spec in self._control_set.readonly():
                w = self._ctrl_widgets.get(spec.name)
                if w:
                    try:
                        val = self._camera.get_ctrl_value(spec.control_type)
                        w.update_readonly(val)
                    except (ASIError, Exception):
                        pass

    @pyqtSlot(int, int)
    def _on_rec_progress(self, got, target):
        if target > 0:
            self._rec_progress.setValue(int(got / target * 100))
            self._rec_lbl.setText(f"REC  {got}/{target}")

    # =====================================================================
    #  FITS recording
    # =====================================================================

    def _start_record(self):
        if not HAS_ASTROPY:
            QMessageBox.critical(self, "No astropy", "pip install astropy")
            return
        if not self._worker or not self._streaming:
            QMessageBox.warning(
                self, "Not streaming", "Start the stream first."
            )
            return

        n = self._nframes_spin.value()
        cam = self._camera
        w, h, _bin, img_t = cam.get_roi()
        dtype = np.uint16 if img_t == int(ImgType.RAW16) else np.uint8

        self._worker.start_recording(n, w, h, dtype)

        self._rec_btn.setText("✕  Cancel")
        self._rec_btn.setStyleSheet("background-color: #5a1414;")
        self._rec_btn.clicked.disconnect()
        self._rec_btn.clicked.connect(self._cancel_record)
        self._rec_lbl.setText(f"REC  0/{n}")
        self._rec_progress.setValue(0)
        self._set_status(f"Recording {n} frames...")

    def _cancel_record(self):
        if self._worker:
            self._worker.cancel_recording()
        self._reset_rec_button()
        self._rec_progress.setValue(0)
        self._rec_lbl.setText("")
        self._set_status("Recording cancelled")

    @pyqtSlot(object, object, float)
    def _on_recording_done(self, cube, timestamps, elapsed):
        """Signal from capture worker -- safe to touch GUI here."""
        self._reset_rec_button()
        self._rec_lbl.setText("SAVING...")

        directory = self._fits_dir.text().strip() or os.getcwd()
        basename = self._fits_basename.text().strip() or "capture"
        stack_mode = self._mode_stack_rb.isChecked()

        cam = self._camera
        actual_fps = cube.shape[0] / elapsed if elapsed > 0 else 0

        meta = {
            "INSTRUME": cam.info.name if cam else "ZWO ASI",
            "NFRAMES": cube.shape[0],
            "STRMFPS": round(actual_fps, 3),
            "ELAPSED": round(elapsed, 4),
            "DEPTH": "RAW16" if self._raw16_rb.isChecked() else "RAW8",
            "STRETCH": self._stretch_combo.currentText(),
        }
        # Include all current control values in FITS header
        if self._settings:
            snap = self._settings.snapshot()
            for k, v in snap.items():
                meta[k[:8].upper()] = v
        if cam:
            w, h, _b, _t = cam.get_roi()
            meta["ROI_W"] = w
            meta["ROI_H"] = h
            meta["ROI_X"] = self._roi_x.value()
            meta["ROI_Y"] = self._roi_y.value()
            if cam.info.is_cooler:
                try:
                    meta["DETTEMP"] = cam.temperature()
                except ASIError:
                    pass

        def _after_save(msg):
            self._set_status(msg)
            self._rec_progress.setValue(100)
            self._rec_lbl.setText("DONE")
            if self._ws_record_done_cb:
                self._ws_record_done_cb(msg)
                self._ws_record_done_cb = None

        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as e:
            _after_save(f"FITS save error: cannot create {directory}: {e}")
            return

        if stack_mode:
            path = os.path.join(directory, f"{basename}.fits")
            save_fits_cube(path, cube, timestamps, meta, _after_save)
        else:
            save_fits_individual(
                directory, basename, cube, timestamps, meta, _after_save
            )

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
    #  WebSocket command handler
    # =====================================================================

    def handle_ws_command(self, cmd):
        """Called on the GUI thread by the WS bridge."""
        action = cmd.get("cmd", "")

        if action == "status":
            cam = self._camera
            result = {
                "cmd": "status",
                "connected": cam is not None,
                "streaming": self._streaming,
                "camera": cam.info.name if cam else None,
            }
            if self._settings:
                result["controls"] = self._settings.snapshot()
            return result

        elif action == "list_cameras":
            if not self._driver:
                return {"cmd": "list_cameras", "cameras": [],
                        "error": "SDK not loaded"}
            cams = []
            try:
                n = self._driver.get_num_cameras()
                for i in range(n):
                    info = CameraInfo.from_struct(
                        self._driver.get_camera_property(i)
                    )
                    cams.append({"index": i, "name": info.name})
            except (ASIError, Exception) as e:
                return {"cmd": "list_cameras", "cameras": [], "error": str(e)}
            return {"cmd": "list_cameras", "cameras": cams}

        elif action == "connect_camera":
            if self._camera is not None:
                return {"cmd": "connect_camera", "ok": True,
                        "camera": self._camera.info.name,
                        "note": "already connected"}
            idx = cmd.get("index", 0)
            # Refresh the combo so the selection round-trips cleanly to the GUI.
            self._refresh_cameras()
            for i in range(self._cam_combo.count()):
                if self._cam_combo.itemData(i) == idx:
                    self._cam_combo.setCurrentIndex(i)
                    break
            else:
                return {"cmd": "connect_camera", "ok": False,
                        "error": f"no camera with index {idx}"}
            self._connect()
            ok = self._camera is not None
            return {
                "cmd": "connect_camera", "ok": ok,
                "camera": self._camera.info.name if ok else None,
            }

        elif action == "disconnect_camera":
            if self._camera is None:
                return {"cmd": "disconnect_camera", "ok": True,
                        "note": "not connected"}
            self._disconnect()
            return {"cmd": "disconnect_camera", "ok": True}

        elif action == "set":
            if self._settings:
                for key, value in cmd.items():
                    if key == "cmd":
                        continue
                    # Handle img_type and ROI separately
                    if key == "img_type":
                        if value == "RAW8":
                            self._raw8_rb.setChecked(True)
                        else:
                            self._raw16_rb.setChecked(True)
                        continue
                    roi_map = {
                        "roi_w": self._roi_w, "roi_h": self._roi_h,
                        "roi_x": self._roi_x, "roi_y": self._roi_y,
                    }
                    if key in roi_map:
                        roi_map[key].setValue(int(value))
                        continue
                    # Try setting as a camera control
                    w = self._ctrl_widgets.get(key)
                    if w:
                        w.set_value(int(value))
                    else:
                        self._settings.set_if_present(key, value, clamp=True)
            self._apply_settings(silent=True)
            return {"cmd": "set", "ok": True}

        elif action == "start_stream":
            self._start_stream()
            return {"cmd": "start_stream", "ok": self._streaming}

        elif action == "stop_stream":
            self._stop_stream()
            return {"cmd": "stop_stream", "ok": True}

        elif action == "record":
            n = cmd.get("n_frames", 100)
            self._nframes_spin.setValue(n)

            # Backward-compat: `path` sets directory+basename in one shot.
            if "path" in cmd:
                p = cmd["path"]
                d, f = os.path.split(p)
                if d:
                    self._fits_dir.setText(d)
                base, _ext = os.path.splitext(f)
                if base:
                    self._fits_basename.setText(base)

            if "directory" in cmd:
                self._fits_dir.setText(str(cmd["directory"]))
            if "basename" in cmd:
                self._fits_basename.setText(str(cmd["basename"]))

            mode = cmd.get("mode", "stack").lower()
            if mode == "individual":
                self._mode_indiv_rb.setChecked(True)
            else:
                self._mode_stack_rb.setChecked(True)

            self._start_record()
            return {
                "cmd": "record", "ack": True,
                "n_frames": n,
                "directory": self._fits_dir.text(),
                "basename": self._fits_basename.text(),
                "mode": "stack" if self._mode_stack_rb.isChecked() else "individual",
            }

        elif action == "cooler":
            self._cooler_on_cb.setChecked(cmd.get("on", False))
            if "target" in cmd:
                self._cooler_temp.setValue(int(cmd["target"]))
            self._apply_cooler()
            return {"cmd": "cooler", "ok": True}

        return {"error": f"unknown command: {action}"}

    # =====================================================================
    #  WebSocket server
    # =====================================================================

    def _start_ws_server(self, port):
        try:
            from .ws_server import WebSocketServer, HAS_WEBSOCKETS
            if not HAS_WEBSOCKETS:
                log.warning("websockets not installed -- WS server disabled")
                return
            self._ws_server = WebSocketServer(self, port)
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
        title = QLabel("ASI STREAM\nDEMO")
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
        btn.clicked.connect(lambda: self._apply_settings(silent=False))
        sb.addWidget(btn)

        # == Display ==
        grp = QGroupBox("DISPLAY")
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        row.addWidget(QLabel("Stretch"))
        self._stretch_combo = QComboBox()
        for name in STRETCH_FUNCS:
            self._stretch_combo.addItem(name)
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

    def _roi_full_frame(self):
        cam = self._camera
        if not cam:
            return
        self._roi_w.setValue(cam.info.max_width)
        self._roi_h.setValue(cam.info.max_height)
        self._roi_x.setValue(0)
        self._roi_y.setValue(0)

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
        self._stop_stream()
        self._cooler_timer.stop()
        if self._ws_server:
            self._ws_server.stop()
        if self._camera:
            self._camera.close()
            self._camera = None
        event.accept()
