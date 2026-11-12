"""Huge videos: sidecar creation must not use -ee / must get a size-scaled timeout and a light fallback;
.srt is deleted after a verified sidecar write whatever the GPS source was."""
import os, shutil, subprocess as sp, sys, tempfile
SCRATCH = tempfile.mkdtemp(prefix="bigv_"); MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import geocore, exifpool
from app.modules.gps_sidecar import router as gps

mp4 = os.path.join(MR, "2026.10.04-12.24.39.MP4")
sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-metadata",
        "creation_time=2026-10-04T10:24:39Z", "-pix_fmt", "yuv420p", mp4], check=True)

# ---- 1. option selection by size
assert geocore._sidecar_attempts(False, 5000) == [(False, False)]
assert geocore._sidecar_attempts(True, 20) == [(True, False), (False, True)]
assert geocore._sidecar_attempts(True, 80) == [(True, True), (False, True)]
assert geocore._sidecar_attempts(True, 800) == [(False, True)]
os.environ["XMP_EE_MAX_MB"] = "1000"
assert geocore._sidecar_attempts(True, 800)[0] == (True, True)
os.environ.pop("XMP_EE_MAX_MB")

# ---- 2a. a "400 MB" file: no -ee, -fast2, long scaled timeout
real_run, real_size = exifpool.run, os.path.getsize
seen = []
size = {"mb": 400e6}
def fake_size(p): return size["mb"] if p == mp4 else real_size(p)
def fake_run(cmd, **kw):
    if "-o" in cmd and mp4 in cmd:
        seen.append((list(cmd), kw.get("timeout")))
        if size["mb"] == 80e6 and len(seen) == 1:
            raise TimeoutError("Timeout")                    # the -ee attempt hangs on the NAS
    return real_run(cmd, **kw)
os.path.getsize = fake_size; geocore.exifpool.run = fake_run
ok, msg = geocore.ensure_xmp_sidecar_ex(mp4, mp4 + ".xmp", True)
assert ok and os.path.exists(mp4 + ".xmp"), msg
first_cmd, first_timeout = seen[0]
assert "-ee" not in first_cmd and "-fast2" in first_cmd, first_cmd
assert first_timeout >= 60 + 400 * 0.25 - 1, first_timeout                       # scaled, not the flat 60 s
os.remove(mp4 + ".xmp")
# ---- 2b. an "80 MB" file: -ee attempt times out -> light retry succeeds
seen.clear(); size["mb"] = 80e6
ok, msg = geocore.ensure_xmp_sidecar_ex(mp4, mp4 + ".xmp", True)
os.path.getsize = real_size; geocore.exifpool.run = real_run
assert ok and len(seen) == 2 and "-ee" in seen[0][0] and "-ee" not in seen[1][0] and "-fast2" in seen[1][0], seen
os.remove(mp4 + ".xmp")

# ---- 3. everything fails -> a reason, not a bare "Konnte ... nicht"
def always_fail(cmd, **kw):
    if "-o" in cmd:
        raise TimeoutError("Timeout nach 160s")
    return real_run(cmd, **kw)
geocore.exifpool.run = always_fail
ok, msg = geocore.ensure_xmp_sidecar_ex(mp4, mp4 + ".xmp", True)
geocore.exifpool.run = real_run
assert not ok and "Timeout" in msg and "MB" in msg, msg
r = None
row = {"path": mp4, "is_video": True, "lat": 47.9, "lon": 16.0, "geo": None, "capture_display": "2026:10:04 12:24:39+02:00"}
geocore.exifpool.run = always_fail
r = gps._execute_one(row, False, False)
geocore.exifpool.run = real_run
assert not r["xmp_written"] and "Konnte XMP-Sidecar nicht erstellen/aktualisieren: " in r["xmp_error"] and "Timeout" in r["xmp_error"], r

# ---- 4. .srt removed after the verified write, even if the position did NOT come from the .srt
srt = os.path.join(MR, "2026.10.04-12.24.39.SRT"); open(srt, "w").write("1\n00:00:00,000 --> 00:00:01,000\n[latitude: 47.1] [longitude: 16.1]\n")
row2 = {**row, "srt_path": srt, "gps_source": "Traccar"}
r2 = gps._execute_one(row2, False, True)
assert r2["xmp_written"] and r2.get("srt_deleted") is True and not os.path.exists(srt), r2
open(srt, "w").write("x")
r3 = gps._execute_one(row2, False, False)                                        # option off -> kept
assert os.path.exists(srt) and not r3.get("srt_deleted")
row4 = {**row2, "lat": None, "lon": None}
r4 = gps._execute_one(row4, False, True)                                         # no position at all -> never deleted blindly
assert os.path.exists(srt) and not r4.get("srt_deleted")
shutil.rmtree(SCRATCH, ignore_errors=True)
print("PASS")
