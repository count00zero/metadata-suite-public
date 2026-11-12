"""overrides.py -- hand-made corrections that sit ON TOP of the cache (and of fresh upstream answers).

The cache keeps what the geocoders said; an override says "at this spot, call it X". It is applied when an answer
is delivered, so removing an override brings the original answer back without purging anything.

File (GEO_OVERRIDES_FILE, default <data dir>/overrides.json), a JSON list, reloaded when the file changes:
  [{"id": "vs-musterdorf", "lat": 48.14543, "lng": 16.32572, "radius_m": 40, "note": "free text",
    "address":   {"placename": "Volksschule"},                 # /addressJSON       (fields of the "address" object)
    "reverse":   {"locality": "Volksschule Musterdorf"},     # /reverse, /context (fields of GeoResult, see README)
    "elevation": 215}]                                          # /elevation, /srtm3 ... (metres)
  "place":     {"name": "Zuhause", "kind": "Eigener Ort", "poi": true}   # "Meine Orte": listed for -GEO / fixed places in the
                                                                 # suite, and (poi) named as a sight nearby
Every section is optional. The nearest override whose area contains the requested point wins (per section).
The area is the circle (lat, lng, radius_m) -- or, if "polygon" is given, that polygon instead:
  "polygon": [[48.1450, 16.3250], [48.1460, 16.3250], [48.1460, 16.3265], [48.1450, 16.3265]]    # [lat, lng] corners
(lat/lng then only is the anchor point the editor shows; a point inside the polygon always matches, however far it
is from the anchor).
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
from typing import Any, Optional

SECTIONS = ("address", "reverse", "elevation")
DEFAULT_RADIUS_M = 25.0


def haversine_m(la1: float, lo1: float, la2: float, lo2: float) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def in_polygon(lat: float, lng: float, poly: list) -> bool:
    """Ray casting in lat/lng space -- exact enough for areas of a few kilometres (not for ones crossing the antimeridian)."""
    inside, n, j = False, len(poly), len(poly) - 1
    for i in range(n):
        yi, xi, yj, xj = poly[i][0], poly[i][1], poly[j][0], poly[j][1]
        if (xi > lng) != (xj > lng) and lat < (yj - yi) * (lng - xi) / (xj - xi) + yi:
            inside = not inside
        j = i
    return inside


def match_distance(it: dict, lat: float, lng: float) -> Optional[float]:
    """Metres to the anchor if the point lies in the item's area (polygon, else circle), else None."""
    d = haversine_m(lat, lng, it["lat"], it["lng"])
    poly = it.get("polygon")
    if poly:
        return d if in_polygon(lat, lng, poly) else None
    return d if d <= it["radius_m"] else None


