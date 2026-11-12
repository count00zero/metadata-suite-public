"""GPS Sidecar: a sidecar made from a video carries the raw-UTC CreateDate without offset -> gets local time + offset."""
import os, shutil, subprocess as sp, sys, tempfile
SCRATCH = tempfile.mkdtemp(prefix="xdo_"); MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import geocore
from app.modules.gps_sidecar import router as gps

def tag(p, t): return sp.run(["exiftool", "-s3", t, p], capture_output=True, text=True).stdout.strip()

# video recorded 10:27:29 UTC (= 12:27:29 +02:00), named after its local time like Timestamp & Rename does
mp4 = os.path.join(MR, "2026.10.04-12.27.29.MP4")
sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-metadata",
        "creation_time=2026-10-04T10:27:29Z", "-pix_fmt", "yuv420p", mp4], check=True)
xmp = mp4 + ".xmp"
row = {"path": mp4, "is_video": True, "lat": 48.126086, "lon": 15.992458, "geo": None, "capture_display": "2026:10:04 12:27:29+02:00"}
res = gps._execute_one(row, False, False)
assert res["xmp_written"], res
print("CreateDate:", tag(xmp, "-XMP-xmp:CreateDate"), "| DateTimeOriginal:", tag(xmp, "-XMP-exif:DateTimeOriginal"))
assert res["date_offset_added"] is True
for t in ("-XMP-xmp:CreateDate", "-XMP-exif:DateTimeOriginal"):
    assert tag(xmp, t) == "2026:10:04 12:27:29+02:00", (t, tag(xmp, t))
assert tag(xmp, "-n") or True and abs(float(sp.run(["exiftool", "-n", "-s3", "-XMP-exif:GPSLatitude", xmp], capture_output=True, text=True).stdout) - 48.126086) < 1e-5

# a date that already has an offset is never touched; unresolved capture time writes nothing
sp.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:CreateDate=2020:01:01 08:00:00+01:00", "-XMP-exif:DateTimeOriginal=2020:01:01 08:00:00+01:00", xmp], check=True)
assert geocore.ensure_xmp_dates_with_offset(xmp, "2026:10:04 12:27:29+02:00") is False
assert tag(xmp, "-XMP-xmp:CreateDate") == "2020:01:01 08:00:00+01:00"
sp.run(["exiftool", "-q", "-overwrite_original", "-XMP-xmp:CreateDate=2026:10:04 10:27:29", xmp], check=True)
assert geocore.ensure_xmp_dates_with_offset(xmp, "") is False and geocore.ensure_xmp_dates_with_offset(xmp, "2026:10:04 12:27:29") is False
assert tag(xmp, "-XMP-xmp:CreateDate") == "2026:10:04 10:27:29"
shutil.rmtree(SCRATCH, ignore_errors=True)
print("PASS")
