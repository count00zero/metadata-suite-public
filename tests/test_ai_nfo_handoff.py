import os, sys, re, html, time, shutil, subprocess, json

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "ai_nfo_handoff_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)

VIDEO_DIR = os.path.join(MEDIA_ROOT, "video")
IMAGE_DIR = os.path.join(MEDIA_ROOT, "image")
os.makedirs(VIDEO_DIR)
os.makedirs(IMAGE_DIR)

VIDEO = os.path.join(VIDEO_DIR, "2026.09.01-09.00.00.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
    "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error"], check=True)

IMAGE = os.path.join(IMAGE_DIR, "2026.09.01-10.00.00.jpg")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240",
    "-frames:v", "1", IMAGE, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.main import app
from app.core import aicore
from fastapi.testclient import TestClient
client = TestClient(app)

# Stub out the actual AI provider call -- we only care about the has_video/handoff
# plumbing here, not real model inference.
class FakeResult:
    def __init__(self, description="Ein Testvideo." if True else ""):
        self.description = description
        self.provider = "fake"
        self.model = "fake"
        self.failures = []

def fake_generate_description(path, geo_context, ai_cfg, is_video):
    return FakeResult()

aicore.generate_description = fake_generate_description


def resolve_job(resp, prefix):
    htmlsrc = resp.text
    while True:
        m = re.search(rf'hx-get="({prefix}/progress/[0-9a-f]+)"', htmlsrc)
        if not m: return htmlsrc
        time.sleep(0.05)
        htmlsrc = client.get(m.group(1)).text
        if "progress-panel" not in htmlsrc: return htmlsrc


def get_hidden(htmlsrc, name):
    m = re.search(rf'name="{name}"\s+value="(.*?)"', htmlsrc, re.S)
    assert m, f"could not find hidden field {name!r} in:\n" + htmlsrc[:2000]
    return html.unescape(m.group(1))


def get_hidden_single_quoted(htmlsrc, name):
    m = re.search(rf"name=\"{name}\"\s+value='(.*?)'", htmlsrc, re.S)
    assert m, f"could not find hidden field {name!r} in:\n" + htmlsrc[:2000]
    return html.unescape(m.group(1))


def run_chain(path, root_path, expect_video):
    # Phase 1: browse/start (scan)
    r = client.post("/ai/browse/start", data={
        "path": path, "root_path": root_path, "depth": 0, "type_filter": "ALL",
        "overwrite_existing": True, "embed_in_image": True,
    })
    scan_html = resolve_job(r, "/ai/browse")
    all_scanned_rows = get_hidden_single_quoted(scan_html, "all_scanned_rows")
    rows = json.loads(all_scanned_rows)
    assert len(rows) >= 1, scan_html[:2000]
    scan_path = get_hidden(scan_html, "path")
    scan_root_path = get_hidden(scan_html, "root_path")
    assert scan_path == path, f"expected path {path!r} carried through, got {scan_path!r}"
    assert scan_root_path == root_path

    selected_paths = [r["path"] for r in rows]

    # Phase 2: generate/start
    r2 = client.post("/ai/generate/start", data={
        "selected": selected_paths, "all_scanned_rows": all_scanned_rows,
        "embed_in_image": True, "path": scan_path, "root_path": scan_root_path,
    })
    gen_html = resolve_job(r2, "/ai/generate")
    all_rows = get_hidden_single_quoted(gen_html, "all_rows")
    gen_rows = json.loads(all_rows)
    assert len(gen_rows) >= 1, gen_html[:2000]
    handoff_path = get_hidden(gen_html, "handoff_path")
    handoff_root_path = get_hidden(gen_html, "handoff_root_path")
    assert handoff_path == path, f"expected handoff_path {path!r}, got {handoff_path!r}:\n" + gen_html[:3000]
    assert handoff_root_path == root_path

    # Phase 3: execute
    r3 = client.post("/ai/execute", data={
        "selected": selected_paths, "all_rows": all_rows, "embed_in_image": True,
        "handoff_path": handoff_path, "handoff_root_path": handoff_root_path,
    })
    results_html = r3.text

    if expect_video:
        assert "Weiter zu NFO Maker" in results_html, results_html[:3000]
        # The link carries a server-side handoff id (not the path itself); following it must
        # prefill the NFO Maker form with the folder that was just processed.
        m = re.search(r'href="(/nfo/\?handoff_id=[0-9a-f]+)"', results_html)
        assert m, results_html[:3000]
        page = client.get(m.group(1)).text
        assert html.escape(path) in page or path in page, page[:1500]
    else:
        assert "Weiter zu NFO Maker" not in results_html, results_html[:3000]

    return results_html


# --- Test 1: batch with a video -> handoff link present ---
run_chain(VIDEO_DIR, MEDIA_ROOT, expect_video=True)
print("AI-Beschreibung -> NFO Maker handoff: link correctly PRESENT for a video batch.")

# --- Test 2: batch with only an image -> handoff link absent ---
run_chain(IMAGE_DIR, MEDIA_ROOT, expect_video=False)
print("AI-Beschreibung -> NFO Maker handoff: link correctly ABSENT for an image-only batch.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL AI-DESCRIPTION -> NFO MAKER HANDOFF CHECKS PASSED")
