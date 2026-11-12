import json
"""Optional geo-service bridge: reverse geocoding goes through the service when GEO_SERVICE_URL is set
(falling back to the local chain only while the service is DOWN), and AI descriptions can carry extra
caption context (AI_CONTEXT=true). With the variables unset nothing changes."""
import html, json, os, re, shutil, subprocess, sys, tempfile, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

SCRATCH = tempfile.mkdtemp(prefix="msuite_test_")
MEDIA_ROOT = os.path.join(SCRATCH, "geoservice_root")
TRIP = os.path.join(MEDIA_ROOT, "2026.10.03-2026.10.05 Testreise")
os.makedirs(TRIP)
os.environ["MEDIA_ROOT"] = MEDIA_ROOT
for k in ("GEO_SERVICE_URL", "GEO_SERVICE_TOKEN", "AI_CONTEXT"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import aicore, geocore, geoservice, infocore

# ---------------------------------------------------------------- fake servers
class Fake:
    def __init__(self, handler):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                u = urlparse(self.path)
                outer.calls.append((u.path, parse_qs(u.query), dict(self.headers)))
                code, body = handler(u.path, parse_qs(u.query))
                data = json.dumps(body).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self): self.srv.shutdown()


SERVICE_MODE = {"mode": "hit"}
GRAZ = {"found": True, "country": "Österreich", "country_a": "AUT", "state": "Steiermark", "county": "", "city": "Graz",
        "locality": "Hauptplatz", "street": "Hauptplatz", "source": "Photon"}

def service_handler(path, q):
    if path == "/reverse":
        m = SERVICE_MODE["mode"]
        return (500, {}) if m == "down" else (200, GRAZ if m == "hit" else {"found": False})
    if path == "/context":
        return 200, {"extra_de": "vormittags im Oktober 2026; Regen bei 11 °C", "text_de": "x",
                     "daypart_de": "Vormittag", "season_de": "Herbst", "month_de": "Oktober", "year": 2026, "weekday_de": "Samstag",
                     "local_time": "2026-10-03T10:00:00+02:00", "weather": {"summary_de": "Regen", "temp_c": 11}, "elevation_m": 372}
    return 404, {}

def photon_handler(path, q):
    return 200, {"features": [{"properties": {"country": "Deutschland", "countrycode": "DE", "city": "Berlin"}}]}

svc, photon = Fake(service_handler), Fake(photon_handler)

# ---------------------------------------------------------------- 1. disabled = old behaviour
assert not geoservice.enabled()
r = geocore.reverse_geocode(52.5, 13.4, photon_server=photon.url)
assert r.city == "Berlin" and r.source == "Photon" and not svc.calls
print("PASS: GEO_SERVICE_URL unset -> local chain only, service never contacted")
# the 'via ...' badge tells where a service answer really came from (cache / fresh / override)
L = geocore.service_source_label
assert L({"source": "Photon", "cached": True}) == "geo-service-Cache, ursprünglich Photon"
assert L({"source": "Photon", "cached": False}) == "Photon frisch, über geo-service"
assert L({"source": "override", "override": "halle-1", "cached": True}) == "Override „halle-1“ (geo-service)"
assert L({}) == "geo-service"
print("PASS: source label (cache / fresh / override)")

# ---------------------------------------------------------------- 2. service hit / empty / down
os.environ["GEO_SERVICE_URL"] = svc.url
os.environ["GEO_SERVICE_TOKEN"] = "sekret"
geoservice.reset_breaker()
n_photon = len(photon.calls)
r = geocore.reverse_geocode(47.07, 15.43, photon_server=photon.url)
assert r.city == "Graz" and r.country_a == "AUT" and r.street == "Hauptplatz", r
assert len(photon.calls) == n_photon, "service answered -> local backends must not be asked"
assert svc.calls[-1][2].get("X-Api-Key") == "sekret" or svc.calls[-1][2].get("X-API-Key") == "sekret", "token must be sent"
print("PASS: service hit used, token sent, local chain untouched")

SERVICE_MODE["mode"] = "empty"
assert geocore.reverse_geocode(1.0, 2.0, photon_server=photon.url) is None and len(photon.calls) == n_photon
print("PASS: 'nothing found' from the service is final (not repeated against the same backends)")

