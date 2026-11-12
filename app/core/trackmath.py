"""trackmath.py -- pure GPS-track maths, no I/O.

SHARED FILE: identical copies live in geo-service/app/ and geo-service/clients/ (the service
and gps_fill.py must agree on interpolation and confidence). tests/test_shared_identical.py
fails when the copies differ -- edit one, copy it over the other.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Optional, Sequence

EARTH_R = 6371008.8
STATIONARY_M = 150.0          # two bracketing points closer than this = "was standing still"


@dataclass(frozen=True)
class Pt:
    epoch: int
    lat: float
    lon: float
    source: str = ""

    def as_dict(self) -> dict:
        return {"epoch": self.epoch, "lat": self.lat, "lon": self.lon, "source": self.source}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


class Track:
    """Time-sorted points with O(log n) bracket lookup."""

    def __init__(self, points: Sequence[Pt]):
        self.points = sorted(points, key=lambda p: p.epoch)
        self.epochs = [p.epoch for p in self.points]

    def __len__(self) -> int:
        return len(self.points)

    def bracket(self, t: float) -> tuple[Optional[Pt], Optional[Pt]]:
        """(last point at/before t, first point at/after t); both are the same point when a
        point sits exactly on t."""
        i = bisect.bisect_right(self.epochs, t)
        before = self.points[i - 1] if i > 0 else None
        j = bisect.bisect_left(self.epochs, t)
        after = self.points[j] if j < len(self.points) else None
        return before, after


def _lerp(b: Pt, a: Pt, t: float) -> tuple[float, float]:
    f = (t - b.epoch) / (a.epoch - b.epoch)
    lat = b.lat + f * (a.lat - b.lat)
    dlon = ((a.lon - b.lon + 540.0) % 360.0) - 180.0          # shortest way round the antimeridian
    lon = ((b.lon + f * dlon + 540.0) % 360.0) - 180.0
    return lat, lon


def confidence(method: str, gap_s: float, span_s: float, dist_m: float) -> tuple[float, int]:
    """(confidence 0..1, uncertainty in metres) -- a documented heuristic, not a probability.

    exact         -> 1.0
    interpolated, was standing still (bracketing points < 150 m apart)
                  -> 0.9 * exp(-span / 12 h): hours between points hardly matter
    interpolated, was moving
                  -> exp(-gap / 15 min) / (1 + (dist / 2) / 1 km): worst-case error is half the way
    nearest (only one side known)
                  -> 0.8 * exp(-gap / 15 min)
    """
    if method == "exact":
        return 1.0, 10
    if method == "interpolated":
        if dist_m <= STATIONARY_M:
            return round(0.9 * math.exp(-span_s / 43200.0), 3), int(25 + dist_m / 2)
        unc = dist_m / 2.0
        return round(math.exp(-gap_s / 900.0) / (1.0 + unc / 1000.0), 3), int(15 + unc)
    return round(0.8 * math.exp(-gap_s / 900.0), 3), int(30 + gap_s * 1.5)


def locate(track: Track, t: float, gap1: int = 600, gap2: int = 21600) -> Optional[dict]:
    """Position at time t. Interpolates between the two bracketing points; with only one
    side known it falls back to the nearest point. None when the nearest point is further
    than gap2 seconds away. tier 1 = nearest point within gap1, tier 2 = within gap2."""
    b, a = track.bracket(t)
    if b is None and a is None:
        return None
    if b is not None and a is not None:
        gap = min(t - b.epoch, a.epoch - t)
        if gap > gap2:
            return None
        if a.epoch == b.epoch:
            lat, lon, method, span, dist = b.lat, b.lon, "exact", 0, 0.0
        else:
            lat, lon = _lerp(b, a, t)
            method, span = "interpolated", a.epoch - b.epoch
            dist = haversine_m(b.lat, b.lon, a.lat, a.lon)
    else:
        p = b or a
        gap = abs(t - p.epoch)
        if gap > gap2:
            return None
        lat, lon, method, span, dist = p.lat, p.lon, "nearest", 0, 0.0
        b, a = (p, None) if b else (None, p)
    conf, unc = confidence(method, gap, span, dist)
    return {
        "lat": round(lat, 6), "lon": round(lon, 6), "method": method,
        "gap_s": int(round(gap)), "span_s": int(span), "dist_m": int(round(dist)),
        "tier": 1 if gap <= gap1 else 2, "confidence": conf, "uncertainty_m": unc,
        "before": b.as_dict() if b else None, "after": a.as_dict() if a else None,
    }


def position_at(track: Track, t: float, max_span: int = 300) -> Optional[tuple[float, float]]:
    """Interpolated position, but only when t is bracketed by points <= max_span seconds
    apart (used by the drift search, where a guess from a far-away point would only add noise)."""
    b, a = track.bracket(t)
    if b is None or a is None:
        return None
    if a.epoch == b.epoch:
        return b.lat, b.lon
    if a.epoch - b.epoch > max_span:
        return None
    return _lerp(b, a, t)


def best_shift(track: Track, ref_lat: float, ref_lon: float, t: float, *, max_shift: int = 900,
               step: int = 10, max_span: int = 300, near_window: int = 120) -> Optional[dict]:
    """Which time shift d makes the track position at (t + d) match a photo that already
    carries GPS? Camera clock = true time - d  =>  drift = -d.

    Only trusted when the minimum is unambiguous: the best distance must be small and clearly
    better than anything >= near_window seconds away (a standing-still track gives a flat
    curve and is rejected). Returns {shift, d_best, d_zero, d_far, ok}."""
    curve: dict[int, float] = {}
    for d in range(-max_shift, max_shift + 1, step):
        pos = position_at(track, t + d, max_span)
        if pos is not None:
            curve[d] = haversine_m(ref_lat, ref_lon, pos[0], pos[1])
    if len(curve) < max(8, (2 * max_shift // step + 1) // 3):
        return None
    best = min(curve, key=curve.get)
    far = [v for d, v in curve.items() if abs(d - best) >= near_window]
    d_best = curve[best]
    d_far = min(far) if far else float("inf")
    ok = d_best <= 60.0 and d_far >= max(2.0 * d_best, d_best + 40.0)
    zero = curve.get(0) if 0 in curve else curve[min(curve, key=abs)]
    return {"shift": best, "d_best": round(d_best, 1), "d_zero": round(zero, 1),
            "d_far": round(d_far, 1) if far else None, "ok": bool(ok)}
