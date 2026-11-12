"""Nominatim- and Photon-compatible facades against a fake upstream world.   python tests/test_compat.py"""
import os
import sys
import threading
import time
import json
import socket
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, install, make_settings  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

LAT, LON = 48.14543, 16.32572

NOM = {"place_id": 1, "osm_type": "way", "osm_id": 4711, "lat": "48.1454", "lon": "16.3257", "category": "amenity",
       "type": "community_centre", "name": "Veranstaltungshalle", "display_name": "x",
       "address": {"amenity": "Veranstaltungshalle", "house_number": "20", "road": "Beispielgasse", "village": "Musterdorf",
                   "county": "Bezirk Baden", "state": "Niederösterreich", "ISO3166-2-lvl4": "AT-3", "postcode": "2552",
                   "country": "Österreich", "country_code": "at"}}
PHOTON = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON, LAT]},
          "properties": {"osm_id": 4711, "osm_type": "W", "osm_key": "amenity", "osm_value": "community_centre",
                         "name": "Veranstaltungshalle", "housenumber": "20", "street": "Beispielgasse", "postcode": "2552",
                         "city": "Musterdorf", "county": "Bezirk Baden", "state": "Niederösterreich",
                         "country": "Österreich", "countrycode": "AT"}}]}


def world(nom=True, photon=True):
    w = World()
    w.on("http://nominatim/reverse", lambda u: Resp(200, NOM if nom else {"error": "Unable to geocode"}))
    w.on("http://nominatim/search", lambda u: Resp(200, [NOM] if nom else []))
    w.on("http://photon/reverse", lambda u: Resp(200, PHOTON if photon else {"type": "FeatureCollection", "features": []}))
    w.on("http://photon/api", lambda u: Resp(200, PHOTON if photon else {"type": "FeatureCollection", "features": []}))
    return w


def apps(w, **kw):
    from app.compat import create_nominatim_app, create_photon_app
    from app.main import create_app
    install(w)
    st = create_app(make_settings(**kw)).state.st
    return st, TestClient(create_nominatim_app(st)), TestClient(create_photon_app(st))


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        sys.exit(1)


# 1. Nominatim jsonv2 / json: the shape Traccar and the Geocoder gem read, one backend call thanks to the cache
w = world()
st, nom, pho = apps(w, chain=["nominatim", "photon"])
r = nom.get("/reverse", params={"format": "jsonv2", "lat": LAT, "lon": LON, "zoom": 18, "addressdetails": 1}).json()
a = r["address"]
check("jsonv2 address", a["road"] == "Beispielgasse" and a["house_number"] == "20" and a["postcode"] == "2552"
      and a["village"] == "Musterdorf" and a["state"] == "Niederösterreich" and a["country_code"] == "at", a)
check("jsonv2 fields", r["category"] == "amenity" and r["osm_type"] == "way" and r["osm_id"] == 4711
      and r["lat"].startswith("48.14") and r["display_name"].startswith("Veranstaltungshalle, 20, Beispielgasse"), r)
r2 = nom.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON}).json()
check("json has 'class', no 'category'", r2["class"] == "amenity" and "category" not in r2)
nom.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON})
check("backend asked once (cache)", w.count("http://nominatim/reverse") == 1, w.count("http://nominatim/reverse"))
g = nom.get("/reverse", params={"format": "geojson", "lat": LAT, "lon": LON}).json()
check("geojson coords [lon,lat]", abs(g["features"][0]["geometry"]["coordinates"][0] - 16.3257) < 1e-3)
x = ET.fromstring(nom.get("/reverse", params={"lat": LAT, "lon": LON}).text)
check("xml default", x.tag == "reversegeocode" and x.find("addressparts/road").text == "Beispielgasse")
check("addressdetails=0", "address" not in nom.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON, "addressdetails": 0}).json())
check("missing lat -> 400", nom.get("/reverse", params={"format": "json"}).status_code == 400)
check("status", nom.get("/status").text == "OK")

# 2. override (the 'Volksschule' case) is laid over, cache stays raw
st.overrides.upsert({"id": "vs", "lat": LAT, "lng": LON, "radius_m": 30,
                     "address": {"placename": "Volksschule", "street": "Beispielgasse", "houseNumber": "20"},
                     "reverse": {"locality": "Volksschule"}})
r = nom.get("/reverse", params={"format": "jsonv2", "lat": LAT, "lon": LON}).json()
check("override name", r["name"] == "Volksschule" and r["display_name"].startswith("Volksschule, 20"), r["display_name"])
p = pho.get("/reverse", params={"lat": LAT, "lon": LON}).json()["features"][0]["properties"]
check("override in photon too", p["name"] == "Volksschule", p)
st.overrides.delete("vs")
r = nom.get("/reverse", params={"format": "jsonv2", "lat": LAT, "lon": LON}).json()
check("override removed -> raw again", r["name"] == "Veranstaltungshalle")

