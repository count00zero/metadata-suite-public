"""childrenJSON: found by scanning boundaries, membership decided by the address chain.   python tests/test_children.py"""
import os
import sys
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import EMPTY_FEATURES, Resp, World, client  # noqa: E402

# toy country XX: state "Teststate" = lon 10..12, lat 40..42; counties A (west), B (south-east), C (north-east);
# each county has two municipalities (split at the middle of its lon range). "Otherstate" lies east of it.
STATE = (10, 40, 12, 42)
COUNTY = {"A": (10, 40, 11, 42), "B": (11, 40, 12, 41), "C": (11, 41, 12, 42)}
OTHER = (12, 40, 14, 42)


def rect(b):
    w, s, e, n = b
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def inside(b, lon, lat):
    return b[0] <= lon < b[2] and b[1] <= lat < b[3]


def muni_of(county, lon):
    w, s, e, n = COUNTY[county]
    mid = (w + e) / 2
    return (f"{county}1", (w, s, mid, n)) if lon < mid else (f"{county}2", (mid, s, e, n))


def locate(lon, lat):
    if inside(STATE, lon, lat):
        c = next(k for k, b in COUNTY.items() if inside(b, lon, lat))
        return "Teststate", c
    if inside(OTHER, lon, lat):
        return "Otherstate", "Z"
    return None, None


calls = {"reverse": 0}
DEAD_EAST = False


def nominatim(url):
    q = parse_qs(urlparse(url).query)
    if "/search" in url:
        if q.get("state") == ["Teststate"] or q.get("county"):
            if q.get("county"):
                box = COUNTY[q["county"][0].replace("County ", "")]
            else:
                box = STATE
            return Resp(200, [{"osm_type": "relation", "osm_id": 1, "lat": "41", "lon": "11", "name": "x",
                               "boundingbox": [str(box[1]), str(box[3]), str(box[0]), str(box[2])], "geojson": rect(box), "address": {}}])
        return Resp(200, [])
    calls["reverse"] += 1
    lat, lon = float(q["lat"][0]), float(q["lon"][0])
    zoom = q.get("zoom", [""])[0]
    state, county = locate(lon, lat)
    if state is None:
        return Resp(200, {"error": "Unable to geocode"})
    addr = {"country_code": "xx", "country": "Testland", "state": state, "county": "County " + county}
    if zoom == "5":
        box = STATE if state == "Teststate" else OTHER
        return Resp(200, {"osm_type": "relation", "osm_id": 10, "lat": "41", "lon": "11", "name": state, "addresstype": "state",
                          "geojson": rect(box), "address": addr})
    if zoom == "8":
        box = COUNTY.get(county, OTHER)
        return Resp(200, {"osm_type": "relation", "osm_id": 20, "lat": str((box[1] + box[3]) / 2), "lon": str((box[0] + box[2]) / 2),
                          "name": "County " + county, "addresstype": "county", "geojson": rect(box), "address": addr})
    if DEAD_EAST and zoom == "10" and lon >= 10.5:
        return Resp(200, {"error": "Unable to geocode"})
    mname, box = muni_of(county, lon) if county != "Z" else ("Z", OTHER)
    addr = {**addr, "town": "Town " + mname}
    return Resp(200, {"osm_type": "relation", "osm_id": 30, "lat": str((box[1] + box[3]) / 2), "lon": str((box[0] + box[2]) / 2),
                      "name": "Town " + mname, "addresstype": "town", "class": "place", "type": "town",
                      "geojson": rect(box), "address": addr})


def world():
    w = World(); w.on("http://pelias", EMPTY_FEATURES); w.on("http://nominatim", nominatim)
    return w


def chain(c):
    leaf = c.get("/findNearbyPlaceNameJSON", params={"lat": 41.5, "lng": 10.5}).json()["geonames"][0]
    h = c.get("/hierarchyJSON", params={"geonameId": leaf["geonameId"]}).json()["geonames"]
    return {r["fcode"]: r for r in h}


