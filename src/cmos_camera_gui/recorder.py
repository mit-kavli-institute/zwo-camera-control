"""
FITS writers.

save_fits_cube        -- single FITS file with the (N, H, W) cube in the
                         primary HDU and all metadata in its header.
save_fits_individual  -- one FITS file per frame, named {basename}_NNNN.fits.

Both run I/O on a daemon thread so the GUI never blocks, and use a QObject
signal bridge to deliver the completion callback on the GUI thread.
"""

import os
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


def _scalar(v) -> float:
    """Unwrap a possibly (value, comment) header entry to just the value."""
    if isinstance(v, tuple) and len(v) == 2:
        return float(v[0])
    return float(v)


def save_fits_cube(path, cube, metadata, on_done):
    """
    Write a FITS cube to disk in a background thread.

    The cube is saved as a single-HDU FITS image: a PrimaryHDU holding
    the (N, H, W) data with all camera/run metadata in its header.

    Parameters
    ----------
    path : str
        Output file path.
    cube : np.ndarray
        (N, H, W) data cube.
    metadata : dict
        FITS header keywords (camera controls, ROI, elapsed, fps, ...).
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
            hdr["BUNIT"] = "ADU"
            elapsed = _scalar(metadata.get("ELAPSED", 0))
            hdr["COMMENT"] = "ZWO ASI streaming demo cube"
            hdr["COMMENT"] = (
                f"Recorded {cube.shape[0]} frames in {elapsed:.3f}s"
            )

            primary = pyfits.PrimaryHDU(data=cube, header=hdr)
            primary.writeto(path, overwrite=True, output_verify='silentfix')

            mb = cube.nbytes / 1e6
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


def save_fits_individual(directory, basename, cube, timestamps, metadata, on_done):
    """
    Write one FITS file per frame: {directory}/{basename}_NNNN.fits.

    Each file gets the full `metadata` dict in its header plus per-frame
    FRAME_IDX, TIMESTAMP (s since start), and DELTA_T (s since previous frame).
    """
    if not HAS_ASTROPY:
        on_done("FITS save error: astropy not installed")
        return

    bridge = _DoneBridge()
    bridge.done.connect(on_done)

    def _worker():
        try:
            os.makedirs(directory, exist_ok=True)
            ts_arr = np.array(timestamps, dtype=np.float64)
            dt_arr = np.diff(ts_arr, prepend=0.0)
            n = cube.shape[0]
            width = max(4, len(str(max(n - 1, 0))))

            written = 0
            total_bytes = 0
            for i in range(n):
                hdr = pyfits.Header()
                for k, v in metadata.items():
                    hdr[k] = v
                hdr["FRAME_ID"] = (int(i), "frame index within the series")
                hdr["TIMESTMP"] = (float(ts_arr[i]), "[s] since recording start")
                hdr["DELTA_T"] = (float(dt_arr[i]), "[s] since previous frame")
                hdr["COMMENT"] = "ZWO ASI streaming demo — individual frame"

                path = os.path.join(
                    directory, f"{basename}_{i:0{width}d}.fits"
                )
                hdu = pyfits.PrimaryHDU(data=cube[i], header=hdr)
                hdu.writeto(path, overwrite=True, output_verify="silentfix")
                written += 1
                total_bytes += cube[i].nbytes

            mb = total_bytes / 1e6
            elapsed = _scalar(metadata.get("ELAPSED", 0))
            fps = n / elapsed if elapsed > 0 else 0
            msg = (
                f"Saved {written} files -> {directory}  "
                f"({mb:.1f} MB, {fps:.1f} fps)"
            )
        except Exception as exc:
            msg = f"FITS save error: {exc}"

        bridge.done.emit(msg)

    threading.Thread(target=_worker, daemon=True, name="FITSSaveIndiv").start()
