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


def _combined_hdu(cube, metadata, method):
    """Combine a (N, H, W) cube into one float32 frame (mean|median|sum)."""
    n = int(cube.shape[0])
    if method == "median":
        comb = np.median(cube, axis=0).astype(np.float32)
    elif method == "sum":
        comb = cube.sum(axis=0, dtype=np.float64).astype(np.float32)
    else:
        comb = cube.mean(axis=0, dtype=np.float64).astype(np.float32)
    hdr = pyfits.Header()
    for k, v in metadata.items():
        hdr[k] = v
    hdr["BUNIT"] = "ADU"
    hdr["NCOMBINE"] = (n, "number of frames combined")
    hdr["COMBINED"] = (method, "combine method (mean|median|sum)")
    hdr["COMMENT"] = "CMOS Control GUI combined frame"

    if method == "sum":
        # A summed stack reads like one long exposure: EXPTIME becomes the
        # net integration; the per-frame exposure moves to EXPFRAME.
        exp_frame = metadata.get("EXPTIME")
        if exp_frame is not None:
            exp_frame_ms = _scalar(exp_frame)
            hdr["EXPFRAME"] = (exp_frame_ms, "[ms] per-frame exposure")
            hdr["EXPTIME"] = (
                exp_frame_ms * n,
                f"[ms] net integration (sum of {n} frames)",
            )
        hdr["COMMENT"] = (
            f"Summed stack: bias/dark pedestal is {n}x a single frame;"
        )
        hdr["COMMENT"] = (
            f"read noise is sqrt({n})x a single frame of equal EXPTIME."
        )
    return pyfits.PrimaryHDU(data=comb, header=hdr)


def _combined_path(path, method):
    base, ext = os.path.splitext(path)
    return f"{base}_{method}{ext or '.fits'}"


def _frame_meta_hdu(n, timestamps=None, frame_meta=None):
    """Build a FRAMEMETA bintable of per-frame values for a cube save.

    Columns: FRAME_ID, TIMESTMP, DELTA_T (host timing) plus one column per
    key found in frame_meta dicts (e.g. GPS_SEQ, GPS_LOCK, DATE-BEG from
    the QHY GPS row). Returns None if there is nothing to record.
    """
    if timestamps is None and not frame_meta:
        return None

    cols = [pyfits.Column(name="FRAME_ID", format="K",
                          array=np.arange(n, dtype=np.int64))]
    if timestamps is not None:
        ts = np.asarray(timestamps, dtype=np.float64)
        cols.append(pyfits.Column(name="TIMESTMP", format="D", unit="s",
                                  array=ts))
        cols.append(pyfits.Column(name="DELTA_T", format="D", unit="s",
                                  array=np.diff(ts, prepend=0.0)))

    if frame_meta:
        keys = []
        for m in frame_meta:
            for k in (m or {}):
                if k not in keys:
                    keys.append(k)
        for k in keys:
            vals = [(m or {}).get(k) for m in frame_meta]
            sample = next((v for v in vals if v is not None), None)
            if isinstance(sample, bool):
                arr = np.array([bool(v) for v in vals])
                cols.append(pyfits.Column(name=k, format="L", array=arr))
            elif isinstance(sample, int):
                arr = np.array([int(v or 0) for v in vals], dtype=np.int64)
                cols.append(pyfits.Column(name=k, format="K", array=arr))
            elif isinstance(sample, float):
                arr = np.array([float(v or 0) for v in vals])
                cols.append(pyfits.Column(name=k, format="D", array=arr))
            else:
                strs = ["" if v is None else str(v) for v in vals]
                width = max(1, max(len(s) for s in strs))
                cols.append(pyfits.Column(name=k, format=f"{width}A",
                                          array=np.array(strs)))

    hdu = pyfits.BinTableHDU.from_columns(cols)
    hdu.header["EXTNAME"] = "FRAMEMETA"
    hdu.header["COMMENT"] = "per-frame metadata (host timing + vendor, e.g. GPS)"
    return hdu


def _atomic_writeto(hdu_or_list, path):
    """Write FITS to a temp file then atomically rename into place, so a
    failed save never leaves a partial file at the target path (WSP reads
    the file the instant is_capturing drops false)."""
    tmp = path + ".part"
    hdu_or_list.writeto(tmp, overwrite=True, output_verify="silentfix")
    os.replace(tmp, path)


