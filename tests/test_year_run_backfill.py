"""Jahreslauf option 'Neue Daten nachtragen' (backfill): context groups missing in old sidecars are added (CtxSchema stamp, only the
difference), and AI text + .nfo are redone ONLY for files that got new data -- also when the run that added the data was interrupted
(the AiStale / NfoStale flags live in the sidecar). Geo-service and AI provider faked."""
import os, re, subprocess as sp, sys, tempfile, time

SCR = tempfile.mkdtemp(prefix="yrbf_")
PH, VI = os.path.join(SCR, "photo") + "/", os.path.join(SCR, "video", "Home-Video") + "/"
for d in (PH + "2026/Tag1", VI + "2026"):
    os.makedirs(d)
os.environ["MEDIA_ROOT"] = SCR
os.environ["ROOT_FOLDERS"] = f"PHOTOS={PH}:IMAGE,VIDEOS={VI}:VIDEO"
os.environ["TIMESTAMP_PRESETS_FILE"] = os.path.join(SCR, "cfg", "presets.json")
os.environ["YEAR_RUN_STATE_DAYS"] = "0"
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
from app.core import geocore, aicore, geoservice, ctxfill
from app.modules.year_run import runner

fails = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond: fails.append(name)

# ---- steps order -------------------------------------------------------------------------------------------------------------------
fo = runner.Folder("VIDEO", "regular", VI + "2026/", VI, label="Videos 2026")
plain = runner.steps_for(fo, runner.Opts(year=2026, steps=("gps", "ai", "nfo", "mtime")))
bf = runner.steps_for(fo, runner.Opts(year=2026, steps=("gps", "ai", "nfo", "mtime"), backfill=True))
check("without backfill: no ctx", plain == ["gps", "ai", "nfo", "mtime"], plain)
check("backfill: ctx is forced in and runs BEFORE ai and nfo", bf == ["gps", "ctx", "ai", "nfo", "mtime"], bf)

# ---- fakes ---------------------------------------------------------------------------------------------------------------------------
CTX = {"daypart_de": "Nachmittag", "season_de": "Sommer", "month_de": "Juni", "year": 2026, "weekday_de": "Montag",
       "local_time": "2026-06-01T10:00:00+02:00", "tz": "Europe/Vienna", "elevation_m": 345.0,
       "weather": {"summary_de": "sonnig", "temp_c": 24, "wind_kmh": 5, "wind_de": "Brise", "precip_mm": 0.0, "cloud_pct": 10, "humidity_pct": 50},
       "daylight": {"label": "Tageslicht", "sunrise": "05:00", "sunset": "21:00"}, "moon": {"label": "Neumond (1 %)"},
       "country_info": {"name": "Kroatien", "text_de": "Währung: Euro (EUR)"}, "extra_de": "x", "parts_de": {}}
CTXCALLS = []
def fake_get(path, params, timeout_s=None):
    if path == "/context":
        CTXCALLS.append(1); return dict(CTX), "ok"
    if path == "/sights/status": return {"place_version": "pv1"}, "ok"
    if path == "/reverse": return {"found": True, "country": "Kroatien", "country_a": "HRV", "city": "Rovinj", "source": "fake"}, "ok"
    if path == "/elevation": return {"elevation": 345.0}, "ok"
    if path == "/timezoneJSON": return {"timezoneId": "Europe/Vienna"}, "ok"
    return None, "down"
geoservice._get = fake_get
geoservice.health = lambda: {"status": "ok"}
geocore.reverse_geocode = lambda lat, lon, **k: geocore.GeoResult(country="Kroatien", country_a="HRV", city="Rovinj", source="fake")
TEXT = ["Alter Text."]
class _Gen:
    provider, model, failures = "fake", "m", []
    @property
    def description(self): return TEXT[0]
aicore.generate_description = lambda *a, **k: _Gen()

def wait(run_id, secs=240):
    t0 = time.time()
    while time.time() - t0 < secs:
        r = runner.get(run_id)
        if r.status != "running": return r
        time.sleep(0.3)
    raise SystemExit("run did not finish")

