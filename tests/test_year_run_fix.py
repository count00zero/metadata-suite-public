"""Jahreslauf never leaves wrong timestamps: the timestamp step always runs last, and a cancelled run asks for an OK that fixes
everything the run wrote (.nfo, thumbs, sidecars ...)."""
import os, sys, tempfile, time
from datetime import datetime

SCR = tempfile.mkdtemp(prefix="yearfix_")
VI = os.path.join(SCR, "video", "Home-Video") + "/"
os.makedirs(VI + "2026"); os.makedirs(os.path.join(SCR, "cfg"))
os.environ.update({"MEDIA_ROOT": SCR, "ROOT_FOLDERS": f"VIDEOS={VI}:VIDEO", "TIMESTAMP_PRESETS_FILE": os.path.join(SCR, "cfg", "presets.json"),
                   "YEAR_RUN_STATE_DAYS": "0"})
os.environ.pop("GEO_SERVICE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import mtimecore
from app.modules.year_run import runner

fails = []
def check(n, c, x=""):
    print(("ok   " if c else "FAIL ") + n, "" if c else x)
    if not c: fails.append(n)

NFO = VI + "2026/2026.06.01-10.00.00.nfo"
THUMB = VI + "2026/2026.06.01-10.00.00-thumb.jpg"
def expected():
    return datetime(2026, 6, 1, 10, 0, 0, tzinfo=mtimecore.zone()).timestamp()
def wait(run, secs=60):
    t0 = time.time()
    while run.status == "running" and time.time() - t0 < secs: time.sleep(0.05)

def writer(run, folder, touched):
    for p in (NFO, THUMB):
        open(p, "w").write("x")            # mtime = now, like the real NFO step
    return "geschrieben"

# 1) normal run, the user did NOT tick the timestamp step -> it runs anyway, last
check("mtime is always last in the order", runner.STEP_ORDER[-1] == "mtime")
o = runner.Opts(year=2026, kind="video", scope="regular", depth=7, steps=("nfo",))
check("steps_for adds mtime even when not selected", runner.steps_for(runner.plan(o)[0], o) == ["nfo", "mtime"], runner.steps_for(runner.plan(o)[0], o))
runner.STEP_FUNCS["nfo"] = writer
run, _ = runner.start(o); wait(run)
check("final step fixed the .nfo and the thumb", abs(os.stat(NFO).st_mtime - expected()) < 2 and abs(os.stat(THUMB).st_mtime - expected()) < 2,
      (os.stat(NFO).st_mtime, expected(), [s["text"] for s in run.summary]))
check("a finished run has nothing pending", run.status == "done" and not run.fix_pending)

# 2) cancelled run: pending until OK
def slow_writer(run, folder, touched):
    writer(run, folder, touched)
    for _ in range(100):
        if run.cancel_event.is_set(): break
        time.sleep(0.05)
    return "abgebrochen"
runner.STEP_FUNCS["nfo"] = slow_writer
o2 = runner.Opts(year=2026, kind="video", scope="regular", depth=7, steps=("nfo",))
run2, _ = runner.start(o2)
time.sleep(0.5); run2.cancel_event.set(); wait(run2)
check("cancelled run asks for confirmation", run2.status == "cancelled" and run2.fix_pending and run2.fix_count >= 2, (run2.status, run2.fix_pending, run2.fix_count))
check("files still have the wrong (current) mtime before OK", abs(os.stat(NFO).st_mtime - expected()) > 60)
check("a new run is not blocked after cancel", runner.active_run() is None)
ok = runner.finish_fix(run2); wait(run2)
check("OK fixes everything the run wrote", ok and not run2.fix_pending and "gesetzt" in run2.fix_done and abs(os.stat(NFO).st_mtime - expected()) < 2
      and abs(os.stat(THUMB).st_mtime - expected()) < 2, (ok, run2.fix_done, run2.status))
check("a second OK does nothing", runner.finish_fix(run2) is False)

# 3) the web endpoints
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
run3, _ = runner.start(o2); time.sleep(0.4); run3.cancel_event.set(); wait(run3)
page = c.get(f"/year/run/{run3.id}").text
check("page shows the OK button after a cancel", "OK – Zeitstempel richtigstellen" in page and "Zeitstempel noch nicht richtig" in page)
r = c.post(f"/year/finish/{run3.id}"); wait(run3)
check("OK endpoint starts the fix", r.status_code == 200)
page = c.get(f"/year/run/{run3.id}").text
check("page confirms the fix", "Zeitstempel richtiggestellt" in page and "OK – Zeitstempel" not in page, page[-600:])
# Monatslauf: photo/<Y>/<Y.MM> AND video/<Y>/<Y.MM> together; no Urlaube, no collage
om = runner.Opts(year=2026, month=11, steps=("gps", "collage"))
pm = runner.plan(om)
check("month run plans photo + video month folders", [f.label for f in pm] == ["Fotos 2026.11", "Videos 2026.11"]
      and all(f.scope == "regular" for f in pm)
      and pm[0].path.rstrip("/").endswith("/2026/2026.11") and pm[1].path.rstrip("/").endswith("/2026/2026.11"), [f.path for f in pm])
check("month run: kind filter", [f.label for f in runner.plan(runner.Opts(year=2026, month=3, kind="video"))] == ["Videos 2026.03"])
check("month run: Urlaube scope ignored, no collage step", "collage" not in runner.steps_for(pm[0], om) and om.title == "2026.11")
check("whole year unchanged", len(runner.plan(runner.Opts(year=2026))) == 4 and runner.Opts(year=2026).title == "2026")
from app.modules.year_run import router as yr
check("router passes the month on", yr._opts(2026, "all", "all", 7, False, [], "Europe/Vienna", month=11).month == 11 and yr._opts(2026, "all", "all", 7, False, [], "x", month=13).month == 0)
print("\nFAILED: %s" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
