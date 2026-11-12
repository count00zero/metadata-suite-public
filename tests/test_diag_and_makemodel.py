import os, re, subprocess, tempfile, time, shutil, sys
MEDIA_ROOT = tempfile.mkdtemp(prefix="diagmm_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
from app.core import geocore
client = TestClient(app)
folder = os.path.join(MEDIA_ROOT, "clips"); os.makedirs(folder)

def mkvideo(name, utc_wallclock, extra=()):
    p = os.path.join(folder, name)
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=blue:s=64x64","-t","1",p],check=True,capture_output=True)
    subprocess.run(["exiftool","-overwrite_original","-api","LargeFileSupport=1",
                    f"-quicktime:time:all={utc_wallclock}+00:00","-Comment=firmware Insta360 Luna Ultra", *extra, p],
                   check=True,capture_output=True)
    return p

# A: raw-UTC video (never backwritten) + stale sidecar -> must be flagged
a = mkvideo("2026.10.03-16.28.57.mp4", "2026:10:03 14:28:58")
geocore.ensure_xmp_sidecar(a, a + ".xmp", True)
# B: already-correct video (local wall clock stored) with no sidecar
b = mkvideo("2026.10.03-17.00.00.mp4", "2026:10:03 17:00:00")
subprocess.run(["exiftool","-overwrite_original","-quicktime:time:all=2026:10:03 17:00:00+02:00",b],check=True,capture_output=True)
geocore.ensure_xmp_sidecar(b, b + ".xmp", True)
geocore.write_dates_to_xmp(b + ".xmp", "2026:10:03 17:00:00+02:00")

# (sidecar creation must have added Make/Model even though the video has none)
side = subprocess.run(["exiftool","-s3","-XMP-tiff:Make","-XMP-tiff:Model",a+".xmp"],capture_output=True,text=True).stdout
print("sidecar Make/Model:", side.split())
assert "Insta360" in side and "Luna" in side

def scan(**kw):
    r = client.post("/timestamp/browse/start", data={"path":folder,"root_path":MEDIA_ROOT,"depth":"0","default_tz":"Europe/Vienna",**kw})
    job = re.search(r'/timestamp/browse/progress/([a-f0-9-]+)', r.text).group(1)
    for _ in range(300):
        x = client.get(f"/timestamp/browse/progress/{job}")
        if "progress-panel" not in x.text: return x.text
        time.sleep(0.05)
    raise RuntimeError("scan hung")

html = scan(camera_model="Luna Ultra")
assert html.count('class="row-checkbox"') == 2, html[:1500]
assert "Rohwert aus der Video-Datei" in html or "weicht vom Ziel ab" in html or "ohne UTC-Offset" in html
assert html.count('data-needs-fix="true"') >= 1, "only the raw-UTC video should be flagged"
assert "mit dem Ziel überein" in html
assert "Nur .xmp-Datum falsch/fehlt" in html
assert 'data-needs-fix="true"' in html and 'data-needs-fix="false"' in html
print("PASS 1: indicator flags only the raw-UTC video; correct one shows ✅")

# diagnostics are shown without a camera filter too (selection buttons need the flags)
assert "diag-row" in scan(include_already_named="true")  # diagnostics now always shown
print("PASS 2: diagnostics also shown without a camera filter")

# EXIF write-back on a video: the video file itself must stay untouched
m = re.search(r"name=\"all_resolutions\" value='(.*?)'", html, re.DOTALL)
rows = m.group(1).replace("&#34;", '"')
r = client.post("/timestamp/execute", data={"selected":[a],"all_resolutions":rows,"do_exifwrite":"true"})
assert r.status_code == 200
own = subprocess.run(["exiftool","-s3","-Make","-Model",a],capture_output=True,text=True).stdout
print("video Make/Model after write-back:", own.split("\n"))
assert "Insta360 Luna Ultra" not in own, "videos are never rewritten"
print("PASS 3: video file not rewritten (no Make/Model added)")

# a file that already has its own Make/Model must NOT be overwritten
c = mkvideo("2026.10.03-18.00.00.mp4", "2026:10:03 16:00:00", extra=["-Make=DJI","-Model=Osmo Action 6"])
res = __import__("app.core.timecore", fromlist=["x"]).resolve_one(c, "EXIF", "Europe/Vienna")
from app.core.executor import apply_resolution
apply_resolution(res, False, True)
kept = subprocess.run(["exiftool","-s3","-Make","-Model",c],capture_output=True,text=True).stdout
assert "DJI" in kept and "Osmo Action 6" in kept and "Insta360" not in kept, kept
print("PASS 4: existing Make/Model left untouched")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
