"""The override editor's map opens on 'Zuhause' (GEO_HOME_LAT/LON, else the place of kind Zuhause).   python tests/test_ui_home.py"""
import json
import os
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from playwright.sync_api import sync_playwright
except Exception:
    print("SKIP: playwright missing")
    sys.exit(0)
from fakes import World, serve  # noqa: E402

TILE = "http://127.0.0.1:9/{z}/{x}/{y}.png"


def cfg_of(base):
    html = urllib.request.urlopen(base + "/ui").read().decode()
    i = html.index("const CFG") if "const CFG" in html else -1
    return html, i


def center(base):
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1100, "height": 900})
        pg.goto(base + "/ui")
        pg.wait_for_selector("#mapcard:not([hidden])")
        pg.wait_for_function("window.geoMap")
        pg.wait_for_timeout(300)
        r = pg.evaluate("(() => { const c = geoMap.getCenter(); return [c.lat, c.lng, geoMap.getZoom()]; })()")
        br.close()
        return r


# 1. GEO_HOME_LAT/LON
srv, base = serve(World(), srtm_dir=os.path.join(tempfile.mkdtemp(), "srtm"), tile_url=TILE, home_lat=48.159136, home_lon=16.332689)
html = urllib.request.urlopen(base + "/ui").read().decode()
assert '"home": [48.159136, 16.332689]' in html, "home not injected"
c = center(base)
assert abs(c[0] - 48.159136) < 0.01 and abs(c[1] - 16.332689) < 0.01 and c[2] == 15, c
srv.should_exit = True

# 2. no env home: the place of kind 'Zuhause' in Meine Orte
srv, base = serve(World(), srtm_dir=os.path.join(tempfile.mkdtemp(), "srtm"), tile_url=TILE)
assert '"home"' not in urllib.request.urlopen(base + "/ui").read().decode()
ovfile = json.loads(urllib.request.urlopen(base + "/overrides").read())["file"]
os.makedirs(os.path.dirname(ovfile), exist_ok=True)
json.dump({"version": 1, "overrides": [{"id": "h1", "lat": 47.07, "lng": 15.43, "radius_m": 30,
                                         "place": {"name": "Daheim", "kind": "Zuhause"}}]}, open(ovfile, "w"))
import time; time.sleep(1.2)       # file watcher / mtime reload
html = urllib.request.urlopen(base + "/ui").read().decode()
assert '"home": [47.07, 15.43]' in html, [l for l in html.splitlines() if "tileUrl" in l][:1]
c = center(base)
assert abs(c[0] - 47.07) < 0.01 and abs(c[1] - 15.43) < 0.01 and c[2] == 15, c
srv.should_exit = True
print("PASS")
