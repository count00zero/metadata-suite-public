"""water.py: rivers / lake from the maritime server as sights, and the /sights category.   python tests/test_water.py"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import backends, water  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


ANSWER = {"contained_in": [{"layer": "iho", "name": "Adriatic"}, {"layer": "lakes_europe", "name": "Neusiedler See"}],
          "rivers": [{"name": "Donau", "lat": 48.22, "lon": 16.42, "distance_km": 1.1},
                     {"name": "Wien", "lat": 48.21, "lon": 16.36, "distance_km": 4.0}, {"name": "ohne Punkt"}]}
calls = []
backends._get_json = lambda url, timeout, headers=None: (calls.append(url) or (ANSWER, True))
s = types.SimpleNamespace(maritime="http://m:5000/", http_timeout=3)
rows = water.near(s, 48.2, 16.4, 5000, 10, "de")
check("lake and rivers returned", [r["name"] for r in rows] == ["Neusiedler See", "Donau", "Wien"], str([r["name"] for r in rows]))
check("kinds", [r["kind"] for r in rows] == ["See", "Fluss", "Fluss"])
check("sight-shaped", all(r["category"] == "water" and "distance_m" in r and "bearing" in r and "direction" in r for r in rows))
check("distance from the nearest river point", 1000 < rows[1]["distance_m"] < 3000, str(rows[1]["distance_m"]))
check("lake distance 0", rows[0]["distance_m"] == 0)
check("radius sent as river_radius_km", "river_radius_km=5.0" in calls[0] and "nearby=0" in calls[0], calls[0])
check("no maritime server -> nothing", water.near(types.SimpleNamespace(maritime="", http_timeout=3), 1, 1, 1000, 5) == [])
backends._get_json = lambda url, timeout, headers=None: (None, False)
check("server down -> nothing", water.near(s, 1, 1, 1000, 5) == [])
sys.exit(1 if FAILS else 0)
