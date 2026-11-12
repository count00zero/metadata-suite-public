"""Altitude + time zone next to the position: sidecar (only where missing) and image (via the XMP snapshot).
python tests/test_place_extras.py   (needs exiftool + ffmpeg, else SKIP)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg missing")
    sys.exit(0)
os.environ["GEO_SERVICE_URL"] = "http://x"
from app.core import geocore, geoservice, xmpsync  # noqa: E402

calls = []
geoservice.enabled = lambda: True
geoservice.elevation = lambda la, lo: (calls.append(("elev", la, lo)), 215.3)[1]
geoservice.timezone_id = lambda la, lo: "Asia/Colombo"

d = tempfile.mkdtemp()


def make(name, *exif):
    p = os.path.join(d, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", p], check=True)
    subprocess.run(["exiftool", "-overwrite_original", "-q", *exif, p], check=True, capture_output=True)
    xmp = p + ".xmp"
    subprocess.run(["exiftool", "-q", "-o", xmp, p], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSLatitude=6.0249942", "-XMP-exif:GPSLongitude=80.2189092", xmp], check=True)
    return p, xmp


def read(path, *tags):
    out = subprocess.run(["exiftool", "-config", geocore.MON_CONFIG, "-j", "-n", "-m", *tags, path], capture_output=True, text=True).stdout
    return json.loads(out)[0]


def fail(msg):
    print("FAIL", msg)
    sys.exit(1)


# 1. nothing there -> service height + zone
img, xmp = make("a.jpg", "-DateTimeOriginal=2026:10:10 10:00:00")
r = geocore.write_place_extras(xmp, 6.0249942, 80.2189092)
m = read(xmp, "-XMP-exif:GPSAltitude", "-XMP-exif:GPSAltitudeRef", "-XMP-mon:AltitudeSource", "-XMP-mon:CtxTimezone")
if not (r["altitude"] == 215.3 and r["altitude_source"] == "dem" and r["timezone"] == "Asia/Colombo"):
    fail(r)
if not (m["GPSAltitude"] == 215.3 and m["GPSAltitudeRef"] == 0 and m["AltitudeSource"] == "dem" and m["CtxTimezone"] == "Asia/Colombo"):
    fail(m)
print("PASS: sidecar gets GPSAltitude(+Ref), AltitudeSource and CtxTimezone")

# 2. second run adds nothing
n = len(calls)
r = geocore.write_place_extras(xmp, 6.0249942, 80.2189092)
if r["altitude"] is not None or r["timezone"] or len(calls) != n:
    fail(("second run must not overwrite/ask again", r))
print("PASS: existing altitude/zone are never overwritten")

# 3. the file's own altitude wins over the DEM (and below sea level -> Ref 1)
img2, xmp2 = make("b.jpg", "-GPSLatitude=6.0", "-GPSLatitudeRef=N", "-GPSLongitude=80.0", "-GPSLongitudeRef=E", "-GPSAltitude=12.5", "-GPSAltitudeRef#=1")
subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSAltitude=", "-XMP-exif:GPSAltitudeRef=", xmp2], check=True)  # sidecar creation had copied it
n = len(calls)
r = geocore.write_place_extras(xmp2, 6.0, 80.0)
m = read(xmp2, "-XMP-exif:GPSAltitude", "-XMP-exif:GPSAltitudeRef", "-XMP-mon:AltitudeSource")
if not (r["altitude_source"] == "camera" and r["altitude"] == -12.5 and m["GPSAltitude"] == 12.5 and m["GPSAltitudeRef"] == 1
        and m["AltitudeSource"] == "camera" and len(calls) == n):
    fail((r, m))
print("PASS: camera altitude (below sea level) preferred over the service")

# 4. embedding into the image: altitude only if the image has none; offset only on opt-in
ok, msg = xmpsync.sync_xmp_to_image(img, xmp)
a = read(img, "-EXIF:GPSAltitude", "-EXIF:GPSAltitudeRef", "-EXIF:OffsetTimeOriginal")
if not (ok and a.get("GPSAltitude") == 215.3 and a.get("GPSAltitudeRef") == 0 and a.get("OffsetTimeOriginal") == "+05:30"):
    fail((ok, msg, a))
print("PASS: embedding writes GPSAltitude and (default on) OffsetTimeOriginal into the image")
subprocess.run(["exiftool", "-q", "-overwrite_original", "-GPSAltitude=999", "-GPSAltitudeRef#=0", "-EXIF:OffsetTimeOriginal=", img], check=True)
os.environ["IMAGE_OFFSET_FROM_ZONE"] = "false"
ok, msg = xmpsync.sync_xmp_to_image(img, xmp)
if "OffsetTimeOriginal" in read(img, "-EXIF:OffsetTimeOriginal"):
    fail("IMAGE_OFFSET_FROM_ZONE=false must not write an offset")
os.environ.pop("IMAGE_OFFSET_FROM_ZONE")
ok, msg = xmpsync.sync_xmp_to_image(img, xmp)
a = read(img, "-EXIF:GPSAltitude", "-EXIF:OffsetTimeOriginal")
if not (ok and a["GPSAltitude"] == 999 and a["OffsetTimeOriginal"] == "+05:30"):
    fail((ok, msg, a))
subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:OffsetTimeOriginal=+01:00", img], check=True)
xmpsync.sync_xmp_to_image(img, xmp)
if read(img, "-EXIF:OffsetTimeOriginal")["OffsetTimeOriginal"] != "+01:00":
    fail("existing offset must stay")
print("PASS: image's own altitude/offset kept; IMAGE_OFFSET_FROM_ZONE=false switches the offset off")

# 5. switch off
os.environ["XMP_PLACE_EXTRAS"] = "false"
img3, xmp3 = make("c.jpg")
if geocore.write_place_extras(xmp3, 1.0, 2.0)["altitude"] is not None or "GPSAltitude" in read(xmp3, "-XMP-exif:GPSAltitude"):
    fail("XMP_PLACE_EXTRAS=false must switch it off")
print("PASS: XMP_PLACE_EXTRAS=false")

# 6. offset arithmetic
if xmpsync._offset_for("Europe/Vienna", "2026:07:01 12:00:00") != "+02:00" or xmpsync._offset_for("America/St_Johns", "2026:01:01 12:00:00") != "-03:30":
    fail("offset arithmetic")
print("PASS: offset arithmetic (DST, half-hour zones)")
print("\nALL PLACE-EXTRAS CHECKS PASSED")
