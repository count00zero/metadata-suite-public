"""findNearby, extendedFindNearby and the extra searchJSON parameters.   python tests/test_geonames_more.py"""
import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import EMPTY_FEATURES, Resp, World, client  # noqa: E402

LAT, LNG = 47.9529, 16.2296


def feat(gid, layer, name, lon, lat, **kw):
    return {"geometry": {"coordinates": [lon, lat]},
            "properties": {"gid": gid, "layer": layer, "name": name, "country_a": "AUT", "country": "Österreich", **kw}}


def pelias(url):
    q = parse_qs(urlparse(url).query)
    if "/v1/reverse" in url:
        layers = q.get("layers", [""])[0]
        if layers.startswith("locality"):
            return Resp(200, {"features": [feat("l:1", "locality", "Musterdorf", 16.233, 47.95, region="Niederösterreich")]})
        if "street" in layers and "venue" in layers:
            return Resp(200, {"features": [feat("v:1", "venue", "Schloss Musterdorf", 16.2300, 47.9530),
                                           feat("s:1", "street", "Hauptstraße", 16.2297, 47.9531)]})
        if layers == "street":
            return Resp(200, {"features": [feat("s:1", "street", "Hauptstraße", 16.2297, 47.9531)]})
        if layers.startswith("venue"):
            return Resp(200, {"features": [feat("v:1", "venue", "Schloss Musterdorf", 16.2300, 47.9530)]})
        return Resp(200, {"features": [feat("v:1", "venue", "Schloss Musterdorf", 16.2300, 47.9530),
                                       feat("s:1", "street", "Hauptstraße", 16.2297, 47.9531),
                                       feat("v:far", "venue", "Weit weg", 16.40, 47.90)]})
    if "/v1/autocomplete" in url or "/v1/search" in url:
        pre = "/v1/autocomplete" in url
        recs = [feat("a:1", "locality", "Baden", 16.23, 48.0, population=25000) if False else feat("a:1", "locality", "Baden", 16.23, 48.0),
                feat("a:2", "locality", "Bad Vöslau", 16.21, 47.97), feat("a:3", "region", "Baden-Württemberg", 9.0, 48.6),
                feat("a:4", "locality", "Wien", 16.37, 48.2)]
        recs[1]["properties"]["country_a"] = "AUT"
        recs[2]["properties"]["country_a"] = "DEU"
        return Resp(200, {"features": recs, "pre": pre})
    return Resp(200, {"features": []})


def nominatim(url):
    q = parse_qs(urlparse(url).query)
    return Resp(200, {"osm_type": "relation", "osm_id": 5, "lat": q["lat"][0], "lon": q["lon"][0], "class": "place", "type": "village",
                      "name": "Musterdorf", "address": {"country_code": "at", "country": "Österreich", "state": "Niederösterreich",
                                                          "county": "Bezirk Baden", "town": "Musterdorf"}})


def test_find_nearby():
    w = World(); w.on("http://pelias", pelias)
    c = client(w)
    j = c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "radius": 1, "maxRows": 10}).json()["geonames"]
    names = [r["name"] for r in j]
    assert "Weit weg" not in names and "Schloss Musterdorf" in names and "Hauptstraße" in names and "Musterdorf" in names, names
    assert j == sorted(j, key=lambda r: r["distance"]), "nearest first"
    assert len(c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "maxRows": 1}).json()["geonames"]) == 1
    only_r = c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "featureClass": "R"}).json()["geonames"]
    assert [r["name"] for r in only_r] == ["Hauptstraße"] and only_r[0]["fcl"] == "R"
    code = c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "featureClass": ["S", "R"], "featureCode": "RD"}).json()["geonames"]
    assert [r["fcode"] for r in code] == ["RD"]
    x = ET.fromstring(c.get("/findNearby", params={"lat": LAT, "lng": LNG}).text)
    assert x.tag == "geonames" and x.find("geoname/name") is not None
    n = len(w.calls)
    c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "radius": 1, "maxRows": 10})
    assert len(w.calls) == n, "cached"
    assert client(World()).get("/findNearbyJSON", params={"lat": LAT, "lng": LNG}).json()["status"]["value"] == 13


