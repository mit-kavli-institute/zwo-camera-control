"""
FITS cube writer.

Saves an (N, H, W) data cube as a primary HDU with rich headers, plus a
BINTABLE extension with per-frame timestamps and inter-frame deltas for
timing-jitter analysis. I/O runs in a daemon thread so the GUI never blocks.

Uses a QObject signal bridge to safely callback on the GUI thread (as opposed
to QTimer.singleShot from a plain thread, which is unreliable on Windows).
"""

import threading

import numpy as np

from PyQt5.QtCore import QObject, pyqtSignal

try:
    from astropy.io import fits as pyfits
    HAS_ASTROPY = True
except ImportError:
    HAS_ASTROPY = False


class _DoneBridge(QObject):
    """One-shot signal bridge: worker thread -> GUI thread."""
    done = pyqtSignal(str)


def save_fits_cube(path, cube, timestamps, metadata, on_done):
    """
    Write a FITS cube to disk in a background thread.

    Parameters
    ----------
    path : str
        Output file path.
    cube : np.ndarray
        (N, H, W) data cube.
    timestamps : list[float]
        Per-frame times in seconds since recording start.
    metadata : dict
        FITS header keywords.
    on_done : callable(str)
        Callback with status message, called on the Qt GUI thread.
    """
    if not HAS_ASTROPY:
        on_done("FITS save error: astropy not installed")
        return

    # Bridge must be created on the GUI thread (here) so its signal
    # delivers to the GUI thread's event loop via QueuedConnection.
    bridge = _DoneBridge()
    bridge.done.connect(on_done)

    def _worker():
        try:
            hdr = pyfits.Header()
            for k, v in metadata.items():
                hdr[k] = v
            hdr["COMMENT"] = "ZWO ASI streaming demo cube"
            hdr["COMMENT"] = (
                f"Recorded {cube.shape[0]} frames in "
                f"{metadata.get('ELAPSED', 0):.3f}s"
            )

            primary = pyfits.PrimaryHDU(data=cube, header=hdr)

            # Per-frame timing table with index, timestamp, and delta-t
            ts_arr = np.array(timestamps, dtype=np.float64)
            dt_arr = np.diff(ts_arr, prepend=0.0)

            cols = [
                pyfits.Column(
                    name="FRAME_IDX", format="J",
                    array=np.arange(cube.shape[0], dtype=np.int32),
                ),
                pyfits.Column(
                    name="TIMESTAMP", format="D",
                    array=ts_arr, unit="s",
                ),
                pyfits.Column(
                    name="DELTA_T", format="D",
                    array=dt_arr, unit="s",
                ),
            ]
            timing_hdu = pyfits.BinTableHDU.from_columns(
                cols, name="FRAME_TIMING"
            )
            timing_hdu.header["COMMENT"] = (
                "TIMESTAMP = seconds since recording start "
                "(time.perf_counter)"
            )
            timing_hdu.header["COMMENT"] = (
                "DELTA_T = inter-frame interval; useful for jitter analysis"
            )

            hdul = pyfits.HDUList([primary, timing_hdu])
            hdul.writeto(path, overwrite=True, output_verify='silentfix')

            mb = cube.nbytes / 1e6
            elapsed = metadata.get("ELAPSED", 0)
            fps = cube.shape[0] / elapsed if elapsed > 0 else 0
            msg = (
                f"Saved {cube.shape[0]} frames -> {path}  "
                f"({mb:.1f} MB, {fps:.1f} fps)"
            )
        except Exception as exc:
            msg = f"FITS save error: {exc}"

        # Emit from worker thread; Qt delivers via QueuedConnection
        # to the GUI thread where bridge lives.
        bridge.done.emit(msg)

    threading.Thread(target=_worker, daemon=True, name="FITSSave").start()
