"""Suite side of the weather archive: media points (sidecar GPS), service call, progress and result page."""
import json, os, re, shutil, subprocess as sp, sys, tempfile, threading, time
SCRATCH = tempfile.mkdtemp(prefix="wxa_"); MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
from http.server import BaseHTTPRequestHandler, HTTPServer
from fastapi.testclient import TestClient

calls = []
STATE = {"weather": "open-meteo", "polls": 0}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path.startswith("/health"): return self._send(200, {"status": "ok", "weather": STATE["weather"]})
        if self.path.startswith("/weather/stats"):
            return self._send(200, {"enabled": True, "archive": {"archived_days": 5, "cells": 2, "first_day": "2025-10-01", "last_day": "2025-10-09"}})
        if self.path.startswith("/weather/backfill"):
            STATE["polls"] += 1
            run = STATE["polls"] < 2
            return self._send(200, {"status": "running" if run else "done", "phase": "weather", "needed_cell_days": 3, "done": 1 if run else 3,
                                    "counts": {"fetched": 2, "cached": 1, "failed": 0, "empty": 0, "recent": 0, "missing": 0},
                                    "track_points": 40, "error": ""})
        self._send(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        calls.append((self.path, body)); self._send(200, {"status": "running", "id": "x"})

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["GEO_SERVICE_URL"] = f"http://127.0.0.1:{srv.server_port}"
from app.main import app
from app.core import fillcore
c = TestClient(app)

def mk(name, gps=True, dto="2025:10:09 14:00:00"):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64", "-frames:v", "1", p], check=True)
    sp.run(["exiftool", "-overwrite_original", "-q", f"-DateTimeOriginal={dto}", "-OffsetTimeOriginal=+02:00", p], check=True)
    if gps:
        sp.run(["exiftool", "-q", "-o", p + ".xmp", p], check=True)
        sp.run(["exiftool", "-q", "-overwrite_original", "-n", "-XMP-exif:GPSLatitude=47.07", "-XMP-exif:GPSLongitude=15.43", p + ".xmp"], check=True)
    return p
mk("2025.10.09-14.00.00_a.jpg"); mk("2025.10.09-15.00.00_b.jpg", gps=False)

def run(form):
    r = c.post("/gpsfill/weather/start", data={"path": MR, "root_path": MR, "depth": "1", **form})
    src = r.text
    for _ in range(100):
        m = re.search(r'hx-get="(/gpsfill/weather/progress/[0-9a-f-]+)"', src)
        if not m: return src
        time.sleep(0.2); src = c.get(m.group(1)).text
    raise AssertionError("no result")

# media only: only the file WITH sidecar GPS becomes a point, epoch = 14:00+02:00 = 12:00Z
out = run({"use_media": "1", "date_from": "", "date_to": ""})
assert "✓ Fertig" in out and "3 verschiedene Orte" in out and "2 neu von Open-Meteo" in out and "Archiv gesamt: 5 Tage" in out, out
path, body = calls[-1]
assert path == "/weather/backfill" and body["tracks"] is False and len(body["points"]) == 1, body
ep, la, lo = body["points"][0]
assert ep == 1760011200 and abs(la - 47.07) < 1e-6 and abs(lo - 15.43) < 1e-6, body

# media + tracks without dates -> defaults 1900-01-01 .. today (end of day UTC); dry run flag travels
import datetime as _dt
STATE["polls"] = 0
out = run({"use_media": "1", "use_tracks": "1", "dry_run": "1"})
body = calls[-1][1]
_today = _dt.datetime.now(_dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
assert body["tracks"] is True and body["dry_run"] is True and body["from"] == -2208988800 and body["to"] == int(_today.timestamp()) + 86399, body
assert body["include_import"] is False and body["devices"] == "default"
assert "Prüfung (nichts abgerufen)" in out and "fehlen noch bzw. würden neu geholt" in out

# tracks only: dates required; with dates -> UTC day range
run({"use_tracks": "1", "date_from": "2025-10-01", "date_to": "2025-10-02", "include_import": "1", "all_devices": "1"})
b = calls[-1][1]; assert b["include_import"] is True and b["devices"] == "all", b
import re as _re
from app.main import app as _a
_page = c.get("/gpsfill/").text
assert not _re.search(r'name="all_devices"[^>]*checked', _page) and _re.search(r'name="include_import"[^>]*checked', _page), "weitere Traccar-Geraete: Standard AUS"
run({"use_tracks": "1"})            # tracks only, no dates -> 1900 .. today
b = calls[-1][1]; assert b["from"] == -2208988800 and b["to"] == int(_today.timestamp()) + 86399, b
_pg = c.get("/gpsfill/").text
assert 'name="date_from" value="1900-01-01"' in _pg and f'value="{_dt.datetime.now().strftime("%Y-%m-%d")}"' in _pg, "form defaults"
STATE["polls"] = 0
run({"use_tracks": "1", "date_from": "2025-10-01", "date_to": "2025-10-03"})
b = calls[-1][1]
assert b["points"] == [] and b["from"] == 1759276800 and b["to"] == 1759276800 + 3 * 86400 - 1, b
from app.core import pathsafety as _ps
_old = _ps.MEDIA_ROOT; _ps.MEDIA_ROOT = "/nonexistent-root"
_o = run({"use_media": "1", "path": "/tmp", "root_path": "/tmp"})
_ps.MEDIA_ROOT = _old
assert "Pfad nicht erlaubt" in _o, _o          # was a silent 403 before (htmx swallows it)
assert "mindestens eine Quelle" in run({})
STATE["weather"] = "none"
assert "WEATHER_PROVIDER=open-meteo" in run({"use_media": "1"})
STATE["weather"] = "open-meteo"
os.environ["GEO_SERVICE_URL"] = ""
assert "GEO_SERVICE_URL ist nicht gesetzt" in run({"use_media": "1"})
os.environ["GEO_SERVICE_URL"] = f"http://127.0.0.1:{srv.server_port}"
# panel is on the page, and fillcore.weather_points ignores media without time/GPS
assert 'hx-post="/gpsfill/weather/start"' in c.get("/gpsfill/").text
M = fillcore.Media; assert fillcore.weather_points([M(path="x", is_video=False)]) == []
srv.shutdown(); shutil.rmtree(SCRATCH, ignore_errors=True)
print("PASS")
