"""GPS Sidecar writes raw Make/Model (standard XMP-tiff) + the mapped readable name (XMP-mon:CameraName)."""
import json, os, shutil, subprocess as sp, sys, tempfile
SCRATCH = tempfile.mkdtemp(prefix="cam_"); MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CAMERA_NAMES_FILE"] = os.path.join(root, "config", "camera_names.example.json")      # the shipped example incl. DJI Neo 2
sys.path.insert(0, root)
from app.core import geocore
from app.modules.gps_sidecar import router as gps

def tags(xmp):
    out = sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-j", "-G1", "-XMP-tiff:all", "-XMP-mon:all", xmp], capture_output=True, text=True).stdout
    return json.loads(out)[0]

def mk(name, *exif):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-metadata",
            "creation_time=2026-10-04T10:24:39Z", "-pix_fmt", "yuv420p", p], check=True)
    if exif:
        sp.run(["exiftool", "-q", "-overwrite_original", *exif, p], check=True)
    return p

def run(p):
    return gps._execute_one({"path": p, "is_video": True, "lat": 47.9, "lon": 16.0, "geo": None,
                             "capture_display": "2026:10:04 12:24:39+02:00"}, False, False)

# DJI Neo 2: no Make/Model in the file, only the vendor tag -> derived raw FC9470 -> mapped "DJI Neo 2"
neo = mk("2026.10.04-12.24.39.MP4", "-XMP-photoshop:Category=pb_file:dvtm_NEO2.proto;model_name:FC9470;pb_version:02.00.07;pb_lib_version:02.01.15;")
r = run(neo); assert r["xmp_written"] and r["camera_name"] == "DJI Neo 2" and r["camera_name_source"] == "override", r
t = tags(neo + ".xmp")
assert t["XMP-tiff:Make"] == "DJI" and t["XMP-tiff:Model"] == "FC9470", t                    # standard fields, raw values
assert t["XMP-mon:CameraName"] == "DJI Neo 2" and t["XMP-mon:CameraNameSource"] == "override", t

# only the Encoder ("DJI NEO2") is there (no Category) -> ('DJI', 'NEO2') -> still "DJI Neo 2"
enc = mk("2026.10.04-12.30.00.MP4")
sp.run(["exiftool", "-q", "-overwrite_original", "-Encoder=DJI NEO2", enc], check=True)
print("encoder:", sp.run(["exiftool", "-s3", "-Encoder", enc], capture_output=True, text=True).stdout.strip())
r = run(enc); t = tags(enc + ".xmp")
assert t["XMP-tiff:Make"] == "DJI" and t["XMP-tiff:Model"] == "NEO2" and r["camera_name"] == "DJI Neo 2", (r, t)

# a phone with real Make/Model in the file -> own rule
sam = mk("2026.10.04-13.00.00.MP4", "-Make=samsung", "-Model=SM-S921B")
r = run(sam); t = tags(sam + ".xmp")
assert r["camera_name"] == "Samsung Galaxy S24" and t["XMP-mon:CameraName"] == "Samsung Galaxy S24", (r, t)
assert t["XMP-tiff:Model"] == "SM-S921B"

# nothing known at all -> no camera fields, no crash, GPS still written
anon = mk("2026.10.04-14.00.00.MP4"); r = run(anon)
assert r["xmp_written"] and r["camera_name"] == "", r
assert "XMP-mon:CameraName" not in tags(anon + ".xmp")

# re-run overwrites the name when the rule changed (no stale name)
sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-q", "-overwrite_original", "-XMP-mon:CameraName=alt", neo + ".xmp"], check=True)
run(neo); assert tags(neo + ".xmp")["XMP-mon:CameraName"] == "DJI Neo 2"

# why a name was NOT translated is shown: missing file / broken JSON / no matching rule
from app.core import devicenames
good = devicenames.OVERRIDES_PATH
bad = os.path.join(SCRATCH, "broken.json"); open(bad, "w").write('{"rules": [{"match": ["FC9470"], "name": "DJI Neo 2"},]}')   # stray comma
for path, expect in ((os.path.join(SCRATCH, "nope.json"), "Datei fehlt"), (bad, "ungültiges JSON")):
    devicenames.OVERRIDES_PATH = path
    r = run(neo)
    assert r["camera_source_label"] == "Rohwert" and expect in r["camera_hint"] and r["camera_name"] == "DJI FC9470", r
devicenames.OVERRIDES_PATH = good
r = run(neo); assert r["camera_name"] == "DJI Neo 2" and "camera_hint" not in r and r["camera_source_label"] == "eigene Regel", r

# the shipped example is valid JSON and carries the rule
ex = json.load(open(os.path.join(root, "config", "camera_names.example.json"), encoding="utf-8"))
assert any(r["name"] == "DJI Neo 2" and "FC9470" in r["match"] for r in ex["rules"])
shutil.rmtree(SCRATCH, ignore_errors=True)
print("PASS")
