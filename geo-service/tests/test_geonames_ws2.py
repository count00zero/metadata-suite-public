"""countryCode, countrySubdivision, geoCodeAddress, streetNameLookup, findNearestAddress, findNearbyStreetsOSM.
python tests/test_geonames_ws2.py"""
import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client  # noqa: E402

LAT, LNG = 47.9529, 16.2296

ADDR = {"osm_type": "way", "osm_id": 7, "lat": "47.9529", "lon": "16.2296", "category": "highway", "type": "residential",
        "name": "Beispielgasse", "display_name": "x",
        "address": {"road": "Beispielgasse", "house_number": "84", "village": "Musterdorf", "county": "Bezirk Baden",
                    "state": "Niederösterreich", "ISO3166-2-lvl4": "AT-3", "postcode": "2552", "country": "Österreich",
                    "country_code": "at"}}


def nominatim(url):
    q = parse_qs(urlparse(url).query)
    if "/reverse" in url:
        if q["lat"][0] == "0.0":                                           # open sea
            return Resp(200, {"error": "Unable to geocode"})
        return Resp(200, ADDR)
    if "/search" in url:
        if q.get("street") == ["Nirgends"] or q.get("q") == ["Nirgends"]:
            return Resp(200, [])
        other = dict(ADDR, osm_id=8, address=dict(ADDR["address"], road="Beispielgasse", village="Leobersdorf", postcode="2544"))
        shop = dict(ADDR, osm_id=9, category="shop", type="bakery", name="Bäckerei")
        return Resp(200, [ADDR, ADDR, other, shop])
    return Resp(404)


def photon(url):
    q = parse_qs(urlparse(url).query)
    assert q.get("osm_tag") == ["highway"], url
    return Resp(200, {"features": [
        {"geometry": {"coordinates": [16.2297, 47.9531]}, "properties": {"name": "Hauptstraße", "osm_key": "highway", "osm_value": "residential", "osm_id": 11, "extent": [16.2290, 47.9535, 16.2310, 47.9525]}},
        {"geometry": {"coordinates": [16.2296, 47.9530]}, "properties": {"name": "Hauptstraße", "osm_key": "highway", "osm_value": "residential", "osm_id": 12}},
        {"geometry": {"coordinates": [16.2300, 47.9540]}, "properties": {"name": "Gartengasse", "osm_key": "highway", "osm_value": "service", "osm_id": 13}},
        {"geometry": {"coordinates": [16.2301, 47.9541]}, "properties": {"name": "Kein Weg", "osm_key": "building", "osm_value": "yes", "osm_id": 14}},
        {"geometry": {"coordinates": [16.2302, 47.9542]}, "properties": {"osm_key": "highway", "osm_value": "footway", "osm_id": 15}},
    ]})


def world():
    w = World()
    w.on("http://nominatim", nominatim)
    w.on("http://photon", photon)
    return w


def test_country_code():
    c = client(world())
    r = c.get("/countryCode", params={"lat": LAT, "lng": LNG})
    assert r.text.strip() == "AT" and r.headers["content-type"].startswith("text/plain"), r.text
    j = c.get("/countryCodeJSON", params={"lat": LAT, "lng": LNG}).json()
    assert j["countryCode"] == "AT" and j["countryName"] and "languages" in j and j["distance"] == 0, j
    root = ET.fromstring(c.get("/countryCode", params={"lat": LAT, "lng": LNG, "type": "xml"}).text)
    assert root.find("country/countryCode").text == "AT"
    assert c.get("/countryCode", params={"lat": LAT, "lng": LNG, "type": "json"}).json()["countryCode"] == "AT"
    sea = c.get("/countryCode", params={"lat": 0.0, "lng": 0.0}).text
    assert sea.startswith("ERR:15:"), sea
    assert c.get("/countryCodeJSON", params={"lat": 0.0, "lng": 0.0}).json()["status"]["value"] == 15
    assert c.get("/countryCode", params={"lat": 95, "lng": 0}).text.startswith("ERR:12:")


