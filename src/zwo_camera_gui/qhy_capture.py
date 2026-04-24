"""
Capture worker for QHY live streaming.

Mirrors the signal contract of capture.CaptureWorker so the GUI can share the
same streaming/recording UI logic.
"""

import queue
import threading
import time

import numpy as np

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot


class QHYCaptureWorker(QObject):
    stats_update = pyqtSignal(float, int, int, float)
    recording_progress = pyqtSignal(int, int)
    recording_done = pyqtSignal(object, object, float)
    error = pyqtSignal(str)

    def __init__(self, camera, exposure_ms):
        super().__init__()
        self.camera = camera
        self.exposure_ms = exposure_ms
        self._stop = threading.Event()
        self.frame_queue = queue.Queue(maxsize=2)

        self._rec_lock = threading.Lock()
        self._rec_cube = None
        self._rec_timestamps = None
        self._rec_target = 0
        self._rec_idx = 0
        self._rec_t0 = 0.0

    def request_stop(self):
        self._stop.set()

    def start_recording(self, n_frames, width, height, dtype):
        with self._rec_lock:
            self._rec_cube = np.empty((n_frames, height, width), dtype=dtype)
            self._rec_timestamps = np.empty(n_frames, dtype=np.float64)
            self._rec_target = n_frames
            self._rec_idx = 0
            self._rec_t0 = time.perf_counter()

    def cancel_recording(self):
        with self._rec_lock:
            self._rec_cube = None
            self._rec_timestamps = None
            self._rec_idx = 0

    @pyqtSlot()
    def run(self):
        stats_interval = 0.5
        prog_interval = 0.1
        last_stats = 0.0
        last_prog = 0.0

        fps_count = 0
        fps_t0 = time.perf_counter()
        total = 0
        fps = 0.0

        self.camera.start_video()
        try:
            while not self._stop.is_set():
                ok, frame = self.camera.get_live_frame()
                if not ok:
                    time.sleep(max(0.002, self.exposure_ms / 1000.0 / 4.0))
                    continue

                now = time.perf_counter()
                total += 1
                fps_count += 1
                dt = now - fps_t0
                if dt >= 1.0:
                    fps = fps_count / dt
                    fps_count = 0
                    fps_t0 = now

                rec_finished = None
                with self._rec_lock:
                    if self._rec_cube is not None:
                        idx = self._rec_idx
                        if idx < self._rec_target:
                            self._rec_cube[idx] = frame
                            self._rec_timestamps[idx] = now - self._rec_t0
                            self._rec_idx = idx + 1
                            if idx + 1 >= self._rec_target:
                                rec_finished = (
                                    self._rec_cube,
                                    self._rec_timestamps.tolist(),
                                    now - self._rec_t0,
                                )
                                self._rec_cube = None
                                self._rec_timestamps = None

                if rec_finished is not None:
                    self.recording_progress.emit(rec_finished[0].shape[0], rec_finished[0].shape[0])
                    self.recording_done.emit(*rec_finished)
                elif now - last_prog >= prog_interval:
                    with self._rec_lock:
                        rec_active = self._rec_cube is not None
                        rec_n = self._rec_idx
                        rec_t = self._rec_target
                    if rec_active:
                        last_prog = now
                        self.recording_progress.emit(rec_n, rec_t)

                try:
                    self.frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

                if now - last_stats >= stats_interval:
                    last_stats = now
                    try:
                        dropped = self.camera.get_dropped()
                    except Exception:
                        dropped = 0
                    try:
                        temp = self.camera.temperature()
                    except Exception:
                        temp = float("nan")
                    self.stats_update.emit(fps, total, dropped, temp)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.camera.stop_video()