# maritime backup is ALWAYS the last resort (open sea / coast), in every scenario
sea = Fake(lambda path, q: (200, {"name": "Indischer Ozean"}))
empty_photon = Fake(lambda path, q: (200, {"features": []}))
SERVICE_MODE["mode"] = "empty"                       # service found nothing and runs without maritime
r = geocore.reverse_geocode(4.0, 73.0, photon_server=empty_photon.url, maritime_server=sea.url)
assert r is not None and r.source == "Maritim-Backup" and r.country == "Indischer Ozean", r
assert geocore.reverse_geocode(4.0, 73.0, photon_server=empty_photon.url) is None          # not configured -> nothing
SERVICE_MODE["mode"] = "down"                        # service down: local chain, photon empty -> maritime
geoservice.reset_breaker()
r = geocore.reverse_geocode(4.0, 73.0, photon_server=empty_photon.url, maritime_server=sea.url)
assert r is not None and r.source == "Maritim-Backup direkt (geo-service nicht erreichbar)", r      # badge says: NOT via the service
SERVICE_MODE["mode"] = "hit"                         # a land hit never touches maritime
n_sea = len(sea.calls)
assert geocore.reverse_geocode(47.07, 15.43, photon_server=empty_photon.url, maritime_server=sea.url).city == "Graz"
assert len(sea.calls) == n_sea
os.environ["GEO_SERVICE_URL"] = ""                   # no service at all
r = geocore.reverse_geocode(4.0, 73.0, photon_server=empty_photon.url, maritime_server=sea.url)
assert r is not None and r.source == "Maritim-Backup"
os.environ["GEO_SERVICE_URL"] = svc.url
geoservice.reset_breaker()
print("PASS: maritime backup is the last resort with service hit/empty/down and without a service")

SERVICE_MODE["mode"] = "down"
geoservice.reset_breaker()
before = len(svc.calls)
r1 = geocore.reverse_geocode(1.0, 2.0, photon_server=photon.url)
r2 = geocore.reverse_geocode(1.0, 2.0, photon_server=photon.url)
assert r1.city == "Berlin" and r2.city == "Berlin", "service DOWN -> local chain takes over"
r3 = geocore.reverse_geocode(1.0, 2.0, photon_server=photon.url)
assert len(svc.calls) - before == 2, "after 2 failures the breaker must stop asking the service (60 s)"
assert r3.city == "Berlin"
print("PASS: service down -> local fallback, breaker stops hammering it after 2 failures")

dead_url = "http://127.0.0.1:9"
os.environ["GEO_SERVICE_URL"] = dead_url
geoservice.reset_breaker()
assert geocore.reverse_geocode(1.0, 2.0, photon_server=photon.url).city == "Berlin"
print("PASS: unreachable service -> local fallback")

# ---------------------------------------------------------------- 3. caption context
os.environ["GEO_SERVICE_URL"] = svc.url
SERVICE_MODE["mode"] = "hit"
geoservice.reset_breaker()

def make(name, *exif):
    p = os.path.join(TRIP, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240", "-frames:v", "1", p], check=True)
    subprocess.run(["exiftool", "-overwrite_original", "-q", *exif, p], check=True, capture_output=True)
    return p

IMG = make("2026.10.03-10.00.00.jpg", "-DateTimeOriginal=2026:10:03 10:00:00", "-OffsetTimeOriginal=+02:00")
subprocess.run(["exiftool", "-q", "-o", IMG + ".xmp", IMG], check=True)
subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSLatitude=47.07", "-XMP-exif:GPSLongitude=15.43",
                "-XMP-photoshop:Country=Österreich", "-XMP-photoshop:City=Graz", IMG + ".xmp"], check=True)

assert geoservice.caption_extra(IMG) == "", "AI_CONTEXT off -> no extra context"
os.environ["AI_CONTEXT"] = "true"
extra = geoservice.caption_extra(IMG)
assert extra == "vormittags im Oktober 2026; Regen bei 11 °C; Wo wars: „Testreise“", extra
ctx_calls = [c for c in svc.calls if c[0] == "/context"]
assert ctx_calls and ctx_calls[-1][1]["time"] == [str(int(datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc).timestamp()))], ctx_calls[-1][1]      # 10:00 +02:00 = 08:00Z
assert geoservice.trip_name("/x/2026.07/a.jpg") == "" and geoservice.trip_name("/x/2026.07.29/a.jpg") == ""
NOGPS = make("2026.10.03-11.00.00.jpg", "-DateTimeOriginal=2026:10:03 11:00:00", "-OffsetTimeOriginal=+02:00")
assert geoservice.caption_extra(NOGPS) == "Wo wars: „Testreise“", "no GPS -> only what needs no coordinates"
os.environ["GEO_SERVICE_URL"] = ""
assert geoservice.caption_extra(IMG) == "", "service off -> no extra context"
os.environ["GEO_SERVICE_URL"] = svc.url
print("PASS: caption_extra (time basis, trip name, switches)")

