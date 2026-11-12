import os, re, json, html as H, subprocess as sp, tempfile, time, sys
MR = tempfile.mkdtemp(prefix="xmpfirst_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
folder = os.path.join(MR, "v"); os.makedirs(folder)
def mk(name, sidecar):
    p = os.path.join(folder, name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-t","1",p],check=True)
    # raw-UTC QuickTime CreateDate, filename = local wall clock (Luna Ultra pattern)
    sp.run(["exiftool","-overwrite_original","-api","QuickTimeUTC=0","-quicktime:CreateDate=2026:10:03 07:14:28","-Comment=Insta360 Luna Ultra",p],check=True,capture_output=True)
    if sidecar is not None:
        open(p+".xmp","w").write(
 f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:exif="http://ns.adobe.com/exif/1.0/" xmp:CreateDate="{sidecar}" exif:DateTimeOriginal="{sidecar}"/></rdf:RDF></x:xmpmeta>') 
    return p
mk("2026.10.03-09.14.27.mp4", None)               # no sidecar
mk("2026.10.03-09.15.27.mp4", "2026-10-03T07:15:27")   # naive (wrong, UTC-ish)
mk("2026.10.03-09.16.27.mp4", "2026-10-03T09:16:27")   # naive but right wall clock
mk("2026.10.03-09.17.27.mp4", "2026-10-03T09:17:27+02:00")  # correct
def poll(url):
    for _ in range(400):
        r = c.get(url)
        if "progress-panel" not in r.text: return r.text
        time.sleep(0.05)
def scan(extra):
    d = {"path":folder,"root_path":MR,"depth":"0","default_tz":"Europe/Vienna","camera_model":"Luna Ultra"}; d.update(extra)
    r = c.post("/timestamp/browse/start", data=d)
    return poll("/timestamp/browse/progress/"+re.search(r'progress/([a-f0-9]+)', r.text).group(1))
h = scan({"only_needs_fix":"true"})
print("rows:", re.findall(r'class="row-checkbox" name="selected" value="[^"]*/([^"/]+)"', h))
names = re.findall(r'class="row-checkbox" name="selected" value="[^"]*/([^"/]+)"', h)
assert "2026.10.03-09.17.27.mp4" not in names, "correct sidecar must be hidden"
assert {"2026.10.03-09.14.27.mp4","2026.10.03-09.15.27.mp4","2026.10.03-09.16.27.mp4"} <= set(names), names
assert "keine .xmp" in h and "ohne UTC-Offset" in h
# fix via do_xmpdate only (NO exif write)
allres = H.unescape(re.search(r"name=\"all_resolutions\"\s+value='([^']*)'", h).group(1))
paths=[r["path"] for r in json.loads(allres) if r.get("row_type")!="companion"]
r = c.post("/timestamp/execute", data={"selected":paths,"all_resolutions":allres,"do_xmpdate":"true"})
assert r.status_code==200
h2 = scan({"only_needs_fix":"true"})
names2 = re.findall(r'class="row-checkbox" name="selected" value="[^"]*/([^"/]+)"', h2)
print("after:", names2)
# the video without sidecar gets one created now (videos only), so nothing remains
assert names2 == [], names2
assert os.path.exists(os.path.join(folder, "2026.10.03-09.14.27.mp4.xmp"))
created = sp.run(["exiftool","-s3","-XMP-xmp:CreateDate",os.path.join(folder, "2026.10.03-09.14.27.mp4.xmp")],capture_output=True,text=True).stdout.strip()
assert created == "2026:10:03 09:14:27+02:00", created
h3 = scan({})
assert "stimmt mit dem Ziel überein – Immich sortiert korrekt" in h3
print("PASS")
