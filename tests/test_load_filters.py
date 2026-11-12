"""Load filters before 'Vorschau laden': OR-combined; none = all; action checkboxes only in the list."""
import os, re, html, time, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="lf_"); os.environ["MEDIA_ROOT"] = MR
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR, "d"); os.makedirs(f)
def mk(n, dto, gps, off):
    p = os.path.join(f, n)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p], check=True)
    d, t = gps.split(" ")
    sp.run(["exiftool","-overwrite_original","-q",f"-DateTimeOriginal={dto}","-GPSLatitude=24.45","-GPSLatitudeRef=N",
            "-GPSLongitude=54.64","-GPSLongitudeRef=E",f"-GPSDateStamp={d}",f"-GPSTimeStamp={t}",
            f"-OffsetTime={off}",f"-OffsetTimeOriginal={off}",p], check=True)
mk("2026.08.05-16.11.24_18.jpg","2026:08:05 16:11:24","2026:08:05 14:11:24","+02:00")   # name wrong -> rename
mk("2026.08.05-18.07.44_15.jpg","2026:08:05 16:07:44","2026:08:05 14:07:44","+02:00")   # name correct
# a video-less, plain third file: correct name, no GPS/offset
p3 = os.path.join(f, "2026.08.05-10.00.00_1.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-frames:v","1",p3], check=True)
def scan(**kw):
    x = c.post("/timestamp/browse/start", data={"path": f, "root_path": MR, "depth": "0", "default_tz": "Europe/Vienna", **kw})
    j = re.search(r'/progress/([a-f0-9-]+)', x.text).group(1)
    for _ in range(600):
        y = c.get(f"/timestamp/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
def names(h): return sorted(os.path.basename(html.unescape(v)) for v in re.findall(r'name="selected" value="([^"]+)"', h))

idx = c.get("/timestamp/").text
form = re.search(r'<form[^>]*id="browse-form".*?</form>', idx, re.S).group(0)
for n in ("only_needs_rename", "only_needs_fix", "only_needs_backwrite"):
    assert f'name="{n}"' in form, n
for n in ("do_rename", "do_xmpdate", "do_exifwrite"):
    assert f'name="{n}"' not in form, n            # moved to the list only

allh = scan(include_already_named="true")
assert len(names(allh)) == 3, names(allh)
# the execute options sit below the list with the texts/tooltips of the former form checkboxes
assert ".xmp-Sidecar-Datum setzen" in allh and "EXIF zurückschreiben (nur Bilder)" in allh
assert re.search(r'name="do_rename" value="true" checked', allh) and re.search(r'name="do_xmpdate" value="true" checked', allh)
assert not re.search(r'name="do_exifwrite" value="true" checked', allh)       # defaults: rename+xmp on, exif off
assert 'name="opts_set"' in allh
# a user's choice survives (opts_set present, do_rename absent = unchecked)
h = scan(include_already_named="true", opts_set="true")
assert not re.search(r'name="do_rename" value="true" checked', h)

r = scan(only_needs_rename="true")
assert names(r) == ["2026.08.05-16.11.24_18.jpg"], names(r)
assert "umbenannt werden müssen" in r and "nicht geladen" in r
# combined = union of the conditions (rename OR needs-fix); superset of rename-only
u = scan(only_needs_rename="true", only_needs_fix="true")
assert set(names(r)) <= set(names(u)) and "ODER" in u, names(u)
# union never smaller than either single condition
fx = scan(only_needs_fix="true")
assert set(names(fx)) <= set(names(u)), (names(fx), names(u))
print("PASS")
