"""
Capture worker -- runs ASIGetVideoData loop on a dedicated QThread.

Frames are delivered via a bounded queue (natural backpressure — if the
GUI is slow, stale frames are silently dropped rather than queuing into
the Qt event loop).  Low-frequency events (stats, recording) use signals.
"""

import ctypes
import queue
import threading
import time
from collections import deque

import numpy as np

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .sdk import (
    ASICamera, ASIError, ASI_SUCCESS, ASI_ERROR_TIMEOUT,
    ImgType, Ctrl, _ERROR_NAMES,
)


class CaptureWorker(QObject):
    """
    Runs ASIGetVideoData in a tight loop.

    This thread is the sole owner of the SDK while streaming: the SDK
    serializes calls per camera, so any call from another thread would
    block for up to one exposure. Other threads submit work via
    ``run_async`` and consume the readonly/stats signals instead.

    Frame delivery
    --------------
    Frames are pushed to ``frame_queue`` (Queue(maxsize=2)) and the GUI
    polls with a QTimer.  This avoids unbounded signal queue growth.

    Signals (low-frequency only)
    ----------------------------
    stats_update(fps, total_frames, dropped, sensor_temp_C)
        Emitted every ~500 ms.
    readonly_update(dict)
        {control_name: raw_value} for the readonly controls, every ~500 ms.
    recording_progress(n_got, n_target)
        Emitted at ~10 Hz during recording.
    recording_done(cube, timestamps, elapsed)
        Emitted once when the pre-allocated cube is full.
    error(msg)
        Non-timeout capture errors.
    """

    stats_update = pyqtSignal(float, int, int, float)
    readonly_update = pyqtSignal(object)
    recording_progress = pyqtSignal(int, int)
    recording_done = pyqtSignal(object, object, float)
    error = pyqtSignal(str)

    def __init__(self, camera, exposure_ms, readonly_ctrls=None):
        super().__init__()
        self.camera = camera
        self.exposure_ms = exposure_ms
        # {name: control_type} of readonly controls to poll with stats
        self._readonly_ctrls = dict(readonly_ctrls or {})
        self._stop = threading.Event()

        # Frame delivery: GUI polls this queue with a QTimer.
        # maxsize=2 means at most 2 frames buffered; overflow is dropped.
        self.frame_queue = queue.Queue(maxsize=2)

        # SDK work submitted from other threads; drained between frames.
        self._cmd_queue = queue.Queue()

        # Recording state (guarded by lock)
        self._rec_lock = threading.Lock()
        self._rec_cube = None
        self._rec_timestamps = None
        self._rec_gate = None
        self._rec_target = 0
        self._rec_idx = 0
        self._rec_t0 = 0.0

        # Per-frame metadata of the last record (None for ZWO: no
        # hardware timestamps; host timestamps are delivered separately).
        self.last_frame_meta = None

        # Recent inter-arrival deltas (feeds the record-gate cadence
        # estimate in the controller).
        self._deltas = deque(maxlen=8)

    def request_stop(self):
        self._stop.set()

    def run_async(self, fn):
        """Run fn on the capture thread between frames. Thread-safe.

        This is how other threads touch the SDK while streaming without
        blocking on the in-flight ASIGetVideoData call.
        """
        self._cmd_queue.put(fn)

    def _drain_cmds(self):
        while True:
            try:
                fn = self._cmd_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as exc:
                self.error.emit(f"deferred SDK call failed: {exc}")

    def recent_median_delta(self):
        """Median of recent frame inter-arrival times, or None."""
        if not self._deltas:
            return None
        vals = sorted(self._deltas)
        return vals[len(vals) // 2]

    def start_recording(self, n_frames, width, height, dtype, gate=None):
        """Begin recording into a pre-allocated cube. Thread-safe.

        gate: optional core.gating.RecordGate -- frames it rejects
        (in-flight / transition frames) are not stored.
        """
        with self._rec_lock:
            self._rec_cube = np.empty((n_frames, height, width), dtype=dtype)
            self._rec_timestamps = np.empty(n_frames, dtype=np.float64)
            self._rec_gate = gate
            self._rec_target = n_frames
            self._rec_idx = 0
            self._rec_t0 = time.perf_counter()

    def cancel_recording(self):
        """Cancel an in-progress recording. Thread-safe."""
        with self._rec_lock:
            self._rec_cube = None
            self._rec_timestamps = None
            self._rec_gate = None
            self._rec_idx = 0

    @pyqtSlot()
    def run(self):
        """Main capture loop. Connected to QThread.started."""
        cam = self.camera
        drv = cam.driver
        cid = cam.cam_id

        w, h, _bin, img_t = cam.get_roi()
        bpp = 2 if img_t == int(ImgType.RAW16) else 1
        dtype = np.uint16 if bpp == 2 else np.uint8
        buf_size = w * h * bpp
        c_buf = (ctypes.c_ubyte * buf_size)()
        timeout_ms = max(2000, int(self.exposure_ms * 2 + 500))

        # Rate-limiting intervals
        stats_interval = 0.5
        prog_interval = 0.1
        last_stats = 0.0
        last_prog = 0.0

        fps_count = 0
        fps_t0 = time.perf_counter()
        total = 0
        fps = 0.0
        prev_arrival = None

        cam.start_video()
        try:
            while not self._stop.is_set():
                self._drain_cmds()
                rc = drv.get_video_data_raw(cid, c_buf, buf_size, timeout_ms)

                if rc == ASI_ERROR_TIMEOUT:
                    continue
                if rc != ASI_SUCCESS:
                    if not self._stop.is_set():
                        name = _ERROR_NAMES.get(rc, str(rc))
                        self.error.emit(
                            f"ASIGetVideoData -> {name} ({rc})"
                        )
                    continue

                frame = np.frombuffer(
                    c_buf, dtype=dtype
                ).reshape((h, w)).copy()
                now = time.perf_counter()
                delta = (now - prev_arrival) if prev_arrival else 0.0
                prev_arrival = now
                if delta > 0:
                    self._deltas.append(delta)
                total += 1
                fps_count += 1
                dt = now - fps_t0
                if dt >= 1.0:
                    fps = fps_count / dt
                    fps_count = 0
                    fps_t0 = now

                # -- Recording (pre-allocated cube) --
                rec_finished = None
                with self._rec_lock:
                    if self._rec_cube is not None and (
                        self._rec_gate is None
                        or self._rec_gate.accept(now, delta)
                    ):
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

                # Emit outside the lock.
                if rec_finished is not None:
                    self.recording_progress.emit(
                        rec_finished[0].shape[0],
                        rec_finished[0].shape[0],
                    )
                    self.recording_done.emit(*rec_finished)
                elif now - last_prog >= prog_interval:
                    with self._rec_lock:
                        rec_active = self._rec_cube is not None
                        rec_n = self._rec_idx
                        rec_t = self._rec_target
                    if rec_active:
                        last_prog = now
                        self.recording_progress.emit(rec_n, rec_t)

                # -- Display frame via queue (drop on overflow) --
                try:
                    self.frame_queue.put_nowait(frame)
                except queue.Full:
                    pass  # GUI is behind; drop this display copy

                # -- Stats (rate-limited to ~2 Hz) --
                if now - last_stats >= stats_interval:
                    last_stats = now
                    try:
                        dropped = cam.get_dropped()
                    except ASIError:
                        dropped = 0
                    try:
                        temp = cam.temperature()
                    except ASIError:
                        temp = float("nan")
                    self.stats_update.emit(fps, total, dropped, temp)

                    # Readonly control values, gathered here so no other
                    # thread ever needs to call into the SDK for them.
                    ro_vals = {}
                    for name, ct in self._readonly_ctrls.items():
                        try:
                            ro_vals[name] = cam.get_ctrl_value(ct)
                        except Exception:
                            pass
                    if ro_vals:
                        self.readonly_update.emit(ro_vals)

        finally:
            cam.stop_video()
