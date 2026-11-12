import os, sys, tempfile, subprocess as sp, re, time
MR = tempfile.mkdtemp(prefix="ns_"); os.environ["MEDIA_ROOT"] = MR
from app.core import timecore
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR,"d"); os.makedirs(f)
def img(n):
    p=os.path.join(f,n); sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True); return p
a=img("2026.07.03-09.35.00_02.jpg"); open(a+".xmp","w").write("<x/>")
b=img("2026.07.03-09.34.13_01.jpg"); open(b+".xmp","w").write("<x/>")
r=timecore.resolve_one(a,"EXIF","Europe/Vienna"); assert r.target_name=="2026.07.03-09.35.00_02.jpg", r.target_name
# wrong timestamp in name vs mtime-less: force different local_str
r.local_str="2026.07.03-10.00.00"; r.__post_init__(); assert r.target_name=="2026.07.03-10.00.00_02.jpg"
def scan(**kw):
    x=c.post("/timestamp/browse/start",data={"path":f,"root_path":MR,"depth":"0","default_tz":"Europe/Vienna",**kw})
    j=re.search(r'/progress/([a-f0-9-]+)',x.text).group(1)
    for _ in range(400):
        y=c.get(f"/timestamp/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
h=scan(include_already_named="true")
assert "Begleitdatei" not in h
print("PASS")
