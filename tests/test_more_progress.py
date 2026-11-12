import os, re, json, subprocess as sp, tempfile, time, sys
MR = tempfile.mkdtemp(prefix="mp_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
paths=[]
for i in range(3):
    p=os.path.join(MR,f"2026.10.0{i+1}-10.00.00.jpg")
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    paths.append(p)
def wait(prefix, r):
    assert "progress-panel" in r.text, r.text[:300]
    jid=re.search(r'/progress/([a-f0-9]+)',r.text).group(1)
    for _ in range(600):
        y=c.get(f"{prefix}/progress/{jid}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
for pre in ("/timestamp","/gps","/ai"):
    r=c.post(f"{pre}/restore-mtime/start",data={"paths":paths})
    t=wait(f"{pre}/restore-mtime",r)
    assert t.count("result-row")==3, (pre,t[:300])
    assert c.post(f"{pre}/restore-mtime",data={"paths":paths}).status_code==200
rows=[{"path":p,"filename":os.path.basename(p),"is_video":False,"new_description":"Ein blaues Bild"} for p in paths]
r=c.post("/ai/execute/start",data={"selected":paths,"all_rows":json.dumps(rows)})
t=wait("/ai/execute",r)
assert t.count("XMP-Sidecar geschrieben")==3, t[:500]
assert c.post("/ai/execute",data={"selected":paths,"all_rows":json.dumps(rows)}).status_code==200
print("PASS")
