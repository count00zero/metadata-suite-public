import os, re, subprocess as sp, tempfile, time, sys
MR = tempfile.mkdtemp(prefix="gpsskip_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
f = os.path.join(MR, "v"); os.makedirs(f)
p = os.path.join(f, "2026.10.03-09.14.27.mp4")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-t","1",p],check=True)
open(p+".xmp","w").write('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" photoshop:Country="Austria" photoshop:City="Wien"/></rdf:RDF></x:xmpmeta>')
def run(data):
    r = c.post("/gps/browse/start", data={"path":f,"root_path":MR,"depth":"0",**data})
    jid = re.search(r'progress/([a-f0-9]+)', r.text).group(1)
    for _ in range(400):
        x = c.get(f"/gps/browse/progress/{jid}")
        if "progress-panel" not in x.text: return x.text
        time.sleep(0.05)
h_checked = run({"skip_already_done":"true"})
assert 'class="row-checkbox"' not in h_checked and "bereits" in h_checked
h_unchecked = run({})   # browser sends nothing for an unchecked checkbox
assert h_unchecked.count('class="row-checkbox"') == 1, h_unchecked[:800]
print("PASS")
