import os, re, subprocess, tempfile, time

MEDIA_ROOT = tempfile.mkdtemp(prefix="companion_regress_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

folder = os.path.join(MEDIA_ROOT, "clips")
os.makedirs(folder, exist_ok=True)

# A NOT-yet-renamed video (camera-native filename) -- its resolution WILL produce a
# different target name, so its companions should still show (they'll actually change).
video = os.path.join(folder, "clip_from_cam.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=64x64", "-t", "1", video],
               check=True, capture_output=True)
subprocess.run(["exiftool", "-overwrite_original", "-AllDates=2026:10:03 16:28:57", video],
               check=True, capture_output=True)
open(video + ".xmp", "w").write("<x:xmpmeta/>")

resp = client.post("/timestamp/browse/start", data={
    "path": folder, "root_path": MEDIA_ROOT, "depth": "0", "default_tz": "Europe/Vienna",
})
assert resp.status_code == 200, resp.text
m = re.search(r'/timestamp/browse/progress/([a-f0-9-]+)', resp.text)
job_id = m.group(1)
for _ in range(200):
    r = client.get(f"/timestamp/browse/progress/{job_id}")
    if "progress-panel" not in r.text:
        html = r.text
        break
    time.sleep(0.05)
else:
    raise RuntimeError("scan never finished")

assert "row-companion" not in html, "companions are never listed as their own rows"
assert "Begleitdateien werden mitgeändert" in html and "clip_from_cam.mp4.xmp" in html
print("PASS: a companion of a file that IS actually being renamed still shows up.")

import shutil
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
