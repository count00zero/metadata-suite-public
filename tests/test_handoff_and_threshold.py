import os, sys, re, html, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "handoff_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
DIR = os.path.join(MEDIA_ROOT, "video")
os.makedirs(DIR)
VIDEO = os.path.join(DIR, "2026.09.01-09.00.00.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=5",
    "-f", "lavfi", "-i", "sine=frequency=500:duration=5",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = DIR
os.environ["NFO_SHORT_VIDEO_SECS"] = "7"   # custom threshold -- the 5s clip above is short under this

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

# --- Test A: NFO_SHORT_VIDEO_SECS=7 makes the 5s clip "short" and shows in the label ---
r = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal"})
scan_html = resolve_job(r, "/nfo/scan")
assert "sehr kurze Videos (&lt;7s)" in scan_html, scan_html[:3000]
assert 'data-is-short="true"' in scan_html, scan_html[:3000]
print("NFO_SHORT_VIDEO_SECS=7 correctly reflected in the UI label and is_short flag.")

# --- Test B: NFO Maker's own index() accepts prefill_path/root_path and auto-submits ---
r2 = client.get(f"/nfo/?path={DIR}&root_path={MEDIA_ROOT}")
seg = r2.text.split("initLastPath() {")[1].split("},")[0]
assert "requestSubmit" in seg, seg
r3 = client.get("/nfo/")
seg3 = r3.text.split("initLastPath() {")[1].split("},")[0]
assert "requestSubmit" not in seg3, seg3
print("NFO Maker: handoff auto-submit correctly gated by prefill_path.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL THRESHOLD + NFO-HANDOFF-PREFILL CHECKS PASSED")
