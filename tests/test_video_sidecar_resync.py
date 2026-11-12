import os, re, json, subprocess, tempfile, time

MEDIA_ROOT = tempfile.mkdtemp(prefix="videoresync_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

folder = os.path.join(MEDIA_ROOT, "clips")
os.makedirs(folder, exist_ok=True)

# Simulate a Luna Ultra video: filename already matches the target pattern (local wall
# clock 16:28:57+02:00), but the video's OWN embedded QuickTime CreateDate is still the
# camera's raw UTC capture time (14:28:58) -- i.e. it was renamed once before but
# "EXIF zurückschreiben" was skipped for this video (the maintainer's actual workflow).
video = os.path.join(folder, "2026.10.03-16.28.57.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64",
                "-f", "lavfi", "-i", "anullsrc", "-t", "1",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", video],
               check=True, capture_output=True)
subprocess.run(["exiftool", "-api", "LargeFileSupport=1",
                "-quicktime:time:all=2026:10:03 14:28:58+00:00",
                "-Make=Arashi Vision", "-Model=Insta360 Luna Ultra",
                "-P", "-overwrite_original", video], check=True, capture_output=True)

# A pre-existing, now-stale .xmp sidecar sitting next to it -- built earlier (e.g. by GPS
# Sidecar) directly from the still-uncorrected video, so it holds the same wrong UTC value.
xmp_path = video + ".xmp"
subprocess.run(["exiftool", "-m", "-q", "-api", "LargeFileSupport=1", "-api", "QuickTimeUTC=0",
                "-ee", "-o", xmp_path, video], check=True, capture_output=True)

before = subprocess.run(["exiftool", "-s", "-CreateDate", xmp_path],
                         capture_output=True, text=True).stdout
print("XMP CreateDate BEFORE:", before.strip())
assert "14:28:58" in before, f"test setup didn't produce the expected stale value: {before}"

# Timestamp & Rename filtered to this camera, then executed with EXIF write-back ticked:
# the video itself is never rewritten, but its stale .xmp sidecar is re-synced.
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

# Just 1 row: the video itself. Its .xmp companion isn't shown as its own row anymore
# (router.py now suppresses a companion whose target name wouldn't actually change --
# and it never would here, since the video's own filename is already correct and this
# tier only corrects the OFFSET, never the wall-clock digits the companion's target name
# is built from).
assert html.count('class="row-checkbox"') == 1, html
assert "Insta360 Luna Ultra" in html
m2 = re.search(r"name=\"all_resolutions\" value='(.*?)'", html, re.DOTALL)
assert m2, "couldn't find all_resolutions hidden field"
all_resolutions = m2.group(1).replace("&#34;", '"')

resp2 = client.post("/timestamp/execute", data={
    "selected": [video], "all_resolutions": all_resolutions,
    "do_rename": "true", "do_exifwrite": "true",
})
assert resp2.status_code == 200, resp2.text

after = subprocess.run(["exiftool", "-s", "-CreateDate", xmp_path],
                        capture_output=True, text=True).stdout
print("XMP CreateDate AFTER: ", after.strip())
assert "16:28:57" in after, f"sidecar was not resynced to the local time: {after}"
assert "14:28:58" not in after

video_raw = subprocess.run(["exiftool", "-api", "QuickTimeUTC=0", "-s", "-CreateDate", video],
                            capture_output=True, text=True).stdout
print("Video's own QuickTime CreateDate AFTER:", video_raw.strip())
assert "14:28:58" in video_raw, "videos are no longer rewritten"

print("\nPASS: the video is left untouched and its stale .xmp sidecar was re-synced to the local time.")

import shutil
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
