"""An empty GPSAltitude in the sidecar ('could not convert string to float') is 'no altitude'; one broken file does not abort
the GPS write for the rest of the folder.   python tests/test_gps_empty_altitude.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import exifpool, xmpsync  # noqa: E402
from app.modules.gps_sidecar import router as g  # noqa: E402


class R:
    stdout = '[{"DateTimeOriginal": "2022:05:01 10:00:00"}]'


real = exifpool.run
exifpool.run = lambda *a, **k: R()
try:
    for alt in ("", None, "abc"):
        assert xmpsync._image_extras_args("x.jpg", {"GPSAltitude": alt}, 5) == [], alt          # no crash, nothing to write
    assert xmpsync._image_extras_args("x.jpg", {"GPSAltitude": "-12.5", "GPSAltitudeRef": "1"}, 5) == ["-GPSAltitude=12.5", "-GPSAltitudeRef#=1"]
    assert xmpsync._image_extras_args("x.jpg", {"GPSAltitude": 430}, 5) == ["-GPSAltitude=430.0", "-GPSAltitudeRef#=0"]
finally:
    exifpool.run = real

# empty coordinates in the sidecar: no GPS args, no exception
real_read = xmpsync._read_current_xmp_fields
real_extras = xmpsync._image_extras_args
xmpsync._read_current_xmp_fields = lambda *a, **k: {"GPSLatitude": "", "GPSLongitude": "", "GPSAltitude": "", "Country": "Italien"}
xmpsync._image_extras_args = lambda *a, **k: []
cap = []
real_ex = xmpsync.exifpool.run
xmpsync.exifpool.run = lambda cmd, **k: (cap.append(cmd), R())[1]
try:
    xmpsync._sync_core("x.jpg", "x.jpg.xmp")
finally:
    xmpsync._read_current_xmp_fields, xmpsync._image_extras_args, xmpsync.exifpool.run = real_read, real_extras, real_ex
assert cap and not any("GPSLatitude" in a for a in cap[0]), cap

# one file that raises is reported, the others are still written
calls = []
real_one = g._execute_one


def fake_one(row, embed, delete_srt):
    calls.append(row["path"])
    if row["path"].endswith("bad.jpg"):
        raise ValueError("could not convert string to float: ''")
    return {"path": row["path"], "xmp_written": True, "xmp_error": "", "image_written": False, "image_error": ""}


g._execute_one = fake_one
real_fin = g._finish_execute
g._finish_execute = lambda results, a, b: {"results": results}
try:
    rows = [{"path": p} for p in ("/a/1.jpg", "/a/bad.jpg", "/a/3.jpg")]
    out = g._run_execute(rows, {r["path"] for r in rows}, True, False, "/a", "/a")
finally:
    g._execute_one, g._finish_execute = real_one, real_fin
res = out["results"]
assert calls == ["/a/1.jpg", "/a/bad.jpg", "/a/3.jpg"], calls
assert [r["xmp_written"] for r in res] == [True, False, True], res
assert "ValueError" in res[1]["xmp_error"] and ".py:" in res[1]["xmp_error"], res[1]
print("PASS")
