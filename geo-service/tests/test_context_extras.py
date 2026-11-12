"""Daylight, moon, holidays, country info, sights, trips, clock check, name clean-up, Meine Orte.
python tests/test_context_extras.py"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import EMPTY_FEATURES, Resp, World, client, gpx  # noqa: E402

VIE = timezone(timedelta(hours=2))


def ep(y, m, d, h=12, mi=0, tz=timezone.utc):
    return int(datetime(y, m, d, h, mi, tzinfo=tz).timestamp())


def photon(country, cc, city, state="", county=""):
    def f(url):
        if "osm_tag" in url:
            return Resp(200, {"features": []})
        pr = {"country": country, "countrycode": cc, "city": city, "name": city}
        if state:
            pr["state"] = state
        if county:
            pr["county"] = county
        return Resp(200, {"features": [{"properties": pr}]})
    return f


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        sys.exit(1)


# ------------------------------------------------------------------ sky, holiday, country info, wind
w = World(); w.on("http://photon", photon("Österreich", "AT", "Wien", "Wien"))
c = client(w)
r = c.get("/context", params={"lat": 48.2082, "lon": 16.3738, "time": ep(2025, 10, 26, 11, 0)}).json()
check("holiday: Nationalfeiertag", (r["holiday"] or {}).get("name") == "Nationalfeiertag" and "Nationalfeiertag" in r["parts_de"]["holiday"], r.get("holiday"))
check("weekend flag (Sunday)", r["weekend"] is True)
check("daylight present, noon is Tag", r["daylight"]["phase"] in ("Tag", "tagsüber") or "Tag" in r["daylight"]["phase"], r["daylight"])
check("moon present", "phase_de" in r["moon"] or "name_de" in r["moon"] or len(r["moon"]) >= 2, r["moon"])
check("no country info at home", "country_info" not in r)
r = c.get("/context", params={"lat": 48.2082, "lon": 16.3738, "time": ep(2025, 10, 27, 11, 0)}).json()
check("ordinary day has no holiday", r["holiday"] is None and "holiday" not in r["parts_de"])

w.on("http://photon", photon("Italien", "IT", "Rom"))
r = c.get("/context", params={"lat": 41.9, "lon": 12.5, "time": ep(2025, 6, 2, 10, 0)}).json()
check("country info abroad", r["country_info"]["capital"] == "Rome" and "Italienisch" in r["country_info"]["languages"], r.get("country_info"))
check("country phrase", "Hauptstadt Rome" in r["parts_de"]["country"], r["parts_de"])
check("Festa della Repubblica", (r["holiday"] or {}).get("country") == "IT", r["holiday"])

# golden hour phrase: Vienna, 2025-06-21, ~20:30 local
r = c.get("/context", params={"lat": 48.2082, "lon": 16.3738, "time": ep(2025, 6, 21, 20, 20, VIE)}).json()
check("golden hour phrase", "goldenen Stunde" in r["parts_de"].get("daylight", ""), r["parts_de"])

# ------------------------------------------------------------------ name clean-up
w2 = World(); w2.on("http://photon", photon("Österreich", "AT", "Marktgemeinde Musterdorf", "Niederösterreich", "Politischer Bezirk Baden"))
c2 = client(w2)
r = c2.get("/reverse", params={"lat": 48.19, "lon": 16.34}).json()
check("prefixes stripped", r["city"] == "Musterdorf" and r["county"] == "Bezirk Baden" and "Marktgemeinde" not in r["label"], r)
st = c2.app.state.st
open(st.aliases.path, "w", encoding="utf-8").write(json.dumps({"city": {"Musterdorf": "Musterdorf (NÖ)"}}))
r = c2.get("/reverse", params={"lat": 48.19, "lon": 16.34}).json()
check("alias applied (cache stays raw)", r["city"] == "Musterdorf (NÖ)", r)
c3 = client(w2, normalize_names=False)
r = c3.get("/reverse", params={"lat": 47.5, "lon": 16.34}).json()
check("normalisation can be switched off", r["city"] == "Marktgemeinde Musterdorf", r)

# ------------------------------------------------------------------ Meine Orte + sights
w4 = World(); w4.on("http://photon", photon("Österreich", "AT", "Graz", "Steiermark"))
c4 = client(w4)
c4.post("/overrides", json={"id": "oma", "lat": 47.0707, "lng": 15.4395, "radius_m": 30,
                            "place": {"name": "Omas Haus", "kind": "Familie", "poi": True}})
c4.post("/overrides", json={"id": "daheim", "lat": 47.9, "lng": 16.2, "radius_m": 50,
                            "place": {"name": "Daheim", "kind": "Zuhause"}})
pl = c4.get("/places").json()["places"]
check("/places lists Meine Orte", {p["name"] for p in pl} == {"Omas Haus", "Daheim"} and next(p for p in pl if p["name"] == "Omas Haus")["poi"])
r = c4.get("/pois", params={"lat": 47.0709, "lon": 15.4397}).json()
check("own place with poi flag is a sight", r["pois"][0]["name"] == "Omas Haus" and r["pois"][0]["distance_m"] < 60, r)


def photon_pois(url):
    if "osm_tag=tourism:attraction" in url:
        return Resp(200, {"features": [{"geometry": {"coordinates": [15.4400, 47.0710]},
                                        "properties": {"name": "Uhrturm", "osm_key": "tourism", "osm_value": "attraction",
                                                       "osm_type": "N", "osm_id": 5}},
                                       {"geometry": {"coordinates": [15.4400, 47.0711]},
                                        "properties": {"name": "Bildstock", "osm_key": "historic", "osm_value": "wayside_shrine"}},
                                       {"geometry": {"coordinates": [15.4401, 47.0711]},
                                        "properties": {"name": "Gedenktafel Test", "osm_key": "historic", "osm_value": "memorial"}},
                                       {"geometry": {"coordinates": [15.4401, 47.0712]},
                                        "properties": {"name": "Altes Wohnhaus", "osm_key": "historic", "osm_value": "building"}},
                                       {"geometry": {"coordinates": [15.4402, 47.0711]},
                                        "properties": {"name": "Alte Kanone", "osm_key": "historic", "osm_value": "cannon"}},
                                       {"geometry": {"coordinates": [15.4402, 47.0712]},
                                        "properties": {"name": "T-34", "osm_key": "historic", "osm_value": "tank"}}]})
    if "osm_tag=historic" in url:
        return Resp(200, {"features": [{"geometry": {"coordinates": [15.4390, 47.0700]},
                                        "properties": {"name": "Schloss Test", "osm_key": "historic", "osm_value": "castle"}},
                                       {"geometry": {"coordinates": [15.4400, 47.0710]},
                                        "properties": {"name": "Uhrturm", "osm_key": "tourism", "osm_value": "attraction"}}]})
    if "osm_tag" in url:
        return Resp(200, {"features": []})
    return photon("Österreich", "AT", "Graz", "Steiermark")(url)


w4.on("http://photon", photon_pois)
r = c4.get("/pois", params={"lat": 47.0709, "lon": 15.4397, "radius": 800, "limit": 20}).json()      # 800 m = another cache bucket than the empty answer above
names = [p["name"] for p in r["pois"]]
check("sights: attraction + castle, de-duplicated, minor ones skipped", "Uhrturm" in names and "Schloss Test" in names
      and names.count("Uhrturm") == 1 and "Bildstock" not in names
      and "Alte Kanone" in names and "T-34" in names and "Gedenktafel Test" not in names and "Altes Wohnhaus" not in names, names)
check("sorted by distance", [p["distance_m"] for p in r["pois"]] == sorted(p["distance_m"] for p in r["pois"]))
n = w4.count("osm_tag")
c4.get("/pois", params={"lat": 47.0709, "lon": 15.4397, "radius": 800})
check("sights cached", w4.count("osm_tag") == n)
ctx = c4.get("/context", params={"lat": 47.0709, "lon": 15.4397, "time": ep(2025, 6, 2)}).json()
check("context carries the sights", ctx["parts_de"]["poi"].startswith("in der Nähe: ") and ctx["pois"], ctx["parts_de"])
ctx = c4.get("/context", params={"lat": 47.0709, "lon": 15.4397, "time": ep(2025, 6, 2), "poi": False}).json()
check("poi=false switches them off", "pois" not in ctx)
c5 = client(w4, poi_enabled=False)
check("GEO_POI=false", "pois" not in c5.get("/context", params={"lat": 47.0709, "lon": 15.4397, "time": ep(2025, 6, 2)}).json())

# ------------------------------------------------------------------ trips
HOME = (47.07, 15.44)
ROME = (41.90, 12.50)
DAYS = {}                                    # UTC date -> position
d0 = datetime(2025, 5, 20, tzinfo=timezone.utc)
for i in range(30):
    day = d0 + timedelta(days=i)
    DAYS[day.strftime("%Y-%m-%d")] = ROME if 10 <= i <= 15 else HOME      # 30 May .. 4 Jun in Rome


def traccar(url):
    day = url.split("from=")[1][:10]
    pos = DAYS.get(day)
    if not pos:
        return Resp(200, content=b"")
    base = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    return Resp(200, content=gpx([(base + h * 3600, pos[0] + h * 1e-5, pos[1]) for h in range(0, 24, 2)]))


def photon_by_pos(url):
    if "osm_tag" in url:
        return Resp(200, {"features": []})
    lat = float(url.split("lat=")[1].split("&")[0])
    return photon("Italien", "IT", "Rom")(url) if lat < 45 else photon("Österreich", "AT", "Graz", "Steiermark")(url)


w6 = World(); w6.on("http://photon", photon_by_pos); w6.on("http://traccar/api/positions/gpx", traccar)
c6 = client(w6, home_country="AT", default_tz="Europe/Vienna", trip_lookback_days=20)
r = c6.get("/trips", params={"from": "2025-05-20", "to": "2025-06-18"}).json()
check("one trip found", len(r["trips"]) == 1, r)
t = r["trips"][0]
check("trip dates + name", t["from"] == "2025-05-30" and t["to"] == "2025-06-04" and t["name"] == "Italien" and t["abroad"], t)
check("folder name", t["folder"] == "2025.05.30-2025.06.04 Italien", t["folder"])
check("home found from the nights", abs(r["home"][0] - HOME[0]) < 0.01, r["home"])
r = c6.get("/trip-at", params={"time": ep(2025, 6, 1, 10)}).json()
check("trip-at inside", r["found"] and r["trip"]["day"] == 3 and r["trip"]["days"] == 6, r)
check("trip-at at home", c6.get("/trip-at", params={"time": ep(2025, 6, 15, 10)}).json()["found"] is False)
ctx = c6.get("/context", params={"lat": ROME[0], "lon": ROME[1], "time": ep(2025, 6, 1, 10)}).json()
check("context says 'auf Reise'", ctx["parts_de"]["trip"] == "auf Reise: Italien (Tag 3 von 6)", ctx["parts_de"])
calls = w6.count("positions/gpx")
c6.get("/trip-at", params={"time": ep(2025, 6, 2, 10)})
c6.get("/trip-at", params={"time": ep(2025, 6, 9, 10)})
check("whole 30-day bucket served from cache", w6.count("positions/gpx") == calls)
# home from config / from a 'Zuhause' place
c6.post("/overrides", json={"id": "h", "lat": 47.07, "lng": 15.44, "radius_m": 50, "place": {"name": "Daheim", "kind": "Zuhause"}})
check("trips with Zuhause place", len(c6.get("/trips", params={"from": "2025-05-21", "to": "2025-06-19"}).json()["trips"]) == 1)
w7 = World(); w7.on("http://photon", photon_by_pos)
c7 = client(w7, traccar_url="")
check("no track source -> clear 503", c7.get("/trips", params={"from": "2025-05-20", "to": "2025-06-18"}).status_code == 503)
check("no track source: context still works", "trip" not in c7.get("/context", params={"lat": 47.0, "lon": 15.4, "time": ep(2025, 6, 1)}).json())

# ------------------------------------------------------------------ clock check
shot = ep(2025, 6, 1, 12, 0)
r = c6.get("/clock-check", params={"time": shot, "gps_time": shot - 3600}).json()
check("gps-time: clock 1 h ahead", r["status"] == "warn" and r["issues"][0]["type"] == "gps-time" and r["issues"][0]["shift_s"] == -3600, r)
check("gps-time ok", c6.get("/clock-check", params={"time": shot, "gps_time": shot - 5}).json()["status"] == "ok")
r = c6.get("/clock-check", params={"time": ep(1999, 1, 1)}).json()
check("year 1999 = unset clock", r["status"] == "warn" and r["issues"][0]["type"] == "sanity")
# own photo GPS in Rome, track says Rome all day -> plausible
r = c6.get("/clock-check", params={"time": shot, "lat": ROME[0], "lon": ROME[1]}).json()
check("photo GPS matches track", r["status"] == "ok", r)
# photo GPS at HOME position while the track says Rome -> mismatch, no better shift exists
r = c6.get("/clock-check", params={"time": shot, "lat": HOME[0], "lon": HOME[1]}).json()
check("photo GPS far from the track", r["status"] == "warn" and r["issues"][0]["type"] == "track", r)
# ------------------------------------------------------------------ weather: wind, precipitation
def hourly(u):
    day = u.split("start_date=")[1][:10]
    return Resp(200, {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(24)], "weather_code": [61] * 24,
                                 "temperature_2m": [9.0] * 24, "precipitation": [1.4] * 24, "cloud_cover": [90] * 24,
                                 "wind_speed_10m": [45.0] * 24, "relative_humidity_2m": [88.0] * 24}})


w8 = World(); w8.on("http://photon", photon("Österreich", "AT", "Graz", "Steiermark")); w8.on("archive-api.open-meteo", hourly)
c8 = client(w8, weather_provider="open-meteo", elevation_providers=[])
ctx = c8.get("/context", params={"lat": 47.07, "lon": 15.44, "time": ep(2025, 6, 2, 10)}).json()
wx = ctx["weather"]
check("weather wind/humidity/precip", wx["wind_kmh"] == 45 and wx["wind_de"] == "starker Wind" and wx["humidity_pct"] == 88 and wx["precip_mm"] == 1.4, wx)
check("weather phrases", ctx["parts_de"]["wind"] == "starker Wind (45 km/h)" and ctx["parts_de"]["precip"] == "1.4 mm Niederschlag", ctx["parts_de"])

print("\nall context-extras tests passed")
