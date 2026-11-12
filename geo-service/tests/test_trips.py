"""Trip detection on synthetic GPS days.   python tests/test_trips.py"""
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import trips  # noqa: E402
from app.trackmath import Pt  # noqa: E402

TZ = ZoneInfo("Europe/Vienna")
HOME = (48.1529, 16.3296)
COLOMBO, MALE, GRAZ = (6.93, 79.85), (4.17, 73.51), (47.07, 15.43)


def pts(day: date, pos, hours=range(0, 24, 3)):
    out = []
    for h in hours:
        t = datetime(day.year, day.month, day.day, h, 30, tzinfo=TZ)
        out.append(Pt(int(t.timestamp()), pos[0] + 0.0001 * h, pos[1], "T"))
    return out


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        sys.exit(1)


allp, d0 = [], date(2026, 8, 1)
plan = {}
for i in range(40):
    d = d0 + timedelta(days=i)
    if 4 <= i <= 14:
        plan[d] = COLOMBO if i < 10 else MALE            # 05.08.-15.08.: Sri Lanka, then Malediven
    elif i == 20:
        plan[d] = GRAZ                                   # a day trip (below min_days=2)
    elif 25 <= i <= 27:
        plan[d] = GRAZ                                   # 3 days in Graz (domestic)
    elif i == 31:
        continue                                         # a day without any data
    else:
        plan[d] = HOME
for d, pos in plan.items():
    allp += pts(d, pos)
dp = trips.daily(allp, TZ)
home = trips.home_from_nights(allp, TZ)
check("home found from the nights", home and abs(home[0] - HOME[0]) < 0.01 and abs(home[1] - HOME[1]) < 0.01, home)
found = trips.detect(dp, home, d0, d0 + timedelta(days=39), away_km=60, min_days=2, max_gap=1)
check("two trips, the day trip is ignored", len(found) == 2, [(t["start"], t["end"]) for t in found])
t1, t2 = found
check("long trip bounds", t1["start"] == date(2026, 8, 5) and t1["end"] == date(2026, 8, 15) and len(t1["away"]) == 11)
check("domestic trip bounds", t2["start"] == date(2026, 8, 26) and t2["end"] == date(2026, 8, 28))
check("min_days=1 keeps the day trip", len(trips.detect(dp, home, d0, d0 + timedelta(days=39), 60, 1, 1)) == 3)
# a single home day inside a trip is bridged, two are not
plan2 = dict(plan); plan2[date(2026, 8, 10)] = HOME
p2 = [p for d, pos in plan2.items() for p in pts(d, pos)]
check("one home day inside a trip is bridged", len(trips.detect(trips.daily(p2, TZ), home, d0, d0 + timedelta(days=39), 60, 2, 1)) == 2)
plan3 = dict(plan2); plan3[date(2026, 8, 11)] = HOME
p3 = [p for d, pos in plan3.items() for p in pts(d, pos)]
check("two home days split it", len(trips.detect(trips.daily(p3, TZ), home, d0, d0 + timedelta(days=39), 60, 2, 1)) == 3)
# days without any data inside a trip are bridged (flight mode)
plan4 = {d: pos for d, pos in plan.items() if not (date(2026, 8, 8) <= d <= date(2026, 8, 9))}
p4 = [p for d, pos in plan4.items() for p in pts(d, pos)]
check("data gap inside a trip is bridged", len(trips.detect(trips.daily(p4, TZ), home, d0, d0 + timedelta(days=39), 60, 2, 1)) == 2)

REV = {COLOMBO: {"country": "Sri Lanka", "country_a": "LKA", "city": "Colombo"}, MALE: {"country": "Malediven", "country_a": "MDV", "city": "Malé"},
       GRAZ: {"country": "Österreich", "country_a": "AUT", "state": "Steiermark", "city": "Graz"}}


def reverse(lat, lon):
    return next((v for k, v in REV.items() if abs(k[0] - lat) < 0.5 and abs(k[1] - lon) < 0.5), None)


n1 = trips.name_trip(t1, dp, reverse, "AT")
check("foreign trip is named after the countries", n1["name"] == "Sri Lanka & Malediven" and n1["abroad"], n1)
n2 = trips.name_trip(t2, dp, reverse, "AT")
check("domestic trip is named after the region", n2["name"] == "Steiermark" and not n2["abroad"], n2)
s1 = trips.summary(t1, n1)
check("folder-style name", s1["folder"] == "2026.08.05-2026.08.15 Sri Lanka & Malediven" and s1["days"] == 11 and s1["max_km"] > 7000, s1)
check("join rules", trips._join(["A", "B", "C", "D"]) == "A, B, C u. a.")
HOME = (47.07, 15.44)
pl = trips.place_title({"city": "Triest", "country": "Italien"}, 45.65, 13.78, HOME, 100)
check("far from home: 'Ort und Umgebung'", pl and pl["name"] == "Triest und Umgebung" and pl["source"] == "place", pl)
check("near home: no title", trips.place_title({"city": "Graz"}, 47.1, 15.5, HOME, 100) is None)
check("off switch / unknown home / no place", trips.place_title({"city": "Triest"}, 45.65, 13.78, HOME, 0) is None
      and trips.place_title({"city": "Triest"}, 45.65, 13.78, None, 100) is None and trips.place_title(None, 45.65, 13.78, HOME, 100) is None)
check("only a country: no 'und Umgebung'", trips.place_title({"country": "Italien"}, 45.65, 13.78, HOME, 100)["name"] == "Italien")
check("locality when no city", trips.place_title({"locality": "Sistiana", "state": "Friaul"}, 45.75, 13.63, HOME, 100)["name"] == "Sistiana und Umgebung")
print("\nall trip tests passed")
