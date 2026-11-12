"""PWA page /nearby: routes, manifest, service worker, and (headless Chromium) the location button.   python tests/test_nearby.py"""
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


w = photon_world()
srv, base = serve(w, tile_url="http://127.0.0.1:9/{z}/{x}/{y}.png", tile_attribution="© test", tile_maxzoom=18)
try:
    page = urllib.request.urlopen(base + "/nearby").read().decode()
    check("page served with injected map config", '"tileUrl": "http://127.0.0.1:9/{z}/{x}/{y}.png"' in page and "/*GEO_UI_CONFIG*/{}" not in page)
    check("page shows reverse-geocoded location details of the chosen point", 'id="loc"' in page and 'api("/reverse"' in page and "showLoc(r.center)" in page)
    man = json.loads(urllib.request.urlopen(base + "/nearby.webmanifest").read())
    check("manifest installable", man["display"] == "standalone" and man["start_url"] == "/nearby" and man["icons"])
    sw = urllib.request.urlopen(base + "/nearby-sw.js")
    check("service worker served for scope /", sw.status == 200 and sw.headers.get("Service-Worker-Allowed") == "/")
    check("icon served", urllib.request.urlopen(base + "/nearby-icon.svg").status == 200)
    for path, mt in (("/favicon.ico", "image/x-icon"), ("/favicon.svg", "image/svg+xml"), ("/apple-touch-icon.png", "image/png"),
                     ("/icon-192.png", "image/png"), ("/icon-512.png", "image/png")):
        r = urllib.request.urlopen(base + path)
        check(f"{path} served", r.status == 200 and r.headers.get("Content-Type", "").startswith(mt) and len(r.read()) > 500)
    check("pages link the icon", 'rel="icon"' in page and 'rel="apple-touch-icon"' in page)
    check("manifest has PNG icons", any(i["src"] == "/icon-512.png" for i in man["icons"]))
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP browser part: playwright missing")
    else:
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            ctx = br.new_context(geolocation={"latitude": 47.0707, "longitude": 15.4395}, permissions=["geolocation"])
            pg = ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(base + "/nearby")
            pg.click("#loc")
            pg.wait_for_selector("li.s")
            names = pg.locator("li.s h3").all_inner_texts()
            check("location button lists sights", "Uhrturm" in names and "Kunsthaus" in names and "Schöckl" not in names, names)
            check("distance, direction and details shown", "m" in pg.locator("li.s .meta").first.inner_text() and pg.locator("li.s a[href^='geo:']").count() >= 1)
            # layout: landscape desktop = list left, map fills the rest of the screen; portrait = full-width map above the list
            vw = pg.evaluate("innerWidth"); vh = pg.evaluate("innerHeight")
            mb = pg.locator("#map").bounding_box()
            check("wide landscape: map fills the right side, full height", mb["x"] > 300 and abs(mb["x"] + mb["width"] - vw) < 2 and abs(mb["height"] - vh) < 2, mb)
            pg.set_viewport_size({"width": 800, "height": 1100})
            pg.wait_for_timeout(300)
            mb = pg.locator("#map").bounding_box()
            check("portrait: map uses the full width", mb["width"] >= 800 - 40 and mb["x"] < 30 and mb["height"] > 300, mb)
            pg.set_viewport_size({"width": 1280, "height": 720})
            pg.wait_for_timeout(300)
            # a point on the map: right click, Shift+click and key P each start a search there
            pg.wait_for_selector("#map .leaflet-container, #map.leaflet-container")
            box = pg.locator("#map").bounding_box()
            pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            pg.click("#map", button="right", position={"x": box["width"] / 2, "y": box["height"] / 2})
            pg.wait_for_function("document.querySelector('#status').textContent.includes('Treffer') || document.querySelector('#status').textContent.includes('Nichts')")
            check("right click sets the point (draggable pin)", pg.locator(".leaflet-marker-draggable").count() == 1)
            pg.fill("#q", "47.0707, 15.4395")
            pg.click("#go")
            pg.wait_for_function("document.querySelector('#status').textContent.includes('47.0707')")
            check("coordinates in the search box are a position", True)
            pg.click("body", position={"x": 5, "y": 5})
            pg.mouse.move(box["x"] + 40, box["y"] + 40)
            pg.keyboard.press("p")
            pg.wait_for_timeout(400)
            pg.wait_for_function("document.querySelector('#status').textContent.includes('Treffer') || document.querySelector('#status').textContent.includes('Nichts')")
            check("key P sets a point under the mouse", pg.locator("#q").input_value() == "")
            pg.select_option("#sort", "distance")
            pg.wait_for_function("document.querySelector('#status').textContent.includes('Treffer')")
            check("no page errors", not errors, errors)
            br.close()
finally:
    pass

print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall nearby checks passed")
sys.exit(1 if FAILS else 0)
