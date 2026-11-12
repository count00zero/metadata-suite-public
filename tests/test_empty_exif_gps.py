"""Empty EXIF GPS tags ('') mean "no GPS" -- they used to crash resolve_one with 'could not convert string to float: '''.
python tests/test_empty_exif_gps.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import timecore  # noqa: E402

d = tempfile.mkdtemp()
f = os.path.join(d, "IMG_0001.jpg")
open(f, "wb").write(b"\xff\xd8\xff\xd9")

real = timecore.get_exif
try:
    for lat, lon, expect in (("", "", None), (None, None, None), ("abc", "1", None), ("", "15.4", None), ("47.07", "15.44", (47.07, 15.44)),
                             (47.07, 15.44, (47.07, 15.44))):
        timecore.get_exif = lambda *a, **k: {"DateTimeOriginal": "2022:05:01 10:00:00", "GPSLatitude": lat, "GPSLongitude": lon}
        r = timecore.resolve_one(f, "EXIF", "Europe/Vienna")
        got = (r.lat, r.lon) if r.lat is not None else None
        assert got == expect, (lat, lon, got)
        assert r.has_gps == (expect is not None and expect != (0, 0)), (lat, lon, r.has_gps)
finally:
    timecore.get_exif = real
print("PASS")
