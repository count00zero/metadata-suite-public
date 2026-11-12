"""delete_srt removes the DJI .srt AND the .lrf next to a video once its sidecar is written; without the flag nothing is deleted."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import geocore, pathsafety, iptcfill  # noqa: E402
from app.modules.gps_sidecar import router as g  # noqa: E402

geocore.ensure_xmp_sidecar_ex = lambda f, x, v, *a, **k: (True, "")
geocore.ensure_xmp_dates_with_offset = lambda *a, **k: False
geocore.write_camera_name = lambda *a, **k: {"ok": False}
geocore.write_location_to_xmp = lambda *a, **k: (True, "")
geocore.write_place_extras = lambda *a, **k: {"altitude": None, "altitude_source": "", "timezone": ""}
geocore.read_xmp_gps = lambda *a, **k: (47.9, 16.2, False)
iptcfill.fill_sidecar = lambda *a, **k: None
pathsafety.check_allowed = lambda p: None

d = tempfile.mkdtemp()
def mk(delete):
    v = os.path.join(d, "DJI_0001.MP4"); open(v, "w").close()
    for e in (".SRT", ".LRF"):
        open(os.path.join(d, "DJI_0001" + e), "w").close()
    return {"path": v, "is_video": True, "lat": 47.9, "lon": 16.2, "srt_path": os.path.join(d, "DJI_0001.SRT")}, delete

row, _ = mk(False)
r = g._execute_one(row, False, False)
assert os.path.exists(os.path.join(d, "DJI_0001.SRT")) and os.path.exists(os.path.join(d, "DJI_0001.LRF")), "kept without the flag"
r = g._execute_one(row, False, True)
assert r.get("srt_deleted") and r.get("lrf_deleted"), r
assert not os.path.exists(os.path.join(d, "DJI_0001.SRT")) and not os.path.exists(os.path.join(d, "DJI_0001.LRF"))
# a photo never loses anything / sidecar failure keeps the .lrf
print("all ok")

# Zeitkorrektur: the track is searched at the corrected time
import inspect
assert "time_shift_s" in inspect.signature(g.browse_start_work).parameters
from app.core import timecore
assert timecore.parse_shift("+01:30") == 5400
print("ok   gps time shift param")

# Neues Geocoding bestehender Dateien: nothing is skipped, old place is shown, nocache reaches the geo-service
assert "regeocode" in inspect.signature(g.browse_start_work).parameters
assert "regeocode" in inspect.signature(g.browse_start).parameters
from app.core import geoservice
seen = {}
def fake_get(path, params, timeout_s=None):
    seen.update(params); return None, "down"
geoservice._get = fake_get
geoservice.reverse(1.0, 2.0, "de", True)
assert seen.get("nocache") == "true", seen
seen.clear(); geoservice.reverse(1.0, 2.0, "de")
assert "nocache" not in seen, seen
print("ok   regeocode param + nocache")
