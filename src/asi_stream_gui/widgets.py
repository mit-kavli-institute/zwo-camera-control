"""
Custom Qt widgets for the streaming demo.

HistogramWidget -- log-scaled histogram with stretch-bound indicators.
ImageDisplay    -- frame viewer with pixel readout on hover.
"""

import numpy as np

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QLabel, QSizePolicy, QWidget


# -- Histogram -----------------------------------------------------------------

class HistogramWidget(QWidget):
    """Log-scaled histogram with dashed lines showing current stretch bounds."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(100)
        self.setMaximumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._bins = None
        self._edges = None
        self._z1 = 0.0
        self._z2 = 255.0
        self._dmin = 0.0
        self._dmax = 255.0

    def update_data(self, data, z1, z2):
        flat = data.ravel().astype(np.float64)
        nbins = max(64, min(512, int(flat.max() - flat.min() + 1)))
        self._bins, self._edges = np.histogram(flat, bins=nbins)
        self._dmin, self._dmax = float(flat.min()), float(flat.max())
        self._z1, self._z2 = z1, z2
        self.update()

    def paintEvent(self, _event):
        if self._bins is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        ml, mb = 4, 18
        pw, ph = W - ml - 4, H - mb - 4
        p.fillRect(self.rect(), QColor("#111"))

        if pw < 10 or ph < 10:
            p.end()
            return

        log_b = np.log1p(self._bins.astype(np.float64))
        peak = log_b.max() if log_b.max() > 0 else 1.0
        n = len(log_b)
        data_range = (self._edges[-1] - self._edges[0]) or 1.0

        bar_w = max(1, int(pw / n))
        for i in range(n):
            bar_h = int(log_b[i] / peak * ph)
            x = ml + int((self._edges[i] - self._edges[0]) / data_range * pw)
            y = H - mb - bar_h
            mid = (self._edges[i] + self._edges[i + 1]) / 2
            in_range = self._z1 <= mid <= self._z2
            color = QColor("#00e87a") if in_range else QColor("#1a3a1a")
            p.fillRect(x, y, bar_w, bar_h, color)

        pen = QPen(QColor("#ff4444"), 1, Qt.DashLine)
        p.setPen(pen)
        for zv in (self._z1, self._z2):
            xv = ml + max(0, min(
                int((zv - self._edges[0]) / data_range * pw), pw
            ))
            p.drawLine(xv, 2, xv, H - mb)

        p.setPen(QColor("#666"))
        p.setFont(QFont("Courier New", 7))
        p.drawText(ml, H - 2, f"{self._dmin:.0f}")
        p.drawText(W - 60, H - 2, f"{self._dmax:.0f}")
        p.end()


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
