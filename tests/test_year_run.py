"""Jahreslauf (module /year/): folder plan like Media_actualyear_nfo_Creator.sh, preview counts, an unattended run through the real
module steps (GPS from EXIF -> geocode (faked) -> sidecar+embed, AI (faked provider), NFO for a video, mtime, collage), one run at a time,
cancel, step failure does not stop the rest."""
import os, subprocess as sp, sys, tempfile, time

SCR = tempfile.mkdtemp(prefix="yearrun_")
PH, VI = os.path.join(SCR, "photo") + "/", os.path.join(SCR, "video", "Home-Video") + "/"
for d in (PH + "2026/Tag1", PH + "Urlaube/2026/Reise/Tag1", VI + "2026"):
    os.makedirs(d)
os.environ["MEDIA_ROOT"] = SCR
os.environ["ROOT_FOLDERS"] = f"PHOTOS={PH}:IMAGE,VIDEOS={VI}:VIDEO"
os.environ["TIMESTAMP_PRESETS_FILE"] = os.path.join(SCR, "cfg", "presets.json")   # keeps year_run_state.json out of the repo
os.environ["YEAR_RUN_STATE_DAYS"] = "0"             # remembered state is tested in test_year_run_state.py
os.environ["BATCH_SIZE"] = "2"                      # prove that the Jahreslauf does not stop after the first batch
os.environ.pop("GEO_SERVICE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from fastapi.testclient import TestClient
from app.main import app
from app.core import geocore, aicore
from app.modules.year_run import runner

c = TestClient(app)
fails = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond: fails.append(name)

def jpg(path, gps=True, stamp="2026:06:01 10:00:00"):
    Image.new("RGB", (64, 48), (200, 120, 40)).save(path)
    a = ["exiftool", "-overwrite_original", "-q", f"-DateTimeOriginal={stamp}", "-OffsetTimeOriginal=+02:00"]
    if gps:
        a += ["-GPSLatitude=48.1591", "-GPSLatitudeRef=N", "-GPSLongitude=16.3328", "-GPSLongitudeRef=E"]
    sp.run(a + [path], check=True)

for i in range(3):
    jpg(PH + f"2026/Tag1/2026.06.01-10.0{i}.00.jpg", stamp=f"2026:06:01 10:0{i}:00")
jpg(PH + "Urlaube/2026/Reise/Tag1/2026.07.01-10.00.00.jpg", stamp="2026:07:01 10:00:00")
jpg(PH + "Urlaube/2026/Reise/Tag1/2026.07.01-10.01.00.jpg", stamp="2026:07:01 10:01:00")
mp4 = VI + "2026/2026.06.02-12.00.00.mp4"
sp.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=64x48:rate=5", "-pix_fmt", "yuv420p", mp4], check=True)
sp.run(["exiftool", "-overwrite_original", "-q", "-CreateDate=2026:06:02 10:00:00", "-GPSLatitude=48.1591", "-GPSLongitude=16.3328", mp4])

# ---- plan / preview --------------------------------------------------------------------------------------------------------------
o = runner.Opts(year=2026)
pl = runner.plan(o)
check("plan: 4 folders in script order", [(f.kind, f.scope) for f in pl] == [("IMAGE", "regular"), ("IMAGE", "urlaub"), ("VIDEO", "regular"), ("VIDEO", "urlaub")], pl)
check("plan paths", pl[0].path == PH + "2026/" and pl[1].path == PH + "Urlaube/2026/" and pl[2].path == VI + "2026/" and pl[3].path == VI + "Urlaube/2026/", [f.path for f in pl])
check("type/scope filters", [f.label for f in runner.plan(runner.Opts(year=2026, kind="video", scope="regular"))] == ["Videos 2026"]
      and len(runner.plan(runner.Opts(year=2026, kind="image", scope="urlaub"))) == 1)
pv = runner.preview(o)
check("preview counts + missing folder", [f.files for f in pv] == [3, 2, 1, 0] and [f.exists for f in pv] == [True, True, True, False], [(f.files, f.exists) for f in pv])
check("steps per folder", runner.steps_for(pv[0], o) == ["rename", "gps", "ctx", "ai", "mtime"] and "collage" in runner.steps_for(pv[1], o)
      and "nfo" in runner.steps_for(pv[2], o) and "nfo" not in runner.steps_for(pv[1], o))

r = c.get("/year/"); check("page", r.status_code == 200 and "Jahreslauf" in r.text)
r = c.post("/year/preview", data={"year": 2026, "kind": "all", "scope": "all", "depth": 7, "steps": ["gps", "ai"]})
check("preview html", r.status_code == 200 and "Jahreslauf starten" in r.text and "Ordner nicht vorhanden" in r.text, r.text[:300])

# ---- fakes: no network ------------------------------------------------------------------------------------------------------------
geocore.reverse_geocode = lambda lat, lon, **k: geocore.GeoResult(country="Österreich", country_a="AUT", state="Wien", city="Wien", source="fake")
class _Gen:
    description, provider, model, failures = "Ein Testbild.", "fake", "m", []
aicore.generate_description = lambda *a, **k: _Gen()

def wait(run_id, secs=240):
    t0 = time.time()
    while time.time() - t0 < secs:
        run = runner.get(run_id)
        if run.status != "running":
            return run
        time.sleep(0.3)
    raise SystemExit("run did not finish")

# ---- real run --------------------------------------------------------------------------------------------------------------------
r = c.post("/year/start", data={"year": 2026, "kind": "all", "scope": "all", "depth": 7, "aim": "1",
                                "steps": ["gps", "ai", "nfo", "mtime", "collage"]})
check("start page", r.status_code == 200 and "Jahr 2026" in r.text, r.text[:300])
run_id = [rid for rid in runner.RUNS][0]
r2 = c.post("/year/start", data={"year": 2026, "steps": ["gps"]})
check("second start while running shows the running one", "läuft bereits" in r2.text or runner.RUNS[run_id].status != "running")
run = wait(run_id)
log = "\n".join(m for _t, _l, m in run.log)
check("run finished", run.status == "done", (run.status, run.error, log[-1500:]))
summ = {(s["folder"], s["step"]): s for s in run.summary}
print("\n".join(f"   {x['folder']} | {x['step']} | {x['text']}" for x in run.summary))
check("no step failed", all(s["ok"] for s in run.summary), [s for s in run.summary if not s["ok"]])
check("GPS wrote sidecars for photos beyond the first batch (BATCH_SIZE=2)", os.path.exists(PH + "2026/Tag1/2026.06.01-10.02.00.jpg.xmp"), log[-1500:])
check("GPS used geocode result", "Wien" in open(PH + "2026/Tag1/2026.06.01-10.00.00.jpg.xmp", encoding="utf-8").read())
check("photo embed: image has the city", "Wien" in sp.run(["exiftool", "-City", PH + "2026/Tag1/2026.06.01-10.00.00.jpg"], capture_output=True, text=True).stdout)
check("AI description in the sidecar", "Ein Testbild." in open(PH + "2026/Tag1/2026.06.01-10.00.00.jpg.xmp", encoding="utf-8").read())
check("video: sidecar + nfo", os.path.exists(mp4 + ".xmp") and os.path.exists(VI + "2026/2026.06.02-12.00.00.nfo"), os.listdir(VI + "2026"))
check("collage for photo/Urlaube", any(f.endswith(".jpg") and "Reise" in f for f in os.listdir(PH + "Urlaube/2026")) or os.path.exists(PH + "Urlaube/2026/Reise/folder.jpg") or os.path.exists(PH + "Urlaube/2026/Reise.jpg"),
      os.listdir(PH + "Urlaube/2026"))
check("missing Urlaube video folder is reported, not fatal", "nicht vorhanden" in log and f"Videos Urlaube 2026" in log)
view = c.get(f"/year/run/{run_id}")
check("run page after the end", view.status_code == 200 and "Fertig" in view.text and "Ergebnis je Schritt" in view.text)
check("log shows sub-step lines; panel is preserved, text updated out-of-band", "↳" in view.text and 'hx-preserve="true"' in view.text and 'hx-swap-oob="innerHTML"' in view.text)

# ---- second run: nothing left to do ------------------------------------------------------------------------------------------------
r = c.post("/year/start", data={"year": 2026, "kind": "image", "scope": "regular", "depth": 7, "steps": ["gps", "ai"]})
run2 = wait([k for k in runner.RUNS if k != run_id][0])
t2 = " | ".join(s["text"] for s in run2.summary)
check("rerun: already done -> nothing to do", "nichts zu tun" in t2, t2)

# ---- ctx without service + failing step does not stop the rest ---------------------------------------------------------------------
def boom(run, folder, touched): raise RuntimeError("kaputt")
runner.STEP_FUNCS["gps"] = boom
r = c.post("/year/start", data={"year": 2026, "kind": "image", "scope": "regular", "depth": 7, "steps": ["gps", "ctx", "mtime"]})
ids = [k for k in runner.RUNS if k not in (run_id, run2.id)]
run3 = wait(ids[0])
st = {s["step"]: s for s in run3.summary}
check("failing step logged, later steps still ran", not st["GPS Sidecar"]["ok"] and st["Kontext nachfüllen"]["ok"] and "GEO_SERVICE_URL" in st["Kontext nachfüllen"]["text"] and "Datei-Zeitstempel" in st, st)

# ---- cancel ----------------------------------------------------------------------------------------------------------------------
import threading
gate = threading.Event()
def slow(run, folder, touched):
    for _ in range(100):
        if run.cancel_event.is_set(): return "abgebrochen"
        time.sleep(0.05)
    return "zu spät"
runner.STEP_FUNCS["gps"] = slow
c.post("/year/start", data={"year": 2026, "kind": "image", "scope": "all", "depth": 7, "steps": ["gps"]})
rid = [k for k in runner.RUNS if k not in (run_id, run2.id, run3.id)][0]
time.sleep(0.3)
c.post(f"/year/cancel/{rid}")
run4 = wait(rid)
check("cancel", run4.status == "cancelled", run4.status)
check("unknown run id is handled", "nicht mehr vorhanden" in c.get("/year/run/doesnotexist").text)

# ---- rename step (first step): unnamed file gets the YYYY.MM.DD-HH.MM.SS name ---------------------------------------------------
jpg(PH + "2026/Tag1/IMG_20260605_091530.jpg", gps=False, stamp="2026:06:05 09:15:30")
before = set(runner.RUNS)
c.post("/year/start", data={"year": 2026, "kind": "image", "scope": "regular", "depth": 7, "steps": ["rename"]})
run_r = wait([k for k in runner.RUNS if k not in before][0])
tr = " | ".join(s["text"] for s in run_r.summary)
names = os.listdir(PH + "2026/Tag1")
check("rename step renamed IMG_20260605_091530.jpg", "IMG_20260605_091530.jpg" not in names and any(f.startswith("2026.06.05-09.15.30") for f in names), tr + str(names))
check("rename is the first step", runner.STEP_ORDER[0] == "rename" and runner.DEFAULT_STEPS[0] == "rename")

# Monatslauf in der Vorschau: beide Monatsordner, Monat wird zum Start weitergereicht
os.makedirs(PH + "2026/2026.06", exist_ok=True); os.makedirs(VI + "2026/2026.06", exist_ok=True)
jpg(PH + "2026/2026.06/2026.06.01-10.00.00.jpg", stamp="2026:06:01 10:00:00")
pv = c.post("/year/preview", data={"year": "2026", "month": "6", "kind": "all", "scope": "all", "depth": "7"}).text
check("month preview: photo + video month folder, month passed on", "Fotos 2026.06" in pv and "Videos 2026.06" in pv and "Urlaube" not in pv
      and 'name="month" value="6"' in pv and "Monat 2026.06" in pv, pv[:600])
print("\nFAILED: %s" % fails if fails else "\nall Jahreslauf tests passed")
sys.exit(1 if fails else 0)

