"""Kontext nachfüllen is its own step between GPS Sidecar and AI-Beschreibung: GPS hands its files over, they load in ONE step
(no 'Nächste laden' although BATCH_SIZE would split them), ALL of them go on to the AI step; GPS Sidecar no longer writes context."""
import os, re, subprocess as sp, sys, tempfile, time

SCRATCH = tempfile.mkdtemp(prefix="ctxhand_")
MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
os.environ["BATCH_SIZE"] = "2"
os.environ.pop("BATCH_SIZE_CTXFILL", None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.main import app
from app.core import geoservice, geocore, handoff, stepper
from fastapi.testclient import TestClient
c = TestClient(app)

def fake_get(path, params, timeout_s=None):
    if path == "/context":
        return {"daypart_de": "Nachmittag", "season_de": "Herbst", "month_de": "Oktober", "year": 2026, "weekday_de": "Sonntag",
                "local_time": "2026-10-11T16:10:58+02:00", "tz": "Europe/Vienna", "elevation_m": 345.0, "extra_de": "x", "parts_de": {}}, "ok"
    if path == "/sights/status":
        return {"place_version": "v1"}, "ok"
    if path == "/elevation":
        return {"elevation": 345.0}, "ok"
    if path == "/timezoneJSON":
        return {"timezoneId": "Europe/Vienna"}, "ok"
    return None, "down"
geoservice._get = fake_get
geoservice.health = lambda: {"status": "ok"}

def img(name):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48", "-frames:v", "1", p, "-loglevel", "error"], check=True)
    sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=48.1591", "-XMP-exif:GPSLongitude=16.3328",
            "-XMP-exif:DateTimeOriginal=2026:10:11 16:10:58+02:00", p + ".xmp"], check=True)
    return p

def poll(resp):
    src = resp.text
    while True:
        m = re.search(r'hx-get="(/ctxfill/(?:scan|apply)/progress/[0-9a-f]+)"', src)
        if not m:
            return src
        time.sleep(0.05)
        src = c.get(m.group(1)).text

files = [img(f"p{i}.jpg") for i in range(5)]
hid = handoff.stash(MR, MR, files)

# the page prefills the folder, carries the id and loads at once
idx = c.get("/ctxfill/", params={"handoff_id": hid}).text
assert f'name="handoff_id" value="{hid}"' in idx and "requestSubmit" in idx, "handoff id / auto-load missing"
assert 'name="handoff_id" value=""' in c.get("/ctxfill/").text

# 5 files, BATCH_SIZE=2: via handoff ALL 5 in one step, no stepping bar
page = poll(c.post("/ctxfill/scan/start", data={"path": MR, "root_path": MR, "depth": 3, "handoff_id": hid}))
assert page.count('name="selected"') == 5, page.count('name="selected"')
assert "Nächste" not in page and "Alle verbleibenden" not in page, "handoff list must not ask for 'weiter laden'"
# without handoff the same folder IS stepped
page2 = poll(c.post("/ctxfill/scan/start", data={"path": MR, "root_path": MR, "depth": 3}))
assert page2.count('name="selected"') == 2 and "Nächste" in page2

# apply only 2 of the 5: all 5 still go on to AI-Beschreibung
carry = re.search(r'name="carry_id" value="([0-9a-f]+)"', page).group(1)
r = poll(c.post("/ctxfill/apply", data={"selected": files[:2], "groups": ["hoehe", "iptc"], "carry_id": carry,
                                        "handoff_path": MR, "handoff_root_path": MR}))
m = re.search(r'href="/ai/\?handoff_id=([0-9a-f]+)"', r)
assert m, r[:600]
assert sorted(handoff.fetch(m.group(1))["file_paths"]) == sorted(files)
assert "GPSAltitude>345" in open(files[0] + ".xmp", encoding="utf-8").read()

# nothing to do -> the way on is still offered (stamp complete files by hand: nothing missing)
without_handoff = poll(c.post("/ctxfill/apply", data={"selected": files[2:3], "groups": ["hoehe"]}))
assert re.search(r'href="/ai/\?handoff_id=([0-9a-f]+)"', without_handoff), "files filled here go on, too"

# stepper: unlimited
s = stepper.start("gps", files, unlimited=True)
assert len(s.paths) == 5 and not s.has_more and not s.active and s.scan_id == ""

# GPS Sidecar: no context any more, hands over to the Kontext step
g = open(os.path.join(ROOT, "app/modules/gps_sidecar/router.py"), encoding="utf-8").read()
assert "write_place_extras" not in g and "iptcfill" not in g
assert "unlimited=from_handoff" in g
res = open(os.path.join(ROOT, "app/modules/gps_sidecar/templates/_results.html"), encoding="utf-8").read()
assert "/ctxfill/?handoff_id=" in res and "/ai/?handoff_id=" not in res
for mod in ("ai_description", "nfo_maker"):
    assert "unlimited=" in open(os.path.join(ROOT, f"app/modules/{mod}/router.py"), encoding="utf-8").read(), mod
# menu: Kontext sits right before AI-Beschreibung
nav = open(os.path.join(ROOT, "app/modules/gps_sidecar/templates/index.html"), encoding="utf-8").read()
assert nav.index("Kontext nachfüllen") < nav.index("AI-Beschreibung") and nav.index("Kontext nachfüllen") > nav.index("GPS Sidecar</span>")
print("PASS")
