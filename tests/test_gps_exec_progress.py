import os, re, json, subprocess as sp, tempfile, time, sys
MR = tempfile.mkdtemp(prefix="gpsex_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
rows=[]
for i in range(3):
    p=os.path.join(MR,f"x{i}.jpg")
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    rows.append({"path":p,"filename":os.path.basename(p),"is_video":False,"lat":48.2,"lon":16.3,"geo":{"country":"Austria","country_a":"AUT","city":"Wien"}})
r=c.post("/gps/execute/start",data={"selected":[x["path"] for x in rows],"all_rows":json.dumps(rows)})
assert "progress-panel" in r.text and "/gps/execute/progress/" in r.text
jid=re.search(r'execute/progress/([a-f0-9]+)',r.text).group(1)
for _ in range(400):
    y=c.get(f"/gps/execute/progress/{jid}")
    if "progress-panel" not in y.text: break
    time.sleep(0.05)
assert y.text.count("XMP-Sidecar geschrieben")==3, y.text[:600]
# cancel
r=c.post("/gps/execute/start",data={"selected":[x["path"] for x in rows],"all_rows":json.dumps(rows)})
jid=re.search(r'execute/progress/([a-f0-9]+)',r.text).group(1)
c.post(f"/gps/execute/cancel/{jid}")
for _ in range(400):
    y=c.get(f"/gps/execute/progress/{jid}")
    if "progress-panel" not in y.text: break
    time.sleep(0.05)
assert "Ergebnis" in y.text
print("PASS")
