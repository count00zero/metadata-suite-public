import os, re, subprocess, tempfile, time, shutil, sys, json
MEDIA_ROOT = tempfile.mkdtemp(prefix="devnames_test_")
os.environ["MEDIA_ROOT"] = MEDIA_ROOT
CFG = os.path.join(MEDIA_ROOT, "camera_names.json")
os.environ["CAMERA_NAMES_FILE"] = CFG
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
folder = os.path.join(MEDIA_ROOT, "photos"); os.makedirs(folder)

def photo(name, make, model, color):
    p = os.path.join(folder, name)
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"color=c={color}:s=64x64","-frames:v","1",p],check=True,capture_output=True)
    subprocess.run(["exiftool","-overwrite_original","-AllDates=2026:10:03 12:00:00",f"-Make={make}",f"-Model={model}",p],check=True,capture_output=True)
photo("2026.10.03-12.00.00.jpg","Samsung","SM-S921B","red")        # NFO rule (a personal label ...)
photo("2026.10.03-12.00.01.jpg","OnePlus","CPH2581","green")       # Google list only
photo("2026.10.03-12.00.02.jpg","Acme","Zork 9000","blue")         # nothing known -> raw

def scan(**kw):
    r = client.post("/timestamp/browse/start", data={"path":folder,"root_path":MEDIA_ROOT,"depth":"0","default_tz":"Europe/Vienna",**kw})
    job = re.search(r'/timestamp/browse/progress/([a-f0-9-]+)', r.text).group(1)
    for _ in range(300):
        x = client.get(f"/timestamp/browse/progress/{job}")
        if "progress-panel" not in x.text: return x.text
        time.sleep(0.05)
    raise RuntimeError

h = scan(include_already_named="true")
assert "Samsung Galaxy S24" in h and "NFO-Maker-Regel" in h
assert "OnePlus 12" in h and "Google-Geräteliste" in h
assert "Acme Zork 9000" in h and "Zork 9000 · " not in h   # raw shown plain, no source note
print("PASS 1: labels + source shown (NFO rule / Google list / raw)")

n = lambda html: html.count('class="row-checkbox"')
assert n(scan(camera_model="S24")) == 1            # translated name only ("Galaxy S24")
assert n(scan(camera_model="Samsung Galaxy S24")) == 1  # full label
assert n(scan(camera_model="SM-S921B")) == 1       # raw code
assert n(scan(camera_model="OnePlus 12")) == 1     # Google name
assert n(scan(camera_model="Zork")) == 1           # raw-only device
assert n(scan(camera_model="nonexistent")) == 0
print("PASS 2: filter matches raw code, Google name and personal label")

json.dump({"rules":[{"match":["CPH2581"],"name":"Opas OnePlus"}]}, open(CFG,"w"))
h = scan(camera_model="Opas")
assert n(h) == 1 and "eigene Regel" in h and "OnePlus 12" not in h
print("PASS 3: own overrides file wins over Google list, applied without restart")
shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
