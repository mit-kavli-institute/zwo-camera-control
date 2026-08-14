"""
Custom Qt widgets for the streaming demo.

HistogramWidget -- log-scaled histogram with stretch-bound indicators.
ImageDisplay    -- frame viewer with pixel readout on hover.
"""

import numpy as np

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget,
)


# -- Histogram -----------------------------------------------------------------

class _HistogramPlot(QWidget):
    """Histogram bars with fixed x-axis (set by data dtype) and stretch markers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._bins = None
        self._edges = None
        self._z1 = 0.0
        self._z2 = 255.0
        self._xmin = 0.0
        self._xmax = 256.0    # histogram range (exclusive upper edge for integer data)
        self._use_log = True

    def set_log(self, use_log):
        self._use_log = bool(use_log)
        self.update()

    def update_data(self, data, z1, z2):
        # Fix x-axis to the full dtype range so the plot doesn't rescale per frame.
        if data.dtype == np.uint8:
            xmin, xmax, nbins = 0, 256, 256        # one bin per value
        elif data.dtype == np.uint16:
            xmin, xmax, nbins = 0, 65536, 1024     # 64 values per bin
        else:
            xmin = float(data.min())
            xmax = float(data.max()) + 1
            nbins = 512

        flat = data.ravel()
        self._bins, self._edges = np.histogram(flat, bins=nbins, range=(xmin, xmax))
        self._xmin, self._xmax = xmin, xmax
        self._z1, self._z2 = z1, z2
        self.update()

    def paintEvent(self, _event):
        if self._bins is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        ml, mb = 4, 14
        pw, ph = W - ml - 4, H - mb - 2
        p.fillRect(self.rect(), QColor("#111"))

        if pw < 10 or ph < 10:
            p.end()
            return

        counts = self._bins.astype(np.float64)
        yvals = np.log1p(counts) if self._use_log else counts
        peak = yvals.max() if yvals.max() > 0 else 1.0
        n = len(yvals)
        data_range = (self._xmax - self._xmin) or 1.0

        for i in range(n):
            bar_h = int(yvals[i] / peak * ph)
            if bar_h <= 0:
                continue
            x_left = ml + int((self._edges[i] - self._xmin) / data_range * pw)
            x_right = ml + int((self._edges[i + 1] - self._xmin) / data_range * pw)
            bar_w = max(1, x_right - x_left)
            y = H - mb - bar_h
            mid = (self._edges[i] + self._edges[i + 1]) / 2
            in_range = self._z1 <= mid <= self._z2
            color = QColor("#00e87a") if in_range else QColor("#1a3a1a")
            p.fillRect(x_left, y, bar_w, bar_h, color)

        pen = QPen(QColor("#ff4444"), 1, Qt.DashLine)
        p.setPen(pen)
        for zv in (self._z1, self._z2):
            xv = ml + max(0, min(
                int((zv - self._xmin) / data_range * pw), pw
            ))
            p.drawLine(xv, 2, xv, H - mb)

        p.setPen(QColor("#666"))
        p.setFont(QFont("Courier New", 7))
        p.drawText(ml, H - 2, f"{int(self._xmin)}")
        xmax_txt = f"{int(self._xmax) - 1}"
        p.drawText(W - 4 - 6 * len(xmax_txt), H - 2, xmax_txt)
        p.end()


class HistogramWidget(QWidget):
    """Panel: pixel histogram + mean/median/std/var stats + log-y toggle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMaximumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(6, 2, 6, 0)
        top.setSpacing(14)

        self._mean_lbl = QLabel("mean —")
        self._median_lbl = QLabel("med —")
        self._std_lbl = QLabel("std —")
        self._var_lbl = QLabel("var —")
        for lbl in (self._mean_lbl, self._median_lbl, self._std_lbl, self._var_lbl):
            lbl.setStyleSheet("color: #aaa; font: 9pt 'Courier New';")
            lbl.setMinimumWidth(95)
            top.addWidget(lbl)
        top.addStretch()

        self._log_chk = QCheckBox("log y")
        self._log_chk.setChecked(True)
        self._log_chk.setStyleSheet("color: #888; font: 9pt 'Courier New';")
        self._log_chk.toggled.connect(self._on_log_toggled)
        top.addWidget(self._log_chk)

        outer.addLayout(top)

        self._plot = _HistogramPlot()
        outer.addWidget(self._plot, stretch=1)

    def update_data(self, data, z1, z2):
        flat = data.ravel()
        mean = float(flat.mean())
        var = float(flat.var())
        std = var ** 0.5
        # Median via subsample when the frame is large — np.median sorts the full
        # array, which dominates update cost on multi-MP frames.
        sample = flat[::max(1, flat.size // 100_000)]
        median = float(np.median(sample))

        self._mean_lbl.setText(f"mean {mean:.1f}")
        self._median_lbl.setText(f"med  {median:.0f}")
        self._std_lbl.setText(f"std  {std:.2f}")
        self._var_lbl.setText(f"var  {var:.1f}")

        self._plot.update_data(data, z1, z2)

    def _on_log_toggled(self, checked):
        self._plot.set_log(checked)


# -- Image display -------------------------------------------------------------

class ImageDisplay(QLabel):
    """
    Displays a numpy frame scaled to fit; reports pixel coords on hover.

    Emits pixel_info(x, y, raw_value) when mouse is over valid pixels.
    """

    pixel_info = pyqtSignal(int, int, int)
    pixel_left = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #060606;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)

        self._raw_frame = None
        self._frame_shape = (1, 1)  # (H, W)

    def set_frame(self, raw_frame, display_8bit):
        """
        Update the display.

        Parameters
        ----------
        raw_frame : original (possibly 16-bit) frame for pixel readout
        display_8bit : uint8 stretched frame for rendering
        """
        self._raw_frame = raw_frame
        h, w = display_8bit.shape[:2]
        self._frame_shape = (h, w)

        display_8bit = np.ascontiguousarray(display_8bit)
        qimg = QImage(display_8bit.data, w, h, w, QImage.Format_Grayscale8)
        qimg._numpy_ref = display_8bit  # prevent GC of backing array

        lbl_w, lbl_h = self.width(), self.height()
        if lbl_w < 4 or lbl_h < 4:
            return

        scale = min(lbl_w / w, lbl_h / h)
        new_w, new_h = int(w * scale), int(h * scale)
        pixmap = QPixmap.fromImage(qimg).scaled(
            new_w, new_h, Qt.KeepAspectRatio, Qt.FastTransformation
        )
        self.setPixmap(pixmap)

    def _map_to_frame(self, mx, my):
        """Map widget coords to frame pixel coords, or None if outside."""
        pm = self.pixmap()
        if pm is None:
            return None
        lbl_w, lbl_h = self.width(), self.height()
        pm_w, pm_h = pm.width(), pm.height()
        fh, fw = self._frame_shape
        ox = (lbl_w - pm_w) // 2
        oy = (lbl_h - pm_h) // 2
        rx, ry = mx - ox, my - oy
        if 0 <= rx < pm_w and 0 <= ry < pm_h:
            px = int(rx / pm_w * fw)
            py = int(ry / pm_h * fh)
            if 0 <= px < fw and 0 <= py < fh:
                return px, py
        return None

    def mouseMoveEvent(self, event):
        coords = self._map_to_frame(event.x(), event.y())
        if coords is not None and self._raw_frame is not None:
            px, py = coords
            val = int(self._raw_frame[py, px])
            self.pixel_info.emit(px, py, val)
        else:
            self.pixel_left.emit()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.pixel_left.emit()
        super().leaveEvent(event)
