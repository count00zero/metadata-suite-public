"""Geo-Overrides embedded in the suite: nav pill on every page, proxy allow-list + token handling, and the editor
(real geo-service as a subprocess, suite app on a local port, headless browser).  python tests/test_geo_overrides_embed.py"""
import glob
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("SKIP: playwright missing")
    sys.exit(0)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def http(url, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# --- menu order on every module page: GPS Sidecar, GPS füllen, ..., Kontext nachfüllen (right before AI-Beschreibung), Collage, Datei-Zeitstempel; Geo-Overrides is in it
for p in glob.glob(os.path.join(ROOT, "app", "modules", "*", "templates", "index.html")):
    s = open(p, encoding="utf-8").read()
    pos = [max(s.find(f">{t}</a>"), s.find(f">{t}</span>")) for t in ("GPS füllen", "Kontext nachfüllen", "AI-Beschreibung", "Collage", "Datei-Zeitstempel")]
    check(f"menu order: {os.path.basename(os.path.dirname(os.path.dirname(p)))}",
          all(x != -1 for x in pos) and pos == sorted(pos) and "/geooverrides/" in s or "geo_overrides" in p and pos == sorted(pos), pos)

# --- geo-service as a real subprocess with a token
work = tempfile.mkdtemp()
gport = free_port()
env = dict(os.environ, GEO_CACHE_DB=os.path.join(work, "c.sqlite"), ELEVATION_PROVIDER="none", GEO_SRTM_PRELOAD="none",
           GEO_SERVICE_TOKEN="tk", GEO_TILE_URL="http://127.0.0.1:9/{z}/{x}/{y}.png", PYTHONPATH=os.path.join(ROOT, "geo-service"), PHOTON_SERVER="", NOMINATIM_SERVER="",
           PELIAS_SERVER="", MARITIME_SERVER="", WEATHER_PROVIDER="none")
svc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(gport), "--log-level", "warning"],
                       cwd=os.path.join(ROOT, "geo-service"), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            http(f"http://127.0.0.1:{gport}/health"); break
        except Exception:
            time.sleep(0.1)
    os.environ["GEO_SERVICE_URL"] = f"http://127.0.0.1:{gport}"
    os.environ["GEO_SERVICE_TOKEN"] = "tk"
    sys.path.insert(0, ROOT)
    import uvicorn
    from app.main import app
    sport = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=sport, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{sport}"

    code, body = http(f"{base}/geooverrides/editor")
    check("editor page served with API base injected", code == 200 and b'window.GEO_API_BASE="/geooverrides/api"' in body and b"Geo-Overrides" in body)
    check("service itself wants the token", http(f"http://127.0.0.1:{gport}/overrides")[0] == 401)
    check("proxy adds the token", http(f"{base}/geooverrides/api/overrides")[0] == 200)
    check("map assets through the proxy", http(f"{base}/geooverrides/api/ui-assets/leaflet/leaflet.js")[0] == 200
          and http(f"{base}/geooverrides/api/ui-assets/leaflet/images/marker-icon.png")[0] == 200)
    import json as _json
    code, body = http(f"{base}/geooverrides/api/context?lat=48.15&lon=16.33&time=1790000000&trip=false")
    cj = _json.loads(body) if code == 200 else {}
    check("details panel: /context through the proxy (daylight, moon, local time)", code == 200 and "daylight" in cj and "moon" in cj and cj.get("tz"), (code, body[:200]))
    check("editor page has the details panel", b'id="details"' in http(f"{base}/geooverrides/editor")[1] and b"showDetails" in http(f"{base}/geooverrides/editor")[1])
    check("proxy allow-list (stats not reachable)", http(f"{base}/geooverrides/api/stats")[0] == 404
          and http(f"{base}/geooverrides/api/weather/backfill", "POST")[0] == 404)

    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(f"{base}/geooverrides/")
        check("suite page shows the active pill after GPS füllen", pg.locator(".module-pill.active").inner_text() == "Geo-Overrides"
              and pg.locator("nav a:has-text('AI-Beschreibung')").count() == 1)
        fr = pg.frame_locator("iframe")
        fr.locator("#mapcard:not([hidden])").wait_for()
        check("map library + tiles config come through the proxy", fr.locator(".leaflet-container").count() == 1)
        fr.locator("#q").fill("48.1454285, 16.3257166")
        fr.locator("#searchform button").click()
        fr.locator("#editor:not([hidden])").wait_for()
        fr.locator("#g-address input[data-key='placename']").fill("Volksschule")
        fr.locator("#g-address input[data-key='street']").fill("Beispielgasse")
        fr.locator("#save").click()
        fr.locator("#savemsg.ok").wait_for()
        code, body = http(f"{base}/geooverrides/api/overrides")
        items = json.loads(body)["overrides"]
        check("saved through the proxy into the service's file", len(items) == 1 and items[0]["address"] == {"placename": "Volksschule", "street": "Beispielgasse"}, items)
        check("list inside the frame shows it", "Volksschule" in fr.locator("#list").inner_text())
        # token rejected by the service -> clear message instead of a token prompt in the browser
        os.environ["GEO_SERVICE_TOKEN"] = "wrong"
        code, body = http(f"{base}/geooverrides/api/overrides")
        check("wrong suite token -> readable error", code == 502 and b"Token" in body, (code, body))
        os.environ["GEO_SERVICE_TOKEN"] = "tk"
        check("no JavaScript errors", not errors, errors)
        br.close()

    svc.terminate(); svc.wait(timeout=10)
    code, body = http(f"{base}/geooverrides/editor")
    check("service down -> friendly page", code == 200 and b"nicht erreichbar" in body)
    code, body = http(f"{base}/geooverrides/api/overrides")
    check("service down -> 502 json", code == 502)
    os.environ["GEO_SERVICE_URL"] = ""
    check("service off -> says so", b"nicht aktiviert" in http(f"{base}/geooverrides/editor")[1] and b"nicht aktiviert" in http(f"{base}/geooverrides/")[1])
finally:
    svc.terminate()

print("\n%s" % ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
