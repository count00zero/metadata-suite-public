"""/reverse extras: house_number, postcode, neighbourhood (only present when the backend knows them).   python tests/test_reverse_extras.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app import backends  # noqa: E402
from app.config import Settings  # noqa: E402
from app import overrides  # noqa: E402

fails = []


def check(name, ok, info=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {info}"))
    if not ok:
        fails.append(name)


NOM = {"lat": "39.96", "lon": "-82.99", "name": "", "address": {"house_number": "1", "road": "South Third Street",
       "neighbourhood": "Uptown District", "quarter": "Downtown", "city": "Columbus", "county": "Franklin County",
       "state": "Ohio", "postcode": "43216", "country": "USA", "country_code": "us"}}
PHO = {"features": [{"geometry": {"coordinates": [-82.99, 39.96]}, "properties": {
       "housenumber": "7", "street": "Capitol Square", "postcode": "43215", "district": "Uptown District",
       "city": "Columbus", "state": "OH", "country": "USA", "countrycode": "US"}}]}
PEL = {"features": [{"properties": {"housenumber": "9", "street": "Main St", "postalcode": "1010", "neighbourhood": "Mitte",
       "locality": "Wien", "country": "Österreich", "country_a": "AUT", "region": "Wien", "name": "Haus"}}]}

orig = backends._get_json
s = Settings.__new__(Settings)
for k, v in dict(photon="http://p", nominatim="http://n", pelias="http://x", http_timeout=1).items():
    setattr(s, k, v)

backends._get_json = lambda url, t, h=None: (NOM, True)
r, _ = backends._nominatim(s, 39.96, -82.99, "de")
check("nominatim", (r.house_number, r.postcode, r.neighbourhood) == ("1", "43216", "Uptown District"), r)
backends._get_json = lambda url, t, h=None: (PHO, True)
r, _ = backends._photon(s, 39.96, -82.99, "de")
check("photon", (r.house_number, r.postcode, r.neighbourhood) == ("7", "43215", "Uptown District"), r)
d = r.to_dict()
check("in response", d["house_number"] == "7" and d["postcode"] == "43215" and d["neighbourhood"] == "Uptown District", d)
backends._get_json = lambda url, t, h=None: (PEL, True)
r, _ = backends._pelias(s, 48.2, 16.4, "de")
check("pelias", (r.house_number, r.postcode, r.neighbourhood) == ("9", "1010", "Mitte"), r)
backends._get_json = orig

e = backends.GeoResult(country="X", city="Y", source="t").to_dict()
check("absent when unknown (old shape unchanged)", not any(k in e for k in backends.EXTRA_FIELDS), e)
rt = backends.GeoResult.from_dict(d)
check("cache round trip", rt.postcode == "43215" and rt.house_number == "7", rt)
o = overrides.apply_reverse({"found": True, **d}, "o1", {"postcode": "99999"})
check("override keeps/patches extras", o["postcode"] == "99999" and o["house_number"] == "7", o)

print("FAILED: " + ", ".join(fails) if fails else "all ok")
sys.exit(1 if fails else 0)
