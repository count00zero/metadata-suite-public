"""trackmath: interpolation, confidence, antimeridian, drift search.   python tests/test_trackmath.py"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.trackmath import Pt, Track, best_shift, confidence, haversine_m, locate, position_at  # noqa: E402


def line_track(seconds=7200, step=10, speed_ms=5.0, lat0=47.0, lon0=15.0):
    """Walks due east at speed_ms; one point every `step` seconds."""
    m_per_deg = 111_320 * math.cos(math.radians(lat0))
    return Track([Pt(t, lat0, lon0 + t * speed_ms / m_per_deg, "T") for t in range(0, seconds + 1, step)])


def test_haversine():
    assert abs(haversine_m(0, 0, 0, 1) - 111_195) < 300
    assert haversine_m(47, 15, 47, 15) == 0


def test_locate_exact_interpolated_nearest():
    tr = Track([Pt(100, 47.0, 15.0, "T"), Pt(160, 47.0006, 15.0, "D")])
    ex = locate(tr, 100)
    assert ex["method"] == "exact" and ex["confidence"] == 1.0 and ex["gap_s"] == 0
    mid = locate(tr, 130)
    assert mid["method"] == "interpolated" and abs(mid["lat"] - 47.0003) < 1e-9 and mid["gap_s"] == 30
    assert mid["before"]["source"] == "T" and mid["after"]["source"] == "D"
    before_start = locate(tr, 40)                        # only the 'after' side exists
    assert before_start["method"] == "nearest" and before_start["after"]["epoch"] == 100 and before_start["before"] is None
    assert locate(tr, 160 + 22000) is None and locate(Track([]), 5) is None


def test_confidence_ordering():
    stat_3h = confidence("interpolated", 5400, 10800, 40)[0]          # standing still for 3 h
    mov_far = confidence("interpolated", 600, 1200, 8000)[0]          # driving, 20 min between points
    mov_near = confidence("interpolated", 30, 60, 500)[0]
    near_only = confidence("nearest", 300, 0, 0)[0]
    assert mov_near > near_only > mov_far, (mov_near, near_only, mov_far)
    assert stat_3h > 0.65 > mov_far
    assert confidence("exact", 0, 0, 0)[0] == 1.0


def test_antimeridian():
    tr = Track([Pt(0, 10.0, 179.9), Pt(60, 10.0, -179.9)])
    r = locate(tr, 30)
    assert abs(abs(r["lon"]) - 180) < 0.01, r


def test_position_at_refuses_wide_gaps():
    tr = Track([Pt(0, 47.0, 15.0), Pt(1000, 47.1, 15.0)])
    assert position_at(tr, 500, max_span=300) is None
    assert position_at(tr, 500, max_span=2000) is not None


def test_best_shift_recovers_clock_error():
    tr = line_track()
    t_true = 3000
    ref_lat, ref_lon = tr.points[t_true // 10].lat, tr.points[t_true // 10].lon
    for ahead in (40, -90, 0, 300):                      # camera clock ahead of true time by `ahead` s
        r = best_shift(tr, ref_lat, ref_lon, t_true + ahead)
        assert r and r["ok"] and r["shift"] == -ahead, (ahead, r)


def test_best_shift_rejects_standing_still():
    tr = Track([Pt(t, 47.0, 15.0, "T") for t in range(0, 7201, 10)])
    r = best_shift(tr, 47.0, 15.0, 3000)
    assert r is None or r["ok"] is False


def test_best_shift_rejects_photo_taken_elsewhere():
    tr = line_track()
    r = best_shift(tr, 48.5, 11.0, 3000)                 # photo position nowhere near the track
    assert r is None or r["ok"] is False


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except Exception as e:                       # noqa: BLE001
                fails += 1; print("FAIL", name, "->", repr(e))
    sys.exit(1 if fails else 0)
