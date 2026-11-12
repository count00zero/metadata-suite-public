"""IPTC Credits/Origin (IrfanView: Date created / Source / Credits) filled only where empty.
python tests/test_iptc_fill.py  (needs exiftool + ffmpeg, else SKIP)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

os.environ["IPTC_CREDIT"] = "Familie Beispiel"      # there is no built-in default credit
if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg missing")
    sys.exit(0)
from app.core import devicenames, geocore, iptcfill, xmpsync  # noqa: E402

d = tempfile.mkdtemp()


def make(name, *exif):
    p = os.path.join(d, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", p], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", *exif, p], check=True, capture_output=True)
    return p


def read(path, *tags):
    out = subprocess.run(["exiftool", "-config", geocore.MON_CONFIG, "-j", "-G1", "-m", *tags, path], capture_output=True, text=True).stdout
    return json.loads(out)[0]


def fail(m):
    print("FAIL", m)
    sys.exit(1)


T = ["-IPTC:DateCreated", "-IPTC:TimeCreated", "-IPTC:Source", "-IPTC:Credit", "-XMP-photoshop:DateCreated", "-XMP-photoshop:Source", "-XMP-photoshop:Credit"]
exp_cam = devicenames.translate("samsung", "SM-S901B")["label"]
assert exp_cam, "camera mapping returned nothing"

# 1. empty IPTC -> filled
img = make("a.jpg", "-DateTimeOriginal=2026:10:04 12:24:39", "-OffsetTimeOriginal=+02:00", "-Make=samsung", "-Model=SM-S901B")
ok, msg, w = iptcfill.fill_image(img)
r = read(img, *T)
if not (ok and r.get("IPTC:DateCreated") == "2026:10:04" and r.get("IPTC:TimeCreated") == "12:24:39+02:00" and r.get("IPTC:Source") == exp_cam
        and r.get("IPTC:Credit") == "Familie Beispiel" and r.get("XMP-photoshop:Credit") == "Familie Beispiel"
        and r.get("XMP-photoshop:Source") == exp_cam and str(r.get("XMP-photoshop:DateCreated")).startswith("2026:10:04 12:24:39")):
    fail((ok, msg, r))
print("PASS: IPTC Date created / Source (mapped camera name) / Credits filled")

# 2. second run: nothing to do; existing values (Samsung style) are never overwritten
ok, msg, w = iptcfill.fill_image(img)
if w:
    fail(("second run wrote", w))
img2 = make("b.jpg", "-DateTimeOriginal=2026:10:04 12:24:39", "-IPTC:DateCreated=2020:01:01", "-IPTC:Source=Mein Handy", "-IPTC:Credit=Oma",
            "-Make=samsung", "-Model=SM-S901B")
iptcfill.fill_image(img2)
r = read(img2, *T)
if not (r["IPTC:DateCreated"] == "2020:01:01" and r["IPTC:Source"] == "Mein Handy" and r["IPTC:Credit"] == "Oma" and "IPTC:TimeCreated" not in r):
    fail(r)
print("PASS: existing IPTC values kept; no TimeCreated without a known offset (exiftool would guess the server zone)")

# 3. switches
os.environ["IPTC_CREDIT"] = ""
img3 = make("c.jpg", "-DateTimeOriginal=2026:10:04 12:24:39")
iptcfill.fill_image(img3)
if "IPTC:Credit" in read(img3, *T):
    fail("empty IPTC_CREDIT must write no credit")
os.environ["IPTC_CREDIT"] = "Familie Beispiel"
os.environ["IPTC_FILL"] = "false"
img4 = make("d.jpg", "-DateTimeOriginal=2026:10:04 12:24:39")
if iptcfill.fill_image(img4)[2] or "IPTC:DateCreated" in read(img4, *T):
    fail("IPTC_FILL=false must switch it off")
os.environ.pop("IPTC_FILL")
print("PASS: IPTC_CREDIT empty / IPTC_FILL=false")

# 4. embedding path: Source comes from the sidecar's camera name, Date created from the image
img5 = make("e.jpg", "-DateTimeOriginal=2026:10:04 12:24:39", "-OffsetTimeOriginal=+02:00")
xmp = img5 + ".xmp"
subprocess.run(["exiftool", "-q", "-o", xmp, img5], check=True)
subprocess.run(["exiftool", "-config", geocore.MON_CONFIG, "-q", "-overwrite_original", "-XMP-mon:CameraName=DJI Neo 2",
                "-XMP-exif:GPSLatitude=47.9", "-XMP-exif:GPSLongitude=16.2", xmp], check=True)
ok, msg = xmpsync.sync_xmp_to_image(img5, xmp)
r = read(img5, *T)
if not (ok and r.get("IPTC:Source") == "DJI Neo 2" and r.get("IPTC:DateCreated") == "2026:10:04" and r.get("IPTC:Credit") == "Familie Beispiel"):
    fail((ok, msg, r))
print("PASS: embedding fills IPTC from the sidecar's camera name")

# 5. sidecar (videos): XMP twins only
vx = os.path.join(d, "v.mp4.xmp")
open(vx, "w").close()
subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:DateTimeOriginal=2026:10:04 12:24:39+02:00", vx], check=True)
ok, msg = iptcfill.fill_sidecar(vx, "DJI Neo 2")
r = read(vx, "-XMP-photoshop:DateCreated", "-XMP-photoshop:Source", "-XMP-photoshop:Credit")
if not (ok and r.get("XMP-photoshop:Source") == "DJI Neo 2" and r.get("XMP-photoshop:Credit") == "Familie Beispiel"
        and str(r.get("XMP-photoshop:DateCreated")).startswith("2026:10:04 12:24:39")):
    fail((ok, msg, r))
if iptcfill.fill_sidecar(vx, "Other")[0] is not True or read(vx, "-XMP-photoshop:Source")["XMP-photoshop:Source"] != "DJI Neo 2":
    fail("sidecar value overwritten")
print("PASS: sidecar gets Credit/Source/DateCreated (never overwritten)")

# 6. hooks are in place
for f, needle in (("app/core/executor.py", "iptcfill.fill_image"), ("app/core/ctxfill.py", "iptcfill.fill_sidecar")):
    if needle not in open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f), encoding="utf-8").read():
        fail(f"hook missing: {f}")
print("PASS: hooks in Timestamp & Rename and Kontext nachfüllen")
print("\nALL IPTC CHECKS PASSED")