def test_country_subdivision():
    c = client(world())
    j = c.get("/countrySubdivisionJSON", params={"lat": LAT, "lng": LNG}).json()
    assert j["countryCode"] == "AT" and j["adminName1"] == "Niederösterreich" and j["adminCode1"] == "03", j
    assert j["codes"] == [{"code": "03", "level": "1", "type": "ISO3166-2"}], j
    root = ET.fromstring(c.get("/countrySubdivision", params={"lat": LAT, "lng": LNG}).text)
    code = root.find("countrySubdivision/code")
    assert code.text == "03" and code.get("type") == "ISO3166-2" and code.get("level") == "1"
    assert c.get("/countrySubdivisionJSON", params={"lat": 0.0, "lng": 0.0}).json()["status"]["value"] == 15


def test_geocode_address_and_nearest_address():
    w = world()
    c = client(w)
    j = c.get("/geoCodeAddressJSON", params={"q": "Beispielgasse 84, Musterdorf"}).json()["address"]
    assert j["street"] == "Beispielgasse" and j["houseNumber"] == "84" and j["postalcode"] == "2552" and j["countryCode"] == "AT", j
    n = w.count("/search")
    c.get("/geoCodeAddressJSON", params={"q": "Beispielgasse 84, Musterdorf"})
    assert w.count("/search") == n, "second call comes from the cache"
    assert c.get("/geoCodeAddressJSON", params={"q": "Nirgends"}).json()["status"]["value"] == 15
    assert c.get("/geoCodeAddressJSON").json()["status"]["value"] == 12
    a = c.get("/findNearestAddressJSON", params={"lat": LAT, "lng": LNG}).json()
    b = c.get("/addressJSON", params={"lat": LAT, "lng": LNG}).json()
    assert a == b and a["address"]["street"] == "Beispielgasse", a
    assert ET.fromstring(c.get("/findNearestAddress", params={"lat": LAT, "lng": LNG}).text).find("address/street").text == "Beispielgasse"


def test_street_name_lookup():
    c = client(world())
    rows = c.get("/streetNameLookupJSON", params={"q": "Beispiel", "country": "AT"}).json()["address"]
    assert [(r["street"], r["placename"], r["postalcode"]) for r in rows] == [("Beispielgasse", "Musterdorf", "2552"), ("Beispielgasse", "Leobersdorf", "2544")], rows
    assert rows[0]["adminCode1"] == "03" and rows[0]["countryCode"] == "AT"
    assert len(c.get("/streetNameLookupJSON", params={"q": "Beispiel", "maxRows": 1}).json()["address"]) == 1
    assert c.get("/streetNameLookupJSON", params={"q": "Beispiel", "adminCode1": "99"}).json()["address"] == []
    assert c.get("/streetNameLookupJSON", params={"q": "Nirgends"}).json()["address"] == []
    assert c.get("/streetNameLookupJSON").json()["status"]["value"] == 12
    assert ET.fromstring(c.get("/streetNameLookup", params={"q": "Beispiel"}).text).find("address/street").text == "Beispielgasse"


def test_nearby_streets():
    c = client(world())
    rows = c.get("/findNearbyStreetsOSMJSON", params={"lat": LAT, "lng": LNG, "radius": 0.3}).json()["streetSegment"]
    names = [r["name"] for r in rows]
    assert names == ["Hauptstraße", "Gartengasse"], names                       # dedupe, no building, no nameless way
    assert rows[0]["wayId"] == "12" and rows[0]["highway"] == "residential" and float(rows[0]["distance"]) < float(rows[1]["distance"]), rows
    assert " " in rows[1]["line"] and "," in rows[1]["line"]
    assert len(c.get("/findNearbyStreetsOSMJSON", params={"lat": LAT, "lng": LNG, "maxRows": 1}).json()["streetSegment"]) == 1
    root = ET.fromstring(c.get("/findNearbyStreetsOSM", params={"lat": LAT, "lng": LNG}).text)
    assert root.find("streetSegment/name").text == "Hauptstraße"


def test_backends_down():
    w = World()
    c = client(w)
    for path, params in (("/countryCodeJSON", {"lat": LAT, "lng": LNG}), ("/countrySubdivisionJSON", {"lat": LAT, "lng": LNG}),
                         ("/findNearbyStreetsOSMJSON", {"lat": LAT, "lng": LNG}), ("/geoCodeAddressJSON", {"q": "x"})):
        v = c.get(path, params=params).json()["status"]["value"]
        assert v == 13, (path, v)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} GeoNames extra checks passed")
