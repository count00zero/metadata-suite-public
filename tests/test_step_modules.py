"""Stepped loading in GPS, AI, NFO, mtime and short-video modules (Timestamp: test_step_timestamp.py)."""
import os, re, time, html, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="stm_"); os.environ["MEDIA_ROOT"] = MR
os.environ["BATCH_SIZE"] = "3"
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
imgs = os.path.join(MR, "img"); vids = os.path.join(MR, "vid"); os.makedirs(imgs); os.makedirs(vids)
for i in range(7):
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=32x32","-frames:v","1",
            os.path.join(imgs, f"2026.08.05-1{i}.00.00_{i}.jpg")], check=True)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64:d=2","-t","2","-pix_fmt","yuv420p",
            os.path.join(vids, f"2026.08.05-1{i}.00.00_{i}.mp4")], check=True)

def run(base, folder, poll, extra=None, **kw):
    d = {"path": folder, "root_path": MR, "depth": "1", **(extra or {}), **kw}
    x = c.post(base, data=d)
    m = re.search(r'/progress/([a-f0-9-]+)', x.text)
    if not m: return x.text
    for _ in range(800):
        y = c.get(f"{poll}/{m.group(1)}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
    raise AssertionError("timeout")
def rows(h): return [html.unescape(v) for v in re.findall(r'name="selected" value="([^"]+)"', h)]
def sid(h):
    m = re.search(r'&#34;scan_id&#34;: &#34;([a-f0-9]+)|&quot;scan_id&quot;: &quot;([a-f0-9]+)|"scan_id": "([a-f0-9]+)', h)
    return next(g for g in m.groups() if g)

def walk(name, base, folder, poll, expect_total, extra=None, per_step=3):
    h1 = run(base, folder, poll, extra); r1 = rows(h1)
    assert len(r1) == per_step, (name, r1)
    assert "Schritt 1" in h1 and f"Nächste {per_step} laden" in h1, (name, h1[:300])
    s = sid(h1)
    h2 = run(base, folder, poll, extra, scan_id=s, offset="3"); r2 = rows(h2)
    assert len(r2) == per_step and not set(r1) & set(r2) and "Schritt 2" in h2, (name, r1, r2)
    h3 = run(base, folder, poll, extra, scan_id=s, offset="6"); r3 = rows(h3)
    assert len(r3) == expect_total - 6 and f"alle {expect_total} Dateien geladen" in h3, (name, r3)
    assert len(set(r1 + r2 + r3)) == expect_total
    # expired id -> fresh scan; stepping off -> all rows, no bar
    assert "Schritt 1" in run(base, folder, poll, extra, scan_id="nope", offset="3")
    os.environ["BATCH_SIZE"] = "0"
    h0 = run(base, folder, poll, extra); assert len(rows(h0)) == expect_total and 'class="step-bar"' not in h0, name
    os.environ["BATCH_SIZE"] = "3"
    print(" ok", name)

walk("gps", "/gps/browse/start", imgs, "/gps/browse/progress", 7, {"default_tz": "Europe/Vienna"})
walk("ai", "/ai/browse/start", imgs, "/ai/browse/progress", 7)
walk("mtime", "/mtime/browse/start", imgs, "/mtime/browse/progress", 7)
walk("nfo", "/nfo/scan/start", vids, "/nfo/scan/progress", 7, {"mode": "normal"})
walk("shortvideos", "/shortvideos/scan/start", vids, "/shortvideos/scan/progress", 7, {"short_video_secs": "10"})

# per-module override beats the global value
os.environ["BATCH_SIZE_AI"] = "5"
assert len(rows(run("/ai/browse/start", imgs, "/ai/browse/progress"))) == 5
assert len(rows(run("/gps/browse/start", imgs, "/gps/browse/progress", {"default_tz": "Europe/Vienna"}))) == 3
del os.environ["BATCH_SIZE_AI"]

# deleted (short) videos between steps are skipped, never duplicated or crashing
h1 = run("/shortvideos/scan/start", vids, "/shortvideos/scan/progress", {"short_video_secs": "10"})
r1 = rows(h1); s = sid(h1)
for p in r1: os.remove(p)
h2 = run("/shortvideos/scan/start", vids, "/shortvideos/scan/progress", {"short_video_secs": "10"}, scan_id=s, offset="3")
assert len(rows(h2)) == 3 and not set(rows(h2)) & set(r1)
print("PASS")
