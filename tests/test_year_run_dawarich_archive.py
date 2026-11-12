"""Jahreslauf GPS step uses the Dawarich archive account automatically when DAWARICH_SERVER + DAWARICH_IMPORT_API_KEY are set."""
import os, sys, tempfile
SCR = tempfile.mkdtemp(prefix="yr_daw_")
os.makedirs(SCR + "/p/2026")
open(SCR + "/p/2026/a.jpg", "wb").write(b"x")
os.environ["MEDIA_ROOT"] = SCR
os.environ["TIMESTAMP_PRESETS_FILE"] = os.path.join(SCR, "cfg", "presets.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.modules.year_run import runner
from app.modules.gps_sidecar import router as g
import uuid

seen = []
g.browse_start_work = lambda job, **kw: (seen.append(kw), {"rows": []})[1]


def go():
    seen.clear()
    f = runner.Folder(kind="IMAGE", scope="regular", path=SCR + "/p/2026/", root_path=SCR + "/p/")
    run = runner.Run(id=uuid.uuid4().hex, opts=runner.Opts(year=2026, depth=0), folders=[f])
    runner.step_gps(run, f, [])
    return seen[0]["use_dawarich_import"], seen[0]["use_dawarich"]


fails = []
def check(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)

for k in ("DAWARICH_SERVER", "DAWARICH_IMPORT_API_KEY"):
    os.environ.pop(k, None)
check("no key -> archive off, normal Dawarich on", go() == (False, True))
os.environ["DAWARICH_SERVER"] = "http://dawarich.invalid:3000"
check("server only -> archive off", go()[0] is False)
os.environ["DAWARICH_IMPORT_API_KEY"] = "k"
check("server + import key -> archive on", go() == (True, True))
os.environ.pop("DAWARICH_SERVER")
check("key without server -> archive off", go()[0] is False)
print("\nFAILED: %s" % fails if fails else "\nall dawarich-archive tests passed")
sys.exit(1 if fails else 0)
