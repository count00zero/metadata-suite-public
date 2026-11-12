import os, re, json, subprocess, tempfile, time

MEDIA_ROOT = tempfile.mkdtemp(prefix="camfilter_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

folder = os.path.join(MEDIA_ROOT, "photos")
os.makedirs(folder, exist_ok=True)

# Simulate a Luna Ultra photo that was WRONGLY named under the old bug: it matches the
# target naming pattern already (so is_already_named() would normally skip it), but the
# baked-in timestamp (14:30 Europe/Vienna Fallback-Zone) is wrong -- the true local time
# (per CreateDate=UTC 10:30 + Model="Insta360 Luna Ultra") should be 14:30 Asia/Dubai-ish
# offset... to keep it simple: give it a WRONG filename timestamp "12.00.00" that does NOT
# match what CreateDate-based UTC-diff would now compute, so will_rename is unambiguous.
wrong_name = os.path.join(folder, "2024.03.15-12.00.00.jpg")
subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1", wrong_name],
               check=True, capture_output=True)
subprocess.run(["exiftool","-overwrite_original",
                "-CreateDate=2024:03:15 10:30:00", "-DateTimeOriginal=",
                "-Model=Insta360 Luna Ultra", "-Make=Arashi Vision",
                wrong_name], check=True, capture_output=True)

# A normal, correctly-named, unrelated photo from a different camera in the same folder --
# must NOT show up when filtering by "Luna Ultra".
other = os.path.join(folder, "2024.03.15-09.00.00.jpg")
subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=red:s=64x64","-frames:v","1", other],
               check=True, capture_output=True)
subprocess.run(["exiftool","-overwrite_original", "-AllDates=2024:03:15 09:00:00",
                "-Model=Pixel 9", "-Make=Google", other], check=True, capture_output=True)

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

print(html)
found = html.count('class="row-checkbox"')
assert found == 1, f"expected exactly 1 row (the Luna Ultra one), got {found}"
assert "Insta360 Luna Ultra" in html
assert "2024.03.15-09.00.00.jpg" not in html, "unrelated Pixel photo leaked into the filtered view"
assert "→ 2024.03.15" in html or "will_rename" in html  # sanity: a new target is shown

print("\nPASS: camera_model filter found exactly the Luna Ultra photo (despite already matching the")
print("naming pattern), showed its detected camera, and excluded the unrelated photo.")

import shutil
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
