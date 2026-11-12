"""Jahreslauf remembers per folder which steps had nothing left to do and skips them while the folder is unchanged."""
import os, sys, tempfile, time

SCR = tempfile.mkdtemp(prefix="yearstate_")
PH = os.path.join(SCR, "photo") + "/"
os.makedirs(PH + "2026/Tag1"); os.makedirs(os.path.join(SCR, "cfg"))
os.environ.update({"MEDIA_ROOT": SCR, "ROOT_FOLDERS": f"PHOTOS={PH}:IMAGE", "TIMESTAMP_PRESETS_FILE": os.path.join(SCR, "cfg", "presets.json"),
                   "YEAR_RUN_STATE_DAYS": "30"})
os.environ.pop("GEO_SERVICE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.modules.year_run import runner

fails = []
def check(n, c, x=""):
    print(("ok   " if c else "FAIL ") + n, "" if c else x)
    if not c: fails.append(n)

open(PH + "2026/Tag1/a.jpg", "wb").write(b"x")
calls = {"gps": 0, "ai": 0}
def fake(name, clean):
    def f(run, folder, touched):
        calls[name] += 1
        run.step_clean = clean[0]
        return "ok"
    return f
gps_clean = [True]
runner.STEP_FUNCS["gps"] = fake("gps", gps_clean)
runner.STEP_FUNCS["ai"] = fake("ai", [False])         # never clean (e.g. provider failures) -> always runs

def go(**kw):
    o = runner.Opts(year=2026, kind="image", scope="regular", depth=7, steps=("gps", "ai"), **kw)
    run, _ = runner.start(o)
    t0 = time.time()
    while run.status == "running" and time.time() - t0 < 60: time.sleep(0.05)
    return run, " | ".join(s["text"] for s in run.summary)

r1, t1 = go()
check("first run executes both steps", calls == {"gps": 1, "ai": 1}, calls)
check("state file written", os.path.exists(os.path.join(SCR, "cfg", "year_run_state.json")))
r2, t2 = go()
check("second run: clean step skipped, unclean one runs", calls == {"gps": 1, "ai": 2} and "übersprungen" in t2, (calls, t2))
r3, t3 = go(recheck=True)
check("recheck ignores the state", calls == {"gps": 2, "ai": 3}, calls)
open(PH + "2026/Tag1/b.jpg", "wb").write(b"y")
r4, t4 = go()
check("a changed folder is checked again", calls["gps"] == 3, calls)
r5, t5 = go()
check("... and remembered again afterwards", calls["gps"] == 3 and "übersprungen" in t5, (calls, t5))
r6, _ = go(aim=True)
check("different options (AIM) -> not skipped", calls["gps"] == 4, calls)
os.environ["YEAR_RUN_STATE_DAYS"] = "0"
r7, t7 = go(aim=True)
check("YEAR_RUN_STATE_DAYS=0 disables skipping", calls["gps"] == 5 and "übersprungen" not in t7, (calls, t7))
os.environ["YEAR_RUN_STATE_DAYS"] = "30"
gps_clean[0] = False
os.remove(PH + "2026/Tag1/b.jpg")
go(); go()
check("a step that is not clean (work left) is never remembered", calls["gps"] == 7, calls)
print("\nFAILED: %s" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
