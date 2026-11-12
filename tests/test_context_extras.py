"""Context extras end to end (suite side): daylight / moon / holiday / wind / sights / country / auto trip / clock check
-> prompt phrases, XMP-mon:Ctx* fields, NFO plot; 'Meine Orte' as the one fixed-place list.
python tests/test_context_extras.py   (needs exiftool + ffmpeg, else SKIP)"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg missing")
    sys.exit(0)
SCRATCH = tempfile.mkdtemp(prefix="msuite_ctx_")
MEDIA = os.path.join(SCRATCH, "media")
os.makedirs(os.path.join(MEDIA, "2026.10.03 Ohne Reise"))
os.makedirs(os.path.join(MEDIA, "Lose"))
os.environ["MEDIA_ROOT"] = MEDIA
for k in ("GEO_SERVICE_URL", "GEO_SERVICE_TOKEN", "AI_CONTEXT", "AI_CONTEXT_EXTRAS", "NFO_PLOT_CONTEXT"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import fillcore, geocore, geoservice, nfocore  # noqa: E402
from app.modules.ai_description import router as ai_router  # noqa: E402


def fail(msg):
    print("FAIL", msg)
    sys.exit(1)


PLACES = [{"id": "oma", "name": "Omas Haus", "kind": "Familie", "poi": True, "lat": 47.07, "lon": 15.43, "radius_m": 30, "note": ""},
          {"id": "legacy-boot", "name": "boot", "kind": "Fester Ort", "poi": False, "lat": 45.5, "lon": 13.6, "radius_m": 30, "note": ""}]
CLOCK = {"mode": "warn"}
CTX = {"extra_de": "abends im Juni 2026", "daypart_de": "Abend", "season_de": "Sommer", "month_de": "Juni", "year": 2026,
       "weekday_de": "Samstag", "local_time": "2026-06-06T20:30:00+02:00", "tz": "Europe/Rome",
       "weather": {"summary_de": "klar", "temp_c": 24, "wind_kmh": 45, "wind_de": "starker Wind", "precip_mm": 1.4, "cloud_pct": 10, "humidity_pct": 60},
       "daylight": {"label": "goldene Stunde (abends)", "sunrise": "05:25", "sunset": "20:40"},
       "moon": {"label": "Vollmond (100 %)"}, "holiday": {"name": "Tag der Republik", "country": "IT"},
       "country_info": {"name": "Italien", "text_de": "Hauptstadt Rome, Währung EUR, Sprachen: Italienisch"},
       "pois": [{"name": "Schloss Test", "kind": "Burg/Schloss", "distance_m": 120}],
       "parts_de": {"daylight": "in der goldenen Stunde vor Sonnenuntergang (20:40)", "moon": "bei Vollmond",
                    "holiday": "am Feiertag Tag der Republik", "wind": "starker Wind (45 km/h)", "precip": "1.4 mm Niederschlag",
                    "country": "Italien: Hauptstadt Rome", "poi": "in der Nähe: Schloss Test (Burg/Schloss, 120 m)",
                    "trip": "auf Reise: Italien (Tag 3 von 6)"},
       "trip": {"name": "Italien", "folder": "2026.06.04-2026.06.09 Italien", "day": 3, "days": 6}}
calls = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        calls.append((u.path, q))
        if u.path == "/context":
            code, body = 200, CTX
        elif u.path == "/places":
            code, body = 200, {"places": PLACES}
        elif u.path == "/reverse":
            code, body = 200, {"found": True, "country": "Österreich", "country_a": "AUT", "state": "Steiermark", "county": "",
                               "city": "Graz", "locality": "", "street": "Hauptplatz 1"}
        elif u.path == "/clock-check":
            code, body = 200, ({"status": "warn", "text_de": "Kamera-Uhr geht vor um 1 h 00 min gegenüber der GPS-Zeit"}
                               if CLOCK["mode"] == "warn" else {"status": "ok", "text_de": "ok"})
        else:
            code, body = 404, {}
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


POSTED = []


def _post(self):
    n = int(self.headers.get("Content-Length") or 0)
    item = json.loads(self.rfile.read(n) or b"{}")
    POSTED.append(item)
    data = json.dumps(item).encode()
    self.send_response(200); self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


H.do_POST = _post
srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["GEO_SERVICE_URL"] = f"http://127.0.0.1:{srv.server_port}"
os.environ["AI_CONTEXT"] = "true"
geoservice.reset_breaker()


def make(folder, name, *exif):
    p = os.path.join(MEDIA, folder, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", p], check=True)
    subprocess.run(["exiftool", "-overwrite_original", "-q", *exif, p], check=True, capture_output=True)
    subprocess.run(["exiftool", "-q", "-o", p + ".xmp", p], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSLatitude=45.5", "-XMP-exif:GPSLongitude=12.4", p + ".xmp"], check=True)
    return p


T = ("-DateTimeOriginal=2026:06:06 20:30:00", "-OffsetTimeOriginal=+02:00")
IMG = make("Lose", "a.jpg", *T, "-GPSLatitude=45.5", "-GPSLatitudeRef=N", "-GPSLongitude=12.4", "-GPSLongitudeRef=E",
           "-GPSDateStamp=2026:06:06", "-GPSTimeStamp=17:30:00")

# 1. default AI extras: daylight, moon, holiday, trip, poi -- not wind/precip/country
extra, f = geoservice.context_info(IMG)
for want in ("in der goldenen Stunde vor Sonnenuntergang (20:40)", "bei Vollmond", "am Feiertag Tag der Republik",
             "auf Reise: Italien (Tag 3 von 6)", "in der Nähe: Schloss Test"):
    if want not in extra:
        fail((want, extra))
if "starker Wind" in extra or "Hauptstadt" in extra or "Niederschlag" in extra:
    fail(("wind/precip/country must be opt-in for the prompt", extra))
print("PASS: default prompt extras (daylight, moon, holiday, trip, sights)")

# 2. every field is available for the sidecar regardless
exp = {"daylight": "goldene Stunde (abends)", "sunrise": "05:25", "sunset": "20:40", "moon": "Vollmond (100 %)", "holiday": "Tag der Republik",
       "wind_kmh": 45, "wind_text": "starker Wind", "precip_mm": 1.4, "cloud_pct": 10, "humidity_pct": 60, "country": "Italien",
       "trip": "Italien", "trip_source": "track"}
for k, v in exp.items():
    if f.get(k) != v:
        fail((k, f.get(k), v))
if "Schloss Test (Burg/Schloss, 120 m)" not in f["pois"] or "Rome" not in f["country_info"]:
    fail(f)
print("PASS: structured fields")

# 3. opt-in extras
os.environ["AI_CONTEXT_EXTRAS"] = "wind,precip,country"
extra2, _ = geoservice.context_info(IMG)
if not ("starker Wind (45 km/h)" in extra2 and "1.4 mm Niederschlag" in extra2 and "Italien: Hauptstadt Rome" in extra2 and "Vollmond" not in extra2):
    fail(extra2)
os.environ["AI_CONTEXT_EXTRAS"] = "none"
extra3, f3 = geoservice.context_info(IMG)
if "Vollmond" in extra3 or "daylight" not in f3:
    fail((extra3, f3))
os.environ["AI_CONTEXT_EXTRAS"] = "all"
if not all(x in geoservice.context_info(IMG)[0] for x in ("Vollmond", "starker Wind", "Hauptstadt")):
    fail("all")
os.environ.pop("AI_CONTEXT_EXTRAS")
print("PASS: AI_CONTEXT_EXTRAS switch")

# 4. folder trip wins over the detected one (and the service is told not to bother)
G = make("2026.10.03 Ohne Reise", "b.jpg", *T)
FT = os.path.join(MEDIA, "2026.06.01-2026.06.10 Ordnerreise")
os.makedirs(FT)
shutil.move(IMG, FT + "/a.jpg"); shutil.move(IMG + ".xmp", FT + "/a.jpg.xmp")
IMG2 = FT + "/a.jpg"
calls.clear()
extra4, f4 = geoservice.context_info(IMG2)
if f4["trip"] != "Ordnerreise" or f4["trip_source"] != "folder" or "Wo wars: „Ordnerreise“" not in extra4 or "auf Reise: Italien" in extra4:
    fail((f4.get("trip"), extra4))
if [c for c in calls if c[0] == "/context"][-1][1].get("trip") != ["false"]:
    fail("service should not detect a trip when the folder names one")
print("PASS: folder trip wins, CtxTripSource")

# 5. clock check: own GPS time in the file -> warning text + status
CLOCK["mode"] = "warn"
calls.clear()
extra5, f5 = geoservice.context_info(IMG2)
cc = [c for c in calls if c[0] == "/clock-check"]
if not cc or "gps_time" not in cc[-1][1] or "lat" not in cc[-1][1]:
    fail(("clock-check must get gps_time + own position", cc))
if f5.get("clock_status") != "warn" or "geht vor" not in f5.get("clock", ""):
    fail(f5)
CLOCK["mode"] = "ok"
f6 = geoservice.context_info(IMG2)[1]
if f6.get("clock_status") != "ok" or f6.get("clock") != "ok":
    fail(f6)
# a position that was FILLED from the track proves nothing about the clock -> not sent
subprocess.run(["exiftool", "-config", fillcore.MON_CONFIG, "-q", "-overwrite_original", "-XMP-mon:GpsSource=track-interpolated", IMG2 + ".xmp"], check=True)
calls.clear()
geoservice.context_info(IMG2)
cc = [c for c in calls if c[0] == "/clock-check"]
if cc and "lat" in cc[-1][1]:
    fail("filled position must not be used as clock evidence")
print("PASS: clock check (GPS time + own position; filled GPS ignored)")

# 6. XMP fields + NFO plot
row = {"path": IMG2, "new_description": "", "is_video": False, "context_fields": {**f, "clock": "Kamera-Uhr geht vor", "clock_status": "warn"}}
res = ai_router._execute_one(row, False)
if not res.get("context_written") or res["xmp_error"]:
    fail(res)
mon = json.loads(subprocess.run(["exiftool", "-config", fillcore.MON_CONFIG, "-j", "-n", "-XMP-mon:all", IMG2 + ".xmp"], capture_output=True, text=True).stdout)[0]
for k, v in (("CtxDaylight", "goldene Stunde (abends)"), ("CtxMoon", "Vollmond (100 %)"), ("CtxHoliday", "Tag der Republik"),
             ("CtxWindKmh", 45), ("CtxPrecipMm", 1.4), ("CtxTripSource", "track"), ("CtxClockStatus", "warn"), ("CtxCountry", "Italien")):
    if mon.get(k) != v:
        fail((k, mon.get(k), v))
m = nfocore.read_mon_fields(IMG2 + ".xmp")
line = nfocore.build_context_plot_line(m)
for want in ("Wetter: klar", "Temp: 24 °C", "Wind: starker Wind (45 km/h)", "Niederschlag: 1.4 mm", "Licht: goldene Stunde (abends) (Aufgang 05:25 · Untergang 20:40)",
             "Mond: Vollmond (100 %)", "Feiertag: Tag der Republik", "Wo wars: Italien", "In der Nähe: Schloss Test", "Land: Italien – Hauptstadt Rome"):
    if want not in line:
        fail((want, line))
if "Kamera-Uhr" in line:
    fail("clock warning does not belong into the plot")
basic = nfocore.build_context_plot_line(m, "basic")
if basic != "[CR][COLOR lightblue]Temp: 24 °C[/COLOR][CR][COLOR lightblue]Wetter: klar[/COLOR]":
    fail(basic)
os.environ["NFO_PLOT_CONTEXT"] = "off"
if nfocore.build_context_plot_line(m) != "":
    fail("off")
os.environ.pop("NFO_PLOT_CONTEXT")
print("PASS: XMP-mon fields written; NFO plot full/basic/off")

# 7. fixed places come from "Meine Orte" only (the old predefined_locations file is gone)
pl = geocore.all_places()
names = [p.name for p in pl]
if names != ["Omas Haus", "boot"]:
    fail(names)
oma = pl[0]
if not (oma.geo.city == "Graz" and "Familie" in oma.label and abs(oma.lat - 47.07) < 1e-9):
    fail(oma)
os.environ["GEO_SERVICE_URL"] = ""
if geocore.all_places() != []:
    fail("without the service the list is empty")
os.environ["GEO_SERVICE_URL"] = f"http://127.0.0.1:{srv.server_port}"
if hasattr(geocore, "load_predefined_locations"):
    fail("legacy loader still present")
print("PASS: place list = Meine Orte only")
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
tc = TestClient(app)
if tc.get("/geooverrides/legacy").status_code != 404:
    fail("legacy endpoint still present")

# Info page: GEO_* variables live in the geo-service container -> shown from its /health "env"
from app.core import infocore as _ic, geoservice as _gs
os.environ.pop("GEO_HOME_LAT", None)
_e, _h = _gs.enabled, _gs.health
_gs.enabled, _gs.health = (lambda: True), (lambda: {"env": {"GEO_HOME_LAT": "48.15"}})
try:
    _rows = {r["name"]: r for g in _ic.env_groups() for r in g["rows"]}
finally:
    _gs.enabled, _gs.health = _e, _h
assert _rows["GEO_HOME_LAT"]["state"] == "set" and _rows["GEO_HOME_LAT"]["value"] == "48.15", _rows["GEO_HOME_LAT"]
assert "GEO_HOME_COUNTRY" in _rows and "GEO_POI_MAX" in _rows
print("PASS: info page shows geo-service env")
srv.shutdown()
print("\nALL PASS")

# old .nfo blocks labelled "Reise:" are replaced by the new "Wo wars:" line (not duplicated)
import tempfile as _tf, os as _os
_d = _tf.mkdtemp(); _n = _os.path.join(_d, "a.nfo")
open(_n, "w", encoding="utf-8").write("<movie><plot>Text[CR][COLOR lightblue]Wetter: klar[/COLOR][CR][COLOR lightblue]Reise: Italien[/COLOR]</plot></movie>")
_line = "[CR][COLOR lightblue]Wetter: klar[/COLOR][CR][COLOR lightblue]Wo wars: Italien[/COLOR]"
_ch, _ = nfocore.patch_nfo_context(_n, _line)
_t = open(_n, encoding="utf-8").read()
if not _ch or "Reise:" in _t or _t.count("Wo wars: Italien") != 1 or _t.count("Wetter: klar") != 1:
    fail(("old Reise line not migrated", _t))
print("ok   Reise -> Wo wars migration")
