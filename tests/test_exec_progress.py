import os, re, json, subprocess, tempfile, time, sys
MEDIA_ROOT = tempfile.mkdtemp(prefix="execprog_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
folder = os.path.join(MEDIA_ROOT, "clips"); os.makedirs(folder)
for n in ("a_cam.mp4","b_cam.mp4","c_cam.mp4"):
    p = os.path.join(folder, n)
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=red:s=64x64","-t","1",p],check=True,capture_output=True)
    subprocess.run(["exiftool","-overwrite_original","-AllDates=2026:10:03 16:28:57",p],check=True,capture_output=True)

def poll(url):
    for _ in range(400):
        r = client.get(url)
        if "progress-panel" not in r.text: return r.text
        time.sleep(0.05)
    raise RuntimeError("never finished")

def scan():
    r = client.post("/timestamp/browse/start", data={"path":folder,"root_path":MEDIA_ROOT,"depth":"0","default_tz":"Europe/Vienna"})
    jid = re.search(r'/timestamp/browse/progress/([a-f0-9]+)', r.text).group(1)
    html = poll(f"/timestamp/browse/progress/{jid}")
    allres = re.search(r'name="all_resolutions"[^>]*value=\'?"?([^\'"]*)', html)
    return html
html = scan()
import html as H
m = re.search(r"name=\"all_resolutions\"\s+value='([^']*)'", html)
assert m, "no all_resolutions"
allres = H.unescape(m.group(1))
rows = json.loads(allres)
paths = [r["path"] for r in rows if r.get("row_type")!="companion"]
assert len(paths)==3, paths
data = {"selected": paths, "all_resolutions": allres, "do_rename":"on","do_exifwrite":"on","handoff_path":folder,"handoff_root_path":MEDIA_ROOT}
r = client.post("/timestamp/execute/start", data=data)
assert "progress-panel" in r.text and "/timestamp/execute/progress/" in r.text and "/timestamp/execute/cancel/" in r.text, r.text[:800]
jid = re.search(r'/timestamp/execute/progress/([a-f0-9]+)', r.text).group(1)
out = poll(f"/timestamp/execute/progress/{jid}")
assert "Ergebnis" in out and "Abgebrochen" not in out, out[:800]
assert not any(os.path.exists(p) for p in paths), "originals should have been renamed"
print("files after:", sorted(os.listdir(folder)))

# cancel: second run on fresh files
folder2 = os.path.join(MEDIA_ROOT,"c2"); os.makedirs(folder2)
for i in "abcdef":
    p=os.path.join(folder2,f"x{i}_cam.mp4")
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=red:s=64x64","-t","1",p],check=True,capture_output=True)
r = client.post("/timestamp/browse/start", data={"path":folder2,"root_path":MEDIA_ROOT,"depth":"0","default_tz":"Europe/Vienna"})
jid = re.search(r'/timestamp/browse/progress/([a-f0-9]+)', r.text).group(1)
html2 = poll(f"/timestamp/browse/progress/{jid}")
m = re.search(r"name=\"all_resolutions\"\s+value='([^']*)'", html2)
allres2 = H.unescape(m.group(1)); paths2=[x["path"] for x in json.loads(allres2) if x.get("row_type")!="companion"]
from app.core import jobs
orig = jobs.create_job
def cj():
    j = orig(); j.cancel_event.set(); return j   # cancelled before first file
    return j
jobs.create_job = cj
import app.modules.timestamp_tool.router as R
R.jobs.create_job = cj
r = client.post("/timestamp/execute/start", data={"selected":paths2,"all_resolutions":allres2,"do_rename":"on","do_exifwrite":"on"})
jid = re.search(r'/timestamp/execute/progress/([a-f0-9]+)', r.text).group(1)
time.sleep(0.5)
out = client.get(f"/timestamp/execute/progress/{jid}").text
assert "progress-panel" not in out
print("cancel result snippet:", re.sub(r"\s+"," ",out)[:200])

# path fence
bad = json.dumps([{"path":"/etc/passwd","row_type":"primary"}])
r = client.post("/timestamp/execute/start", data={"selected":["/etc/passwd"],"all_resolutions":bad})
assert r.status_code in (400,403), r.status_code
print("PASS")