def test_counties_of_a_state():
    c = client(world())
    h = chain(c)
    j = c.get("/childrenJSON", params={"geonameId": h["ADM1"]["geonameId"]}).json()
    assert j["totalResultsCount"] == 3 and "complete" not in j, j
    assert [r["name"] for r in j["geonames"]] == ["County A", "County B", "County C"], j
    assert all(r["fcode"] == "ADM2" and r["countryCode"] == "XX" and "lat" in r for r in j["geonames"])
    assert "_ctx" not in j["geonames"][0]
    # the ids are the very ones hierarchy hands out for the county
    assert h["ADM2"]["geonameId"] in [r["geonameId"] for r in j["geonames"]]


def test_municipalities_of_a_county_and_skip_logic():
    c = client(world())
    h = chain(c)
    counties = c.get("/childrenJSON", params={"geonameId": h["ADM1"]["geonameId"]}).json()["geonames"]
    a = next(r for r in counties if r["name"] == "County A")
    before = calls["reverse"]
    j = c.get("/childrenJSON", params={"geonameId": a["geonameId"], "style": "FULL"}).json()
    assert [r["name"] for r in j["geonames"]] == ["Town A1", "Town A2"], j
    assert all(r["fcl"] == "P" and r["fcode"] == "PPLA3" for r in j["geonames"])
    used = calls["reverse"] - before
    assert used <= 12, f"only a handful of lookups thanks to the covered-area skip, got {used}"
    again = calls["reverse"]
    assert c.get("/childrenJSON", params={"geonameId": a["geonameId"]}).json()["totalResultsCount"] == 2
    assert calls["reverse"] == again, "answer comes from the cache"


def test_country_xml_paging_and_edges():
    c = client(world())
    h = chain(c)
    # a populated place has no children
    leaf_id = h["PPL"]["geonameId"] if "PPL" in h else None
    first = c.get("/findNearbyPlaceNameJSON", params={"lat": 41.5, "lng": 10.5}).json()["geonames"][0]
    assert c.get("/childrenJSON", params={"geonameId": first["geonameId"]}).json() == {"totalResultsCount": 0, "geonames": []}
    x = ET.fromstring(c.get("/children", params={"geonameId": h["ADM1"]["geonameId"], "maxRows": 2}).text)
    assert x.find("totalResultsCount").text == "3" and len(x.findall("geoname")) == 2
    assert c.get("/childrenJSON", params={"geonameId": 987654}).json()["status"]["value"] == 15
    assert client(World()).get("/childrenJSON", params={"geonameId": 1}).json()["status"]["value"] == 15


def test_incomplete_when_probe_cap_hits():
    global DEAD_EAST
    os.environ["GN_CHILDREN_MAX_PROBES"] = "10"
    DEAD_EAST = True
    try:
        c = client(world())
        h = chain(c)
        counties = c.get("/childrenJSON", params={"geonameId": h["ADM1"]["geonameId"]}).json()["geonames"]
        a = next(r for r in counties if r["name"] == "County A")
        j = c.get("/childrenJSON", params={"geonameId": a["geonameId"]}).json()
        assert j.get("complete") is False and j["totalResultsCount"] >= 1, j
        n = calls["reverse"]
        c.get("/childrenJSON", params={"geonameId": a["geonameId"]})
        assert calls["reverse"] > n, "an incomplete list is never cached"
    finally:
        DEAD_EAST = False
        os.environ.pop("GN_CHILDREN_MAX_PROBES", None)


def test_geometry():
    from app import children as ch
    sq = [rect((0, 0, 4, 4))["coordinates"][0], [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]]     # with a hole
    assert ch.point_in([sq], 3, 3) and not ch.point_in([sq], 1.5, 1.5) and not ch.point_in([sq], 5, 5)
    g = ch.grid((0, 0, 4, 4), [sq], 1.0)
    assert (3.5, 3.5) in g and (1.5, 1.5) not in g


def test_membership_is_checked_by_address():
    from app import children as ch
    ctx = {"level": "ADM2", "cc": "XX", "state": "Teststate", "county": "County A", "name": "County A"}
    ok = {"country_code": "xx", "state": "Teststate", "county": "County A"}
    assert ch._is_member(ok, ctx, "PPL")
    assert not ch._is_member({**ok, "state": "Otherstate"}, ctx, "PPL")
    assert not ch._is_member({**ok, "county": "County B"}, ctx, "PPL")
    assert not ch._is_member({**ok, "country_code": "yy"}, ctx, "ADM2")


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} children checks passed")