# --- structured context -> XMP-mon:Ctx* (independent of the AI text)
from app.core import fillcore
from app.modules.ai_description import router as ai_router
_, fld = geoservice.context_info(IMG)
assert fld["daypart"] == "Vormittag" and fld["season"] == "Herbst" and fld["temp_c"] == 11 and fld["trip"] == "Testreise", fld
assert fld["elevation"] == 372, fld
assert ("Höhe", "372 m") in ai_router.context_items(fld), ai_router.context_items(fld)
os.environ["AI_CONTEXT_ELEVATION"] = "true"
assert geoservice.caption_extra(IMG).endswith("Regen bei 11 °C; auf ca. 372 m Seehöhe; Wo wars: „Testreise“"), geoservice.caption_extra(IMG)
os.environ.pop("AI_CONTEXT_ELEVATION")
row = {"path": IMG, "new_description": "", "is_video": False, "context_fields": fld}     # AI produced NO text
res = ai_router._execute_one(row, False)
assert res["context_written"] and not res["xmp_error"], res
mon = json.loads(subprocess.run(["exiftool", "-config", fillcore.MON_CONFIG, "-j", "-G1", "-XMP-mon:all", IMG + ".xmp"], capture_output=True, text=True).stdout)[0]
assert mon["XMP-mon:CtxSeason"] == "Herbst" and mon["XMP-mon:CtxWeather"] == "Regen" and mon["XMP-mon:CtxTempC"] == 11 \
    and mon["XMP-mon:CtxElevation"] == 372 and mon["XMP-mon:CtxTrip"] == "Testreise" and mon["XMP-mon:CtxDaypart"] == "Vormittag" and "XMP-dc:Description" not in \
    json.loads(subprocess.run(["exiftool", "-j", "-G1", "-XMP-dc:Description", IMG + ".xmp"], capture_output=True, text=True).stdout)[0], mon
assert "Description" not in subprocess.run(["exiftool", "-XMP-dc:Description", IMG + ".xmp"], capture_output=True, text=True).stdout
os.environ["XMP_CONTEXT"] = "false"
res = ai_router._execute_one({**row, "new_description": "Ein Test."}, False)
assert res["xmp_written"] and "context_written" not in res
os.environ.pop("XMP_CONTEXT")
print("PASS: context fields written to XMP-mon without AI text; XMP_CONTEXT=false switches it off")

# ---------------------------------------------------------------- 4. prompt
p0 = aicore.build_prompts("Graz")[1]
p1 = aicore.build_prompts("Graz", False, "nachmittags im Oktober 2026; Regen bei 11 °C")[1]
assert aicore.build_prompts("Graz", False, "")[1] == p0 and "Rahmeninformationen" not in p0
assert "nachmittags im Oktober 2026; Regen bei 11 °C" in p1 and "nie bei Innenräumen" in p1
assert p1.index("Graz") < p1.index("Rahmeninformationen") < p1.index("Beschreibe in 3 bis 4")
# --- length / context budget from the environment -------------------------------------------------------
import re as _re
assert "2 bis 3 klaren Sätzen" in p0 and "maximal 50 Wörter" in p0           # default without context = original CLI budget
assert "3 bis 4 klaren Sätzen" in p1 and "maximal 75 Wörter" in p1 and "höchstens 2 Angaben" in p1, p1[-900:]
assert "Hauptsache" in p1
os.environ.update(AI_CONTEXT_MAX="all", AI_SENTENCES_CONTEXT="2-5", AI_WORDS_MAX_CONTEXT="90", AI_SENTENCES="1-2", AI_WORDS_MAX="30")
q1 = aicore.build_prompts("Graz", False, "nachmittags; Regen")[1]
assert "alle passenden Angaben" in q1 and "2 bis 5 klaren Sätzen" in q1 and "maximal 90 Wörter" in q1
assert "1 bis 2 klaren Sätzen" in aicore.build_prompts("Graz")[1] and "maximal 30 Wörter" in aicore.build_prompts("Graz")[1]
os.environ["AI_CONTEXT_MAX"] = "1"
assert "höchstens eine Angabe" in aicore.build_prompts("Graz", False, "x")[1]
os.environ.update(AI_CONTEXT_MAX="quatsch", AI_SENTENCES_CONTEXT="9-1", AI_WORDS_MAX_CONTEXT="abc")   # garbage -> safe defaults
q2 = aicore.build_prompts("Graz", False, "x")[1]
assert "höchstens 2 Angaben" in q2 and "maximal 75 Wörter" in q2 and "bis 4 klaren" in q2.replace("9 bis 9", "x") or "klaren Sätzen" in q2
assert aicore.output_token_budget(50) == 220 and aicore.output_token_budget(90) == 288 and aicore.output_token_budget(400) == 1280
for k in ("AI_CONTEXT_MAX", "AI_SENTENCES_CONTEXT", "AI_WORDS_MAX_CONTEXT", "AI_SENTENCES", "AI_WORDS_MAX"):
    os.environ.pop(k, None)
