"""Own GeoNames database: import of the dumps and the queries on it.   python tests/test_gazetteer.py"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gazfix  # noqa: E402
from app import gazetteer as gz  # noqa: E402


def built(zip_places=False, **kw):
    d = gazfix.make(zip_places)
    db = os.path.join(tempfile.mkdtemp(), "gazetteer.sqlite")
    counts = gz.build(d, db, ("de", "en"), log=lambda m: None, **kw)
    return gz.Gazetteer(db, lambda cc: {"AT": "Austria", "DE": "Germany"}.get(cc, "")), counts, d, db


def test_build_and_meta():
    g, counts, d, db = built()
    assert counts["place"] == 13 and counts["alt"] == 4 and counts["postal"] == 4 and counts["hierarchy"] == 5, counts   # historic name dropped (fr is not wanted)
    assert g.available() and g.meta()["n_place"] == "13" and g.has("postal") and g.has("hierarchy")
    assert not os.path.exists(db + ".building")
    g2, c2, *_ = built(zip_places=True)
    assert c2["place"] == 13, "allCountries.zip works like the .txt"
    try:
        gz.build(tempfile.mkdtemp(), db + "x", log=lambda m: None)
        assert False
    except FileNotFoundError:
        pass


def test_nearby():
    g, *_ = built()
    pl = g.nearby(47.9529, 16.2296, limit=3, fclasses=["P"])
    assert [r["name"] for r in pl] == ["Musterdorf", "Bad Vöslau", "Baden"], pl
    assert pl[0]["fcode"] == "PPL" and pl[0]["population"] == 2500 and pl[0]["geonameId"] == 2774313
    assert pl[0]["adminName1"] == "Lower Austria" and pl[0]["adminName2"] == "Bezirk Baden" and pl[0]["countryName"] == "Austria"
    assert pl[0]["fcodeName"] == "populated place" and pl[0]["distance"] < 0.1 and pl == sorted(pl, key=lambda r: r["distance"])
    allc = g.nearby(47.9529, 16.2296, limit=10, radius_km=1)
    assert {r["name"] for r in allc} == {"Musterdorf", "Schloss Musterdorf", "Hauptstraße"}, [r["name"] for r in allc]
    assert [r["fcode"] for r in g.nearby(47.9529, 16.2296, limit=5, radius_km=1, fclasses=["R"])] == ["RD"]
    far = g.nearby(47.0, 15.0, limit=1, fclasses=["P"])
    assert far and far[0]["name"] == "Graz", "the circle widens until something is found"
    assert g.nearby(0, 0, limit=1, radius_km=5) == []
    de = g.nearby(48.2085, 16.3721, limit=1, fclasses=["P"], lang="de")
    assert de[0]["name"] == "Wien" and de[0]["toponymName"] == "Vienna"
    assert g.nearby(48.2085, 16.3721, limit=1, fclasses=["P"], lang="fr")[0]["name"] == "Vienna", "fr is not imported -> primary name"


def test_search():
    g, *_ = built()
    recs, total = g.search("Wien")
    assert [r["geonameId"] for r in recs] == [2761369] and total == 1, "found through the German alternate name"
    assert g.search("Vienna")[0][0]["name"] == "Vienna"
    assert g.search("wien", lang="de")[0][0]["name"] == "Wien"
    recs, total = g.search("Baden")
    assert recs[0]["name"] == "Baden", "an exact name match ranks first"
    assert [r["name"] for r in g.search("Bad", countries=["DE"])[0]] == ["Baden-Baden"], "whole word 'Bad' only matches Baden-Baden (token Bad) ... via prefix retry"
    pre, _ = g.search("Muster", startswith="Muster")
    assert [r["fcode"] for r in pre] == ["PPL", "ADM3"] and all(r["name"] == "Musterdorf" for r in pre), pre     # "starts with": the castle does not
    names = [r["name"] for r in g.search("Bad", startswith="Bad", order_pop=True)[0]]
    assert names == ["Baden-Baden", "Baden", "Bad Vöslau", "Bezirk Baden"][:len(names)] and names[0] == "Baden-Baden" and "Bad Vöslau" in names, names
    assert [r["name"] for r in g.search("Musterdorf", fclasses=["A"])[0]] == ["Musterdorf"]
    assert [r["fcode"] for r in g.search("Musterdorf", fcodes=["CSTL"])[0]] == ["CSTL"]
    assert [r["name"] for r in g.search("Musterdorf", equals="musterdorf")[0]][:1] == ["Musterdorf"]
    bb, _ = g.search("Baden", bbox=(16.0, 47.9, 16.5, 48.1))
    assert [r["name"] for r in bb] == ["Baden", "Bezirk Baden"], [r["name"] for r in bb]       # both lie in the box; populated place first (exact name)
    big, _ = g.search("Baden", min_pop=50000)
    assert [r["name"] for r in big] == ["Bezirk Baden", "Baden-Baden"]
    page, total = g.search("Baden", limit=1, offset=1)
    assert len(page) == 1 and total >= 2
    assert g.search("")[0] == [] and g.search("nichtvorhanden")[0] == []
    assert g.search("Köln")[1] == 0


def test_hierarchy_and_children():
    g, *_ = built()
    assert [r["fcode"] for r in g.parents(2774313)] == ["PCLI", "ADM1", "ADM2", "ADM3"], [r["fcode"] for r in g.parents(2774313)]
    kids, total = g.children(2770542)
    assert [r["name"] for r in kids] == ["Bezirk Baden", "Bezirk Mistelbach"] and total == 2
    assert [r["name"] for r in g.children(2782113)[0]] == ["Lower Austria"]
    assert g.children(2774313)[1] == 0


def test_children_without_hierarchy_file():
    d = gazfix.make()
    os.remove(os.path.join(d, "hierarchy.txt"))
    db = os.path.join(tempfile.mkdtemp(), "g.sqlite")
    gz.build(d, db, ("de",), log=lambda m: None)
    g = gz.Gazetteer(db)
    assert not g.has("hierarchy")
    assert [r["name"] for r in g.children(2770542)[0]] == ["Bezirk Baden", "Bezirk Mistelbach"]
    assert [r["name"] for r in g.children(2782113)[0]] == ["Lower Austria"]
    assert [r["fcode"] for r in g.parents(2774313)] == ["PCLI", "ADM1", "ADM2", "ADM3"]


def test_postal():
    g, *_ = built()
    near = g.postal_near(47.9529, 16.2296, radius_km=10, limit=5)
    assert [r["code"] for r in near] == ["2552", "2540", "2500"] and near[0]["placeName"] == "Musterdorf" and near[0]["distance"] < 0.1
    assert near[0]["adminName1"] == "Niederösterreich" and near[0]["adminCode1"] == "03"
    assert [r["code"] for r in g.postal_search(code="25", country="AT")] == ["2500", "2540", "2552"]
    assert [r["code"] for r in g.postal_search(place="bad v")] == ["2540"]
    assert g.postal_search() == []


def test_concurrent_requests():
    """Regression: many threads on one Gazetteer (the load test) must never mix up rows ('tuple index out of range')."""
    import concurrent.futures
    import threading
    g0, _c, d, db = built()
    pts = [(47.9529, 16.2296), (48.2085, 16.3721), (47.0, 15.0), (47.5, 12.2)]

    def work(g, i):
        lat, lng = pts[i % len(pts)]
        return [r["geonameId"] for r in g.nearby(lat, lng, limit=3, fclasses=["P"], lang="de")], g.search("kott", limit=3)[1]

    want = [work(g0, i) for i in range(len(pts))]
    for _round in range(25):                       # cold caches every round: the first calls race on the shared lookups
        g = gz.Gazetteer(db, lambda cc: {"AT": "Austria", "DE": "Germany"}.get(cc, ""))
        gate = threading.Barrier(24)

        def go(i):
            gate.wait()
            return work(g, i)

        with concurrent.futures.ThreadPoolExecutor(24) as ex:
            res = list(ex.map(go, range(24)))
        assert all(r == want[i % len(pts)] for i, r in enumerate(res)), "every thread got the same complete answer as a single caller"


def test_nearby_is_nearest_first_even_with_many_candidates():
    """The database sorts by distance and only a few rows reach Python: the answer must still be the true nearest ones."""
    g, *_ = built()
    one = g.nearby(47.9529, 16.2296, limit=1, fclasses=["P"])
    assert one[0]["name"] == "Musterdorf"
    every = g.nearby(47.9529, 16.2296, limit=50, radius_km=1000)
    assert [r["distance"] for r in every] == sorted(r["distance"] for r in every) and every[0]["name"] == "Musterdorf"


def test_missing_database():
    g = gz.Gazetteer("/nonexistent/gazetteer.sqlite")
    assert not g.available() and g.nearby(1, 2) == [] and g.search("x") == ([], 0) and g.get(1) is None and g.postal_near(1, 2) == []


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} gazetteer checks passed")
