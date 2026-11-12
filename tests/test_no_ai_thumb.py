import os, sys, shutil, subprocess

import tempfile
SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "no_ai_thumb_root")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
os.makedirs(MEDIA_ROOT)

VIDEO = os.path.join(MEDIA_ROOT, "2026.09.01-09.00.00.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=3",
    "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
    "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", VIDEO, "-loglevel", "error"], check=True)

os.environ["MEDIA_ROOT"] = MEDIA_ROOT

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.core import aicore

THUMB = aicore.get_video_thumb_path(VIDEO)

# --- Test 1: generate_description() for a video must NOT create a persisted thumb ---
assert not os.path.exists(THUMB)

class FakeCfg:
    providers = ["ollama"]
    ollama_url = "http://fake"
    ollama_model = "fake-model"
    google_api_key = google_model = ""

# Stub the actual provider call so we don't need a real AI backend -- we only care
# about the thumb side effect, not the generated text.
aicore._PROVIDER_CALLERS["ollama"] = lambda image_b64, sp, p, cfg: ("Ein Testvideo.", None)

result = aicore.generate_description(VIDEO, "", FakeCfg(), is_video=True)
assert result.description == "Ein Testvideo.", result.failures
assert not os.path.exists(THUMB), "generate_description() must NOT persist a -thumb.jpg anymore"
print("PASS: generating an AI description for a video does not create a persisted -thumb.jpg.")

# --- Test 2: if a thumb ALREADY exists (e.g. from NFO Maker), it's reused untouched,
#     not deleted, not regenerated (no ffmpeg call -- verified by checking mtime/bytes
#     are unchanged after a second generate_description() call). ---
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=80x60",
    "-frames:v", "1", THUMB, "-loglevel", "error"], check=True)
existing_bytes = open(THUMB, "rb").read()
existing_mtime = os.path.getmtime(THUMB)

result2 = aicore.generate_description(VIDEO, "", FakeCfg(), is_video=True)
assert result2.description == "Ein Testvideo.", result2.failures
assert os.path.exists(THUMB), "an already-existing thumb must be left in place"
assert open(THUMB, "rb").read() == existing_bytes, "existing thumb must be reused untouched, not regenerated"
assert os.path.getmtime(THUMB) == existing_mtime, "existing thumb's mtime must be untouched (no ffmpeg re-write)"
print("PASS: an already-existing -thumb.jpg (e.g. from NFO Maker) is reused as-is, never touched.")

shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
print("\nALL AI-DESCRIPTION THUMB-REMOVAL CHECKS PASSED")
