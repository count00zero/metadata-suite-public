import os, sys, re, html, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "exec_disable_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
DIR = os.path.join(MEDIA_ROOT, "video", "Home-Video", "2026.09")
os.makedirs(DIR)
VIDEO = os.path.join(DIR, "2026.09.01-09.00.00.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=8",
    "-f", "lavfi", "-i", "sine=frequency=500:duration=8",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = os.path.join(MEDIA_ROOT, "video", "Home-Video")
os.environ["PHOTO_ROOT"] = os.path.join(MEDIA_ROOT, "photo")

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)

def resolve_job(resp, prefix):
    htmlsrc = resp.text
    while True:
        m = re.search(rf'hx-get="({prefix}/progress/[0-9a-f]+)"', htmlsrc)
        if not m: return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc: return htmlsrc

def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=\'(.*?)\'', htmlsrc, re.S)
    assert m, htmlsrc[:1500]
    return html.unescape(m.group(1))

# NFO Maker: scan -> generate, check exec-btn id present (enabled) in scan list, and the
# OOB disable button present in the generate results.
r = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal"})
scan_html = resolve_job(r, "/nfo/scan")
assert 'id="exec-btn">NFOs erstellen</button>' in scan_html, scan_html[:2000]
all_rows_json = get_hidden(scan_html, "all_rows")
r2 = client.post("/nfo/generate/start", data={"selected": [VIDEO], "all_rows": all_rows_json})
results_html = resolve_job(r2, "/nfo/generate")
assert 'id="exec-btn" disabled hx-swap-oob="true"' in results_html, results_html[:2000]
print("NFO Maker: exec-btn correctly disabled via OOB swap after generate completes.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

# --- Timestamp & Rename: a synchronous module (no job) -- check /execute directly ---
MEDIA_ROOT2 = os.path.join(SCRATCH, "exec_disable_root2")
shutil.rmtree(MEDIA_ROOT2, ignore_errors=True)
os.makedirs(MEDIA_ROOT2)
DIR2 = os.path.join(MEDIA_ROOT2, "photo")
os.makedirs(DIR2)
IMG = os.path.join(DIR2, "2026.09.01-09.00.00.jpg")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", IMG, "-loglevel", "error"], check=True)
os.environ["MEDIA_ROOT"] = MEDIA_ROOT2
from app.core import pathsafety; pathsafety.MEDIA_ROOT = MEDIA_ROOT2  # fence is read once at import
r3 = client.post("/timestamp/browse/start", data={"path": DIR2, "root_path": MEDIA_ROOT2, "depth": 1, "type_filter": "ALL", "include_already_named": "true"})
browse_html = resolve_job(r3, "/timestamp/browse")
assert 'id="exec-btn">Ausführen</button>' in browse_html, browse_html[:2000]
all_res_json = get_hidden(browse_html, "all_resolutions")
r4 = client.post("/timestamp/execute", data={"selected": [IMG], "all_resolutions": all_res_json, "handoff_path": DIR2, "handoff_root_path": MEDIA_ROOT2})
exec_html = resolve_job(r4, "/timestamp/execute") if "progress-panel" in r4.text else r4.text
assert 'id="exec-btn" disabled hx-swap-oob="true"' in exec_html, exec_html[:3000]
print("Timestamp & Rename: exec-btn correctly disabled via OOB swap after execute completes.")

shutil.rmtree(MEDIA_ROOT2, ignore_errors=True)
print("\nALL EXEC-BTN DISABLE CHECKS PASSED")