def test_find_nearby_nominatim_fallback():
    w = World(); w.on("http://pelias", EMPTY_FEATURES); w.on("http://nominatim", nominatim)
    j = client(w).get("/findNearbyJSON", params={"lat": LAT, "lng": LNG}).json()["geonames"]
    assert j and j[0]["name"] == "Musterdorf"


def test_extended_find_nearby():
    w = World(); w.on("http://pelias", pelias); w.on("http://nominatim", nominatim)
    c = client(w)
    j = c.get("/extendedFindNearbyJSON", params={"lat": LAT, "lng": LNG}).json()["geonames"]
    assert [r["fcl"] for r in j][-3:] == ["A", "A", "A"] and j[-1]["fcode"] == "PCLI", j
    assert [r["fcode"] for r in j][-3:-1] == ["ADM1", "ADM2"][::-1] or [r["fcode"] for r in j][-3:] == ["ADM2", "ADM1", "PCLI"], [r["fcode"] for r in j]
    assert j[0]["name"] in ("Schloss Musterdorf", "Hauptstraße"), "nearest spot first"
    assert len({r["geonameId"] for r in j}) == len(j)
    assert ET.fromstring(c.get("/extendedFindNearby", params={"lat": LAT, "lng": LNG}).text).tag == "geonames"


def test_search_parameters():
    w = World(); w.on("http://pelias", pelias)
    c = client(w)
    g = lambda **p: [r["name"] for r in c.get("/searchJSON", params={"q": "Bad", **p, "maxRows": 20}).json()["geonames"]]
    assert g() == ["Baden", "Bad Vöslau", "Baden-Württemberg", "Wien"]
    assert g(name_startsWith="Bad") == ["Baden", "Bad Vöslau", "Baden-Württemberg"]
    assert any("/v1/autocomplete" in u for u in w.calls), "prefix search uses autocomplete"
    assert g(name_equals="Wien") == ["Wien"]
    assert g(country="AT") == ["Baden", "Bad Vöslau", "Wien"]
    assert g(country="AT,DE") == ["Baden", "Bad Vöslau", "Baden-Württemberg", "Wien"]
    assert g(country=["DE"]) == ["Baden-Württemberg"]
    assert g(featureClass="A") == ["Baden-Württemberg"]
    assert g(featureClass="P", featureCode="PPL") == ["Baden", "Bad Vöslau", "Wien"]
    assert g(east=16.3, west=16.0, north=48.1, south=47.9) == ["Baden", "Bad Vöslau"]
    assert any("boundary.rect.min_lon=16.0" in u for u in w.calls)
    assert g(isNameRequired="true", q="Wien") == ["Wien"]
    assert g(startRow=1, maxRows=1)  # still pages
    # population ordering + cities threshold need a source with population
    def with_pop(url):
        r = pelias(url).json()
        for i, f in enumerate(r["features"]):
            f["properties"]["addendum"] = {"gn": {"population": [5000, 12000, 0, 1900000][i]}}
        return Resp(200, r)
    w2 = World(); w2.on("http://pelias", with_pop); c2 = client(w2)
    o = [r["name"] for r in c2.get("/searchJSON", params={"q": "x", "orderby": "population", "maxRows": 20}).json()["geonames"]]
    assert o == ["Wien", "Bad Vöslau", "Baden", "Baden-Württemberg"], o
    ci = [r["name"] for r in c2.get("/searchJSON", params={"q": "x", "cities": "cities5000", "maxRows": 20}).json()["geonames"]]
    assert ci == ["Baden", "Bad Vöslau", "Wien"], ci
    assert c.get("/searchJSON", params={"name_startsWith": "Bad"}).json()["geonames"], "name_startsWith alone is enough"


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} checks passed")