def go(**kw):
    run, _c = runner.start(runner.Opts(year=2026, depth=7, **kw))
    r = wait(run.id)
    assert r.status == "done", (r.status, r.error, "\n".join(m for _t, _l, m in r.log)[-1500:])
    return r

def xmp(p): return open(p + ".xmp", encoding="utf-8").read()

# ---- a photo and a video, GPS from EXIF; first run WITHOUT geo-service context (legacy state), AI text "Alter Text." -------------
img = PH + "2026/Tag1/2026.06.01-10.00.00.jpg"
Image.new("RGB", (64, 48), (200, 120, 40)).save(img)
sp.run(["exiftool", "-overwrite_original", "-q", "-DateTimeOriginal=2026:06:01 10:00:00", "-OffsetTimeOriginal=+02:00",
        "-GPSLatitude=45.08", "-GPSLatitudeRef=N", "-GPSLongitude=13.64", "-GPSLongitudeRef=E", img], check=True)
mp4 = VI + "2026/2026.06.02-12.00.00.mp4"
sp.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=64x48:rate=5", "-pix_fmt", "yuv420p", mp4], check=True)
sp.run(["exiftool", "-overwrite_original", "-q", "-CreateDate=2026:06:02 10:00:00", "-GPSLatitude=45.08", "-GPSLongitude=13.64", mp4])
os.environ["GEO_SERVICE_URL"] = ""
go(steps=("gps", "ai", "nfo", "mtime"), aim=True)
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
nfo = mp4[:-4] + ".nfo"
check("start state: AI text + nfo exist, sidecars have no context yet", "Alter Text." in xmp(img) and os.path.exists(nfo) and "CtxSchema" not in xmp(img) and "CtxDaypart" not in xmp(img))
open(nfo, "a", encoding="utf-8").write("<!--SENTINEL-->")

# ---- interrupted scenario: a run that only adds the context (no backfill) leaves the AI text / nfo stale, flagged in the sidecar ----
go(steps=("ctx", "mtime"))
check("ctx added the data + stamped the sidecars", "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in xmp(img) and "CtxCountryInfo>Währung: Euro (EUR)<" in xmp(img) and "CtxWeather>sonnig" in xmp(img), xmp(img)[-900:])
check("photo flagged AiStale (text older than the data); video AiStale + NfoStale",
      "AiStale>" in xmp(img) and "NfoStale" not in xmp(img) and "AiStale>" in xmp(mp4) and "NfoStale>" in xmp(mp4))
check("the AI text was NOT redone by that run", "Alter Text." in xmp(img) and "SENTINEL" in open(nfo, encoding="utf-8").read())

# ---- backfill run: nothing new to add, but the flagged files get new AI text and a fresh .nfo ---------------------------------------
TEXT[0] = "Neuer Text mit Wetter."
CTXCALLS.clear()
r = go(steps=("gps", "ai", "nfo", "mtime"), backfill=True)
log = "\n".join(m for _t, _l, m in r.log)
check("AI text redone for the flagged photo and video", "Neuer Text mit Wetter." in xmp(img) and "Neuer Text mit Wetter." in xmp(mp4), xmp(img)[-600:])
check("flags cleared", "AiStale" not in xmp(img) and "AiStale" not in xmp(mp4) and "NfoStale" not in xmp(mp4), xmp(mp4)[-500:])
check("the .nfo was rewritten (sentinel gone)", "SENTINEL" not in open(nfo, encoding="utf-8").read())
check("log names the backfill", "älter als die neuen Daten" in log, log[-1200:])
check("stamped sidecars were not asked again by the ctx step", not CTXCALLS or len(CTXCALLS) <= 2, len(CTXCALLS))

# ---- third run: nothing flagged any more -> nothing redone ----------------------------------------------------------------------------
TEXT[0] = "Sollte nicht erscheinen."
go(steps=("gps", "ai", "nfo", "mtime"), backfill=True)
check("no flags -> no regeneration", "Sollte nicht erscheinen." not in xmp(img) and "Neuer Text mit Wetter." in xmp(img))

print("\n%s" % ("ALL OK" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