# 2b. imported 'Zuhause' case: the override's street carries the number ("... 84"); the geocoder's own number (20) must go
st.overrides.upsert({"id": "home", "lat": LAT, "lng": LON, "radius_m": 30, "place": {"name": "Zuhause", "kind": "Zuhause"},
                     "reverse": {"street": "Beispielgasse 84", "city": "Musterdorf", "locality": "Zuhause"}})
r = nom.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON}).json()
check("override street split: number 84, street clean, upstream number gone",
      r["address"].get("house_number") == "84" and r["address"].get("road") == "Beispielgasse"
      and "20" not in r["display_name"] and "84, Beispielgasse" in r["display_name"], r["display_name"])
p = pho.get("/reverse", params={"lat": LAT, "lon": LON}).json()["features"][0]["properties"]
check("same in photon", p.get("housenumber") == "84" and p.get("street") == "Beispielgasse", p)
st.overrides.delete("home")

# 3. Photon format
f = pho.get("/reverse", params={"lon": LON, "lat": LAT, "lang": "de"}).json()
pr = f["features"][0]["properties"]
check("photon reverse", f["type"] == "FeatureCollection" and pr["city"] == "Musterdorf" and pr["countrycode"] == "AT"
      and pr["housenumber"] == "20" and pr["postcode"] == "2552" and pr["osm_type"] == "W" and pr["country"] == "Österreich", pr)
check("photon geometry [lon,lat]", f["features"][0]["geometry"]["coordinates"][0] == 16.3257 or abs(f["features"][0]["geometry"]["coordinates"][0] - 16.3257) < 1e-3)
check("photon /api", pho.get("/api", params={"q": "Musterdorf", "limit": 3}).json()["features"][0]["properties"]["city"] == "Musterdorf")
check("photon /api needs q", pho.get("/api").status_code == 400)
check("photon status", pho.get("/status").json()["status"] == "Ok")
s = nom.get("/search", params={"q": "Musterdorf", "format": "jsonv2", "limit": 2}).json()
check("nominatim /search", isinstance(s, list) and s[0]["name"] == "Veranstaltungshalle", s)

# 4. nothing found
w2 = World()
w2.on("http://nominatim/reverse", lambda u: Resp(200, {"error": "Unable to geocode"}))
w2.on("http://photon/reverse", lambda u: Resp(200, {"features": [], "type": "FeatureCollection"}))
w2.on("http://pelias/", lambda u: Resp(200, {"features": []}))
st2, nom2, pho2 = apps(w2)
check("not found (nominatim)", nom2.get("/reverse", params={"format": "jsonv2", "lat": 0.1, "lon": 0.1}).json() == {"error": "Unable to geocode"})
check("not found (photon)", pho2.get("/reverse", params={"lat": 0.1, "lon": 0.1}).json()["features"] == [])

# 5. only Photon behind it: the Nominatim port still answers
w3 = world(nom=False)
st3, nom3, pho3 = apps(w3, nominatim="", chain=["photon"])
r = nom3.get("/reverse", params={"format": "jsonv2", "lat": LAT, "lon": LON}).json()
check("nominatim port on photon backend", r["address"]["road"] == "Beispielgasse" and r["address"]["postcode"] == "2552"
      and r["address"]["city"] == "Musterdorf", r)

# 6. only an override knows the spot
st2.overrides.upsert({"id": "x", "lat": 0.1, "lng": 0.1, "radius_m": 50, "address": {"placename": "Insel", "countryCode": "AT"}})
check("override alone answers", nom2.get("/reverse", params={"format": "jsonv2", "lat": 0.1, "lon": 0.1}).json().get("name") == "Insel")

# 7. optional token
w4 = world()
st4, nom4, pho4 = apps(w4, token="sek", compat_auth=True)
check("auth: 401", nom4.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON}).status_code == 401)
check("auth: ?key=", nom4.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON, "key": "sek"}).status_code == 200)
check("auth: header", pho4.get("/reverse", params={"lat": LAT, "lon": LON}, headers={"X-Api-Key": "sek"}).status_code == 200)
st5, nom5, _ = apps(world(), token="sek")
check("open by default even with a token", nom5.get("/reverse", params={"format": "json", "lat": LAT, "lon": LON}).status_code == 200)

# 8. really on its own port (urllib), through serve.py's building blocks
import uvicorn  # noqa: E402
from app.compat import create_photon_app  # noqa: E402
s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
srv = uvicorn.Server(uvicorn.Config(create_photon_app(st), host="127.0.0.1", port=port, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(100):
    if srv.started:
        break
    time.sleep(0.05)
j = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/reverse?lon={LON}&lat={LAT}", timeout=5))
check("real HTTP on its own port", j["features"][0]["properties"]["city"] == "Musterdorf")
srv.should_exit = True
import app.serve  # noqa: E402,F401  (imports cleanly)
print("\nall compat tests passed")
