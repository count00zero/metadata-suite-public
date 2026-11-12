import os, re, json, subprocess, tempfile, time

MEDIA_ROOT = tempfile.mkdtemp(prefix="nfobug_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

folder = os.path.join(MEDIA_ROOT, "videos")
os.makedirs(folder, exist_ok=True)

stem = "2026.10.03-16.28.57"
video = os.path.join(folder, stem + ".mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64", "-t", "1", video],
               check=True, capture_output=True)
subprocess.run(["exiftool", "-overwrite_original", "-AllDates=2026:10:03 16:28:57", video],
               check=True, capture_output=True)

# Full set of companions a finished pipeline run leaves behind: .xmp sidecar, .nfo
# (NFO Maker), poster/fanart/thumb (NFO Maker's own artwork) -- all sharing the video's
# exact stem, same as the maintainer's real library.
xmp = video + ".xmp"
open(xmp, "w").write("<x:xmpmeta/>")
nfo = os.path.join(folder, stem + ".nfo")
open(nfo, "w").write("<movie></movie>")
for suffix in ("-poster", "-fanart", "-thumb"):
    open(os.path.join(folder, stem + suffix + ".jpg"), "wb").write(
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=16x16",
                        "-frames:v", "1", "-f", "image2", "-"],
                       capture_output=True).stdout
    )

resp = client.post("/timestamp/browse/start", data={
    "path": folder, "root_path": MEDIA_ROOT, "depth": "0", "default_tz": "Europe/Vienna",
    "include_already_named": "true",
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

primary_rows = html.count('class="row ')          # primary rows (note trailing space before optional warn class)
companion_rows = html.count('class="row row-companion"')
print("primary rows:", primary_rows, "companion rows:", companion_rows)
print("'.nfo' appears as filename text:", html.count(stem + ".nfo"))

assert primary_rows == 1, f"expected exactly 1 primary row (the video itself), got {primary_rows}\n{html[:3000]}"
# Nothing is actually going to change here (the video's own resolved name already
# matches its current name), so none of its companions (.xmp, .nfo, poster, fanart,
# thumb) should show either -- there's nothing for them to do.
assert companion_rows == 0, f"expected 0 companion rows (nothing would change), got {companion_rows}"
assert stem + ".nfo" not in html

print("\nPASS: with the full companion set present, 'Umbenannte einblenden' shows exactly")
print("one PRIMARY row (the video) and none of its unchanged companions -- the .nfo isn't")
print("treated as its own independent candidate, and the .xmp/.nfo/poster/fanart/thumb")
print("companions aren't shown at all since nothing would actually happen to them.")

import shutil
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
