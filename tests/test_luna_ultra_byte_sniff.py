import os, re, subprocess, tempfile, time

MEDIA_ROOT = tempfile.mkdtemp(prefix="lunasniff_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

folder = os.path.join(MEDIA_ROOT, "clips")
os.makedirs(folder, exist_ok=True)

# A Luna Ultra video exactly as the maintainer's real files are: filename already in the target
# pattern, and NO EXIF/QuickTime Make/Model tag at all (confirmed against his real .xmp
# dump earlier) -- the camera identity only shows up as a raw string somewhere in the
# file's own binary data, same as the old bash script's head/tail `strings | grep` found.
video = os.path.join(folder, "2026.10.03-16.28.57.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64", "-t", "1", video],
               check=True, capture_output=True)
subprocess.run(["exiftool", "-overwrite_original", "-AllDates=2026:10:03 16:28:57",
                "-Comment=firmware Insta360 Luna Ultra build 42", video],
               check=True, capture_output=True)

no_tag = subprocess.run(["exiftool", "-s", "-Make", "-Model", video],
                         capture_output=True, text=True).stdout
print("Make/Model tags on this test file:", repr(no_tag.strip()))
assert no_tag.strip() == "", "test setup should leave Make/Model genuinely empty"

# Confirm the sniff function directly first.
import sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.core import timecore
make, model = timecore.sniff_camera_from_bytes(video)
print("sniff_camera_from_bytes ->", make, "/", model)
assert model == "Insta360 Luna Ultra"

# Now the real end-to-end path: the camera_model filter in Timestamp & Rename.
resp = client.post("/timestamp/browse/start", data={
    "path": folder, "root_path": MEDIA_ROOT, "depth": "0", "default_tz": "Europe/Vienna",
    "camera_model": "Luna Ultra",
})
assert resp.status_code == 200, resp.text
m = re.search(r'/timestamp/browse/progress/([a-f0-9-]+)', resp.text)
assert m, resp.text[:2000]
job_id = m.group(1)
for _ in range(200):
    r = client.get(f"/timestamp/browse/progress/{job_id}")
    if "progress-panel" not in r.text:
        html = r.text
        break
    time.sleep(0.05)
else:
    raise RuntimeError("scan never finished")

assert html.count('class="row-checkbox"') == 1, html
assert "Insta360 Luna Ultra" in html

print("\nPASS: a video with NO EXIF Make/Model tag at all, but the Insta360 Luna Ultra")
print("signature buried in its own binary data, is now correctly found by the")
print("camera_model filter via the raw byte-signature fallback.")

import shutil
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
