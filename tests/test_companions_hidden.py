import os, sys, tempfile, subprocess as sp, re, time, json, html as _h
MR = tempfile.mkdtemp(prefix="ch_"); os.environ["MEDIA_ROOT"] = MR
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR,"d"); os.makedirs(f)
p=os.path.join(f,"IMG_x.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
sp.run(["exiftool","-q","-overwrite_original","-DateTimeOriginal=2026:07:04 19:50:56","-OffsetTimeOriginal=+02:00",p],check=True)
open(p+".xmp","w").write("<x/>")
x=c.post("/timestamp/browse/start",data={"path":f,"root_path":MR,"depth":"0","default_tz":"Europe/Vienna","mode":"EXIF","include_already_named":"true","do_rename":"true"})
j=re.search(r'/progress/([a-f0-9-]+)',x.text).group(1)
for _ in range(400):
    y=c.get(f"/timestamp/browse/progress/{j}")
    if "progress-panel" not in y.text: break
    time.sleep(0.05)
h=y.text
print(re.findall(r'class="orig"[^>]*>([^<]+)<',h), "Begleit" in h)
assert not re.findall(r'class="orig"[^>]*>[^<]*\.xmp',h)
assert "Begleitdateien werden mitgeändert" in h and ".jpg.xmp" in h
assert h.count('name="selected"')==1, h.count('name="selected"')
rows=json.loads(_h.unescape(re.search(r"name=\"all_resolutions\" value='(.*?)'",h,re.S).group(1)))
r=c.post("/timestamp/execute",data={"selected":[p],"all_resolutions":json.dumps(rows),"do_rename":"true"})
got=sorted(os.listdir(f)); print(got)
assert got==["2026.07.04-19.50.56.jpg","2026.07.04-19.50.56.jpg.xmp"], got
print("PASS")
