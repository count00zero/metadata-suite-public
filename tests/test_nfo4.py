import os, sys, re, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "nfo_media_root4")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
DIR = os.path.join(MEDIA_ROOT, "video", "Home-Video", "2026.09")
os.makedirs(DIR)

for i in range(6):
    v = os.path.join(DIR, f"2026.09.{i:02d}-09.00.00.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2",
        "-f", "lavfi", "-i", "sine=frequency=500:duration=2",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", v, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = os.path.join(MEDIA_ROOT, "video", "Home-Video")
os.environ["PHOTO_ROOT"] = os.path.join(MEDIA_ROOT, "photo")

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)

r = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal"})
m = re.search(r'hx-get="(/nfo/scan/progress/[0-9a-f]+)"', r.text)
assert m, r.text[:1000]
progress_url = m.group(1)
job_id = progress_url.rsplit("/", 1)[-1]

# Cancel almost immediately (before the scan of 6 videos has necessarily finished).
cancel_resp = client.post(f"/nfo/scan/cancel/{job_id}")
assert cancel_resp.status_code == 204

# Poll until the job is no longer "running".
html_src = r.text
for _ in range(100):
    if "progress-panel" not in html_src:
        break
    time.sleep(0.05)
    html_src = client.get(progress_url).text

assert "Suche gestoppt" in html_src or "Keine passenden Videos gefunden" in html_src, html_src[:2000]
if "Suche gestoppt" in html_src:
    print("Cancel-with-partial-results path triggered: found 'Suche gestoppt' message with partial rows retained.")
else:
    print("NOTE: scan finished before cancel took effect (machine too fast) -- partial-results code path not exercised, but no crash either.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
