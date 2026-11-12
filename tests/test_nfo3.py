import os, sys, re, html, json, time, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "nfo_media_root3")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)
DIR = os.path.join(MEDIA_ROOT, "video", "Home-Video", "2026.09")
os.makedirs(DIR)

VIDEO_LONG = os.path.join(DIR, "2026.09.01-09.00.00.mp4")
VIDEO_SHORT = os.path.join(DIR, "2026.09.02-10.00.00.mp4")
VIDEO_WITH_NFO = os.path.join(DIR, "2026.09.03-11.00.00.mp4")

def make_video(path, duration):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=500:duration={duration}",
        "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", path, "-loglevel", "error"], check=True)

make_video(VIDEO_LONG, 15)
make_video(VIDEO_SHORT, 3)   # < 10s -- should be flagged "is_short"
make_video(VIDEO_WITH_NFO, 15)

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

# Pre-create an .nfo for VIDEO_WITH_NFO, simulating "already processed before".
nfo_path_existing = os.path.splitext(VIDEO_WITH_NFO)[0] + ".nfo"
with open(nfo_path_existing, "w", encoding="utf-8") as fh:
    fh.write("<movie><title>placeholder</title></movie>")

# --- Test 1: two-stage scan -- normal mode, stage 1 should hide VIDEO_WITH_NFO ---
r = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal"})
scan_html = resolve_job(r)
assert "2 Video(s) gefunden" in scan_html, scan_html[:2000]
assert VIDEO_WITH_NFO not in scan_html
assert "existing_count" not in scan_html  # just making sure no raw leak
assert "Weiter scannen, um existierende" in scan_html, scan_html[:3000]
assert "1 weitere Video(s)" in scan_html, scan_html[:3000]
print("Stage-1 scan correctly hides the video that already has an .nfo, and shows the 'Weiter scannen' hint.")

# --- Test 2: include_existing=true should surface all 3 ---
r2 = client.post("/nfo/scan/start", data={"path": DIR, "root_path": MEDIA_ROOT, "depth": 1, "mode": "normal", "include_existing": "true"})
scan_html2 = resolve_job(r2)
assert "3 Video(s) gefunden" in scan_html2, scan_html2[:2000]
assert VIDEO_WITH_NFO in scan_html2
print("include_existing=true correctly surfaces the video that already has an .nfo too.")

# --- Test 3: short-video flag present for the 3s clip, absent for the 8s clips ---
assert 'data-is-short="true"' in scan_html2
short_row_m = re.search(r'value="' + re.escape(VIDEO_SHORT) + r'".*?</label>', scan_html2, re.S)
assert short_row_m, "could not isolate the short video's row"
# Confirm the row containing VIDEO_SHORT's checkbox is the one marked is_short=true by
# checking the preceding data-is-short attribute on the same <label>.
label_m = re.search(r'<label class="row[^"]*"\s+data-needs-attention="[^"]*"\s+data-is-short="(true|false)">\s*<input type="checkbox" class="row-checkbox" name="selected" value="' + re.escape(VIDEO_SHORT) + '"', scan_html2)
assert label_m and label_m.group(1) == "true", scan_html2[:4000]
label_m_long = re.search(r'<label class="row[^"]*"\s+data-needs-attention="[^"]*"\s+data-is-short="(true|false)">\s*<input type="checkbox" class="row-checkbox" name="selected" value="' + re.escape(VIDEO_LONG) + '"', scan_html2)
assert label_m_long and label_m_long.group(1) == "false", scan_html2[:4000]
print("is_short correctly flags only the <10s clip.")

# --- Test 4: needs_attention -- all 3 videos have no .xmp sidecar at all, so all flagged ---
assert scan_html2.count('data-needs-attention="true"') == 3
assert "kein .xmp-Sidecar" in scan_html2
print("needs_attention correctly flags videos with no .xmp sidecar.")

# --- Test 5: delete endpoint -- server-side re-check rejects the long video, accepts the short one ---
poster_short = os.path.splitext(VIDEO_SHORT)[0] + "-poster.jpg"
with open(poster_short, "wb") as fh:
    fh.write(b"fake")
r3 = client.post("/nfo/delete", data={"selected": [VIDEO_SHORT, VIDEO_LONG]})
del_html = r3.text
assert "gelöscht" in del_html and VIDEO_SHORT.split("/")[-1] in del_html, del_html[:2000]
assert not os.path.exists(VIDEO_SHORT), "short video should have been deleted"
assert not os.path.exists(poster_short), "companion poster.jpg should have been deleted too"
assert os.path.exists(VIDEO_LONG), "long video must NOT be deleted even though it was 'selected'"
assert "nicht &lt; 10s" in del_html, del_html[:2000]
print("Delete endpoint deleted only the actually-short video + its companion file, and server-side re-verified duration for the long one (rejected).")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL TWO-STAGE-SCAN / SMART-FILTER / DELETE CHECKS PASSED")
