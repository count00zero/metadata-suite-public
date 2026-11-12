import os, sys, tempfile, subprocess as sp, re, time, json, html as _h
MR = tempfile.mkdtemp(prefix="ut_"); os.environ["MEDIA_ROOT"] = MR
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR,"d"); os.makedirs(f)
def img(n, dt="2026:07:03 09:35:00"):
    p=os.path.join(f,n); sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    sp.run(["exiftool","-q","-overwrite_original",f"-DateTimeOriginal={dt}","-OffsetTimeOriginal=+02:00",p],check=True); return p
a=img("camA.jpg"); b=img("camB.jpg"); d=img("camC.jpg","2026:07:03 09:36:00")
existing=img("2026.07.03-09.36.00.jpg","2026:07:03 09:36:00")   # plain name already on disk
def scan():
    x=c.post("/timestamp/browse/start",data={"path":f,"root_path":MR,"depth":"0","default_tz":"Europe/Vienna","mode":"EXIF"})
    j=re.search(r'/progress/([a-f0-9-]+)',x.text).group(1)
    for _ in range(400):
        y=c.get(f"/timestamp/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
h=scan()
m=re.search(r"name=\"all_resolutions\" value='(.*?)'",h,re.S); rows=json.loads(_h.unescape(m.group(1)))
t={os.path.basename(r["path"]):r["target_name"] for r in rows if r.get("row_type")=="primary"}
print(t)
assert t["camA.jpg"]=="2026.07.03-09.35.00_01.jpg" and t["camB.jpg"]=="2026.07.03-09.35.00_02.jpg", t
assert t["camC.jpg"]=="2026.07.03-09.36.00_01.jpg", t
r=c.post("/timestamp/execute",data={"selected":[x["path"] for x in rows],"all_resolutions":json.dumps(rows),"do_rename":"true"})
got=sorted(os.listdir(f)); print(got)
assert "2026.07.03-09.35.00_01.jpg" in got and "2026.07.03-09.35.00_02.jpg" in got and "2026.07.03-09.36.00_01.jpg" in got and "2026.07.03-09.36.00.jpg" in got
print("PASS")