def save_fits_cube(path, cube, metadata, on_done, combine="none",
                   combine_only=False, timestamps=None, frame_meta=None,
                   wsp_single=False):
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
    combine : {"none", "mean", "median", "sum"}
        Additionally save a combined float32 frame as
        ``{path minus ext}_{combine}.fits`` (NCOMBINE/COMBINED headers).
    combine_only : bool
        With combine != "none": skip the cube, save only the combined
        frame.
    timestamps : list[float], optional
        Per-frame host timestamps [s since record start]; stored in a
        FRAMEMETA bintable extension of the cube file.
    frame_meta : list[dict], optional
        Per-frame vendor metadata (e.g. QHY GPS seq/UTC); stored as
        FRAMEMETA columns alongside the host timing.
    wsp_single : bool
        WSP contract output (SummerCameraGuiHandoff R3): for a 1-frame
        cube, write a single 2D image HDU (frame metadata as header
        cards, no FRAMEMETA extension); multi-frame cubes keep the 3D
        primary. Writes are atomic (temp + rename) either way.
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
            elapsed = _scalar(metadata.get("ELAPSED", 0))
            fps = cube.shape[0] / elapsed if elapsed > 0 else 0
            parts = []

            if not (combine != "none" and combine_only):
                hdr = pyfits.Header()
                for k, v in metadata.items():
                    hdr[k] = v
                hdr["BUNIT"] = "ADU"
                # FITS headers must be printable ASCII -- no unicode dashes
                hdr["COMMENT"] = "CMOS Control GUI frame cube"
                hdr["COMMENT"] = (
                    f"Recorded {cube.shape[0]} frames in {elapsed:.3f}s"
                )
                if wsp_single and cube.shape[0] == 1:
                    # WSP R3: single 2D image HDU at the exact path.
                    if frame_meta and frame_meta[0]:
                        for k, v in frame_meta[0].items():
                            hdr[k] = v
                    if timestamps:
                        hdr["TIMESTMP"] = (float(timestamps[0]),
                                           "[s] since recording start")
                    primary = pyfits.PrimaryHDU(data=cube[0], header=hdr)
                    _atomic_writeto(primary, path)
                else:
                    primary = pyfits.PrimaryHDU(data=cube, header=hdr)
                    hdus = [primary]
                    meta_hdu = _frame_meta_hdu(cube.shape[0], timestamps,
                                               frame_meta)
                    if meta_hdu is not None:
                        hdus.append(meta_hdu)
                    _atomic_writeto(pyfits.HDUList(hdus), path)
                mb = cube.nbytes / 1e6
                parts.append(
                    f"{cube.shape[0]} frames -> {path}  "
                    f"({mb:.1f} MB, {fps:.1f} fps)"
                )

            if combine != "none":
                cpath = _combined_path(path, combine)
                hdu = _combined_hdu(cube, metadata, combine)
                hdu.writeto(cpath, overwrite=True, output_verify='silentfix')
                parts.append(f"{combine} of {cube.shape[0]} -> {cpath}")

            msg = "Saved " + "; ".join(parts)
        except Exception as exc:
            msg = f"FITS save error: {exc}"

        # Emit from worker thread; Qt delivers via QueuedConnection
        # to the GUI thread where bridge lives.
        bridge.done.emit(msg)

    threading.Thread(target=_worker, daemon=True, name="FITSSave").start()


def save_fits_individual(directory, basename, cube, timestamps, metadata,
                         on_done, combine="none", combine_only=False,
                         frame_meta=None):
    """
    Write one FITS file per frame: {directory}/{basename}_NNNN.fits.

    Each file gets the full `metadata` dict in its header plus per-frame
    FRAME_IDX, TIMESTAMP (s since start), and DELTA_T (s since previous
    frame), plus any per-frame vendor metadata (frame_meta[i] dict, e.g.
    QHY GPS seq/UTC) as header cards.

    combine/combine_only: as in save_fits_cube — additionally (or only)
    save {basename}_{combine}.fits.
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
            skip_frames = combine != "none" and combine_only
            for i in range(n if not skip_frames else 0):
                hdr = pyfits.Header()
                for k, v in metadata.items():
                    hdr[k] = v
                hdr["FRAME_ID"] = (int(i), "frame index within the series")
                hdr["TIMESTMP"] = (float(ts_arr[i]), "[s] since recording start")
                hdr["DELTA_T"] = (float(dt_arr[i]), "[s] since previous frame")
                if frame_meta and i < len(frame_meta) and frame_meta[i]:
                    for k, v in frame_meta[i].items():
                        hdr[k] = v
                # FITS headers must be printable ASCII -- no unicode dashes
                hdr["COMMENT"] = "CMOS Control GUI individual frame"

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
            parts = []
            if not skip_frames:
                parts.append(
                    f"{written} files -> {directory}  "
                    f"({mb:.1f} MB, {fps:.1f} fps)"
                )
            if combine != "none":
                cpath = os.path.join(
                    directory, f"{basename}_{combine}.fits"
                )
                hdu = _combined_hdu(cube, metadata, combine)
                hdu.writeto(cpath, overwrite=True, output_verify="silentfix")
                parts.append(f"{combine} of {n} -> {cpath}")
            msg = "Saved " + "; ".join(parts)
        except Exception as exc:
            msg = f"FITS save error: {exc}"

        bridge.done.emit(msg)

    threading.Thread(target=_worker, daemon=True, name="FITSSaveIndiv").start()
