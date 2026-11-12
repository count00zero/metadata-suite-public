"""/around: place + water + sights + elevation + timezone in one answer.   python tests/test_around.py"""
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client, photon_graz  # noqa: E402

LAT, LNG = 45.0, 14.0


def maritime(url):
    q = parse_qs(urlparse(url).query)
    if "river_radius_km" in q:
        return Resp(200, {"contained_in": [{"layer": "lakes", "name": "Bodensee"}],
                          "rivers": [{"name": "Rhein", "lat": 45.001, "lon": 14.001}, {"name": "Ill", "lat": 45.01, "lon": 14.0}]})
    return Resp(200, {"name": "Kvarner Bucht", "distance_km": 2.0,
                      "contained_in": [{"layer": "iho", "name": "Adriatisches Meer", "lang": "de"}]})


def world(with_maritime=True):
    w = World()
    w.on("http://photon/reverse", lambda u: Resp(200, {"features": [{"geometry": {"coordinates": [LNG, LAT]}, "properties": {
        "country": "Kroatien", "countrycode": "HR", "city": "Rijeka", "state": "Primorje", "name": "Riva", "street": "Riva",
        "housenumber": "3", "postcode": "51000"}}]}))
    w.on("http://photon/api", lambda u: Resp(200, {"features": [{"geometry": {"coordinates": [LNG + 0.001, LAT + 0.001]}, "properties": {
        "name": "Burg Trsat", "osm_key": "historic", "osm_value": "castle", "osm_id": 5, "osm_type": "N", "country": "Kroatien"}}]}))
    if with_maritime:
        w.on("http://maritime", maritime)
    return w


def test_around_all_parts():
    c = client(world(), maritime="http://maritime")
    j = c.get("/around", params={"lat": LAT, "lon": LNG, "radius": 3000}).json()
    assert j["center"] == {"lat": LAT, "lon": LNG} and "errors" not in j, j
    assert j["place"]["city"] == "Rijeka" and j["place"]["house_number"] == "3" and j["place"]["postcode"] == "51000", j["place"]
    assert j["water"]["sea"] == "Adriatisches Meer" and j["water"]["feature"] == "Kvarner Bucht" and j["water"]["lake"] == "Bodensee", j["water"]
    assert [r["name"] for r in j["water"]["rivers"]] == ["Rhein", "Ill"], j["water"]
    assert j["sights"]["count"] >= 0 and "items" in j["sights"] and "text_de" in j["sights"]
    assert "metres" in j["elevation"] and "source" in j["elevation"]
    assert "timezone" in j


def test_include_and_validation():
    c = client(world(), maritime="http://maritime")
    j = c.get("/around", params={"lat": LAT, "lon": LNG, "include": "place,elevation"}).json()
    assert set(j) >= {"place", "elevation"} and "water" not in j and "sights" not in j and "timezone" not in j, set(j)
    r = c.get("/around", params={"lat": LAT, "lon": LNG, "include": "place,wetter"})
    assert r.status_code == 422 and "wetter" in str(r.json()), r.text
    assert c.get("/around", params={"lat": 95, "lon": 0}).status_code == 422


def test_a_failing_part_does_not_cost_the_others():
    w = world(with_maritime=False)
    c = client(w, maritime="http://maritime")                    # configured but not answering -> water is just empty
    j = c.get("/around", params={"lat": LAT, "lon": LNG}).json()
    assert j["place"]["city"] == "Rijeka" and j["water"]["rivers"] == [] and j["water"]["sea"] == "", j
    w2 = World()                                                 # nothing reachable at all
    c2 = client(w2)
    j2 = c2.get("/around", params={"lat": LAT, "lon": LNG}).json()
    assert "place" in j2["errors"] and "sights" in j2["errors"] and "elevation" in j2, j2
    assert j2["water"]["available"] is False


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} /around checks passed")
