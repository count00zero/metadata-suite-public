import os, re, time, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="or_"); os.environ["MEDIA_ROOT"] = MR
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f=os.path.join(MR,"d"); os.makedirs(f)
def mk(n,dto,gps,off=None):
    p=os.path.join(f,n)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    d,t=gps.split(" ")
    a=["exiftool","-overwrite_original","-q",f"-DateTimeOriginal={dto}","-GPSLatitude=24.45","-GPSLatitudeRef=N","-GPSLongitude=54.64","-GPSLongitudeRef=E",f"-GPSDateStamp={d}",f"-GPSTimeStamp={t}"]
    if off: a+=[f"-OffsetTime={off}",f"-OffsetTimeOriginal={off}"]
    sp.run(a+[p],check=True)
mk("2026.08.05-16.11.24_18.jpg","2026:08:05 16:11:24","2026:08:05 14:11:24","+02:00")   # wrong name -> rename
mk("2026.08.05-18.07.44_15.jpg","2026:08:05 16:07:44","2026:08:05 14:07:44","+02:00")   # already correct
def scan(**kw):
    x=c.post("/timestamp/browse/start",data={"path":f,"root_path":MR,"depth":"0","default_tz":"Europe/Vienna",**kw})
    j=re.search(r'/progress/([a-f0-9-]+)',x.text).group(1)
    for _ in range(600):
        y=c.get(f"/timestamp/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
h=scan(include_already_named="true")
# both files listed, flags on the checkboxes, selection buttons with counts
assert "16.11.24_18" in h and "18.07.44_15" in h
import re as _r
boxes=_r.findall(r'<input type="checkbox" class="row-checkbox"[^>]*>',h)
assert len(boxes)==2, boxes
ren=[b for b in boxes if 'data-will-rename="true"' in b]; assert len(ren)==1 and "16.11.24_18" in ren[0], boxes
assert "data-needs-fix=" in boxes[0] and "data-needs-backwrite=" in boxes[0]
assert "Nur umbenennen (1)" in h and "selectBy('needsFix', true)" in h and "Nur .xmp-Datum falsch/fehlt" in h
idx = c.get("/timestamp/").text
assert 'name="only_needs_rename"' in idx and 'name="do_rename"' not in idx   # load filter above, action checkbox only in the list
print("PASS")
