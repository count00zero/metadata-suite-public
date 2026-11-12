"""/radar: page with injected config, style URL derived from GEO_TILE_URL, vendored MapLibre served, no CDN.   python tests/test_radar_page.py"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import serve  # noqa: E402
from test_sights import photon_world  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def cfg_of(page):
    a = page.index("window.GEO_RADAR = ") + len("window.GEO_RADAR = ")
    return json.loads(page[a:page.index(";</script>", a)])


w = photon_world()
srv, base = serve(w, tile_url="http://192.168.1.33:8777/styles/streets/{z}/{x}/{y}.png", tile_attribution="© test", home_lat=48.159, home_lon=16.332)
try:
    page = urllib.request.urlopen(base + "/radar").read().decode()
    c = cfg_of(page)
    check("config injected, no placeholder left", "/*GEO_RADAR_CONFIG*/" not in page and c["radarColor"] == 2 and c["radarMaxZoom"] == 7)
    check("style URL derived from GEO_TILE_URL", c["styleUrl"] == "http://192.168.1.33:8777/styles/streets/style.json", c)
    check("opens at home ([lng, lat])", c.get("center") == [16.332, 48.159], c)
    check("RainViewer-compatible API default", c["radarApi"].endswith("/public/weather-maps.json"))
    check("no CDN: MapLibre is served locally", "cdnjs" not in page and "/ui-assets/maplibre-gl/maplibre-gl.js" in page)
    js = urllib.request.urlopen(base + "/ui-assets/maplibre-gl/maplibre-gl.js")
    check("MapLibre JS + CSS served", js.status == 200 and len(js.read()) > 100000
          and urllib.request.urlopen(base + "/ui-assets/maplibre-gl/maplibre-gl.css").status == 200)
finally:
    srv.should_exit = True

srv, base = serve(w, tile_url="http://x/{z}/{x}/{y}.png", radar_style_url="https://maps.example.org/style.json", radar_api="http://192.168.1.33:8080/public/weather-maps.json")
try:
    c = cfg_of(urllib.request.urlopen(base + "/radar").read().decode())
    check("explicit style URL and own radar server win", c["styleUrl"] == "https://maps.example.org/style.json" and c["radarApi"].startswith("http://192.168.1.33:8080"), c)
finally:
    srv.should_exit = True

# browser part: mocked style + radar API + tiles (the real tileserver / RainViewer are not reachable here)
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("SKIP browser part: playwright missing")
else:
    import base64
    PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    STYLE = {"version": 8, "sources": {}, "layers": [{"id": "bg", "type": "background", "paint": {"background-color": "#dde"}}]}
    API = {"host": "http://radar.test", "radar": {"past": [{"time": 1790000000 + 600 * i, "path": f"/v2/radar/{i}"} for i in range(4)]}}
    srv, base = serve(w, radar_style_url="http://style.test/style.json", radar_api="http://radar.test/public/weather-maps.json", home_lat=48.159, home_lon=16.332)
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(args=["--use-gl=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
            pg = b.new_page(viewport={"width": 420, "height": 800})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.route("http://style.test/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(STYLE), headers={"Access-Control-Allow-Origin": "*"}))
            pg.route("http://radar.test/public/**", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(API), headers={"Access-Control-Allow-Origin": "*"}))
            pg.route("http://radar.test/v2/**", lambda r: r.fulfill(status=200, content_type="image/png", body=PNG, headers={"Access-Control-Allow-Origin": "*"}))
            pg.goto(base + "/radar")
            try:
                pg.wait_for_function("document.getElementById('slider').max === '3'", timeout=15000)
                ok = True
            except Exception:
                ok = False
            check("radar frames loaded into the slider", ok, pg.inner_text("#status"))
            check("time shown", pg.inner_text("#time").strip() not in ("", "–"))
            pg.click("#play"); pg.fill("#slider", "1") if False else None
            pg.click("#now")
            check("'Jetzt' jumps to the newest frame", pg.input_value("#slider") == "3")
            check("no JavaScript errors", not errs, errs)
            b.close()
    finally:
        srv.should_exit = True

print("\nFAILED: %s" % FAILS if FAILS else "\nALL OK")
sys.exit(1 if FAILS else 0)
