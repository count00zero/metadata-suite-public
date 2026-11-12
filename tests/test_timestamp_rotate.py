"""Timestamp & Rename: checkbox "Bilder verlustfrei aufrecht drehen" (default on) rotates JPEGs at write time, before the rename."""
import os, sys, tempfile
MR = tempfile.mkdtemp(prefix="tsrot_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
from app.core import rotatecore
from app.modules.timestamp_tool import router as t
fails = []
def check(n, c, x=""):
    print(("ok   " if c else "FAIL ") + n, "" if c else x)
    if not c: fails.append(n)

calls = []
class Out:
    renamed_to = None; exif_written = False; touched = True; error = ""; exif_error = ""
    def to_dict(self): return {"path": "x", "touched": True}
t.apply_resolution = lambda res, *a, **k: (calls.append(("apply", res.path)) or Out())
def fake_rotate(path, backup=True):
    calls.append(("rotate", path, backup))
    return {"ok": True, "rotated": path.endswith(".jpg"), "message": "verlustfrei gedreht (nichts abgeschnitten)", "backup": "", "trimmed_px": 0}
rotatecore.rotate_lossless = fake_rotate
t._resolution_from_dict = lambda row: type("R", (), {"path": row["path"], "ext": os.path.splitext(row["path"])[1], "zieldate": "", "epoch": 0})()

rows = [{"path": f"{MR}/a.jpg", "row_type": "primary"}, {"path": f"{MR}/b.png", "row_type": "primary"}, {"path": f"{MR}/c.mp4", "row_type": "primary"}]
sel = {r["path"] for r in rows}
out = t._run_execute(rows, sel, False, False, 10, "", "", do_rotate=True)
rot = [c for c in calls if c[0] == "rotate"]
check("only JPEGs are rotated, without backup (default)", [os.path.basename(c[1]) for c in rot] == ["a.jpg"] and rot[0][2] is False, rot)
i_rot, i_apply = calls.index(rot[0]), calls.index(("apply", f"{MR}/a.jpg"))
check("rotation happens before the write", i_rot < i_apply)
check("result carries the rotation line", any("verlustfrei gedreht" in (r.get("rotation") or "") for r in out["results"]))
calls.clear(); t._run_execute(rows, sel, False, False, 10, "", "", do_rotate=True, rotate_backup=True)
check("backup switch on -> backup requested", [c[2] for c in calls if c[0] == "rotate"] == [True], calls)
calls.clear(); t._run_execute(rows, sel, False, False, 10, "", "", do_rotate=False)
check("off -> nothing rotated", not [c for c in calls if c[0] == "rotate"])

# failure is reported, the write still happens
calls.clear()
rotatecore.rotate_lossless = lambda p, backup=True: {"ok": False, "rotated": False, "message": "exiftran ist nicht installiert", "backup": ""}
out = t._run_execute(rows[:1], {rows[0]["path"]}, False, False, 10, "", "", do_rotate=True)
check("failed rotation: message shown, write continues", out["results"][0].get("rotation_error") and ("apply", rows[0]["path"]) in calls)

# UI: default on, carried through the step bar
c = TestClient(app)
open(os.path.join(MR, "2026.01.01-10.00.00.jpg"), "wb").write(b"x")
import re, time
r = c.post("/timestamp/browse/start", data={"path": MR, "root_path": MR, "depth": "0"})
src = r.text
for _ in range(200):
    m = re.search(r'hx-get="(/timestamp/browse/progress/[0-9a-f-]+)"', src)
    if not m: break
    time.sleep(0.05); src = c.get(m.group(1)).text
check("checkbox on the list, default checked", re.search(r'name="do_rotate"[^>]*checked', src) is not None, src[-600:])
check("backup switch exists and is off by default", re.search(r'name="rotate_backup"', src) is not None and re.search(r'name="rotate_backup"[^>]*checked', src) is None)
check("step bar carries the option", "[name=do_rotate]" in open(os.path.join(os.path.dirname(t.__file__), "templates", "_file_list.html"), encoding="utf-8").read())
src2 = c.post("/timestamp/browse/start", data={"path": MR, "root_path": MR, "depth": "0", "opts_set": "true"}).text
for _ in range(200):
    m = re.search(r'hx-get="(/timestamp/browse/progress/[0-9a-f-]+)"', src2)
    if not m: break
    time.sleep(0.05); src2 = c.get(m.group(1)).text
check("unchecked stays unchecked", re.search(r'name="do_rotate"[^>]*checked', src2) is None)
print("\nFAILED: %s" % fails if fails else "\nall passed"); sys.exit(1 if fails else 0)
