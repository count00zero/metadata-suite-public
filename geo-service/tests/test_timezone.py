"""GeoNames timezone service.   python tests/test_timezone.py"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import World, client  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def minutes(s):
    return int(s[11:13]) * 60 + int(s[14:16])


c = client(World())
r = c.get("/timezoneJSON", params={"lat": "6.02499420", "lng": "80.21890920", "date": "2026-10-10"}).json()
check("zone + country", r.get("timezoneId") == "Asia/Colombo" and r.get("countryCode") == "LK" and r.get("countryName") == "Sri Lanka", r)
check("offsets", r.get("gmtOffset") == 5.5 and r.get("rawOffset") == 5.5 and r.get("dstOffset") == 5.5, r)
check("sunrise ~05:55 / sunset ~17:57 local", abs(minutes(r["sunrise"]) - (5 * 60 + 55)) <= 5 and abs(minutes(r["sunset"]) - (17 * 60 + 57)) <= 5, (r["sunrise"], r["sunset"]))
check("fields like GeoNames", set(r) == {"sunrise", "lat", "lng", "countryCode", "gmtOffset", "rawOffset", "sunset", "timezoneId",
                                         "dstOffset", "countryName", "time"}, sorted(r))
v = c.get("/timezoneJSON", params={"lat": 48.15, "lng": 16.33, "date": "2026-07-01"}).json()
check("Vienna: winter 1 / summer 2, raw 1", (v["gmtOffset"], v["dstOffset"], v["rawOffset"]) == (1.0, 2.0, 1.0) and v["countryCode"] == "AT", v)
check("Vienna sunrise on 2026-07-01 ~05:00 local", abs(minutes(v["sunrise"]) - (5 * 60 + 5)) <= 10, v["sunrise"])
s = c.get("/timezoneJSON", params={"lat": -33.87, "lng": 151.2, "date": "2026-01-15"}).json()
check("Sydney: DST in January, raw = standard", s["timezoneId"] == "Australia/Sydney" and s["gmtOffset"] == 11.0 and s["rawOffset"] == 10.0, s)
x = c.get("/timezone", params={"lat": 6.0249942, "lng": 80.2189092}).text
root = ET.fromstring(x)
check("XML flavour", root.find("timezone/timezoneId").text == "Asia/Colombo" and root.find("timezone/countryCode").text == "LK", x[:200])
check("errors as status", "status" in c.get("/timezoneJSON").json() and "status" in c.get("/timezoneJSON", params={"lat": 1, "lng": 2, "date": "x"}).json())
polar = c.get("/timezoneJSON", params={"lat": 78.2, "lng": 15.6, "date": "2026-06-21"}).json()
check("polar day -> sunrise/sunset null", polar["sunrise"] is None and polar["sunset"] is None, polar)
cs = client(World(), token="sekrit")
check("token via ?token=", cs.get("/timezoneJSON", params={"lat": 1, "lng": 2}).status_code == 401
      and cs.get("/timezoneJSON", params={"lat": 6.02, "lng": 80.2, "token": "sekrit"}).status_code == 200)
print("\n%s" % ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
