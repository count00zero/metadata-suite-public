import os, sys, re, html, time, shutil, subprocess, json

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "xmpsync_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)

IMAGE = os.path.join(MEDIA_ROOT, "2026.09.01-09.00.00.jpg")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240",
    "-frames:v", "1", IMAGE, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["AI_PROVIDER"] = "ollama"
os.environ["OLLAMA_URL"] = "http://fake"
os.environ["OLLAMA_MODEL"] = "fake-model"

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from app.core import aicore
from fastapi.testclient import TestClient
client = TestClient(app)


def resolve_job(resp, prefix):
    htmlsrc = resp.text
    while True:
        m = re.search(rf'hx-get="({prefix}/progress/[0-9a-f]+)"', htmlsrc)
        if not m:
            return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc:
            return htmlsrc


def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=[\'"](.*?)[\'"]', htmlsrc, re.S)
    assert m, f"could not find hidden field {name!r} in:\n" + htmlsrc[:2000]
    return html.unescape(m.group(1))


def exiftool_tag(path, tag):
    res = subprocess.run(["exiftool", "-m", "-T", tag, path], capture_output=True, text=True)
    return res.stdout.strip()


# --- Test 1: GPS Sidecar's own index.html checkbox defaults to UNCHECKED now ---
idx_html = client.get("/gps/").text
m = re.search(r'name="embed_in_image"[^>]*>', idx_html)
assert m, idx_html[:500]
assert "checked" not in m.group(0), f"GPS Sidecar's embed_in_image checkbox should default unchecked: {m.group(0)}"
print("PASS: GPS Sidecar's 'ins Bild einbetten' checkbox defaults to unchecked.")

idx_html_ai = client.get("/ai/").text
m2 = re.search(r'name="embed_in_image"[^>]*>', idx_html_ai)
assert m2, idx_html_ai[:500]
assert "checked" in m2.group(0), f"AI-Beschreibung's embed_in_image checkbox should stay default checked: {m2.group(0)}"
print("PASS: AI-Beschreibung's 'ins Bild einbetten' checkbox still defaults to checked.")

# --- Test 2: GPS Sidecar execute WITHOUT embed_in_image -- image must be untouched,
#     .xmp must have GPS+location. ---
r = client.post("/gps/browse/start", data={
    "path": MEDIA_ROOT, "root_path": MEDIA_ROOT, "depth": 0, "default_tz": "Europe/Vienna",
    "skip_already_done": "true",
    # embed_in_image deliberately omitted -- mirrors an unchecked checkbox
})
browse_html = resolve_job(r, "/gps/browse")
all_scanned_rows = get_hidden(browse_html, "all_scanned_rows")
rows = json.loads(all_scanned_rows)
assert len(rows) == 1, browse_html[:2000]
row = rows[0]
row["lat"], row["lon"] = 48.2082, 16.3738
row["geo"] = {"country": "Österreich", "country_a": "AUT", "state": "Wien", "city": "Wien",
              "locality": "", "street": "", "source": "test"}
all_rows_json = json.dumps([row])

before_image_desc = exiftool_tag(IMAGE, "-XMP-dc:Description")
before_image_gps = exiftool_tag(IMAGE, "-GPSLatitude")

r2 = client.post("/gps/execute", data={
    "selected": [IMAGE], "all_rows": all_rows_json,
    # embed_in_image omitted
})
exec_html = r2.text
assert r2.status_code == 200, exec_html[:500]
xmp_path = IMAGE + ".xmp"
assert os.path.exists(xmp_path)
xmp_country = exiftool_tag(xmp_path, "-XMP-photoshop:Country")
assert xmp_country == "Österreich", f"xmp country mismatch: {xmp_country!r}\n{exec_html[:2000]}"

after_image_gps = exiftool_tag(IMAGE, "-GPSLatitude")
assert after_image_gps == before_image_gps and after_image_gps in ("", "-"), (
    f"image should NOT have been touched when embed_in_image was omitted: "
    f"before={before_image_gps!r} after={after_image_gps!r}"
)
print("PASS: GPS Sidecar execute without embed_in_image writes only the .xmp, leaves the image file untouched.")

# --- Test 3: AI-Beschreibung execute WITH embed_in_image (default) -- image gets BOTH
#     the GPS/location (already in .xmp from step 2) AND the new description, in one
#     sync_xmp_to_image() call. ---
aicore._PROVIDER_CALLERS["ollama"] = lambda image_b64, sp, p, cfg: ("Ein Testbild aus Wien.", None)

r3 = client.post("/ai/browse/start", data={
    "path": MEDIA_ROOT, "root_path": MEDIA_ROOT, "depth": 0, "type_filter": "ALL",
    "overwrite_existing": True, "embed_in_image": True,
})
ai_scan_html = resolve_job(r3, "/ai/browse")
all_scanned_rows_ai = get_hidden(ai_scan_html, "all_scanned_rows")
ai_rows = json.loads(all_scanned_rows_ai)
assert len(ai_rows) == 1, ai_scan_html[:2000]

r4 = client.post("/ai/generate/start", data={
    "selected": [IMAGE], "all_scanned_rows": all_scanned_rows_ai, "embed_in_image": True,
})
ai_gen_html = resolve_job(r4, "/ai/generate")
all_rows_ai = get_hidden(ai_gen_html, "all_rows")

r5 = client.post("/ai/execute", data={
    "selected": [IMAGE], "all_rows": all_rows_ai, "embed_in_image": True,
})
ai_exec_html = r5.text
assert r5.status_code == 200, ai_exec_html[:500]

final_image_gps = exiftool_tag(IMAGE, "-GPSLatitude")
final_image_country = exiftool_tag(IMAGE, "-XMP-photoshop:Country")
final_image_desc = exiftool_tag(IMAGE, "-XMP-dc:Description")

assert final_image_gps not in ("", "-"), f"image should now have GPS embedded (synced from .xmp): {final_image_gps!r}\n{ai_exec_html[:2000]}"
assert final_image_country == "Österreich", f"image should have country embedded too: {final_image_country!r}"
assert final_image_desc == "Ein Testbild aus Wien.", f"image should have the new description: {final_image_desc!r}"
print("PASS: AI-Beschreibung execute with embed_in_image writes GPS+Location (from GPS Sidecar's earlier .xmp) AND the new description into the image, in one sync.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL XMP-SYNC-ON-LAST-STEP CHECKS PASSED")