print("PASS: prompt gets the extra context only when given")

# ---------------------------------------------------------------- 5. through the real AI router
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)
seen = {}
class Cfg:
    providers = ["ollama"]; ollama_url = "http://fake"; ollama_model = "m"; google_api_key = google_model = ""
aicore._PROVIDER_CALLERS["ollama"] = lambda b64, sp, prompt, cfg: (seen.setdefault("prompt", prompt) and "Eine Frau geht spazieren.", None)

def resolve_job(resp, prefix):
    src = resp.text
    while True:
        m = re.search(rf'hx-get="({prefix}/progress/[0-9a-f]+)"', src)
        if not m: return src
        time.sleep(0.05)
        src = client.get(m.group(1)).text
        if "progress-panel" not in src: return src

def hidden(src, name):
    m = re.search(rf"name=\"{name}\"\s+value='(.*?)'", src, re.S) or re.search(rf'name="{name}"\s+value="(.*?)"', src, re.S)
    assert m, name
    return html.unescape(m.group(1))

from app.core import settings
settings.load_ai_settings = lambda: Cfg()
r = client.post("/ai/browse/start", data={"path": TRIP, "root_path": MEDIA_ROOT, "depth": 0, "type_filter": "IMAGE",
                                          "overwrite_existing": True, "embed_in_image": False})
scan = resolve_job(r, "/ai/browse")
rows = json.loads(hidden(scan, "all_scanned_rows"))
sel = [x["path"] for x in rows if x["path"] == IMG]
assert sel, rows
g = client.post("/ai/generate/start", data={"selected": sel, "all_scanned_rows": hidden(scan, "all_scanned_rows"),
                                            "embed_in_image": False, "path": TRIP, "root_path": MEDIA_ROOT})
out = resolve_job(g, "/ai/generate")
assert "Weitere Kontexte" in out and "Tageszeit: Vormittag" in out and "Uhrzeit: 10:00" in out, out[:1500]
assert "Weitere gesicherte Rahmeninformationen" in seen["prompt"] and "Wo wars" in seen["prompt"]
assert 'Ort muss dafür NICHT' in seen["prompt"] or "NICHT" in seen["prompt"]       # the place instruction is still there
print("PASS: AI-Beschreibung router passes the context to the model and shows it in the list")

# ---------------------------------------------------------------- 6. info page knows the new variables
names = {n for _, _, items in infocore.ENV_GROUPS for n, _, _ in items}
assert {"GEO_SERVICE_URL", "GEO_SERVICE_TOKEN", "GEO_SERVICE_TIMEOUT", "AI_CONTEXT"} <= names
assert any(t["name"] == "Geo-Service" and t["url"].endswith("/health") for t in infocore.service_targets())
os.environ["GEO_SERVICE_TOKEN"] = "sekret-xyz"
assert "sekret-xyz" not in client.get("/info/").text and "sekret-xyz" not in client.get("/info/report.txt").text
print("PASS: info page lists the new settings, token stays masked")

svc.stop(); photon.stop()
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\nALL GEO-SERVICE BRIDGE CHECKS PASSED")
