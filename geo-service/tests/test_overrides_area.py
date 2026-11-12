"""Override areas: circle and polygon.   python tests/test_overrides_area.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.overrides import Overrides, in_polygon  # noqa: E402

d = tempfile.mkdtemp()
o = Overrides(os.path.join(d, "ov.json"))


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        sys.exit(1)


SQ = [[48.1450, 16.3250], [48.1460, 16.3250], [48.1460, 16.3265], [48.1450, 16.3265]]
check("in_polygon inside/outside", in_polygon(48.1455, 16.3257, SQ) and not in_polygon(48.1470, 16.3257, SQ)
      and not in_polygon(48.1455, 16.3300, SQ))
concave = [[0, 0], [4, 0], [4, 4], [2, 1], [0, 4]]       # (lat, lng): a bow with the notch cut in from the lng=4 side
check("concave polygon: body inside, notch outside", in_polygon(2, 0.5, concave) and not in_polygon(2, 3, concave), (in_polygon(2, 0.5, concave), in_polygon(2, 3, concave)))

o.upsert({"id": "area", "lat": 48.1455, "lng": 16.3257, "radius_m": 5, "polygon": SQ, "reverse": {"locality": "Schulareal"}})
check("inside polygon but far from anchor matches", o.find("reverse", 48.14515, 16.32505) is not None)      # ~40 m from the anchor
check("circle radius is ignored when a polygon exists", o.find("reverse", 48.1455, 16.3257) is not None)
check("outside polygon does not match", o.find("reverse", 48.1461, 16.3257) is None)
o.upsert({"id": "circle", "lat": 48.1500, "lng": 16.3300, "radius_m": 30, "elevation": 200})
check("circle still works", o.find("elevation", 48.1501, 16.3300)[0] == "circle" and o.find("elevation", 48.1510, 16.3300) is None)
check("polygon survives the file round trip", json.load(open(o.path))[0]["polygon"] == SQ)
check("at() sees polygons", o.at(48.14515, 16.32505)["id"] == "area")
for bad in ([[1, 2], [3, 4]], [[1, 2], [3, 4], ["x", 1]], [[100, 2], [3, 4], [5, 6]], "nope"):
    try:
        o.upsert({"id": "bad", "lat": 1, "lng": 1, "polygon": bad, "elevation": 1})
        check(f"bad polygon rejected {bad!r}", False)
    except ValueError:
        pass
check("bad polygons rejected", True)
# a broken entry in the file only disables itself
raw = json.load(open(o.path)) + [{"id": "broken", "lat": 1, "lng": 1, "polygon": [[1, 2]], "elevation": 5}]
json.dump(raw, open(o.path, "w"))
o2 = Overrides(o.path)
check("one bad entry does not disable the rest", len(o2.items()) == 2 and "broken" in o2.error, o2.error)
print("\nall override-area tests passed")
