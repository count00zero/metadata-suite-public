"""One track request per trip/month (not per day); bisect nearest_point; end-to-end with mocked sources."""
import os, re, sys, time, random, tempfile, html, subprocess as sp
from datetime import datetime, timezone
MR = tempfile.mkdtemp(prefix="tw_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import geocore
DAY = 86400
def ep(y, m, d, h=12): return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp())

# --- window planning -------------------------------------------------------------------------
w = geocore.plan_track_windows([ep(2026,8,d) for d in (5,6,7,9,10,12,14,17,19,22)])    # gaps <=3 days -> 1 window
assert len(w) == 1 and w[0][0] < datetime(2026,8,5,12,tzinfo=timezone.utc) and w[0][1] > datetime(2026,8,22,12,tzinfo=timezone.utc), w
w = geocore.plan_track_windows([ep(2026,8,d) for d in (1,2,3)] + [ep(2026,8,28), ep(2026,8,30)])   # 25-day hole -> 2 windows
assert len(w) == 2 and w[0][1] < w[1][0], w
assert len(geocore.plan_track_windows([ep(2026,8,5,0), ep(2026,8,5,23)])) == 1    # same day: one window, not cut at midnight
# padding: edge photos keep their neighbours
lo, hi = geocore.plan_track_windows([ep(2026,8,5,0)])[0]
assert (ep(2026,8,5,0) - int(lo.timestamp())) == 12*3600 and (int(hi.timestamp()) - ep(2026,8,5,0)) == 12*3600
# very long span is chunked (31 days default), every capture time is inside some window
eps = [ep(2026,1,1) + i*DAY for i in range(0, 100)]
ws = geocore.plan_track_windows(eps)
assert len(ws) == 4, len(ws)
assert all(any(a.timestamp() <= e <= b.timestamp() for a, b in ws) for e in eps)
assert geocore.plan_track_windows([]) == []
assert len(geocore.plan_track_windows([ep(2026,8,1), ep(2026,8,6)], bridge_days=10)) == 1
assert len(geocore.plan_track_windows([ep(2026,8,1), ep(2026,8,6)], bridge_days=1)) == 2

# --- bisect == brute force, incl. edges, ties and unsorted-list fallback ------------------------
random.seed(5)
pts = [geocore.TrackPoint(epoch=random.randint(0, 10**6), lat=random.random(), lon=random.random(), source="T") for _ in range(2000)]
tr = geocore.merge_tracks(pts)
assert isinstance(tr, geocore.SortedTrack) and [p.epoch for p in tr] == sorted(p.epoch for p in tr)
for _ in range(500):
    t = random.randint(-1000, 10**6 + 1000)
    got = geocore.nearest_point(tr, t, 10**9)
    brute = min(tr, key=lambda p: abs(p.epoch - t))
    assert abs(got[0].epoch - t) == abs(brute.epoch - t) and got[1] == abs(brute.epoch - t)
assert geocore.nearest_point(tr, 10**6 + 10**5, 60) is None            # beyond max gap
assert geocore.nearest_point(geocore.merge_tracks([]), 5, 60) is None
# duplicates from overlapping windows collapse
a = geocore.TrackPoint(epoch=5, lat=1.0, lon=2.0, source="Traccar")
assert len(geocore.merge_tracks([a], [geocore.TrackPoint(epoch=5, lat=1.0, lon=2.0, source="Traccar")])) == 1
assert len(geocore.merge_tracks([a], [geocore.TrackPoint(epoch=5, lat=1.0, lon=2.0, source="Dawarich")])) == 2
# speed: a month-sized track (200k points) answers 2000 lookups well under a second
big = geocore.merge_tracks([geocore.TrackPoint(epoch=i * 10, lat=0.0, lon=0.0, source="T") for i in range(200_000)])
t0 = time.time()
for i in range(2000): geocore.nearest_point(big, i * 997, 600)
assert time.time() - t0 < 1.0, time.time() - t0

# --- end to end: GPS scan with mocked Traccar/Dawarich counts requests -------------------------------
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
os.environ.update(TRACCAR_URL="http://x", TRACCAR_TOKEN="t")
calls = []
def fake_traccar(url, token, devs, f, t, timeout=60):
    calls.append(("traccar", f, t)); return [geocore.TrackPoint(epoch=int(f.timestamp()) + 3600, lat=48.2, lon=16.3, source="Traccar")]
geocore.fetch_traccar_track = fake_traccar
folder = os.path.join(MR, "trip"); os.makedirs(folder)
def mk(name, day):
    p = os.path.join(folder, name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=32x32","-frames:v","1",p], check=True)
    sp.run(["exiftool","-overwrite_original","-q",f"-DateTimeOriginal=2026:08:{day:02d} 10:00:00","-OffsetTimeOriginal=+02:00",p], check=True)
for d in (5, 6, 7, 9, 10): mk(f"2026.08.{d:02d}-10.00.00_{d}.jpg", d)
def scan(**kw):
    x = c.post("/gps/browse/start", data={"path": folder, "root_path": MR, "depth": "0", "default_tz": "Europe/Vienna",
                                          "traccar_device_ids": "7", **kw})
    j = re.search(r'/progress/([a-f0-9-]+)', x.text).group(1)
    for _ in range(600):
        y = c.get(f"/gps/browse/progress/{j}")
        if "progress-panel" not in y.text: return y.text
        time.sleep(0.05)
h = scan()
assert len(calls) == 1, calls                      # 5 different days -> ONE Traccar request
assert calls[0][1] < datetime(2026,8,5,tzinfo=timezone.utc) and calls[0][2] > datetime(2026,8,10,tzinfo=timezone.utc)
# a second trip 40 days later in the same folder -> a second window, not a 40-day hole fetched
calls.clear()
p = os.path.join(folder, "2026.09.20-10.00.00_20.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=32x32","-frames:v","1",p], check=True)
sp.run(["exiftool","-overwrite_original","-q","-DateTimeOriginal=2026:09:20 10:00:00","-OffsetTimeOriginal=+02:00",p], check=True)
h = scan()
assert len(calls) == 2 and "2 Abrufe je Quelle" in h, (len(calls), re.findall(r"\d+ Track-Punkt[^<]*", h))

# --- geo-service configured: tracks come from it (direct Traccar not touched), fallback when it is down ---
from app.core import geoservice
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
svc = []
def fake_range(t0, t1, sources="", devices="", include_import=False):
    svc.append((t0, t1, sources, devices, include_import))
    return [(t0 + 3600, 48.2, 16.3, "Traccar")]
geoservice.track_range = fake_range
calls.clear()
h = scan(use_dawarich="1")
assert len(svc) == 2 and not calls, (svc, calls)                 # same 2 windows, but via the service
assert svc[0][2] == "traccar,dawarich" and svc[0][3] == "7" and svc[0][4] is False, svc[0]
assert "2 Abrufe je Quelle" in h
def down(*a, **k): raise geoservice.ServiceError("boom")
geoservice.track_range = down
h = scan()
assert len(calls) == 2, calls                                    # fell back to the direct Traccar fetch
assert "geo-service nicht nutzbar" in h
os.environ["GEO_SERVICE_URL"] = ""
print("PASS")
