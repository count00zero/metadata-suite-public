import os, sys, re, html, json, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "nfo_media_root2")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
DIR = os.path.join(MEDIA_ROOT, "video", "Home-Video", "2026.08")
os.makedirs(DIR)
VIDEO = os.path.join(DIR, "2026.08.03-09.00.00.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=8",
    "-f", "lavfi", "-i", "sine=frequency=500:duration=8",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT
os.environ["VIDEO_ROOT"] = os.path.join(MEDIA_ROOT, "video", "Home-Video")
os.environ["PHOTO_ROOT"] = os.path.join(MEDIA_ROOT, "photo")

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from fastapi.testclient import TestClient
client = TestClient(app)

def resolve_job(resp):
    htmlsrc = resp.text
    while True:
        m = re.search(r'hx-get="(/nfo/[a-z]+/progress/[0-9a-f]+)"', htmlsrc)
        if not m: return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc: return htmlsrc

def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value=\'(.*?)\'', htmlsrc, re.S)
    assert m, htmlsrc[:1500]
    return html.unescape(m.group(1))

# scan + generate once (no AI/geo at all -- no .xmp)
r = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal"})
scan_html = resolve_job(r)
all_rows_json = get_hidden(scan_html, "all_rows")
r2 = client.post("/nfo/generate/start", data={"selected": [VIDEO], "all_rows": all_rows_json})
results_html = resolve_job(r2)
assert "1 erstellt" in results_html, results_html[:1500]
nfo_path = os.path.splitext(VIDEO)[0] + ".nfo"
mtime1 = os.path.getmtime(nfo_path)
print("First generate OK.")

# Scan again: without force, generate/start on the same video should SKIP (not overwrite)
# (include_existing: the default scan holds back videos that already have an .nfo)
r3 = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal", "include_existing": "true"})
scan_html2 = resolve_job(r3)
all_rows_json2 = get_hidden(scan_html2, "all_rows")
r4 = client.post("/nfo/generate/start", data={"selected": [VIDEO], "all_rows": all_rows_json2})
results_html2 = resolve_job(r4)
assert "1 übersprungen" in results_html2, results_html2[:1500]
print("Regression OK: without force, existing .nfo is skipped on re-generate.")

# Now WITH force=true (simulating the checkbox ticked after the scan, via hx-include/live form)
r5 = client.post("/nfo/generate/start", data={"selected": [VIDEO], "all_rows": all_rows_json2, "force": "true"})
results_html3 = resolve_job(r5)
assert "1 erstellt" in results_html3, results_html3[:1500]
mtime2 = os.path.getmtime(nfo_path)
print("force=true OK: .nfo regenerated (mtime unchanged since mtimecore restores it, but content rewritten).")

# --repair-italic mode: write an old-style [I]/[/I] nfo by hand, then scan for it.
with open(nfo_path, "r", encoding="utf-8") as fh:
    content = fh.read()
content = content.replace("<plot>", "<plot>[I]").replace("</plot>", "[/I]</plot>")
with open(nfo_path, "w", encoding="utf-8") as fh:
    fh.write(content)

r6 = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "repair-italic"})
scan_html3 = resolve_job(r6)
assert "1 Video(s) gefunden" in scan_html3, scan_html3[:2000]
print("repair-italic mode found the video with old [I]/[/I] formatting.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL FORCE/REPAIR-MODE CHECKS PASSED")
