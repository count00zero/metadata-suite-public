import os, re, subprocess, tempfile, time, shutil, sys, stat
MEDIA_ROOT = tempfile.mkdtemp(prefix="retry_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
real = shutil.which("exiftool")
from fastapi.testclient import TestClient
from app.main import app
from app.core import timecore
from app.core.executor import apply_resolution
client = TestClient(app)
folder = os.path.join(MEDIA_ROOT, "clips"); os.makedirs(folder)

def video(name, qt_wallclock, local_qt=False):
    p = os.path.join(folder, name)
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=blue:s=64x64","-t","1",p],check=True,capture_output=True)
    subprocess.run([real,"-overwrite_original",f"-quicktime:time:all={qt_wallclock}+00:00","-Comment=Insta360 Luna Ultra",p],check=True,capture_output=True)
    return p
v = video("2026.10.03-16.28.57.mp4", "2026:10:03 14:28:58")
res = timecore.resolve_one(v, "EXIF", "Europe/Vienna")
assert res.camera_model == "Insta360 Luna Ultra"

# 1) videos are never rewritten
out = apply_resolution(res, False, True)
assert not out.exif_written and not out.exif_error and "kein EXIF-Rückschreiben" in out.exif_note, out
qt = subprocess.run([real,"-api","QuickTimeUTC=0","-s3","-QuickTime:CreateDate",v],capture_output=True,text=True).stdout.strip()
assert qt.startswith("2026:10:03 14:28:58"), qt
print("PASS 1: video untouched, note shown")

# 4) backend filter only_needs_backwrite: videos never need a backwrite, so nothing is listed
good = video("2026.10.03-18.00.00.mp4", "2026:10:03 18:00:00")
subprocess.run([real,"-overwrite_original","-quicktime:time:all=2026:10:03 18:00:00+02:00",good],check=True,capture_output=True)
v3 = video("2026.10.03-19.00.00.mp4", "2026:10:03 17:00:00")   # raw UTC again
def scan(**kw):
    r = client.post("/timestamp/browse/start", data={"path":folder,"root_path":MEDIA_ROOT,"depth":"0","default_tz":"Europe/Vienna",**kw})
    job = re.search(r'/timestamp/browse/progress/([a-f0-9-]+)', r.text).group(1)
    for _ in range(400):
        x = client.get(f"/timestamp/browse/progress/{job}")
        if "progress-panel" not in x.text: return x.text
        time.sleep(0.05)
    raise RuntimeError
h = scan(only_needs_backwrite="true")
names = re.findall(r'class="orig"[^>]*>([^<]+)<', h)
print("listed:", names)
assert names == [], names   # videos never need a backwrite now
print("PASS 4: only_needs_backwrite lists no videos")

# 5) execute from the UI with a sidecar present: sidecar dates pinned
open(v3+".xmp","w").write("<x:xmpmeta/>")
m = re.search(r"name=\"all_resolutions\" value='(.*?)'", scan(only_needs_fix="true"), re.DOTALL)
rows = m.group(1).replace("&#34;", '"')
r = client.post("/timestamp/execute", data={"selected":[v3],"all_resolutions":rows,"do_exifwrite":"true"})
assert ".xmp-Sidecar-Datum gesetzt" in r.text, r.text[:800]
sd = subprocess.run([real,"-s3","-XMP-xmp:CreateDate",v3+".xmp"],capture_output=True,text=True).stdout.strip()
assert sd.startswith("2026:10:03 19:00:00") and "+02:00" in sd, sd
print("PASS 5: sidecar CreateDate pinned to local time + offset:", sd)
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
