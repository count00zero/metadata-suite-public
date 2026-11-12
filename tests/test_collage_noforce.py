import os, sys, json, re, html, shutil, time

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "media_root_bugtest2")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
YEAR_DIR = os.path.join(MEDIA_ROOT, "Urlaube")
TRIP_DIR = os.path.join(YEAR_DIR, "Trip One")
os.makedirs(TRIP_DIR)
from PIL import Image
for i in range(3):
    Image.new("RGB", (64,64), (10,10,10)).save(os.path.join(TRIP_DIR, f"img{i}.jpg"))

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["PHOTO_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = ""

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)

def resolve_job(resp):
    htmlsrc = resp.text
    while True:
        m = re.search(r'hx-get="(/collage/[a-z]+/progress/[0-9a-f]+)"', htmlsrc)
        if not m: return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc: return htmlsrc

def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=\'(.*?)\'', htmlsrc, re.S)
    assert m, htmlsrc[:1500]
    return html.unescape(m.group(1))

output_path = os.path.join(YEAR_DIR, "Trip One.jpg")
Image.new("RGB", (50,50)).save(output_path)

r = client.post("/collage/discover/start", data={"path": YEAR_DIR, "root_path": MEDIA_ROOT})
trip_list_html = resolve_job(r)
all_trips_json = get_hidden(trip_list_html, "all_trips")
trip_path = json.loads(all_trips_json)[0]["path"]

# force NOT sent at all (checkbox unchecked -> field absent), existing trip selected anyway
r2 = client.post("/collage/generate/start", data={
    "selected": [trip_path], "all_trips": all_trips_json,
    "layout": "mosaic", "width": "800", "height": "600",
})
results_html = resolve_job(r2)
assert "1 übersprungen" in results_html, results_html[:1500]
print("Regression OK: without force, existing trip is still skipped.")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
