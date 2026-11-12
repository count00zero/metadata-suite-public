"""Start page '/': dashboard with module tiles, async status strip (geo-service up / down / off), home pill on every page."""
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
from fastapi.testclient import TestClient
from app.main import app
from app.core import geoservice

c = TestClient(app)
r = c.get("/", follow_redirects=False)
assert r.status_code == 200, r.status_code
for href in ("/timestamp/", "/gps/", "/gpsfill/", "/geooverrides/", "/ctxfill/", "/ai/", "/nfo/", "/collage/", "/mtime/", "/shortvideos/", "/endpoints/", "/info/"):
    assert f'class="tile" href="{href}"' in r.text, href
assert 'hx-get="/home/status"' in r.text and "Basis-Ordner" in r.text

# status: up
geoservice.health = lambda: {"version": "1.0", "uptime_s": 90061, "weather": "open-meteo", "tracks": {"traccar": True, "dawarich": False},
                             "geocoders": {"photon": {"configured": True, "open": False}, "pelias": {"configured": False},
                                           "nominatim": {"configured": True, "open": True, "retry_in_s": 30}}}
geoservice.weather_stats = lambda: {"archive": {"archived_days": 4576, "cells": 846, "first_day": "2020-01-01", "last_day": "2026-10-12"}}
geoservice.places = lambda: [{"id": "a"}, {"id": "b"}]
geoservice.overrides_list = lambda: [1, 2, 3]
s = c.get("/home/status").text
assert "v1.0" in s and "1 d 1 h 1 min" in s and "Open-Meteo aktiv" in s and "4576 Tage" in s and "846 Zellen" in s, s
assert "traccar ✓" in s and "dawarich –" in s and "photon" in s and "nominatim ⚠" in s and "pelias" not in s, s
assert "2 Orte" in s and "3 Overrides" in s, s
# down
def boom(): raise geoservice.ServiceError("/health: timeout")
geoservice.health = boom
s = c.get("/home/status").text
assert "nicht erreichbar" in s and "timeout" in s, s
# off
os.environ["GEO_SERVICE_URL"] = ""
assert "nicht aktiviert" in c.get("/home/status").text
# a home pill on every module page
for p in glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "modules", "*", "templates", "index.html")):
    if os.sep + "home" + os.sep in p:
        continue
    assert 'href="/" title="Start (Dashboard)"' in open(p, encoding="utf-8").read(), p
print("PASS")
