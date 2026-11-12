"""water.py -- rivers and lakes around a point for /sights?categories=water (and the /nearby page).

They come from the maritime server (geonames-marine-geocoder): `rivers` = the rivers within river_radius_km with the
nearest point on the line, `contained_in` (layer lakes*) = the lake the point lies in. Entries look like the other sights
(name, kind, category, lat, lon, distance_m, bearing, direction, score) so the page needs no special case."""
from __future__ import annotations

from . import backends
from . import sights as sm

CATEGORY = "water"
LABEL_DE = "Gewässer (Flüsse, Seen)"
_LAKE_LAYERS = {"osm_lakes", "lakes", "lakes_europe", "lakes_north_america"}


def _row(name, kind, lat, lon, center, radius_m, extra=None):
    d = sm.hav_m(center[0], center[1], lat, lon)
    b = sm.bearing(center[0], center[1], lat, lon)
    row = {"name": name, "kind": kind, "category": CATEGORY, "lat": lat, "lon": lon, "distance_m": int(round(d)),
           "bearing": b, "direction": sm.direction_de(b), "score": round(0.6 * max(0.0, 1 - d / max(radius_m, 1)) + 0.3, 3)}
    row.update(extra or {})
    return row


def near(s, lat: float, lon: float, radius_m: int, limit: int, lang: str = "de") -> list:
    """Rivers within radius_m and the lake the point lies in; [] without a maritime server or when it does not answer."""
    if not getattr(s, "maritime", ""):
        return []
    km = max(radius_m / 1000.0, 0.05)
    data, _up = backends._get_json(
        f"{s.maritime.rstrip('/')}/reverse?lat={lat}&lon={lon}&lang={lang}&nearby=0&river_radius_km={km}"
        f"&river_max={max(1, min(50, limit))}", s.http_timeout)
    if not isinstance(data, dict):
        return []
    out = []
    for c in data.get("contained_in") or []:
        if isinstance(c, dict) and c.get("layer") in _LAKE_LAYERS and c.get("name"):
            out.append(_row(c["name"], "See", lat, lon, (lat, lon), radius_m))
    for r in data.get("rivers") or []:
        if isinstance(r, dict) and r.get("name") and r.get("lat") is not None and r.get("lon") is not None:
            kind = {"stream": "Bach", "canal": "Kanal", "ditch": "Graben", "drain": "Entwässerungsgraben"}.get(
                str((r.get("attrs") or {}).get("waterway") or "").lower(), "Fluss")
            out.append(_row(r["name"], kind, float(r["lat"]), float(r["lon"]), (lat, lon), radius_m))
    return out
