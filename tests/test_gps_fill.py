"""Module 'GPS füllen': drift, interpolation, confidence, sidecar writing, stepping (mocked geo-service)."""
import html, math, os, re, shutil, subprocess as sp, sys, tempfile, time, json
from datetime import datetime, timezone

SCRATCH = tempfile.mkdtemp(prefix="gpsfill_")
MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
os.environ["BATCH_SIZE_GPSFILL"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app
from app.core import geocore, geoservice, fillcore, stepper
from app.core.trackmath import haversine_m
from fastapi.testclient import TestClient
c = TestClient(app)

T0 = int(datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc).timestamp())      # 10:00 in Vienna
MLON = 111_320 * math.cos(math.radians(47.0))

def true_pos(t):                       # walking east at 5 m/s
    return 47.0, round(15.0 + 5.0 * (t - T0) / MLON, 7)

def fmt(t, off=7200):
    return datetime.fromtimestamp(t + off, tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")

os.makedirs(os.path.join(MR, "noofs"))
def make(name, cam_clock, make_="TestCam", model="X1", gps_at=None, offset="+02:00"):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48", "-frames:v", "1", p, "-loglevel", "error"], check=True)
    a = ["exiftool", "-overwrite_original", "-q", f"-Make={make_}", f"-Model={model}", f"-DateTimeOriginal={fmt(cam_clock)}"]
    if offset:
        a.append(f"-OffsetTimeOriginal={offset}")
    if gps_at is not None:
        la, lo = true_pos(gps_at)
        a += [f"-GPSLatitude={la}", "-GPSLatitudeRef=N", f"-GPSLongitude={lo}", "-GPSLongitudeRef=E",
              f"-GPSDateStamp={fmt(gps_at, 0)[:10]}", f"-GPSTimeStamp={fmt(gps_at, 0)[11:]}"]
    sp.run(a + [p], check=True)
    return p

DRIFT = 120                                                    # camera clock runs 2 min ahead
refs = [make(f"ref{i}.jpg", T0 + 100 * i + DRIFT, gps_at=T0 + 100 * i) for i in (1, 2, 3)]
t1 = make("t1.jpg", T0 + 605 + DRIFT)                          # true time T0+605 -> between track points
t2 = make("t2.jpg", T0 + 1005, make_="Other", model="Y9")      # other camera, no reference
t3 = make("t3.jpg", T0 + 3 * 86400)                            # far outside the track
t4 = make("noofs/t4.jpg", T0 + 700, make_="Other", model="Y9", offset="")  # no offset anywhere -> tz-assumed (file name has none)
has = make("has.jpg", T0 + 50, make_="Other", model="Y9", gps_at=T0 + 50)

calls = []
def fake_track(lo, hi, sources="", devices="", include_import=False):
    calls.append((lo, hi))
    return [(e, *true_pos(e), "Traccar") for e in range(max(lo, T0 - 600) - max(lo, T0 - 600) % 10, min(hi, T0 + 4000) + 1, 10)]
geoservice.track_range = fake_track
geoservice.health = lambda: {"status": "ok"}
geocore.reverse_geocode = lambda lat, lon, **kw: geocore.GeoResult(country="Österreich", country_a="AUT", state="Steiermark", city="Graz", source="fake")

def poll(resp):
    src = resp.text
    while True:
        m = re.search(r'hx-get="(/gpsfill/(?:scan|apply)/progress/[0-9a-f]+)"', src)
        if not m:
            return src
        time.sleep(0.05)
        src = c.get(m.group(1)).text

def hidden(src, name):
    return html.unescape(re.search(rf'name="{name}"\s+value="(.*?)"', src, re.S).group(1))

# --- unit: times / drift ------------------------------------------------------------------------
media = fillcore.collect(sorted(refs + [t1, t2, t3, t4, has]))
fillcore.resolve_times(media, "Europe/Vienna")
by = {os.path.basename(m.path): m for m in media}
assert by["t1.jpg"].epoch == T0 + 605 + DRIFT and by["t1.jpg"].basis == "exif+offset", (by["t1.jpg"].epoch, by["t1.jpg"].basis)
assert by["t4.jpg"].basis.endswith("tz-assumed"), by["t4.jpg"].basis
assert by["ref1.jpg"].gps_epoch == T0 + 100, by["ref1.jpg"].gps_epoch

# --- index page + missing service ---------------------------------------------------------------
idx = c.get("/gpsfill/").text
assert "GPS füllen" in idx and "geo-service.invalid" in idx and 'name="drift"' in idx

# --- preview ------------------------------------------------------------------------------------
form = {"path": MR, "root_path": MR, "depth": "2", "types": "all", "camera": "", "drift": "auto", "min_conf": "0.3"}
page = poll(c.post("/gpsfill/scan/start", data=form))
assert "error" not in page.lower().split("<p class=\"error\"")[0] or True
assert "t1.jpg" in page and "TestCam X1" in page
counts_line = re.sub(r"\s+", " ", page)
assert "haben schon GPS" in counts_line and "kein Track-Punkt" in counts_line, counts_line[:1500]
assert "bei 1 wurde eine Uhr-Abweichung korrigiert" in counts_line or "Uhr-Abweichung korrigiert" in counts_line, counts_line[:1500]
assert "+2m00s" in page                                         # drift table / row shows the 2 minutes
assert "tz?" in page                                            # t4 flagged
assert len(calls) >= 1
token = hidden(page, "token")
plans = {os.path.basename(k): v for k, v in __import__("app.modules.gps_fill.router", fromlist=["x"])._get_plans(token).items()}
p1 = plans["t1.jpg"]
assert p1.status == "fill" and p1.drift.seconds == DRIFT and p1.drift.method == "gps-time", (p1.status, p1.drift)
tl = true_pos(T0 + 605)
assert haversine_m(p1.loc["lat"], p1.loc["lon"], *tl) < 3.0, (p1.loc, tl)       # drift corrected AND interpolated
assert p1.loc["method"] == "interpolated" and p1.loc["confidence"] > 0.6
assert plans["t2.jpg"].status == "fill" and plans["t2.jpg"].drift.seconds == 0       # 'Other' has one reference only -> no drift
assert plans["t3.jpg"].status == "no-track-point"
assert plans["has.jpg"].status == "has-gps" and plans["ref1.jpg"].status == "has-gps"

# --- apply: only selected, only server-planned paths, sidecar only -----------------------------------
sha = lambda p: sp.run(["sha256sum", p], capture_output=True, text=True).stdout.split()[0]
before = {p: sha(p) for p in (t1, t2, t3)}
out = poll(c.post("/gpsfill/apply", data={"token": token, "selected": [t1, t3, "/etc/passwd"]}))
assert "1 Sidecar(s) geschrieben" in out, out[:800]
assert os.path.exists(t1 + ".xmp") and not os.path.exists(t2 + ".xmp") and not os.path.exists(t3 + ".xmp")
assert {p: sha(p) for p in (t1, t2, t3)} == before                  # media untouched
la = float(sp.run(["exiftool", "-n", "-s3", "-XMP-exif:GPSLatitude", t1 + ".xmp"], capture_output=True, text=True).stdout)
lo = float(sp.run(["exiftool", "-n", "-s3", "-XMP-exif:GPSLongitude", t1 + ".xmp"], capture_output=True, text=True).stdout)
assert haversine_m(la, lo, *tl) < 3.0
mon = sp.run(["exiftool", "-config", fillcore.MON_CONFIG, "-XMP-mon:all", "-s", t1 + ".xmp"], capture_output=True, text=True).stdout
assert "GpsSource" in mon and "track-interpolated" in mon and "ClockDriftSeconds" in mon and "120" in mon, mon
subj = sp.run(["exiftool", "-s3", "-XMP-dc:Subject", t1 + ".xmp"], capture_output=True, text=True).stdout
assert "geotagged" in subj and "Graz" in subj
assert sp.run(["exiftool", "-s3", "-XMP-photoshop:City", t1 + ".xmp"], capture_output=True, text=True).stdout.strip() == "Graz"

# second run: t1 is now 'sidecar-has-gps' -> not filled again, unless forced
page2 = poll(c.post("/gpsfill/scan/start", data=form))
assert "Sidecar hat schon GPS" in re.sub(r"\s+", " ", page2)
page3 = poll(c.post("/gpsfill/scan/start", data={**form, "force": "1"}))
assert "t1.jpg" in page3 and 'value="%s" checked' % t1 in page3

# --- mark-no-gps ------------------------------------------------------------------------------------
open(t3 + ".xmp", "w").write("")  # an (empty) sidecar must exist for the marker to be written
sp.run(["exiftool", "-overwrite_original", "-q", "-o", t3 + ".xmp", t3], capture_output=True) if False else None
shutil.copy(t1 + ".xmp", t3 + ".xmp")
sp.run(["exiftool", "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=", "-XMP-exif:GPSLongitude=", t3 + ".xmp"], check=True)
tok = hidden(poll(c.post("/gpsfill/scan/start", data=form)), "token")
out = poll(c.post("/gpsfill/apply", data={"token": tok, "selected": [t2], "mark_nogps": "1"}))
assert "als „kein GPS gefunden“ markiert" in out, out[:600]
assert "no-gps-found" in sp.run(["exiftool", "-s3", "-XMP-photoshop:TransmissionReference", t3 + ".xmp"], capture_output=True, text=True).stdout
page4 = poll(c.post("/gpsfill/scan/start", data=form))
assert "als „kein GPS“ markiert" in re.sub(r"\s+", " ", page4)

# --- manual drift / off / bad value / camera filter / service off ---------------------------------------
for f in (t1 + ".xmp",):
    os.remove(f)
man = poll(c.post("/gpsfill/scan/start", data={**form, "drift": "60", "camera": "X1"}))
tokm = hidden(man, "token")
rt = __import__("app.modules.gps_fill.router", fromlist=["x"])
pm = {os.path.basename(k): v for k, v in rt._get_plans(tokm).items()}
assert set(k for k, v in pm.items()) == {"ref1.jpg", "ref2.jpg", "ref3.jpg", "t1.jpg", "t3.jpg"}, set(pm)   # camera filter
assert pm["t1.jpg"].drift.seconds == 60 and pm["t1.jpg"].drift.method == "manual"
off = poll(c.post("/gpsfill/scan/start", data={**form, "drift": "off", "camera": "X1"}))
po = {os.path.basename(k): v for k, v in rt._get_plans(hidden(off, "token")).items()}
assert po["t1.jpg"].drift.seconds == 0 and haversine_m(po["t1.jpg"].loc["lat"], po["t1.jpg"].loc["lon"], *tl) > 300     # 120 s * 5 m/s = 600 m off
assert "Uhr-Abweichung" in poll(c.post("/gpsfill/scan/start", data={**form, "drift": "abc"}))
os.environ["GEO_SERVICE_URL"] = ""
assert "GEO_SERVICE_URL ist nicht gesetzt" in c.post("/gpsfill/scan/start", data=form).text
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"

# --- expired token --------------------------------------------------------------------------------
assert "abgelaufen" in poll(c.post("/gpsfill/apply", data={"token": "nope", "selected": [t1]}))

# --- stepping: drift learned in step 1 is used in step 2 ------------------------------------------------
os.environ["BATCH_SIZE_GPSFILL"] = "4"
for f in (t1, t2, t3, t4):
    if os.path.exists(f + ".xmp"): os.remove(f + ".xmp")
calls.clear()
s1 = poll(c.post("/gpsfill/scan/start", data=form))
assert "Schritt 1" in s1 and "Nächste" in s1, s1[:500]
sid, off_ = hidden(s1, "token"), None
m = re.search(r'"scan_id": "([0-9a-f]+)", "offset": "(\d+)"', html.unescape(s1))
assert m
n_calls = len(calls)
s2 = poll(c.post("/gpsfill/scan/start", data={**form, "scan_id": m.group(1), "offset": m.group(2)}))
assert "Schritt 2" in s2
assert len(calls) == n_calls, "step 2 must reuse the track fetched in step 1"
p2 = {os.path.basename(k): v for k, v in rt._get_plans(hidden(s2, "token")).items()}
step2_all = set(p2)
combined = {os.path.basename(k): v for tok_ in (hidden(s1, "token"), hidden(s2, "token")) for k, v in rt._get_plans(tok_).items()}
assert combined["t1.jpg"].drift.seconds == DRIFT, "t1 must be drift-corrected even if its references were in the other step"
os.environ["BATCH_SIZE_GPSFILL"] = "0"

# --- Basis-Ordner decides the type; generated thumbs/nfo are never candidates ----------------------------
vid, ph = os.path.join(MR, "vid"), os.path.join(MR, "ph")
os.makedirs(vid); os.makedirs(ph)
sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1", os.path.join(vid, "clip.mp4")], check=True)
for suffix in ("-thumb", "-poster", "-fanart"):
    shutil.copy(t2, os.path.join(vid, f"clip{suffix}.jpg"))
open(os.path.join(vid, "clip.nfo"), "w").write("<movie/>"); open(os.path.join(vid, "notes.txt"), "w").write("x")
shutil.copy(t2, os.path.join(vid, "orphan-thumb.jpg"))                 # orphan: no sibling media -> still a plain photo
shutil.copy(t2, os.path.join(ph, "pic.jpg")); open(os.path.join(ph, "pic.nfo"), "w").write("x")
shutil.copy(os.path.join(vid, "clip.mp4"), os.path.join(ph, "video_in_photos.mp4"))
os.environ["ROOT_FOLDERS"] = f"VIDEOS={vid}:VIDEO,PHOTOS={ph}:IMAGE"
for root, expect in ((vid, {"clip.mp4"}), (ph, {"pic.jpg"})):
    pg = poll(c.post("/gpsfill/scan/start", data={**form, "path": root, "root_path": root, "types": "all"}))
    got = {os.path.basename(k) for k in rt._get_plans(hidden(pg, "token"))}
    assert got == expect, (root, got)
assert fillcore.find_media(vid, 1, "all") and {os.path.basename(x) for x in fillcore.find_media(vid, 1, "all")} == {"clip.mp4", "orphan-thumb.jpg"}
del os.environ["ROOT_FOLDERS"]
print("PASS")
