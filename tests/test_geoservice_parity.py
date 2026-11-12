"""Sync guard: the suite's geocore and the geo-service (geo-service/app/backends.py) parse the SAME
Photon/Nominatim/Pelias answers into the SAME place fields. If someone changes the parsing in one
place only, this fails. Runs both against a local fake geocoder (no network)."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

os.environ["MEDIA_ROOT"] = tempfile.mkdtemp(prefix="parity_")
os.environ.pop("GEO_SERVICE_URL", None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "geo-service"))
from app.core import geocore as suite                                   # noqa: E402
import importlib.util                                                   # noqa: E402

# the service's package is also called "app" -> load it under its own name
import types
svc_pkg = os.path.join(ROOT, "geo-service", "app")
gs = types.ModuleType("gs_app"); gs.__path__ = [svc_pkg]; sys.modules["gs_app"] = gs
from gs_app import backends, config                                     # noqa: E402

PAYLOADS = {
    "photon": [
        {"features": [{"properties": {"country": "Österreich", "countrycode": "AT", "state": "Steiermark", "county": "Graz",
                                      "city": "Graz", "locality": "Innere Stadt", "street": "Hauptplatz", "name": "Rathaus"}}]},
        {"features": [{"properties": {"country": "Deutschland", "countrycode": "DE", "name": "Brocken"}}]},
        {"features": [{"properties": {}}]}, {"features": []},
    ],
    "nominatim": [
        {"name": "Rathaus", "address": {"country": "Österreich", "country_code": "at", "state": "Steiermark", "county": "Graz",
                                        "town": "Graz", "road": "Hauptplatz"}},
        {"address": {"country": "Sri Lanka", "country_code": "lk", "village": "Ella", "suburb": "X"}},
        {"address": {}}, {},
    ],
    "pelias": [
        {"features": [{"properties": {"country": "Österreich", "country_a": "AUT", "region": "Steiermark", "county": "Graz",
                                      "locality": "Graz", "name": "Hauptplatz 1", "street": "Hauptplatz"}}]},
        {"features": [{"properties": {"country": "Malediven", "country_a": "MDV", "county": "Male", "name": "Strand"}}]},
        {"features": []},
    ],
}
FIELDS = ("country", "country_a", "state", "county", "city", "locality", "street", "sublocation")
state = {"kind": None, "i": 0}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        path = urlparse(self.path).path
        kind = state["kind"]
        body = PAYLOADS[kind][state["i"]] if path in ("/reverse", "/v1/reverse") else {}
        # alpha-3 follow-up to pelias (no lang param) answers like the real one
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_port}"

n = 0
for kind, items in PAYLOADS.items():
    for i in range(len(items)):
        state.update(kind=kind, i=i)
        a = suite.reverse_geocode(47.0, 15.0, **{f"{kind}_server": URL, "lang": "de"})
        s = config.Settings(**{kind: URL}, chain=[kind])
        b, _tried, _up = backends.reverse_chain(s, backends.Breaker(3, 60), 47.0, 15.0, "de")
        if a is None or a.is_empty:
            assert b is None or not (b.country or b.city), (kind, i, a, b)
        else:
            assert b is not None, (kind, i, "service found nothing, suite did", a)
            for f in FIELDS:
                assert getattr(a, f) == getattr(b, f), (kind, i, f, getattr(a, f), getattr(b, f))
        n += 1
srv.shutdown()
print(f"PASS: suite geocore and geo-service parse {n} backend answers identically")
