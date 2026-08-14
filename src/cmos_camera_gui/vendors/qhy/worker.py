"""
QHY capture worker -- polls GetQHYCCDLiveFrame on a dedicated thread.

Implements the HANDOFF.md acquisition method:
- the camera streams continuously in LIVE mode; this thread is the sole
  SDK owner while streaming (other threads submit work via run_async);
- after BeginQHYCCDLive, stale frames drain out of the camera DDR --
  flush until ~0.4 s of silence before trusting the stream;
- a record keeps N frames after the RecordGate rejects transition /
  in-flight frames (the exposure re-issue at record start produces 2-3
  short/hybrid frames plus one GPS seq skip);
- TEC keep-alive (ControlQHYCCDTemp) is re-issued every ~3 s from here;
- when GPS is enabled, row 0 carries a binary header parsed into
  per-frame metadata; GPS sequence gaps are the honest dropped-frame
  count (one skip per exposure switch is expected and falls in the
  discarded transition).
"""

import queue
import threading
import time
from collections import deque

import numpy as np

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

from .sdk_wrapper import parse_gps_from_frame


class QhyCaptureWorker(QObject):
    """Same signal surface as the ZWO CaptureWorker."""

    stats_update = pyqtSignal(float, int, int, float)
    readonly_update = pyqtSignal(object)
    recording_progress = pyqtSignal(int, int)
    recording_done = pyqtSignal(object, object, float)
    error = pyqtSignal(str)

    def __init__(self, camera, exposure_ms, readonly_ctrls=None):
        super().__init__()
        self.camera = camera
        self.exposure_ms = exposure_ms
        self._readonly_ctrls = dict(readonly_ctrls or {})
        self._stop = threading.Event()

        self.frame_queue = queue.Queue(maxsize=2)
        self._cmd_queue = queue.Queue()

        self._rec_lock = threading.Lock()
        self._rec_cube = None
        self._rec_timestamps = None
        self._rec_meta = None
        self._rec_gate = None
        self._rec_target = 0
        self._rec_idx = 0
        self._rec_t0 = 0.0

        self.last_frame_meta = None   # per-frame meta of the last record
        self.last_gps = None          # most recent decoded GPS block

        self._deltas = deque(maxlen=8)
        self._dropped_seq = 0
        self._last_seq = None

    # -- cross-thread API (mirrors ZWO CaptureWorker) -----------------

    def request_stop(self):
        self._stop.set()

    def run_async(self, fn):
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
        if not self._deltas:
            return None
        vals = sorted(self._deltas)
        return vals[len(vals) // 2]

    def start_recording(self, n_frames, width, height, dtype, gate=None):
        with self._rec_lock:
            self._rec_cube = np.empty((n_frames, height, width), dtype=dtype)
            self._rec_timestamps = np.empty(n_frames, dtype=np.float64)
            self._rec_meta = []
            self._rec_gate = gate
            self._rec_target = n_frames
            self._rec_idx = 0
            self._rec_t0 = time.perf_counter()

    def cancel_recording(self):
        with self._rec_lock:
            self._rec_cube = None
            self._rec_timestamps = None
            self._rec_meta = None
            self._rec_gate = None
            self._rec_idx = 0

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _gps_meta(gps) -> dict:
        date_beg = (
            f"{gps['year']:04d}-{gps['month']:02d}-{gps['day']:02d}"
            f"T{gps['hour']:02d}:{gps['minute']:02d}:{gps['second']:02d}"
        )
        return {
            "GPS_SEQ": int(gps["seq"]),
            "GPS_LOCK": bool(gps["locked"]),
            "DATE-BEG": date_beg,
            "GPS_LAT": int(gps["lat"]),
            "GPS_LON": int(gps["lon"]),
        }

    # -- main loop ----------------------------------------------------

    @pyqtSlot()
    def run(self):
        cam = self.camera

        try:
            cam.start_video()
        except Exception as exc:
            self.error.emit(f"BeginQHYCCDLive failed: {exc}")
            return

        # Flush stale DDR frames (HANDOFF §3). Stale frames burst out
        # back-to-back, so stop at the first 0.35 s gap -- but cap the
        # whole drain at 1 s: at short exposures the fresh stream never
        # goes silent, and waiting for silence would drain forever.
        t0 = time.perf_counter()
        t_last = t0
        drained = 0
        while not self._stop.is_set():
            now = time.perf_counter()
            if now - t_last >= 0.35 or now - t0 >= 1.0:
                break
            if cam.get_live_frame() is not None:
                drained += 1
                t_last = time.perf_counter()
            else:
                time.sleep(0.002)

        stats_interval = 0.5
        prog_interval = 0.1
        keepalive_interval = 3.0
        last_stats = 0.0
        last_prog = 0.0
        last_keepalive = time.perf_counter()

        fps_count = 0
        fps_t0 = time.perf_counter()
        total = 0
        fps = 0.0
        prev_arrival = None
        loop_start = time.perf_counter()
        warned_no_frames = False

        try:
            while not self._stop.is_set():
                self._drain_cmds()

                now = time.perf_counter()
                if now - last_keepalive >= keepalive_interval:
                    last_keepalive = now
                    try:
                        cam.thermal_keepalive()
                    except Exception:
                        pass

                f = cam.get_live_frame()
                if f is None:
                    # Emit stats even between frames (long exposures).
                    if now - last_stats >= stats_interval:
                        last_stats = now
                        self._emit_stats(fps, total)
                    # Watchdog: a healthy stream must deliver within a
                    # few exposures; report instead of looking dead.
                    if (total == 0 and not warned_no_frames
                            and now - loop_start
                            > max(5.0, 3 * self.exposure_ms / 1000.0)):
                        warned_no_frames = True
                        self.error.emit(
                            f"no frames from camera "
                            f"{now - loop_start:.0f}s after stream start "
                            f"(exposure {self.exposure_ms:.0f} ms)"
                        )
                    time.sleep(0.001)
                    continue

                w, h, bpp, ch, data = f
                arrival = time.perf_counter()
                delta = (arrival - prev_arrival) if prev_arrival else 0.0
                prev_arrival = arrival
                if delta > 0:
                    self._deltas.append(delta)

                frame = np.frombuffer(data, dtype=np.uint16).reshape(
                    (h, w)
                ).copy()

                total += 1
                fps_count += 1
                dt = arrival - fps_t0
                if dt >= 1.0:
                    fps = fps_count / dt
                    fps_count = 0
                    fps_t0 = arrival

                # -- GPS metadata / sequence accounting --
                frame_meta = {}
                if cam.gps_enabled:
                    gps = parse_gps_from_frame(data, w, bpp, ch)
                    if gps:
                        frame_meta = self._gps_meta(gps)
                        self.last_gps = frame_meta
                        seq = gps["seq"]
                        if (self._last_seq is not None
                                and seq > self._last_seq + 1):
                            self._dropped_seq += seq - self._last_seq - 1
                        self._last_seq = seq

                # -- Recording (gate transitions, then keep N) --
                rec_finished = None
                with self._rec_lock:
                    if self._rec_cube is not None:
                        gate = self._rec_gate
                        if gate is not None and not gate.accept(
                                arrival, delta):
                            pass   # transition/in-flight frame: discard
                        else:
                            idx = self._rec_idx
                            if idx < self._rec_target:
                                self._rec_cube[idx] = frame
                                self._rec_timestamps[idx] = (
                                    arrival - self._rec_t0
                                )
                                self._rec_meta.append(frame_meta)
                                self._rec_idx = idx + 1
                                if idx + 1 >= self._rec_target:
                                    self.last_frame_meta = self._rec_meta
                                    rec_finished = (
                                        self._rec_cube,
                                        self._rec_timestamps.tolist(),
                                        arrival - self._rec_t0,
                                    )
                                    self._rec_cube = None
                                    self._rec_timestamps = None
                                    self._rec_meta = None
                                    self._rec_gate = None

                if rec_finished is not None:
                    self.recording_progress.emit(
                        rec_finished[0].shape[0], rec_finished[0].shape[0]
                    )
                    self.recording_done.emit(*rec_finished)
                elif arrival - last_prog >= prog_interval:
                    with self._rec_lock:
                        rec_active = self._rec_cube is not None
                        rec_n = self._rec_idx
                        rec_t = self._rec_target
                    if rec_active:
                        last_prog = arrival
                        self.recording_progress.emit(rec_n, rec_t)

                # -- Display frame (drop on overflow) --
                try:
                    self.frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

                # -- Stats --
                if arrival - last_stats >= stats_interval:
                    last_stats = arrival
                    self._emit_stats(fps, total)

        finally:
            cam.stop_video()

    def _emit_stats(self, fps, total):
        try:
            temp = self.camera.temperature()
        except Exception:
            temp = float("nan")
        self.stats_update.emit(fps, total, self._dropped_seq, temp)
        ro_vals = {}
        for name, ct in self._readonly_ctrls.items():
            try:
                ro_vals[name] = self.camera.get_ctrl_value(ct)
            except Exception:
                pass
        if ro_vals:
            self.readonly_update.emit(ro_vals)
