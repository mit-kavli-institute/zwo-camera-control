"""QHY GPS row-0 parser and per-frame metadata formatting."""

from cmos_camera_gui.vendors.qhy.sdk_wrapper import (
    parse_gps_from_frame, _decode_js, _jd_to_date,
)
from cmos_camera_gui.vendors.qhy.worker import QhyCaptureWorker


def _row(seq=123456, lat=987654, lon=456789, flag=51, js=973_382_400):
    row = bytearray(200)
    row[0:4] = seq.to_bytes(4, "big")
    row[9:13] = lat.to_bytes(4, "big")
    row[13:17] = lon.to_bytes(4, "big")
    row[17] = flag
    row[18:22] = js.to_bytes(4, "big")
    return bytes(row)


def test_byte_packing_roundtrip():
    gps = parse_gps_from_frame(_row(), w=100, bpp=16, channels=1)
    assert gps["seq"] == 123456
    assert gps["lat"] == 987654
    assert gps["lon"] == 456789
    assert gps["locked"] is True

    jd, hh, mm, ss = _decode_js(973_382_400)
    yy, mo, dd = _jd_to_date(jd)
    assert (gps["year"], gps["month"], gps["day"]) == (yy, mo, dd)
    assert (gps["hour"], gps["minute"], gps["second"]) == (hh, mm, ss)


def test_lock_flag():
    gps = parse_gps_from_frame(_row(flag=0), 100, 16, 1)
    assert gps["locked"] is False


def test_short_buffer_returns_none():
    assert parse_gps_from_frame(b"\x00" * 32, 100, 16, 1) is None


def test_frame_meta_formatting():
    meta = QhyCaptureWorker._gps_meta(
        {"seq": 7, "locked": True, "lat": 1, "lon": 2,
         "year": 2026, "month": 8, "day": 14,
         "hour": 3, "minute": 4, "second": 5}
    )
    assert meta["GPS_SEQ"] == 7
    assert meta["GPS_LOCK"] is True
    assert meta["DATE-BEG"] == "2026-08-14T03:04:05"
