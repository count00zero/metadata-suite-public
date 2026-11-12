"""GeoNames-compatible endpoints against a fake upstream world.   python tests/test_geonames.py"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import EMPTY_FEATURES, SERVER_ERROR, Resp, World, client  # noqa: E402

LAT, LNG = 47.9529, 16.2296          # Musterdorf


def pelias_places(url):
    if "layers=locality" in url:
        return Resp(200, {"features": [
            {"geometry": {"coordinates": [16.2330, 47.9500]},
             "properties": {"gid": "wof:locality:1", "layer": "locality", "name": "Musterdorf", "country": "Österreich",
                            "country_a": "AUT", "region": "Niederösterreich", "region_a": "NI", "county": "Bezirk Baden"}},
            {"geometry": {"coordinates": [16.2100, 47.9700]},
             "properties": {"gid": "wof:locality:2", "layer": "locality", "name": "Leobersdorf", "country": "Österreich",
                            "country_a": "AUT", "region": "Niederösterreich"}}]})
    if "layers=postalcode" in url:
        return Resp(200, {"features": [
            {"geometry": {"coordinates": [16.2300, 47.9530]},
             "properties": {"gid": "p:1", "layer": "postalcode", "name": "2552", "postalcode": "2552", "locality": "Musterdorf",
                            "country_a": "AUT", "region": "Niederösterreich"}},
            {"geometry": {"coordinates": [16.2200, 47.9600]},
             "properties": {"gid": "p:2", "layer": "postalcode", "name": "2544", "postalcode": "2544", "locality": "Leobersdorf",
                            "country_a": "AUT"}}]})
    return Resp(200, {"features": []})


def nominatim(url):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    if "/search" in url:
        return Resp(200, [{"osm_type": "node", "osm_id": 9, "lat": str(LAT), "lon": str(LNG), "name": "Musterdorf",
                           "class": "place", "type": "village", "address": {"country_code": "at", "country": "Österreich"}}])
    lat = float(q["lat"][0])
    code = "2552" if lat >= LAT else "2544"
    zoom = q.get("zoom", [""])[0]
    return Resp(200, {"osm_type": "relation" if zoom in ("12", "14") else "way", "osm_id": 5 if zoom in ("12", "14") else int(lat * 1e4),
                      "lat": q["lat"][0], "lon": q["lon"][0], "class": "place", "type": "village",
                      "name": "Musterdorf" if not q.get("accept-language") else "Musterdorf (de)",
                      "address": {"country_code": "at", "country": "Österreich" if not q.get("accept-language") else "Austria",
                                  "state": "Niederösterreich", "county": "Bezirk Baden", "town": "Musterdorf",
                                  "postcode": code, "ISO3166-2-lvl4": "AT-3"}})


MEDIUM = ["toponymName", "name", "lat", "lng", "geonameId", "countryCode", "countryName", "fcl", "fcode", "distance"]


def test_place_pelias_medium_order_json_and_xml():
    w = World(); w.on("http://pelias", pelias_places)
    c = client(w)
    j = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG}).json()
    g = j["geonames"]
    assert [r["name"] for r in g] == ["Musterdorf", "Leobersdorf"], g           # nearest first
    assert list(g[0]) == MEDIUM, list(g[0])
    assert g[0]["countryCode"] == "AT" and g[0]["fcl"] == "P" and g[0]["lat"] == "47.95000"
    assert isinstance(g[0]["geonameId"], int) and isinstance(g[0]["distance"], float)
    x = c.get("/findNearbyPlaceName", params={"lat": LAT, "lng": LNG, "maxRows": 1})
    root = ET.fromstring(x.text)
    assert root.tag == "geonames" and [e.tag for e in root.find("geoname")] == MEDIUM and len(root.findall("geoname")) == 1


def test_styles():
    w = World(); w.on("http://pelias", pelias_places)
    c = client(w)
    p = {"lat": LAT, "lng": LNG, "maxRows": 1}
    s = c.get("/findNearbyPlaceNameJSON", params={**p, "style": "SHORT"}).json()["geonames"][0]
    assert "countryName" not in s and "fcode" in s
    l = c.get("/findNearbyPlaceNameJSON", params={**p, "style": "LONG"}).json()["geonames"][0]
    assert {"fclName", "fcodeName", "population", "adminCode1", "adminName1"} <= set(l) and "adminName2" not in l, list(l)
    f = c.get("/findNearbyPlaceNameJSON", params={**p, "style": "FULL"}).json()["geonames"][0]
    assert "adminName2" in f and f["adminName2"] == "Bezirk Baden"


def test_place_fallbacks_and_lang():
    w = World(); w.on("http://pelias", EMPTY_FEATURES); w.on("http://nominatim", nominatim)
    c = client(w)
    r = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG, "lang": "en"}).json()["geonames"][0]
    assert r["toponymName"] == "Musterdorf" and r["name"] == "Musterdorf (de)" and r["countryName"] == "Austria", r
    assert w.count("zoom=12") == 2, "lang triggers one extra, localized lookup"
    w2 = World(); w2.on("http://pelias", SERVER_ERROR); w2.on("http://nominatim", SERVER_ERROR)
    w2.on("http://photon", lambda u: Resp(200, {"features": [{"geometry": {"coordinates": [LNG, LAT]},
        "properties": {"osm_type": "N", "osm_id": 3, "name": "Musterdorf", "countrycode": "AT", "country": "Österreich",
                       "osm_key": "place", "osm_value": "village"}}]}))
    r2 = client(w2).get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG}).json()["geonames"]
    assert r2[0]["name"] == "Musterdorf" and r2[0]["fcode"] == "PPL"


def test_ids_stable_and_cache():
    w = World(); w.on("http://pelias", pelias_places)
    c = client(w)
    a = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG})
    n = w.count("http://pelias")
    b = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG})
    assert b.headers.get("x-geo-cache") == "hit" and w.count("http://pelias") == n and a.json() == b.json()
    other = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT + 0.0001, "lng": LNG}).json()["geonames"]
    assert other[0]["geonameId"] == a.json()["geonames"][0]["geonameId"], "same place -> same synthetic id"


def test_postal_pelias_layer():
    w = World(); w.on("http://pelias", pelias_places); w.on("http://nominatim", nominatim)
    c = client(w)
    j = c.get("/findNearbyPostalCodesJSON", params={"lat": LAT, "lng": LNG, "radius": 5}).json()["postalCodes"]
    assert [r["postalcode"] for r in j] == ["2552", "2544"], j
    assert list(j[0]) == ["postalcode", "name", "countryCode", "lat", "lng", "adminCode1", "adminName1", "adminName2",
                          "adminName3", "distance"], list(j[0])
    assert j[0]["name"] == "Musterdorf" and j[0]["adminCode1"] == "03" and j[0]["adminName2"] == "Bezirk Baden"
    root = ET.fromstring(c.get("/findNearbyPostalCodes", params={"lat": LAT, "lng": LNG}).text)
    assert [e.tag for e in root] == ["code", "code"]
    far = c.get("/findNearbyPostalCodesJSON", params={"lat": LAT, "lng": LNG, "radius": 0.2}).json()["postalCodes"]
    assert [r["postalcode"] for r in far] == ["2552"], "radius filter"


def test_postal_nominatim_ring_fallback_and_center_lookup():
    w = World(); w.on("http://pelias", EMPTY_FEATURES); w.on("http://nominatim", nominatim)
    c = client(w, gn_probe_rings=1, gn_probe_points=8)
    j = c.get("/findNearbyPostalCodesJSON", params={"lat": LAT, "lng": LNG, "radius": 3, "maxRows": 10}).json()["postalCodes"]
    assert {r["postalcode"] for r in j} == {"2552", "2544"}, j
    assert w.count("zoom=18") == 9, "centre + one ring of 8"
    j2 = c.get("/findNearbyPostalCodesJSON", params={"postalcode": "2552", "country": "AT"}).json()["postalCodes"]
    assert j2, "centre resolved through a Nominatim postcode search"
    assert any("postalcode=2552" in u and "/search" in u for u in w.calls)


def test_errors_auth_and_limits():
    w = World()
    c = client(w)                                                    # nothing reachable
    e = c.get("/findNearbyPlaceNameJSON", params={"lat": 1, "lng": 2}).json()
    assert e == {"status": {"message": "no geocoding backend reachable", "value": 13}}, e
    bad = c.get("/findNearbyPostalCodesJSON").json()
    assert bad["status"]["value"] == 20, bad
    assert "<value>20</value>" in c.get("/findNearbyPostalCodes").text
    assert c.get("/findNearbyPlaceNameJSON", params={"lat": 1}).status_code == 422
    w.on("http://pelias", pelias_places)
    cl = client(w, token="sekrit", gn_max_rows=1)
    q = {"lat": LAT, "lng": LNG, "maxRows": 50}
    assert cl.get("/findNearbyPlaceNameJSON", params=q).status_code == 401
    ok = cl.get("/findNearbyPlaceNameJSON", params={**q, "token": "sekrit", "username": "x"})
    assert ok.status_code == 200 and len(ok.json()["geonames"]) == 1, "token via query + maxRows ceiling"
    assert cl.get("/findNearbyPlaceNameJSON", params=q, headers={"x-api-key": "sekrit"}).status_code == 200


def pelias_search(url):
    if "/v1/search" in url:
        return Resp(200, {"features": [
            {"geometry": {"coordinates": [16.2330, 47.9500]}, "properties": {"gid": "wof:locality:1", "layer": "locality",
             "name": "Musterdorf", "country": "Österreich", "country_a": "AUT", "region": "Niederösterreich", "county": "Bezirk Baden"}},
            {"geometry": {"coordinates": [10.0, 50.0]}, "properties": {"gid": "wof:locality:9", "layer": "locality",
             "name": "Musterdorf (DE)", "country": "Deutschland", "country_a": "DEU"}}]})
    return Resp(200, {"features": []})


def test_search_get_hierarchy():
    w = World(); w.on("http://pelias", pelias_search); w.on("http://nominatim", nominatim)
    c = client(w)
    j = c.get("/searchJSON", params={"q": "Musterdorf", "maxRows": 10}).json()
    assert j["totalResultsCount"] == 2 and [r["name"] for r in j["geonames"]] == ["Musterdorf", "Musterdorf (DE)"], j
    assert list(j["geonames"][0]) == MEDIUM[:-1], "no distance in search results"
    only_at = c.get("/searchJSON", params={"name": "Musterdorf", "country": "AT"}).json()
    assert [r["countryCode"] for r in only_at["geonames"]] == ["AT"]
    page = c.get("/searchJSON", params={"q": "Musterdorf", "maxRows": 1, "startRow": 1}).json()
    assert [r["name"] for r in page["geonames"]] == ["Musterdorf (DE)"]
    x = ET.fromstring(c.get("/search", params={"q": "Musterdorf"}).text)
    assert x.find("totalResultsCount").text == "2" and len(x.findall("geoname")) == 2
    assert c.get("/searchJSON").json()["status"]["value"] == 20

    gid = j["geonames"][0]["geonameId"]
    got = c.get("/getJSON", params={"geonameId": gid, "style": "LONG"}).json()
    assert got["geonameId"] == gid and got["name"] == "Musterdorf" and got["adminName1"] == "Niederösterreich", got
    assert ET.fromstring(c.get("/get", params={"geonameId": gid}).text).tag == "geoname"
    assert c.get("/getJSON", params={"geonameId": 999999}).json()["status"]["value"] == 15

    h = c.get("/hierarchyJSON", params={"geonameId": gid}).json()["geonames"]
    assert [r["fcode"] for r in h] == ["PCLI", "ADM1", "ADM2", "PPL"], h      # leaf name == city -> no duplicate PPL
    assert [r["name"] for r in h][:3] == ["Austria", "Niederösterreich", "Bezirk Baden"], h
    assert h[-1]["geonameId"] == gid
    h2 = [r["geonameId"] for r in c.get("/hierarchyJSON", params={"geonameId": gid}).json()["geonames"]]
    assert h2 == [r["geonameId"] for r in h], "synthetic ancestor ids are stable"


def test_search_fallback_and_unreachable():
    w = World(); w.on("http://pelias", EMPTY_FEATURES)
    w.on("http://nominatim", nominatim)
    c = client(w)
    j = c.get("/searchJSON", params={"q": "Musterdorf"}).json()
    assert j["totalResultsCount"] == 1 and j["geonames"][0]["name"] == "Musterdorf"
    assert w.count("/v1/search") == 1 and w.count("/search?") >= 1
    assert client(World()).get("/searchJSON", params={"q": "x"}).json()["status"]["value"] == 13


def test_country_info():
    w = World()
    w.on("http://nominatim", lambda u: Resp(200, [{"boundingbox": ["46.37", "49.02", "9.53", "17.16"], "osm_type": "relation",
                                                   "osm_id": 1, "lat": "47", "lon": "13", "address": {}}]))
    c = client(w)
    at = c.get("/countryInfoJSON", params={"country": "AT"}).json()["geonames"][0]
    assert at["countryName"] == "Austria" and at["capital"] == "Vienna" and at["currencyCode"] == "EUR" \
        and at["isoAlpha3"] == "AUT" and at["continentName"] == "Europe" and at["south"] == 46.37 and at["east"] == 17.16, at
    again = c.get("/countryInfoJSON", params={"country": "at"}).json()["geonames"][0]
    assert again == at and w.count("http://nominatim") == 1, "bounding box is cached"
    allc = c.get("/countryInfoJSON").json()
    assert allc["totalResultsCount"] >= 240 and "south" not in allc["geonames"][0]
    root = ET.fromstring(c.get("/countryInfo", params={"country": "DE"}).text)
    assert root.find("country/countryName").text == "Germany"
    assert c.get("/countryInfoJSON", params={"country": "ZZ"}).json()["geonames"] == []


def test_address():
    w = World(); w.on("http://nominatim", lambda u: Resp(200, {
        "osm_type": "way", "osm_id": 77, "lat": "47.9454285", "lon": "16.2257166", "class": "amenity", "type": "school",
        "name": "Volksschule Musterdorf", "display_name": "Volksschule Musterdorf, 20, Beispielgasse, Musterdorf",
        "address": {"road": "Beispielgasse", "house_number": "20", "postcode": "2552", "country_code": "at",
                    "state": "Niederösterreich", "ISO3166-2-lvl4": "AT-3"}}))
    c = client(w)
    a = c.get("/addressJSON", params={"lat": 47.9454285, "lng": 16.2257166}).json()["address"]
    assert a["street"] == "Beispielgasse" and a["houseNumber"] == "20" and a["postalcode"] == "2552" \
        and a["adminCode1"] == "03" and a["countryCode"] == "AT", a
    root = ET.fromstring(c.get("/address", params={"lat": 47.9454285, "lng": 16.2257166}).text)
    assert root.find("address/street").text == "Beispielgasse"
    assert w.count("http://nominatim") == 1, "second call from the cache"
    w.on("http://nominatim", lambda u: Resp(200, {"error": "Unable to geocode"}))
    assert c.get("/addressJSON", params={"lat": 1.0, "lng": 2.0}).json()["status"]["value"] == 15


def nominatim_postcodes(url):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    if q.get("postalcode", [""])[0].startswith("25") or q.get("q", [""])[0] == "Leobersdorf":
        mk = lambda i, code, town, la, lo, extra=None: {"osm_type": "relation", "osm_id": i, "lat": la, "lon": lo,
            "class": "boundary", "type": "postal_code", "name": code, "display_name": f"{code}, {town}, Österreich",
            "address": {"postcode": code, "town": town, "county": "Bezirk Baden", "state": "Niederösterreich",
                        "country_code": "at", "ISO3166-2-lvl4": "AT-3", **(extra or {})}}
        return Resp(200, [mk(1, "2552", "Musterdorf", "47.95", "16.23"), mk(2, "2544", "Leobersdorf", "47.91", "16.21"),
                          mk(3, "2552", "Musterdorf", "47.95", "16.23"), mk(4, "2552", "Gainfarn", "47.93", "16.20")])
    return Resp(200, [])


def test_postal_code_search_and_lookup():
    w = World(); w.on("http://nominatim", nominatim_postcodes)
    c = client(w)
    j = c.get("/postalCodeSearchJSON", params={"postalcode": "2552", "country": "AT"}).json()["postalCodes"]
    assert [(r["postalCode"], r["placeName"]) for r in j] == [("2552", "Musterdorf"), ("2552", "Gainfarn")], j   # prefix filter + duplicate dropped
    assert list(j[0]) == ["postalCode", "placeName", "countryCode", "lat", "lng", "adminCode1", "adminName1", "adminName2"] or \
        "adminCode2" in j[0], list(j[0])
    assert j[0]["countryCode"] == "AT" and j[0]["adminCode1"] == "03" and j[0]["adminName2"] == "Bezirk Baden"
    assert any("countrycodes=AT" in u for u in w.calls)
    pre = c.get("/postalCodeSearchJSON", params={"postalcode": "25"}).json()["postalCodes"]
    assert {r["postalCode"] for r in pre} == {"2552", "2544"}
    byname = c.get("/postalCodeSearchJSON", params={"placename": "Leobersdorf"}).json()["postalCodes"]
    assert byname and byname[0]["postalCode"] in ("2552", "2544")
    x = ET.fromstring(c.get("/postalCodeSearch", params={"postalcode": "2544"}).text)
    assert [e.tag for e in x] == ["code"] and x.find("code/postalCode").text == "2544"
    lk = c.get("/postalCodeLookupJSON", params={"postalcode": "2552", "country": "AT"}).json()["postalcodes"]
    assert lk[0]["postalcode"] == "2552" and "postalCode" not in lk[0] and lk[0]["placeName"] == "Musterdorf", lk
    n = len(w.calls)
    c.get("/postalCodeSearchJSON", params={"postalcode": "2552", "country": "AT"})
    assert len(w.calls) == n, "repeated search comes from the cache"
    assert c.get("/postalCodeSearchJSON").json()["status"]["value"] == 20
    assert c.get("/postalCodeLookupJSON").json()["status"]["value"] == 20
    assert c.get("/postalCodeSearchJSON", params={"postalcode": "9999"}).json() == {"postalCodes": []}
    assert client(World()).get("/postalCodeLookupJSON", params={"postalcode": "1"}).json()["status"]["value"] == 13


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} GeoNames checks passed")
