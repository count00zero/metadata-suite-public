"""/sights, findNearbyWikipedia and the shared code behind /pois and /context.   python tests/test_sights.py"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client  # noqa: E402

LAT, LON = 47.0707, 15.4395           # Graz


def feat(name, key, value, lat, lon, osm="N1", **extra):
    props = {"name": name, "osm_key": key, "osm_value": value, "osm_type": osm[0], "osm_id": int(osm[1:])}
    if extra:
        props["extra"] = extra
    return {"geometry": {"coordinates": [lon, lat]}, "properties": props}


def photon_world(calls=None):
    """Photon answers per osm_tag filter; every request is recorded in the World."""
    w = World()

    def photon(url):
        if "osm_tag=tourism:attraction" in url:
            return Resp(200, {"features": [
                feat("Uhrturm", "tourism", "attraction", 47.0738, 15.4378, "N5", wikidata="Q702418", website="https://uhrturm.example",
                     opening_hours="Mo-Su 09:00-18:00", description="Wahrzeichen von Graz"),
                feat("Kleine Skulptur", "tourism", "attraction", 47.0710, 15.4400, "N6")]})
        if "osm_tag=historic" in url:
            return Resp(200, {"features": [
                feat("Schloss Eggenberg", "historic", "castle", 47.0723, 15.3967, "W9", wikipedia="de:Schloss Eggenberg (Graz)", wikidata="Q695466"),
                feat("Schloss Eggenberg", "historic", "castle", 47.0724, 15.3968, "N10", wikidata="Q695466"),     # node + outline = one sight
                feat("Bildstock", "historic", "wayside_shrine", 47.0709, 15.4397, "N11"),
                feat("Stadtpfarrkirche", "historic", "church", 47.0705, 15.4410, "N12")]})
        if "osm_tag=tourism:museum" in url:
            return Resp(200, {"features": [feat("Kunsthaus", "tourism", "museum", 47.0714, 15.4337, "W20", wikipedia="de:Kunsthaus Graz")]})
        if "osm_tag=natural:peak" in url:
            return Resp(200, {"features": [feat("Schöckl", "natural", "peak", 47.1989, 15.4672, "N30")]})
        if "osm_tag" in url:
            return Resp(200, {"features": []})
        return Resp(200, {"features": []})
    w.on("http://photon", photon)
    return w


def test_ranking_details_dedupe_and_bearing():
    w = photon_world()
    c = client(w)
    r = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 5000, "limit": 20}).json()
    names = [x["name"] for x in r["sights"]]
    assert names.count("Schloss Eggenberg") == 1, names                  # node + outline merged
    assert "Bildstock" not in names                                      # skip catalogue still applies
    assert r["sights"][0]["name"] in ("Uhrturm", "Schloss Eggenberg", "Kunsthaus"), names
    assert [x["score"] for x in r["sights"]] == sorted((x["score"] for x in r["sights"]), reverse=True)
    uhr = next(x for x in r["sights"] if x["name"] == "Uhrturm")
    assert uhr["category"] == "attraction" and uhr["website"] == "https://uhrturm.example" and uhr["opening_hours"].startswith("Mo-Su")
    assert uhr["description"] == "Wahrzeichen von Graz" and uhr["wikidata"] == "Q702418" and uhr["wikidata_url"].endswith("/Q702418")
    assert uhr["direction"] in ("N", "NO") and 0 <= uhr["bearing"] < 360, uhr
    sch = next(x for x in r["sights"] if x["name"] == "Schloss Eggenberg")
    assert sch["wikipedia_url"] == "https://de.wikipedia.org/wiki/Schloss_Eggenberg_(Graz)" and sch["category"] == "castle", sch
    skulptur = next(x for x in r["sights"] if x["name"] == "Kleine Skulptur")
    assert skulptur["score"] < uhr["score"], "a sight with wikidata and details outranks an anonymous one"
    assert r["source"] == "Photon" and r["center"]["lat"] == LAT and r["count"] == len(r["sights"]) and r["text_de"].startswith("in der Nähe: ")


def test_sort_limit_categories_and_validation():
    c = client(photon_world())
    r = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 5000, "sort": "distance", "limit": 3}).json()
    d = [x["distance_m"] for x in r["sights"]]
    assert len(d) == 3 and d == sorted(d), d
    m = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 20000, "categories": "museum,peak"}).json()
    assert {x["name"] for x in m["sights"]} == {"Kunsthaus", "Schöckl"} and m["categories"] == ["museum", "peak"], m
    bad = c.get("/sights", params={"lat": LAT, "lon": LON, "categories": "pizza"})
    assert bad.status_code == 422 and "castle" in str(bad.json()), bad.text
    assert c.get("/sights", params={"lat": 99, "lon": 0}).status_code == 422
    assert c.get("/sights").status_code == 422, "nothing to search around"
    assert c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 30000}).status_code == 422, "radius is capped at 20 km"
    ok = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 20000})
    assert ok.status_code == 200


def test_radius_cuts_and_cache_v3():
    w = photon_world()
    c = client(w)
    near = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 300, "limit": 20}).json()
    assert "Schöckl" not in {x["name"] for x in near["sights"]} and all(x["distance_m"] <= 300 for x in near["sights"])
    n = w.count("osm_tag")
    assert n >= 9, "one Photon request per tag (incl. zoo, theme park, nature reserve)"
    c.get("/sights", params={"lat": LAT + 0.0001, "lon": LON, "radius": 300})
    c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 450, "categories": "castle"})
    assert w.count("osm_tag") == n, "same ~100 m cell and radius bucket: served from the v3 cache"
    st = c.app.state.st
    keys = [k for k in (st.cache.keys("geonames") if hasattr(st.cache, "keys") else [])]
    assert not keys or all("|v3" in k for k in keys if k.startswith("poi|"))


def test_own_places_rank_high():
    c = client(photon_world())
    c.post("/overrides", json={"id": "oma", "lat": 47.0709, "lng": 15.4397, "radius_m": 30,
                               "place": {"name": "Omas Haus", "kind": "Familie", "poi": True}})
    r = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 2000}).json()
    assert r["sights"][0]["name"] == "Omas Haus" and r["sights"][0]["own"] is True, r["sights"][:2]
    only = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 2000, "categories": "own"}).json()
    assert [x["name"] for x in only["sights"]] == ["Omas Haus"]


def test_place_name_and_address_lookup():
    w = photon_world()
    w.on("http://nominatim/search", lambda u: Resp(200, [{"lat": str(LAT), "lon": str(LON), "display_name": "Graz, Steiermark, Österreich"}]))
    c = client(w)
    r = c.get("/sights", params={"q": "Graz", "radius": 5000}).json()
    assert r["center"]["label"].startswith("Graz") and r["count"] >= 3, r
    a = c.get("/sights", params={"address": "Hauptplatz 1, Graz", "radius": 5000}).json()
    assert a["count"] >= 3
    w2 = photon_world()
    w2.on("http://nominatim/search", lambda u: Resp(200, []))
    nf = client(w2).get("/sights", params={"q": "Nirgendwo"})
    assert nf.status_code == 404, nf.text


def test_no_photon_is_503_but_own_places_still_work():
    c = client(World(), photon="")
    assert c.get("/sights", params={"lat": LAT, "lon": LON}).status_code == 503
    c.post("/overrides", json={"id": "oma", "lat": 47.0709, "lng": 15.4397, "radius_m": 30, "place": {"name": "Omas Haus", "poi": True}})
    r = c.get("/sights", params={"lat": LAT, "lon": LON}).json()
    assert [x["name"] for x in r["sights"]] == ["Omas Haus"]


def test_find_nearby_wikipedia():
    c = client(photon_world())
    j = c.get("/findNearbyWikipediaJSON", params={"lat": LAT, "lng": LON, "radius": 5, "maxRows": 10, "lang": "de"}).json()["geonames"]
    titles = [e["title"] for e in j]
    assert set(titles) == {"Uhrturm", "Schloss Eggenberg", "Kunsthaus"}, titles          # only those with wikipedia/wikidata
    assert [float(e["distance"]) for e in j] == sorted(float(e["distance"]) for e in j)
    sch = next(e for e in j if e["title"] == "Schloss Eggenberg")
    assert sch["wikipediaUrl"] == "de.wikipedia.org/wiki/Schloss_Eggenberg_(Graz)" and sch["lang"] == "de" and sch["rank"] > 0
    uhr = next(e for e in j if e["title"] == "Uhrturm")
    assert "Special:GoToLinkedPage/dewiki/Q702418" in uhr["wikipediaUrl"] and uhr["summary"] == "Wahrzeichen von Graz"
    x = ET.fromstring(c.get("/findNearbyWikipedia", params={"lat": LAT, "lng": LON, "radius": 5, "maxRows": 2}).text)
    assert len(x.findall("entry")) == 2 and x.find("entry/title") is not None
    assert c.get("/findNearbyWikipediaJSON", params={"lat": LAT, "lng": LON, "radius": 50}).status_code == 422


def test_vienna_ranking_notability_beats_a_nearby_obscure_place():
    """The real Stephansplatz answer (index with Wikidata + Wikimedia importance): the well-known sights lead."""
    from app import sights as S
    D = [("Stephansdom", "attraction", 51, 59, .5381, 1, 1, 1, 1), ("Virgilkapelle", "museum", 34, 3, .2655, 1, 1, 1, 1),
         ("Haas-Haus", "attraction", 78, 14, .2823, 1, 1, 0, 1), ("Erzb. Palais", "castle", 101, 4, .2805, 1, 0, 0, 1),
         ("Mozarthaus", "museum", 187, 17, .3972, 1, 1, 1, 1), ("Palais Equitable", "castle", 122, 8, .2428, 1, 0, 0, 1),
         ("Dom Museum", "museum", 123, 1, .2428, 1, 1, 1, 1), ("Stock im Eisen", "attraction", 97, 8, .2489, 1, 0, 0, 1),
         ("Schatzkammer", "museum", 120, 0, 0, 1, 1, 0, 1), ("Graben", "attraction", 153, 17, .3861, 1, 0, 0, 1)]
    sc = {r[0]: S._score({"category": r[1], "sitelinks": r[3], "importance": r[4], "wikidata": "Q" if r[5] else "",
                          "website": "x" if r[6] else "", "opening_hours": "x" if r[7] else "", "description": "x" if r[8] else ""}, r[2], 1500) for r in D}
    order = sorted(sc, key=sc.get, reverse=True)
    assert order[0] == "Stephansdom" and order[1] == "Mozarthaus", order
    assert order.index("Graben") < order.index("Palais Equitable") and order.index("Graben") < order.index("Schatzkammer"), order
    assert order[-1] == "Schatzkammer", "a museum without any Wikipedia article comes last"


def test_top_sight_needs_a_notable_close_sight():
    from app import sights as S
    items = [{"name": "Bank", "importance": 0.01, "score": 1.9, "distance_m": 3, "kind": "Sitz"},
             {"name": "Stephansdom", "importance": 0.82, "score": 1.5, "distance_m": 40, "kind": "Kirche", "wikidata": "Q1"},
             {"name": "Haus", "importance": 0.35, "score": 1.1, "distance_m": 10, "kind": "Haus"}]
    assert S.top_sight(items, 0.30)["name"] == "Stephansdom"
    assert S.top_sight(items, 0.90) is None
    assert S.top_sight([{"name": "Zuhause", "own": True, "score": 0.5, "distance_m": 5, "kind": "Zuhause"}], 0.9)["name"] == "Zuhause"
    assert S.top_sight([], 0.3) is None


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} sights checks passed")


def test_water_category_adds_rivers_and_lake_from_the_maritime_server():
    w = photon_world()
    w.on("http://maritime", lambda url: Resp(200, {
        "contained_in": [{"layer": "lakes", "name": "Bodensee", "lang": "de"}],
        "rivers": [{"name": "Rhein", "lang": "de", "lat": LAT + 0.01, "lon": LON, "distance_km": 1.1}]}))
    c = client(w, maritime="http://maritime")
    only = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 5000, "categories": "water"}).json()
    assert [(x["name"], x["kind"]) for x in only["sights"]] == [("Bodensee", "See"), ("Rhein", "Fluss")] or \
           [(x["name"], x["kind"]) for x in only["sights"]] == [("Rhein", "Fluss"), ("Bodensee", "See")], only["sights"]
    assert only["categories"] == ["water"]
    mixed = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 5000, "categories": "museum,water", "limit": 20}).json()
    kinds = {x["category"] for x in mixed["sights"]}
    assert kinds == {"museum", "water"}, kinds
    plain = c.get("/sights", params={"lat": LAT, "lon": LON, "radius": 5000}).json()
    assert all(x["category"] != "water" for x in plain["sights"])       # only on request
