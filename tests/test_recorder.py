"""FITS writers: combine modes, FRAMEMETA, per-frame cards, WSP output."""

import os

import numpy as np
from astropy.io import fits

from cmos_camera_gui.recorder import save_fits_cube, save_fits_individual


CUBE = np.random.default_rng(1).integers(
    100, 1000, size=(7, 32, 48), dtype=np.uint16)
META = {"INSTRUME": "test", "NFRAMES": 7, "ELAPSED": (1.0, "s"),
        "EXPTIME": (10.0, "[ms] exposure time")}
TS = [i * 0.1 for i in range(7)]
FM = [{"GPS_SEQ": 10 + i, "GPS_LOCK": True,
       "DATE-BEG": f"2026-08-14T00:00:0{i}"} for i in range(7)]


def test_cube_plus_mean(run_save, tmp_path):
    p = str(tmp_path / "a.fits")
    run_save(save_fits_cube, p, CUBE, META, combine="mean")
    assert os.path.exists(p)
    with fits.open(str(tmp_path / "a_mean.fits")) as h:
        d = h[0].data
        assert d.dtype.kind == "f" and d.dtype.itemsize == 4
        assert np.allclose(d, CUBE.mean(axis=0), atol=1e-3)
        assert h[0].header["NCOMBINE"] == 7
        assert h[0].header["COMBINED"] == "mean"


def test_median_combine_only_skips_cube(run_save, tmp_path):
    p = str(tmp_path / "b.fits")
    run_save(save_fits_cube, p, CUBE, META, combine="median",
             combine_only=True)
    assert not os.path.exists(p)
    with fits.open(str(tmp_path / "b_median.fits")) as h:
        assert np.allclose(h[0].data, np.median(CUBE, axis=0), atol=1e-3)


def test_sum_net_exptime(run_save, tmp_path):
    p = str(tmp_path / "d.fits")
    run_save(save_fits_cube, p, CUBE, META, combine="sum",
             combine_only=True)
    with fits.open(str(tmp_path / "d_sum.fits")) as h:
        assert np.allclose(h[0].data, CUBE.sum(axis=0), atol=1e-2)
        assert h[0].header["NCOMBINE"] == 7
        assert abs(h[0].header["EXPTIME"] - 70.0) < 1e-6
        assert abs(h[0].header["EXPFRAME"] - 10.0) < 1e-6


def test_framemeta_bintable(run_save, tmp_path):
    p = str(tmp_path / "gps.fits")
    run_save(save_fits_cube, p, CUBE[:4], META,
             timestamps=TS[:4], frame_meta=FM[:4])
    with fits.open(p) as h:
        t = h["FRAMEMETA"].data
        assert list(t["GPS_SEQ"]) == [10, 11, 12, 13]
        assert t["DATE-BEG"][2] == "2026-08-14T00:00:02"
        assert abs(t["TIMESTMP"][3] - 0.3) < 1e-9


def test_individual_per_frame_cards(run_save, tmp_path):
    run_save(save_fits_individual, str(tmp_path), "ind",
             CUBE[:4], TS[:4], META, frame_meta=FM[:4])
    with fits.open(str(tmp_path / "ind_0002.fits")) as h:
        assert h[0].header["GPS_SEQ"] == 12
        assert h[0].header["DATE-BEG"] == "2026-08-14T00:00:02"


def test_wsp_single_is_2d_no_extensions(run_save, tmp_path):
    p = str(tmp_path / "wsp.fits")
    run_save(save_fits_cube, p, CUBE[:1], META,
             timestamps=TS[:1], frame_meta=FM[:1], wsp_single=True)
    with fits.open(p) as h:
        assert h[0].data.ndim == 2
        assert len(h) == 1                      # no FRAMEMETA extension
        assert h[0].header["GPS_SEQ"] == 10     # meta as header cards
    assert not os.path.exists(p + ".part")      # atomic write cleaned up


def test_wsp_multi_frame_is_single_cube_file(run_save, tmp_path):
    p = str(tmp_path / "wsp3.fits")
    run_save(save_fits_cube, p, CUBE[:3], META,
             timestamps=TS[:3], frame_meta=FM[:3], wsp_single=True)
    with fits.open(p) as h:
        assert h[0].data.shape[0] == 3


def test_ascii_only_headers(run_save, tmp_path):
    # em-dash regression: astropy rejects non-ASCII header text
    p = str(tmp_path / "ascii.fits")
    msg = run_save(save_fits_individual, str(tmp_path), "ascii",
                   CUBE[:2], TS[:2], META)
    assert not msg.startswith("FITS save error"), msg
