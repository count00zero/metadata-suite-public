"""Collage metadata source: a photo WITH position + date is chosen (image or .xmp sidecar), sidecar-only values reach the collage."""
import os, subprocess, sys, tempfile, shutil
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
if not shutil.which("exiftool"):
    print("skip (no exiftool)"); sys.exit(0)
from PIL import Image
from app.core import collagecore as cc

d = tempfile.mkdtemp()
def img(n):
    p = os.path.join(d, n); Image.new("RGB", (60, 40), (9, 9, 9)).save(p); return p
bare1, bare2, withsc = img("a.jpg"), img("b.jpg"), img("c.jpg")
subprocess.run(["exiftool", "-q", "-o", withsc + ".xmp", "-XMP-exif:GPSLatitude=48.1454285", "-XMP-exif:GPSLongitude=16.3257166",
                "-XMP-exif:DateTimeOriginal=2023:04:29 13:53:42+02:00", withsc], check=True)

# 1) the source is never one of the bare photos
for _ in range(10):
    assert cc.pick_metadata_source([bare1, bare2, withsc]) == withsc
# 2) nothing has both -> still returns one of them, no crash
assert cc.pick_metadata_source([bare1, bare2]) in (bare1, bare2)

# 3) sidecar-only GPS/date reach the collage (image itself carries nothing)
out = img("out.jpg")
ok, msg = cc.copy_metadata_from_source(withsc, out)
assert ok, msg
r = subprocess.run(["exiftool", "-n", "-s3", "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-OffsetTimeOriginal", out],
                   capture_output=True, text=True).stdout.split("\n")
assert r[0].startswith("48.145") and r[1].startswith("16.325"), r
assert r[2] == "2023:04:29 13:53:42" and r[3] == "+02:00", r
print("all ok", r[:4])
