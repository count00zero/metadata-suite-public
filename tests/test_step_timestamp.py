import os, re, time, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="stt_"); os.environ["MEDIA_ROOT"] = MR
os.environ["BATCH_SIZE"] = "3"
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR, "d"); os.makedirs(f)
for i in range(8):
    p = os.path.join(f, f"2026.08.05-1{i}.00.00_{i}.jpg")
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=32x32","-frames:v","1",p],check=True)
def scan(**kw):
    x = c.post("/timestamp/browse/start", data={"path": f, "root_path": MR, "depth": "0", "default_tz": "Europe/Vienna", "include_already_named": "true", **kw})
    j = re.search(r'/progress/([a-f0-9-]+)', x.text).group(1)
    for _ in range(600):
        y = c.get(f"/timestamp/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
def rows(h): return re.findall(r'class="row-checkbox"[^>]*value="([^"]+)"', h) or re.findall(r'value="([^"]*\.jpg)"', h)
h1 = scan()
r1 = rows(h1); assert len(r1) == 3, r1
assert "Schritt 1" in h1 and "Nächste 3 laden" in h1 and "5 übrig" in h1
sid = re.search(r'&quot;scan_id&quot;: &quot;([a-f0-9]+)&quot;|"scan_id": "([a-f0-9]+)"', h1); sid = sid.group(1) or sid.group(2)
h2 = scan(scan_id=sid, offset="3"); r2 = rows(h2)
assert len(r2) == 3 and not set(r1) & set(r2), (r1, r2)
assert "Schritt 2" in h2
h3 = scan(scan_id=sid, offset="6"); r3 = rows(h3)
assert len(r3) == 2 and "alle 8 Dateien geladen" in h3, h3[:500]
assert len(set(r1 + r2 + r3)) == 8
# unknown scan_id -> fresh scan, step 1
h4 = scan(scan_id="deadbeef", offset="3"); assert "Schritt 1" in h4 and len(rows(h4)) == 3
# BATCH_SIZE=0 -> everything, no bar
os.environ["BATCH_SIZE"] = "0"
h5 = scan(); assert len(rows(h5)) == 8 and "step-bar" not in h5.replace(".step-bar", "")
# per-module override
os.environ["BATCH_SIZE"] = "3"; os.environ["BATCH_SIZE_TIMESTAMP"] = "5"
assert len(rows(scan())) == 5
print("PASS")
