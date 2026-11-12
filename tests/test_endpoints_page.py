"""Endpoint page (/endpoints/): catalog vs geo-service/ENDPOINTS.md, samples from the overrides (home first), links, nav icon."""
import os
import re
import sys

os.environ.setdefault("MEDIA_ROOT", "/tmp")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["GEO_SERVICE_URL"] = "http://geo-service:8000"
os.environ["GEO_SERVICE_PUBLIC_URL"] = ""
from fastapi.testclient import TestClient

from app.core import endpointscatalog as cat, geoservice
from app.main import app
from app.modules.endpoints import router as ep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. every endpoint path of ENDPOINTS.md is in the catalog (and the other way round)
md = open(os.path.join(ROOT, "geo-service", "ENDPOINTS.md"), encoding="utf-8").read()
doc = set()
for m in re.finditer(r"http://192\.168\.1\.33:(\d+)(/[A-Za-z0-9_/\-]*)", md):
    port = {"8430": "api", "8431": "nom", "8432": "photon"}[m.group(1)]
    doc.add((port, m.group(2) or "/"))
doc = {d for d in doc if d[1] not in ("/", "/overrides/volksschule-musterdorf")}
have = cat.all_paths()
missing = sorted(d for d in doc if d not in have)
assert not missing, f"in ENDPOINTS.md but not on the page: {missing}"
extra = sorted(h for h in have if h not in doc and h[1] not in ("/reverse-batch", "/weather/backfill/cancel", "/overrides/mein-ort"))
assert not extra, f"on the page but not in ENDPOINTS.md: {extra}"

# 2. page with sample points from the overrides (home first)
OV = [{"id": "z", "lat": 47.1, "lng": 15.1, "radius_m": 20, "reverse": {"label": "Zebra", "city": "Graz", "country_a": "AUT"}},
      {"id": "JSBG-84", "lat": 48.159135709944, "lng": 16.3326890203595, "radius_m": 25, "place": {"name": "Zuhause", "kind": "Zuhause"},
       "address": {"postalcode": "2552"}, "reverse": {"city": "Musterdorf", "country_a": "AUT"}}]
geoservice.overrides_list = lambda: OV
geoservice.enabled = lambda: True
c = TestClient(app)
r = c.get("/endpoints/", headers={"host": "192.168.1.33:8420"})
assert r.status_code == 200, r.text[:300]
h = r.text
assert "http://192.168.1.33:8430" in h and "http://192.168.1.33:8431" in h and "http://192.168.1.33:8432" in h, "bases from the request host"
i_home, i_z, i_def = h.index("🏠 Zuhause"), h.index(">Zebra<"), h.index("Volksschule Musterdorf")
assert i_home < i_z < i_def, "home first, default last"
assert '"lat": 48.159135709944' in h and '"postal": "2552"' in h and '"city": "Musterdorf"' in h
assert h.count('target="_blank"') >= 40 and 'rel="noopener"' in h
assert h.count('data-method="GET"') >= 4 and "data-script" in h and "Schnelltest per curl" in h
assert "curl" in h and 'data-method="DELETE"' in h and 'data-method="POST"' in h

# 3. public URL override, nav icon on every page
os.environ["GEO_SERVICE_PUBLIC_URL"] = "https://geo.example.org"; os.environ["GEO_PUBLIC_PORT"] = "9000"
assert "https://geo.example.org:9000" in c.get("/endpoints/").text
os.environ["GEO_SERVICE_PUBLIC_URL"] = ""; os.environ.pop("GEO_PUBLIC_PORT")
for path in ("/timestamp/", "/gps/", "/gpsfill/", "/geooverrides/", "/ctxfill/", "/ai/", "/nfo/", "/collage/", "/mtime/", "/shortvideos/", "/info/"):
    t = c.get(path).text
    assert 'href="/endpoints/"' in t and t.index('href="/endpoints/"') < t.index('href="/info/"') if path != "/info/" else 'href="/endpoints/"' in t, path
# 4. clickable id links: real ids for the example point, filled in by the page
assert h.count('class="tpl idlink"') >= 6 and "<code class=\"url tpl\"" not in h
def fake_get(path, params, timeout_s=None):
    if path == "/findNearbyPlaceNameJSON":
        return {"geonames": [{"geonameId": 7}]}, "ok"
    if path == "/hierarchyJSON":
        return {"geonames": [{"fcode": "PCLI", "geonameId": 2}, {"fcode": "ADM1", "geonameId": 3}, {"fcode": "ADM2", "geonameId": 4}, {"fcode": "PPL", "geonameId": 7}]}, "ok"
    return None, "down"
geoservice._get = fake_get
geoservice._ids_cache.clear()
assert c.get("/endpoints/ids", params={"lat": 47.9, "lng": 16.2}).json() == {"id": 7, "state": 3, "county": 4}
assert "/endpoints/ids?lat=" in h and "{state_id}" in h
geoservice._get = lambda *a, **k: (None, "down")
geoservice._ids_cache.clear()
assert c.get("/endpoints/ids", params={"lat": 1, "lng": 2}).json() == {}
print("PASS")
