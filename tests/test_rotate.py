"""Datei-Zeitstempel: optional find + lossless rotate (exiftran) of JPEGs that are upright only via EXIF Orientation."""
import html, json, os, re, shutil, subprocess as sp, sys, tempfile, time
SCRATCH = tempfile.mkdtemp(prefix="rot_"); MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not shutil.which("exiftran"):
    print("SKIP: exiftran not installed"); sys.exit(0)
from app.main import app
from app.core import rotatecore
from fastapi.testclient import TestClient
c = TestClient(app)

def mk(name, size, orient, fmt="jpg", mtime="2021-05-06 07:08:09"):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s={size}", "-pix_fmt", "yuvj420p", "-frames:v", "1", p], check=True)
    sp.run(["exiftool", "-overwrite_original", "-q", "-n", f"-Orientation={orient}", "-DateTimeOriginal=2026:10:02 18:41:50", p], check=True)
    sp.run(["touch", "-d", mtime, p]); return p
dims = lambda p: tuple(int(x) for x in sp.run(["exiftool", "-n", "-s3", "-ImageWidth", "-ImageHeight", p], capture_output=True, text=True).stdout.split())
orient = lambda p: sp.run(["exiftool", "-n", "-s3", "-Orientation", p], capture_output=True, text=True).stdout.strip()

aligned = mk("2026.10.02-18.41.50_a.jpg", "960x640", 6)            # 16-multiple: nothing to cut
ragged = mk("2026.10.02-18.41.51_b.jpg", "1000x750", 8)            # 750 / 1000 not multiples of 16: edge pared
upright = mk("2026.10.02-18.41.52_c.jpg", "640x480", 1)
heic_like = mk("2026.10.02-18.41.53_d.png", "320x240", 6)          # not a JPEG: must be ignored
side = aligned + ".xmp"; sp.run(["exiftool", "-q", "-o", side, "-n", "-XMP-tiff:Orientation=6", aligned], check=True)
assert rotatecore.needs_rotation(aligned)["orientation"] == 6 and rotatecore.needs_rotation(upright) is None and rotatecore.needs_rotation(heic_like) is None

def poll(resp):
    src = resp.text
    while True:
        m = re.search(r'hx-get="(/mtime/browse/progress/[0-9a-f-]+)"', src)
        if not m: return src
        time.sleep(0.05); src = c.get(m.group(1)).text

form = {"path": MR, "root_path": MR, "depth": "0", "type_filter": "ALL"}
page = poll(c.post("/mtime/browse/start", data=form))
assert "zu drehen" not in page and 'name="rotate"' not in page               # option off -> behaviour as before
page = poll(c.post("/mtime/browse/start", data={**form, "find_rotation": "1"}))
assert page.count('name="rotate"') == 2 and "2 JPEG(s) zu drehen" in page and "1 Nicht-JPEG mit Drehung ignoriert" in page, page[:3000]
assert "Backup vor dem Drehen" in page
assert not re.search(r'name="rotate_backup"[^>]*checked', page), "backup switch must be off by default"
all_rows = html.unescape(re.search(r"name=\"all_rows\" value='(.*?)'>", page, re.S).group(1))
w0 = dims(aligned)

# execute: rotate both, backup on, keep (and for 'aligned' also fix) the mtime
out = c.post("/mtime/execute", data={"all_rows": all_rows, "selected": [aligned, ragged], "rotate": [aligned, ragged], "rotate_backup": "1"}).text
assert out.count("verlustfrei gedreht") == 2, out
assert orient(aligned) == "1" and orient(ragged) == "1"
assert dims(aligned) == (640, 960), dims(aligned)                              # lossless swap, nothing cut
dr = dims(ragged); assert dr[0] <= 750 and dr[1] <= 1000 and 750 - dr[0] < 16 and 1000 - dr[1] < 16, dr          # exiftran may pare up to 15 px, depending on MCU layout
assert "nichts abgeschnitten" in out and (dr == (750, 1000) or "abgeschnitten" in out.replace("nichts abgeschnitten", ""))
assert os.path.exists(aligned + "~") and dims(aligned + "~") == (960, 640) and orient(aligned + "~") == "6"   # backup = untouched original
assert sp.run(["exiftool", "-n", "-s3", "-XMP-tiff:Orientation", side], capture_output=True, text=True).stdout.strip() == "1"   # sidecar reset
# mtime: rotation-only rows keep their mtime (exiftran -p); the filename-date fix happens when the row differs
mt = lambda p: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.stat(p).st_mtime))
assert os.path.exists(upright) and orient(upright) == "1" and dims(heic_like) == (320, 240) and orient(heic_like) == "6"   # untouched
# second scan: nothing left to rotate
page2 = poll(c.post("/mtime/browse/start", data={**form, "find_rotation": "1"}))
assert 'name="rotate"' not in page2
# no backup + deselected rotation
again = mk("2026.10.02-18.41.54_e.jpg", "960x640", 3)
page3 = poll(c.post("/mtime/browse/start", data={**form, "find_rotation": "1"}))
rows3 = html.unescape(re.search(r"name=\"all_rows\" value='(.*?)'>", page3, re.S).group(1))
o1 = c.post("/mtime/execute", data={"all_rows": rows3, "selected": [again]}).text      # row selected, rotate box unticked
assert orient(again) == "3" and "gedreht" not in o1.replace("Zeitstempel", "")
o2 = c.post("/mtime/execute", data={"all_rows": rows3, "selected": [again], "rotate": [again]}).text
assert orient(again) == "1" and not os.path.exists(again + "~"), "no backup unless asked"
# missing exiftran / non-jpeg messages
assert rotatecore.rotate_lossless(heic_like)["ok"] is False and "kein JPEG" in rotatecore.rotate_lossless(heic_like)["message"]
print("PASS")
