import os, sys, json, re, html, shutil

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "media_root_bugtest")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)

YEAR_DIR = os.path.join(MEDIA_ROOT, "Urlaube")
TRIP_DIR = os.path.join(YEAR_DIR, "2026.08.05-2026.08.22 Sri Lanka & Malediven")
os.makedirs(TRIP_DIR)

from PIL import Image
for i in range(3):
    Image.new("RGB", (64, 64), (i*30, 10, 10)).save(os.path.join(TRIP_DIR, f"img{i}.jpg"))

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["PHOTO_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = ""
os.environ["ROOT_FOLDERS"] = f"TEST={MEDIA_ROOT}"

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

import time

def resolve_job(resp, progress_url_prefix):
    htmlsrc = resp.text
    while True:
        m = re.search(r'hx-get="(/collage/[a-z]+/progress/[0-9a-f]+)"', htmlsrc)
        if not m:
            return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc:
            return htmlsrc


def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=\'(.*?)\'', htmlsrc, re.S)
    assert m, f"hidden field {name} not found in:\n{htmlsrc[:2000]}"
    return html.unescape(m.group(1))

# Step 1: pre-create an "existing" collage output so the trip shows as already_exists.
output_path = os.path.join(YEAR_DIR, "2026.08.05-2026.08.22 Sri Lanka & Malediven.jpg")
Image.new("RGB", (100, 100), (0, 0, 0)).save(output_path)
orig_mtime = os.path.getmtime(output_path)

# Step 2: discover trips WITHOUT force checked (simulating user not yet checking the box).
r = client.post("/collage/discover/start", data={"path": YEAR_DIR, "root_path": MEDIA_ROOT})
assert r.status_code == 200, r.text
trip_list_html = resolve_job(r, "/collage/discover/progress/")
all_trips_json = get_hidden(trip_list_html, "all_trips")
trips_data = json.loads(all_trips_json)
print("Discovered trips:", [t["name"] for t in trips_data])
assert len(trips_data) == 1
trip_path = trips_data[0]["path"]
assert trips_data[0]["already_exists"] is True

# Step 3: simulate the REAL bug scenario -- generate/start is now called with force=True
# (as if the user checked "Vorhandene Collagen überschreiben" AFTER seeing the trip list,
# which is what hx-include="#browse-form" now delivers live) and the existing trip selected.
r2 = client.post("/collage/generate/start", data={
    "selected": [trip_path],
    "all_trips": all_trips_json,
    "force": "true",
    "layout": "mosaic",
    "width": "800", "height": "600", "border": "4",
    "jitter": "0.55", "size_variation": "0.35", "spacing": "0",
})
assert r2.status_code == 200, r2.text
results_html = resolve_job(r2, "/collage/generate/progress/")
print("\n--- /collage/generate/start (force=true) result ---")
assert "skipped_exists" not in results_html or "übersprungen" not in results_html, \
    "BUG A still present: trip was skipped despite force=true"
assert "1 erstellt" in results_html, results_html[:1500]
print("Bug A FIXED: force=true overwrote the existing collage.")

new_mtime = os.path.getmtime(output_path)
from PIL import Image as _I
assert _I.open(output_path).size != (50, 50), 'placeholder collage (50x50) was not replaced'  # file was rewritten
print("Output file was actually regenerated on disk.")

# Step 4: pull settings_json + row_id out of the result row, click "Neu erzeugen" (regenerate).
settings_json = get_hidden(results_html, "settings_json")
m_row = re.search(r'id="(collage-row-[a-f0-9]+)"', results_html)
assert m_row, "no row id found in result row -- row_id not being rendered"
row_id = m_row.group(1)
print("row_id:", row_id, "(must be plain hex -- a valid CSS id regardless of trip name)")
assert re.fullmatch(r"collage-row-[a-f0-9]{32}", row_id), f"row_id not DOM/CSS-safe: {row_id}"

m_trip_path = re.search(r'name="trip_path" value="(.*?)"', results_html)
m_trip_name = re.search(r'name="trip_name" value="(.*?)"', results_html)
m_trip_out = re.search(r'name="trip_output_path" value="(.*?)"', results_html)
assert m_trip_path and m_trip_name and m_trip_out, results_html[:2000]

r3 = client.post("/collage/regenerate", data={
    "trip_path": html.unescape(m_trip_path.group(1)),
    "trip_name": html.unescape(m_trip_name.group(1)),
    "trip_output_path": html.unescape(m_trip_out.group(1)),
    "trip_image_count": 3,
    "settings_json": settings_json,
})
assert r3.status_code == 200, r3.text
print("\n--- /collage/regenerate result ---")
assert row_id in r3.text, "regenerate response doesn't carry the same row_id (hx-target mismatch)"
assert "erstellt" in r3.text, r3.text[:1500]
print("Bug B FIXED: regenerate endpoint returns a row with a stable, CSS-safe id and succeeds.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL CHECKS PASSED")