class Overrides:
    def __init__(self, path: str):
        self.path = path
        self._items: list = []
        self._sig = None
        self.error = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------ reading
    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._items, self._sig, self.error = [], None, ""
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):                   # tolerate {"overrides": [...]}
                data = data.get("overrides", [])
            items, bad = [], []
            for x in data:
                if not isinstance(x, dict):
                    continue
                try:
                    n = self._normalize(x)
                except ValueError as e:                  # one broken entry must not disable all the others
                    bad.append(f"{x.get('id', '?')}: {e}")
                    continue
                if n:
                    items.append(n)
            self._items, self.error = items, ("; ".join(bad) if bad else "")
        except (OSError, ValueError) as e:               # keep the last good list, say what is wrong
            self.error = f"{type(e).__name__}: {e}"
        self._sig = sig

    @staticmethod
    def _normalize(x: dict) -> Optional[dict]:           # raises ValueError for a broken polygon
        lat, lng = _num(x.get("lat")), _num(x.get("lng", x.get("lon")))
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        r = _num(x.get("radius_m"))
        out = {"id": str(x.get("id") or f"{lat:.5f},{lng:.5f}"), "lat": lat, "lng": lng,
               "radius_m": r if r is not None and r > 0 else DEFAULT_RADIUS_M}
        if x.get("note"):
            out["note"] = str(x["note"])
        poly = x.get("polygon")
        if poly:
            try:
                pts = [[float(p[0]), float(p[1])] for p in poly]
            except (TypeError, ValueError, IndexError):
                raise ValueError("polygon: list of [lat, lng] pairs expected")
            if len(pts) < 3 or len(pts) > 500 or any(not (-90 <= a <= 90 and -180 <= b <= 180) for a, b in pts):
                raise ValueError("polygon: 3 to 500 valid [lat, lng] corners expected")
            out["polygon"] = pts
        pl = x.get("place")
        if isinstance(pl, dict) and str(pl.get("name") or "").strip():
            out["place"] = {"name": str(pl["name"]).strip()[:80], "kind": str(pl.get("kind") or "Eigener Ort").strip()[:40],
                            "poi": bool(pl.get("poi"))}
        for sec in SECTIONS:
            v = x.get(sec)
            if sec == "elevation":
                if isinstance(v, dict):
                    v = v.get("value", v.get("elevation"))
                if _num(v) is not None:
                    out[sec] = _num(v)
            elif isinstance(v, dict) and v:
                out[sec] = v
        return out if any(s in out for s in SECTIONS) or "place" in out else None

    def items(self) -> list:
        with self._lock:
            self._load()
            return list(self._items)

    def find(self, section: str, lat: float, lng: float) -> Optional[tuple]:
        """(id, patch) of the nearest override that has `section` and whose radius contains the point."""
        best = None
        for it in self.items():
            if section not in it:
                continue
            d = match_distance(it, lat, lng)
            if d is not None and (best is None or d < best[0]):
                best = (d, it["id"], it[section])
        return (best[1], best[2]) if best else None

    def places(self) -> list:
        """The 'Meine Orte' entries (items that carry a "place" name)."""
        return [it for it in self.items() if "place" in it]

    def at(self, lat: float, lng: float) -> Optional[dict]:
        """The nearest override (any section) whose radius contains the point -- what the editor GUI preloads."""
        best = None
        for it in self.items():
            d = match_distance(it, lat, lng)
            if d is not None and (best is None or d < best[0]):
                best = (d, it)
        return best[1] if best else None

    # ------------------------------------------------------------ writing
    def _save(self, items: list) -> None:
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".overrides-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        self._sig = None

    def upsert(self, raw: dict) -> dict:
        item = self._normalize(raw)
        if item is None:
            raise ValueError("need lat, lng and at least one of: address, reverse, elevation (or a place name)")
        if not re.fullmatch(r"[\w.,:@ +-]{1,80}", item["id"]):
            raise ValueError("id: letters, digits and . , : @ + - _ space only (max 80)")
        with self._lock:
            self._load()
            items = [x for x in self._items if x["id"] != item["id"]] + [item]
            self._save(items)
            self._load()
        return item

    def delete(self, oid: str) -> bool:
        with self._lock:
            self._load()
            items = [x for x in self._items if x["id"] != oid]
            if len(items) == len(self._items):
                return False
            self._save(items)
            self._load()
        return True


# ---------------------------------------------------------------------------- applying
GEORESULT_FIELDS = ("country", "country_a", "state", "county", "city", "locality", "street", "source",
                    "house_number", "postcode", "neighbourhood")


def apply_reverse(payload: dict, oid: str, patch: dict) -> dict:
    """Patch a /reverse answer. Plain fields are merged and `label`/`sublocation` are derived again from them,
    unless the patch states them explicitly."""
    from .backends import GeoResult
    base = {k: payload.get(k, "") for k in GEORESULT_FIELDS} if payload.get("found") else {}
    base.update({k: str(v) for k, v in patch.items() if k in GEORESULT_FIELDS})
    d = GeoResult.from_dict(base).to_dict()
    if "source" not in patch:
        d["source"] = "override"
    for k in ("sublocation", "label"):
        if k in patch:
            d[k] = str(patch[k])
    return {**payload, "found": True, **d, "override": oid}


def apply_address(payload: dict, oid: str, patch: dict) -> dict:
    a = dict(payload.get("address") or {})
    a.update({k: v for k, v in patch.items()})
    return {"address": a}            # shape stays exactly the GeoNames one (no extra keys for strict clients)
