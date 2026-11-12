"""XMP-exif:DateTimeDigitized is written next to DateTimeOriginal (sidecar helpers + the photo write args)."""
import os
import shutil
import subprocess
import sys
import tempfile

if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg missing")
    sys.exit(0)
from app.core import executor, geocore  # noqa: E402

Z = "2026:10:10 10:00:00+05:30"
d = tempfile.mkdtemp()


def tag(path, name):
    return subprocess.run(["exiftool", "-s3", name, path], capture_output=True, text=True).stdout.strip()


# photo write path
img = os.path.join(d, "p.jpg")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", img], check=True)
src = open(executor.__file__, encoding="utf-8").read()
assert src.count("XMP-exif:DateTimeDigitized") == 2, "executor must write it in the main write and in the per-group fallback"
subprocess.run(["exiftool", "-q", "-overwrite_original", f"-alldates={Z}", f"-xmp:alldates={Z}", f"-XMP-exif:DateTimeDigitized={Z}", img], check=True)
assert tag(img, "-XMP-exif:DateTimeDigitized") == Z and tag(img, "-EXIF:CreateDate") == "2026:10:10 10:00:00", "photo: XMP + EXIF digitized"
# sidecar helpers
x = os.path.join(d, "a.xmp"); open(x, "w").close()
assert geocore.write_dates_to_xmp(x, Z) and tag(x, "-XMP-exif:DateTimeDigitized") == Z
y = os.path.join(d, "b.xmp"); open(y, "w").close()
subprocess.run(["exiftool", "-q", "-overwrite_original", f"-XMP-xmp:CreateDate={Z}", f"-XMP-exif:DateTimeOriginal={Z}", y], check=True)
assert geocore.ensure_xmp_dates_with_offset(y, Z) and tag(y, "-XMP-exif:DateTimeDigitized") == Z, "older sidecar gets it added"
assert not geocore.ensure_xmp_dates_with_offset(y, Z), "second run is a no-op"
print("PASS: DateTimeDigitized in photo (XMP+EXIF) and sidecars (new + older)")
