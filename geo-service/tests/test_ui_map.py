"""Map in the override editor (Leaflet + raster tile URL), driven in headless Chromium.   python tests/test_ui_map.py"""
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
    return Resp(200, {"name": "Veranstaltungshalle", "lat": "47.5", "lon": "14.5", "osm_type": "way", "osm_id": 1,
                      "class": "amenity", "type": "community_centre",
                      "address": {"road": "Beispielgasse", "house_number": "20", "postcode": "2552", "country_code": "at",
                                  "country": "Österreich", "state": "Niederösterreich"}})


w = World()
w.on("nominatim", nominatim)
w.on("photon", photon_graz)
w.on("api.open-meteo.com/v1/elevation", lambda u: Resp(200, {"elevation": [183.0]}))
work = tempfile.mkdtemp()
srv, base = serve(w, srtm_dir=os.path.join(work, "srtm"), tile_url="http://127.0.0.1:9/{z}/{x}/{y}.png",
                  tile_attribution="© test", tile_maxzoom=18)
ovfile = json.loads(urllib.request.urlopen(base + "/overrides").read())["file"]
FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def get(path):
    return json.loads(urllib.request.urlopen(base + path).read())


try:
    check("assets are served", urllib.request.urlopen(base + "/ui-assets/leaflet/leaflet.js").status == 200
          and urllib.request.urlopen(base + "/ui-assets/leaflet/images/marker-icon.png").status == 200)
    check("config is injected into /ui", '"tileUrl": "http://127.0.0.1:9/{z}/{x}/{y}.png"' in urllib.request.urlopen(base + "/ui").read().decode())
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1100, "height": 1000})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: d.accept())
        pg.goto(base + "/ui")
        pg.wait_for_selector("#mapcard:not([hidden])")
        pg.wait_for_function("window.geoMap")
        check("map card shown with Leaflet", pg.evaluate("typeof L === 'object' && !!document.querySelector('.leaflet-container')"))

        # 1. click -> lookup at that spot, marker + circle
        center = pg.evaluate("(() => { const c = geoMap.getCenter(); return [c.lat, c.lng]; })()")
        pg.click("#map")
        pg.wait_for_selector("#editor:not([hidden])")
        pg.wait_for_function("document.querySelector('#lat').value !== ''")
        lat, lng = float(pg.input_value("#lat")), float(pg.input_value("#lng"))
        check("click sets the point", abs(lat - center[0]) < 0.02 and abs(lng - center[1]) < 0.02, (lat, lng, center))
        check("marker and circle drawn", pg.locator(".leaflet-marker-icon").count() == 1 and pg.locator(".leaflet-overlay-pane path").count() >= 1)
        check("address looked up from the click", pg.input_value("#g-address input[data-key='street']") == "Beispielgasse")

        # 2. radius from the map: click 120 px to the right of the marker
        pg.click("#m-radius")
        box = pg.locator("#map").bounding_box()
        mx, my = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        pg.mouse.click(mx + 120, my)
        want = pg.evaluate("(() => { const m = geoMap.containerPointToLatLng([geoMap.getSize().x/2 + 120, geoMap.getSize().y/2]); "
                           "return geoMap.distance(geoMap.getCenter(), m); })()")
        got = float(pg.input_value("#radius"))
        check("radius set from a map click", abs(got - want) < max(3, want * 0.03), (got, want))
        pg.fill("#radius", "60"); pg.dispatch_event("#radius", "change")

        # 3. polygon: 4 corners around the marker
        pg.click("#m-poly")
        for dx, dy in ((-70, -50), (70, -50), (70, 50), (-70, 50)):
            pg.mouse.click(mx + dx, my + dy)
        pg.click("#m-polydone")
        check("polygon drawn", "4 Punkten" in pg.inner_text("#areainfo") and pg.is_disabled("#radius"), pg.inner_text("#areainfo"))
        pg.fill("#g-address input[data-key='placename']", "Testareal")
        if os.environ.get("UI_SHOT"):
            pg.locator("#mapcard").scroll_into_view_if_needed(); pg.wait_for_timeout(800)
            pg.screenshot(path=os.environ["UI_SHOT"])
        pg.click("#save")
        pg.wait_for_selector("#savemsg.ok")
        saved = json.load(open(ovfile))
        check("polygon saved with 4 corners", len(saved) == 1 and len(saved[0]["polygon"]) == 4 and saved[0]["address"] == {"placename": "Testareal"}, saved)
        cl = sum(p[0] for p in saved[0]["polygon"]) / 4, sum(p[1] for p in saved[0]["polygon"]) / 4
        inside = get(f"/addressJSON?lat={cl[0] + 0.0001}&lng={cl[1] - 0.0001}")["address"]["placename"]
        c0 = saved[0]["polygon"][0]
        outside = get(f"/addressJSON?lat={c0[0] + 0.01}&lng={c0[1] - 0.01}")["address"]["placename"]
        check("inside the polygon the override applies, outside it does not", inside == "Testareal" and outside == "Veranstaltungshalle", (inside, outside))
        check("overview layer draws the saved area", pg.locator(".leaflet-overlay-pane path").count() >= 2)

        # 4. clicking inside the saved area loads the override again
        pg.locator("#map").scroll_into_view_if_needed()
        pg.wait_for_timeout(800)
        box = pg.locator("#map").bounding_box()
        cp = pg.evaluate("([a, b]) => { const q = geoMap.latLngToContainerPoint([a, b]); return [q.x, q.y]; }", list(cl))   # layout-independent: relative to the polygon, not to the box centre
        pg.mouse.click(box["x"] + cp[0] + 30, box["y"] + cp[1] + 10)
        pg.wait_for_function("document.querySelector('#searchmsg').textContent.includes('Bestehender Override')")
        check("click inside an area opens that override", pg.input_value("#g-address input[data-key='placename']") == "Testareal"
              and "4 Punkten" in pg.inner_text("#areainfo"))
        # 5. remove the area -> circle again
        pg.click("#m-polyclear")
        check("area removed -> radius editable again", not pg.is_disabled("#radius") and pg.locator(".leaflet-marker-icon").count() == 1)
        # 6. editing ONLY the area of an existing override must save (its fields may equal the geocoder's raw values)
        anchor = saved[0]
        urllib.request.urlopen(urllib.request.Request(base + "/overrides/" + saved[0]["id"], method="DELETE"))
        urllib.request.urlopen(urllib.request.Request(base + "/overrides", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"id": "same-as-raw", "lat": anchor["lat"], "lng": anchor["lng"], "radius_m": 40,
                             "address": {"placename": "Veranstaltungshalle"}}).encode()))
        pg.reload()
        pg.wait_for_selector("#list tr")
        pg.click("#list button:has-text('Bearbeiten')")
        pg.wait_for_function("document.querySelector('#searchmsg').textContent.includes('geladen')")
        pg.fill("#radius", "99"); pg.dispatch_event("#radius", "change")
        pg.click("#save")
        pg.wait_for_selector("#savemsg.ok")
        s2 = json.load(open(ovfile))
        check("only the radius changed -> saved, fields kept", len(s2) == 1 and s2[0]["radius_m"] == 99 and s2[0]["address"] == {"placename": "Veranstaltungshalle"}, s2)
        pg.click("#m-poly")
        pg.locator("#map").scroll_into_view_if_needed(); pg.wait_for_timeout(800)
        box = pg.locator("#map").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        for dx, dy in ((-60, -40), (60, -40), (0, 60)):
            pg.mouse.click(cx + dx, cy + dy)
        pg.click("#m-polydone")
        pg.click("#save")
        pg.wait_for_selector("#savemsg.ok")
        s3 = json.load(open(ovfile))
        check("only a new polygon -> saved, fields kept", len(s3[0].get("polygon", [])) == 3 and s3[0]["address"] == {"placename": "Veranstaltungshalle"}, s3)
        # ... but a field the user resets with the arrow button is still dropped (and then nothing is left)
        pg.locator("#editor").scroll_into_view_if_needed()
        pg.click("#g-address input[data-key='placename'] + button")
        pg.click("#save")
        check("reset of the only field -> nothing left to save", "Nichts" in pg.inner_text("#savemsg") or "kein Feld" in pg.inner_text("#savemsg"), pg.inner_text("#savemsg"))
        # a name under 'Mein Ort' alone is a valid entry (no field override needed)
        pg.fill("#pname", "Testhalle"); pg.fill("#pkind", "Familie"); pg.check("#ppoi")
        pg.click("#save")
        pg.wait_for_selector("#savemsg.ok")
        s4 = json.load(open(ovfile))
        check("Mein Ort alone saves", s4[0].get("place") == {"name": "Testhalle", "kind": "Familie", "poi": True} and "address" not in s4[0], s4)
        pg.reload(); pg.wait_for_selector("#list tr")
        check("list shows the place", "Testhalle" in pg.inner_text("#list"))
        check("no JavaScript errors", not errors, errors)
        br.close()
finally:
    srv.should_exit = True

print("\n%s" % ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
