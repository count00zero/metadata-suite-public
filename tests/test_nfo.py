import os, sys, re, html, json, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "nfo_media_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)

TRIP_DIR = os.path.join(MEDIA_ROOT, "video", "Home-Video", "2026.08.05-2026.08.22 Sri Lanka & Malediven")
os.makedirs(TRIP_DIR)
VIDEO = os.path.join(TRIP_DIR, "2026.08.10-14.30.00.mp4")

subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=640x480:d=12",
    "-f", "lavfi", "-i", "sine=frequency=600:duration=12",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error",
], check=True)

# Pre-write an .xmp sidecar with geo + a clean AI description, mimicking GPS Sidecar /
# AI Description modules having already run on this video.
XMP = VIDEO + ".xmp"
subprocess.run([
    "exiftool", "-o", XMP,
    "-XMP-photoshop:Country=Sri Lanka", "-XMP-photoshop:City=Galle",
    "-XMP-dc:Description=Zwei Personen laufen an einem Strand entlang.",
    VIDEO,
], check=True, capture_output=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = os.path.join(MEDIA_ROOT, "video", "Home-Video")
os.environ["PHOTO_ROOT"] = os.path.join(MEDIA_ROOT, "photo")
os.environ["NFO_ACTORS"] = "Anna Beispiel|Anna, Papa, Cameraman|/storage/videos/.actors/Anna_Beispiel.jpg"

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)

def resolve_job(resp):
    htmlsrc = resp.text
    while True:
        m = re.search(r'hx-get="(/nfo/[a-z]+/progress/[0-9a-f]+)"', htmlsrc)
        if not m:
            return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc:
            return htmlsrc

def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=\'(.*?)\'', htmlsrc, re.S)
    assert m, f"hidden field {name} not found:\n{htmlsrc[:2000]}"
    return html.unescape(m.group(1))

# --- Phase 1: scan ---
r = client.post("/nfo/scan/start", data={
    "path": os.path.dirname(TRIP_DIR), "root_path": MEDIA_ROOT, "depth": 2, "mode": "normal",
})
scan_html = resolve_job(r)
assert "1 Video(s) gefunden" in scan_html, scan_html[:2000]
assert "Sri Lanka" in scan_html
print("Phase 1 (scan) OK -- found the video, geo summary shown:",
      "Galle" in scan_html)

all_rows_json = get_hidden(scan_html, "all_rows")

# --- Phase 2: generate (force off, ai on, via live browse-form fields) ---
r2 = client.post("/nfo/generate/start", data={
    "selected": [VIDEO],
    "all_rows": all_rows_json,
    "ai_description_enabled": "true",
})
results_html = resolve_job(r2)
assert "1 erstellt" in results_html, results_html[:3000]
print("Phase 2 (generate) OK -- 1 erstellt")

nfo_path = os.path.splitext(VIDEO)[0] + ".nfo"
assert os.path.isfile(nfo_path), "NFO file was not written"
with open(nfo_path, encoding="utf-8") as fh:
    nfo_xml = fh.read()
assert "Zwei Personen laufen an einem St" in nfo_xml, "AI description not embedded in plot"
assert "Galle" in nfo_xml, "Geo line not embedded in plot"
assert "Anna Beispiel" in nfo_xml, "Configured actor list not embedded"
import xml.dom.minidom as minidom
minidom.parseString(nfo_xml)
print("NFO XML parses OK, contains AI description + geo + actors.")

for suffix in ("-poster.jpg", "-fanart.jpg", "-thumb.jpg"):
    assert os.path.isfile(os.path.splitext(VIDEO)[0] + suffix), f"missing {suffix}"
assert os.path.isfile(os.path.join(TRIP_DIR, "folder.jpg")), "missing folder.jpg"
print("Thumbnails (poster/fanart/thumb/folder.jpg) all written.")

# --- row_id / regenerate (the Collage-bug lesson applied up front) ---
m_row = re.search(r'id="(nfo-row-[a-f0-9]+)"', results_html)
assert m_row, "no row id in result row"
row_id = m_row.group(1)
assert re.fullmatch(r"nfo-row-[a-f0-9]{32}", row_id)
print("row_id is a valid, DOM/CSS-safe hash:", row_id)

r3 = client.post("/nfo/regenerate", data={"video_path": VIDEO, "ai_description_enabled": "true"})
assert r3.status_code == 200, r3.text
assert row_id in r3.text
assert "erstellt" in r3.text
print("Regenerate OK -- same row_id returned, NFO rewritten.")

# --- Regression: two-stage scan -- a video that already has an .nfo is held back by default
# (with a "weiter scannen" hint) and only shown, flagged "existiert bereits", with include_existing ---
r4 = client.post("/nfo/scan/start", data={
    "path": os.path.dirname(TRIP_DIR), "root_path": MEDIA_ROOT, "depth": 2, "mode": "normal",
})
scan_html2 = resolve_job(r4)
assert "0 Video(s) gefunden" in scan_html2 and "bereits eine .nfo" in scan_html2, scan_html2[:2000]
r5 = client.post("/nfo/scan/start", data={
    "path": os.path.dirname(TRIP_DIR), "root_path": MEDIA_ROOT, "depth": 2, "mode": "normal",
    "include_existing": "true",
})
scan_html3 = resolve_job(r5)
assert "1 Video(s) gefunden" in scan_html3 and "existiert bereits" in scan_html3, scan_html3[:2000]
print("Regression OK: re-scan shows the video as already having an .nfo.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL NFO MAKER CHECKS PASSED")
