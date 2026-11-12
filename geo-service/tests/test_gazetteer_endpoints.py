"""The GeoNames endpoints answered from the own database, with every geocoder down.   python tests/test_gazetteer_endpoints.py"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gazfix  # noqa: E402
from fakes import Resp, World, client  # noqa: E402
from app import gazetteer as gz  # noqa: E402

LAT, LNG = 47.9529, 16.2296


def make_client(**kw):
    d = gazfix.make()
    db = os.path.join(tempfile.mkdtemp(), "gazetteer.sqlite")
    gz.build(d, db, ("de", "en"), log=lambda m: None)
    return client(World(), gn_gazetteer_db=db, **kw)          # no route at all: every geocoder is unreachable


def test_place_and_nearby_with_real_ids():
    c = make_client()
    j = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG, "maxRows": 3, "style": "LONG"}).json()["geonames"]
    assert [r["name"] for r in j] == ["Musterdorf", "Bad Vöslau", "Baden"], j
    assert j[0]["geonameId"] == 2774313 and j[0]["population"] == 2500 and j[0]["fcode"] == "PPL" and j[0]["adminName1"] == "Lower Austria"
    assert j[0]["countryName"] == "Austria", "the country name comes from countryInfo.txt"
    de = c.get("/findNearbyPlaceNameJSON", params={"lat": 48.2085, "lng": 16.3721, "maxRows": 1, "lang": "de"}).json()["geonames"]
    assert de[0]["name"] == "Wien"
    n = c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "radius": 1, "maxRows": 10}).json()["geonames"]
    assert {r["name"] for r in n} == {"Musterdorf", "Schloss Musterdorf", "Hauptstraße"}
    r = c.get("/findNearbyJSON", params={"lat": LAT, "lng": LNG, "featureClass": "S"}).json()["geonames"]
    assert [x["fcode"] for x in r] == ["CSTL"]
    x = ET.fromstring(c.get("/findNearbyPlaceName", params={"lat": LAT, "lng": LNG, "maxRows": 1}).text)
    assert x.find("geoname/geonameId").text == "2774313"


def test_open_sea_asks_the_maritime_server_before_pelias():
    d = gazfix.make()
    db = os.path.join(tempfile.mkdtemp(), "gazetteer.sqlite")
    gz.build(d, db, ("de", "en"), log=lambda m: None)
    w = World()
    w.on("maritime/reverse", lambda url: Resp(200, {"name": "Strasse von Otranto"}))
    w.on("pelias", lambda url: Resp(200, {"features": []}))
    c = client(w, gn_gazetteer_db=db, maritime="http://maritime")
    g = c.get("/findNearbyPlaceNameJSON", params={"lat": 0, "lng": -30}).json()["geonames"]
    assert len(g) == 1 and g[0]["name"] == "Strasse von Otranto" and g[0]["fcl"] == "H" and g[0]["fcode"] == "SEA", g
    assert w.count("pelias") == 0 and w.count("nominatim") == 0, "the slow geocoders were not asked"
    w2 = World()                                    # maritime has nothing -> only then the rest of the chain
    w2.on("maritime/reverse", lambda url: Resp(200, {}))
    w2.on("pelias", lambda url: Resp(200, {"features": []}))
    c2 = client(w2, gn_gazetteer_db=db, maritime="http://maritime")
    c2.get("/findNearbyPlaceNameJSON", params={"lat": 0, "lng": -30})
    assert w2.count("maritime/reverse") == 1 and w2.count("pelias") == 1
    on_land = c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG, "maxRows": 1}).json()["geonames"]
    assert on_land[0]["name"] == "Musterdorf" and w.count("maritime/reverse") == 1, "on land the maritime server is never asked"


def test_open_water_without_maritime_data_uses_geonames_water_bodies_and_far_hits_ask_maritime():
    """Adriatic off Rimini: the maritime server says 'Unbekannt' -> nearest sea of the own GeoNames data. Strait of Messina:
    Photon names a village 2 km away -> the maritime server is asked and names the strait (country kept)."""
    d = gazfix.make()
    with open(os.path.join(d, "allCountries.txt"), "a", encoding="utf-8") as f:
        f.write(gazfix.place(3183, "Adriatic Sea", 43.0, 15.0, "H", "SEA", "", pop=0) + "\n")
        f.write(gazfix.place(2523, "Golf von Venedig", 45.3, 13.0, "H", "GULF", "IT", pop=0) + "\n")
    db = os.path.join(tempfile.mkdtemp(), "gazetteer.sqlite")
    gz.build(d, db, ("de", "en"), log=lambda m: None)
    w = World()
    w.on("maritime/reverse", lambda url: Resp(200, {"name": "Unbekannt"}))
    c = client(w, gn_gazetteer_db=db, maritime="http://maritime", chain=["photon", "nominatim", "gazetteer", "maritime"])
    r = c.get("/reverse", params={"lat": 44.0, "lon": 13.5, "nocache": "true"}).json()
    assert r["found"] and r["locality"] == "Golf von Venedig" and r["source"] == "Maritim-Backup", r      # nearest water body
    assert [t["backend"] for t in r["tried"]][-1] == "maritime" and r["tried"][-1]["status"] == "hit", r["tried"]
    # no water body within 300 km -> still found:false (nothing invented)
    far = c.get("/reverse", params={"lat": 0.0, "lon": -30.0, "nocache": "true"}).json()
    assert far["found"] is False, far

    w2 = World()
    w2.on("photon", lambda u: Resp(200, {"features": [{"geometry": {"coordinates": [15.6339, 38.2616]},
                                                        "properties": {"country": "Italien", "countrycode": "IT", "city": "Messina", "locality": "Ganzirri",
                                                                       "state": "Sizilien", "county": "Messina"}}]}))
    w2.on("maritime/reverse", lambda u: Resp(200, {"name": "Straße von Messina"}))
    r2 = client(w2, gn_gazetteer_db=db, maritime="http://maritime").get("/reverse", params={"lat": 38.25, "lon": 15.62}).json()
    assert r2["label"] == "Straße von Messina, Italien" and r2["city"] == "" and r2["state"] == "", r2
    near = {"geometry": {"coordinates": [15.6203, 38.2501]}, "properties": {"country": "Italien", "countrycode": "IT", "city": "Messina", "state": "Sizilien", "county": "Messina"}}
    w3 = World()
    w3.on("photon", lambda u: Resp(200, {"features": [near]}))
    w3.on("maritime/reverse", lambda u: Resp(200, {"name": "SOLLTE NICHT KOMMEN"}))
    r3 = client(w3, gn_gazetteer_db=db, maritime="http://maritime").get("/reverse", params={"lat": 38.25, "lon": 15.62}).json()
    assert r3["city"] == "Messina" and w3.count("maritime/reverse") == 0, "a hit right at the point is never second-guessed"


def test_timing_log_prints_slow_requests(capsys=None):
    import io
    import contextlib
    c = make_client(slow_log_ms=0)                    # 0 = log every request
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        c.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG, "maxRows": 1})
    line = buf.getvalue().strip().splitlines()[-1]
    assert "GET /findNearbyPlaceNameJSON?lat=47.9529" in line and "-> 200" in line and line.endswith(" ms") and "T" in line[:23], line
    c2 = make_client(slow_log_ms=-1)                  # off
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        c2.get("/findNearbyPlaceNameJSON", params={"lat": LAT, "lng": LNG})
    assert buf2.getvalue() == ""


def test_postal_codes():
    c = make_client()
    j = c.get("/findNearbyPostalCodesJSON", params={"lat": LAT, "lng": LNG, "radius": 10, "maxRows": 5}).json()["postalCodes"]
    assert [r["postalcode"] for r in j] == ["2552", "2540", "2500"] and j[0]["name"] == "Musterdorf" and j[0]["adminCode1"] == "03", j
    by = c.get("/findNearbyPostalCodesJSON", params={"postalcode": "2540", "country": "AT", "radius": 5}).json()["postalCodes"]
    assert by[0]["postalcode"] == "2540" and "2552" in [r["postalcode"] for r in by]
    s = c.get("/postalCodeSearchJSON", params={"postalcode": "25", "country": "AT", "maxRows": 10}).json()["postalCodes"]
    assert [r["postalCode"] for r in s] == ["2500", "2540", "2552"] and s[0]["placeName"] == "Baden"
    lk = c.get("/postalCodeLookupJSON", params={"postalcode": "2552", "country": "AT"}).json()["postalcodes"]
    assert lk[0]["postalcode"] == "2552" and lk[0]["placeName"] == "Musterdorf"
    assert c.get("/postalCodeSearchJSON", params={"placename": "Bad V"}).json()["postalCodes"][0]["postalCode"] == "2540"


def test_search():
    c = make_client()
    g = lambda **p: c.get("/searchJSON", params={**p, "maxRows": 20}).json()
    j = g(q="Wien")
    assert j["totalResultsCount"] == 1 and j["geonames"][0]["geonameId"] == 2761369
    assert [r["name"] for r in g(name_startsWith="Muster")["geonames"]] == ["Musterdorf", "Musterdorf"]
    assert [r["name"] for r in g(q="Baden", country="DE")["geonames"]] == ["Baden-Baden"]
    assert [r["name"] for r in g(q="Bad", name_startsWith="Bad", orderby="population")["geonames"]][0] == "Baden-Baden"
    assert [r["name"] for r in g(q="Baden", cities="cities15000")["geonames"]] == ["Baden", "Bezirk Baden", "Baden-Baden"]
    assert [r["fcode"] for r in g(q="Musterdorf", featureClass="S")["geonames"]] == ["CSTL"]
    assert g(q="Baden", east=16.5, west=16.0, north=48.1, south=47.9, maxRows=1)["totalResultsCount"] == 2
    assert g(q="Wien", lang="de")["geonames"][0]["name"] == "Wien"
    x = ET.fromstring(c.get("/search", params={"q": "Wien"}).text)
    assert x.find("totalResultsCount").text == "1"


def test_get_hierarchy_children_extended():
    c = make_client()
    got = c.get("/getJSON", params={"geonameId": 2774313, "style": "FULL"}).json()
    assert got["name"] == "Musterdorf" and got["population"] == 2500 and got["adminName2"] == "Bezirk Baden"
    h = c.get("/hierarchyJSON", params={"geonameId": 2774313}).json()["geonames"]
    assert [r["geonameId"] for r in h] == [2782113, 2770542, 2781503, 7871054, 2774313], "real ids, top first"
    k = c.get("/childrenJSON", params={"geonameId": 2770542}).json()
    assert k["totalResultsCount"] == 2 and [r["name"] for r in k["geonames"]] == ["Bezirk Baden", "Bezirk Mistelbach"] and "complete" not in k
    top = c.get("/childrenJSON", params={"geonameId": 2782113, "style": "SHORT"}).json()["geonames"]
    assert [r["name"] for r in top] == ["Lower Austria"]
    e = c.get("/extendedFindNearbyJSON", params={"lat": LAT, "lng": LNG}).json()["geonames"]
    assert [r["geonameId"] for r in e][-1] == 2782113 and e[0]["fcl"] in ("S", "R") and 2774313 in [r["geonameId"] for r in e], e
    assert c.get("/getJSON", params={"geonameId": 5}).json()["status"]["value"] == 15


def test_reverse_chain_falls_back_to_the_database():
    c = make_client()
    r = c.get("/reverse", params={"lat": 47.96, "lon": 16.23, "lang": "de"}).json()
    assert r["found"] and r["city"] == "Musterdorf" and r["state"] == "Lower Austria" and r["country_a"] == "AUT" and r["source"] == "GeoNames", r
    assert [t["backend"] for t in r["tried"]][-1] == "gazetteer"
    far = c.get("/reverse", params={"lat": 10.0, "lon": 100.0}).json()
    assert far["found"] is False
    h = c.get("/health", params={"probe": "true"}).json()["geocoders"]["gazetteer"]
    assert h["configured"] and h["probe"]["reachable"] and "13 places" in h["probe"]["detail"]
    st = c.get("/stats").json()["gazetteer"]
    assert st["available"] and st["n_place"] == "13"


def test_synthetic_ids_do_not_collide_with_real_ones():
    c = make_client()
    from app.geonames import IdRegistry
    w = World()
    w.on("http://nominatim", lambda u: Resp(200, {"osm_type": "node", "osm_id": 1, "lat": "1", "lon": "2", "class": "place", "type": "village",
                                                 "name": "X", "address": {"country_code": "at"}}))
    from fakes import client as cl
    c2 = cl(w, gn_gazetteer_db="/nonexistent/g.sqlite")
    gid = c2.get("/findNearbyPlaceNameJSON", params={"lat": 1, "lng": 2}).json()["geonames"][0]["geonameId"]
    assert gid >= IdRegistry.OFFSET, gid


def test_without_database_nothing_changes():
    c = client(World(), gn_gazetteer_db="/nonexistent/g.sqlite")
    assert c.get("/findNearbyPlaceNameJSON", params={"lat": 1, "lng": 2}).json()["status"]["value"] == 13
    assert c.get("/health").json()["geocoders"]["gazetteer"]["configured"] is False


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} gazetteer endpoint checks passed")
