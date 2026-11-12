"""Override editor GUI, driven in a real headless browser against the real app on a local port.
python tests/test_ui.py   (needs playwright + chromium, else SKIP)"""
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
from fakes import Resp, World, photon_graz, serve  # noqa: E402


def nominatim(url):
    if "/search" in url:
        return Resp(200, [{"osm_type": "way", "osm_id": 1, "lat": "48.14543", "lon": "16.32572", "name": "Volksschule Musterdorf",
                           "class": "amenity", "type": "school", "display_name": "Volksschule Musterdorf, Musterdorf",
                           "address": {"country_code": "at", "country": "Österreich", "state": "Niederösterreich"}}])
    return Resp(200, {"name": "Veranstaltungshalle", "lat": "48.14543", "lon": "16.32572", "osm_type": "way", "osm_id": 1,
                      "class": "amenity", "type": "community_centre",
                      "address": {"road": "Beispielgasse", "house_number": "20", "postcode": "2552", "country_code": "at",
                                  "country": "Österreich", "state": "Niederösterreich"}})


w = World()
w.on("nominatim", nominatim)
w.on("photon", photon_graz)
w.on("api.open-meteo.com/v1/elevation", lambda u: Resp(200, {"elevation": [183.0]}))
work = tempfile.mkdtemp()
srv, base = serve(w, srtm_dir=os.path.join(work, "srtm"), gn_search_order=["nominatim"])
ovfile = json.loads(urllib.request.urlopen(base + "/overrides").read())["file"]
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def get(path):
    return json.loads(urllib.request.urlopen(base + path).read())


try:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(base + "/ui")
        check("root redirects to the GUI", pg.title() == "Geo-Overrides")
        check("editor hidden at first, empty list shown", pg.is_hidden("#editor") and pg.is_visible("#empty"))

        # 1. address search -> editor opens with the ORIGINAL values
        pg.fill("#q", "Volksschule Musterdorf")
        pg.click("#searchform button")
        pg.wait_for_selector("#editor:not([hidden])")
        val = lambda grid, key: pg.input_value(f"#{grid} input[data-key='{key}']")
        check("address looked up (original placename)", val("g-address", "placename") == "Veranstaltungshalle", val("g-address", "placename"))
        check("street/postcode prefilled", val("g-address", "street") == "Beispielgasse" and val("g-address", "postalcode") == "2552")
        check("reverse part prefilled", val("g-reverse", "city") == "Graz" and val("g-reverse", "country") == "Österreich")
        check("elevation prefilled from the fallback", val("g-elevation", "elevation") == "183", val("g-elevation", "elevation"))
        check("coordinates from the hit", abs(float(pg.input_value("#lat")) - 48.14543) < 1e-6)
        check("timezone shown", "Europe/Vienna" in pg.inner_text("#tzinfo"), pg.inner_text("#tzinfo"))

        # 2. override parts
        pg.fill("#g-address input[data-key='placename']", "Volksschule")
        pg.fill("#g-reverse input[data-key='locality']", "Volksschule Musterdorf")
        pg.fill("#g-elevation input[data-key='elevation']", "215")
        check("changed fields are highlighted", pg.eval_on_selector("#g-address input[data-key='placename']", "e => e.classList.contains('changed')")
              and not pg.eval_on_selector("#g-address input[data-key='street']", "e => e.classList.contains('changed')"))
        pg.fill("#note", "OSM: Veranstaltungshalle")
        pg.fill("#radius", "40")

        if os.environ.get("UI_SHOT"):
            pg.set_viewport_size({"width": 1100, "height": 900})
            pg.screenshot(path=os.environ["UI_SHOT"], full_page=True)
            pg.wait_for_timeout(800)          # let the smooth scroll settle before clicking

        # 3. save
        pg.click("#save")
        try:
            pg.wait_for_selector("#savemsg.ok", timeout=5000)
        except Exception:
            print("savemsg:", repr(pg.inner_text("#savemsg")))
            raise
        saved = json.load(open(ovfile))
        check("file written with ONLY the changed fields", len(saved) == 1 and saved[0]["address"] == {"placename": "Volksschule"}
              and saved[0]["reverse"] == {"locality": "Volksschule Musterdorf"} and saved[0]["elevation"] == 215
              and saved[0]["radius_m"] == 40 and saved[0]["note"] == "OSM: Veranstaltungshalle", saved)
        check("override is live", get("/addressJSON?lat=48.14543&lng=16.32572")["address"]["placename"] == "Volksschule"
              and get("/srtm3JSON?lat=48.14543&lng=16.32572")["srtm3"] == 215)
        check("list shows the entry", pg.locator("#list tr").count() == 1 and "Volksschule" in pg.inner_text("#list"))

        # 4. coordinates input; existing override is preloaded, originals still the raw ones
        pg.fill("#q", "48.14543, 16.32572")
        pg.click("#searchform button")
        pg.wait_for_function("document.querySelector('#searchmsg').textContent.includes('Bestehender Override')")
        check("existing override preloaded + highlighted", val("g-address", "placename") == "Volksschule"
              and pg.eval_on_selector("#g-address input[data-key='placename']", "e => e.classList.contains('changed')")
              and "Veranstaltungshalle" in pg.inner_text("#g-address .orig >> nth=3"), pg.inner_text("#g-address"))
        # reset one field -> it is dropped from the override on save
        pg.click("#g-reverse input[data-key='locality'] + button")
        pg.click("#save")
        pg.wait_for_selector("#savemsg.ok")
        saved = json.load(open(ovfile))
        check("reset field removed on re-save (same id replaced)", len(saved) == 1 and "reverse" not in saved[0] and saved[0]["address"], saved)

        # 5. nothing changed -> refuses; edit + delete via list
        pg.click("#clear")
        pg.click("#save")
        check("nothing to save is reported", "Nichts geändert" in pg.inner_text("#savemsg"))
        pg.click("#list button:has-text('Bearbeiten')")
        pg.wait_for_function("document.querySelector('#searchmsg').textContent.includes('geladen')")
        pg.click("#list button:has-text('Löschen')")
        pg.wait_for_function("document.querySelector('#list').children.length === 0")
        check("delete via list", json.load(open(ovfile)) == [] and get("/addressJSON?lat=48.14543&lng=16.32572")["address"]["placename"] == "Veranstaltungshalle")
        check("no JavaScript errors", not errors, errors)
        br.close()
finally:
    srv.should_exit = True

print("\n%s" % ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
