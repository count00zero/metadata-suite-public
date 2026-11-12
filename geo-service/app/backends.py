"""backends.py -- reverse geocoding against Photon / Nominatim / Pelias (+ maritime backup).

The response parsing is the same as metadata-suite's geocore.py (so a place gets the same
fields whichever component asked), but this module additionally:
  * tells "backend unreachable / 5xx" apart from "backend answered, nothing here" -- only the
    former counts against a backend's circuit breaker and keeps the answer out of the cache;
  * skips a backend for GEO_BREAKER_COOLDOWN seconds after GEO_BREAKER_FAILURES failures in a
    row, so a hung Pelias doesn't add its timeout to every single lookup.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from .config import Settings
from .iso import alpha3


EXTRA_FIELDS = ("house_number", "postcode", "neighbourhood")


@dataclass
class GeoResult:
    country: str = ""
    country_a: str = ""   # ISO 3166-1 alpha-3
    state: str = ""
    county: str = ""
    city: str = ""
    locality: str = ""
    street: str = ""
    source: str = ""
    house_number: str = ""    # extra detail; only part of the response when the backend knows it
    postcode: str = ""
    neighbourhood: str = ""
    dist_m: float = -1.0   # how far the matched object lies from the asked point (-1 = unknown); not part of the response

    @property
    def sublocation(self) -> str:
        if self.locality and self.street:
            return f"{self.locality}, {self.street}"
        return self.locality or self.street

    @property
    def label(self) -> str:
        """Short human label: 'Graz, Steiermark, Österreich' (parts that exist, no repeats)."""
        parts: list[str] = []
        for p in (self.city or self.locality, self.state or self.county, self.country):
            if p and p not in parts:
                parts.append(p)
        return ", ".join(parts)

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in
             ("country", "country_a", "state", "county", "city", "locality", "street", "source")}
        for f in EXTRA_FIELDS:
            if getattr(self, f):
                d[f] = getattr(self, f)
        d["sublocation"] = self.sublocation
        d["label"] = self.label
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GeoResult":
        return cls(**{k: d.get(k, "") for k in
                      ("country", "country_a", "state", "county", "city", "locality", "street", "source") + EXTRA_FIELDS})


class Breaker:
    def __init__(self, failures: int, cooldown: int):
        self.failures, self.cooldown = failures, cooldown
        self._n: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, name: str) -> bool:
        return time.time() >= self._open_until.get(name, 0.0)

    def ok(self, name: str) -> None:
        with self._lock:
            self._n[name] = 0
            self._open_until.pop(name, None)

    def bad(self, name: str) -> None:
        with self._lock:
            self._n[name] = self._n.get(name, 0) + 1
            if self._n[name] >= self.failures:
                self._open_until[name] = time.time() + self.cooldown
                self._n[name] = 0

    def state(self, name: str) -> dict:
        until = self._open_until.get(name, 0.0)
        return {"open": time.time() < until, "retry_in_s": max(0, int(until - time.time()))}


def _get_json(url: str, timeout: float, headers: Optional[dict] = None) -> tuple[Optional[dict], bool]:
    """(json | None, reachable). 5xx / network error / invalid JSON on a 200 -> not reachable."""
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
    except requests.RequestException:
        return None, False
    if r.status_code >= 500:
        return None, False
    if r.status_code != 200:
        return None, True
    try:
        return r.json(), True
    except ValueError:
        return None, False


def fetch_tile(url: str, timeout: float):
    """(status, body, content-type) of one raster tile; (0, b"", "") when the tile server is unreachable."""
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException:
        return 0, b"", ""
    return r.status_code, r.content, (r.headers or {}).get("content-type", "image/png")


def _hav_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(a))


def _photon(s: Settings, lat: float, lon: float, lang: str):
    data, up = _get_json(f"{s.photon.rstrip('/')}/reverse?lon={lon}&lat={lat}&lang={lang}", s.http_timeout)
    try:
        feat = data["features"][0]
        p = feat["properties"]
    except (KeyError, IndexError, TypeError):
        return None, up
    if not (p.get("country") or p.get("city")):
        return None, up
    dist = -1.0
    try:
        x, y = feat["geometry"]["coordinates"][:2]
        dist = _hav_m(lat, lon, float(y), float(x))
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return GeoResult(country=p.get("country") or "", city=p.get("city") or "",
                     locality=p.get("locality") or p.get("name") or "", county=p.get("county") or "",
                     state=p.get("state") or "", street=p.get("street") or "",
                     country_a=alpha3(p.get("countrycode")), source="Photon", dist_m=dist,
                     house_number=p.get("housenumber") or "", postcode=p.get("postcode") or "",
                     neighbourhood=p.get("district") or ""), up


def _nominatim(s: Settings, lat: float, lon: float, lang: str):
    data, up = _get_json(f"{s.nominatim.rstrip('/')}/reverse?lat={lat}&lon={lon}&format=jsonv2&accept-language={lang}",
                         s.http_timeout, {"User-Agent": "geo-service/1.0"})
    if not data:
        return None, up
    a = data.get("address") or {}
    city = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or ""
    if not (a.get("country") or city):
        return None, up
    dist = -1.0
    try:
        dist = _hav_m(lat, lon, float(data["lat"]), float(data["lon"]))
    except (KeyError, TypeError, ValueError):
        pass
    return GeoResult(country=a.get("country") or "", city=city,
                     locality=a.get("locality") or data.get("name") or a.get("road") or "",
                     county=a.get("county") or "", state=a.get("state") or "", street=a.get("road") or "",
                     country_a=alpha3(a.get("country_code")), source="Nominatim", dist_m=dist,
                     house_number=a.get("house_number") or "", postcode=a.get("postcode") or "",
                     neighbourhood=a.get("neighbourhood") or a.get("quarter") or a.get("suburb") or ""), up


def _pelias(s: Settings, lat: float, lon: float, lang: str):
    data, up = _get_json(f"{s.pelias.rstrip('/')}/v1/reverse?point.lat={lat}&point.lon={lon}&lang={lang}", s.http_timeout)
    try:
        p = data["features"][0]["properties"]
    except (KeyError, IndexError, TypeError):
        return None, up
    city = p.get("locality") or p.get("county") or ""
    if not (p.get("country") or city):
        return None, up
    return GeoResult(country=p.get("country") or "", city=city, locality=p.get("name") or "",
                     county=p.get("county") or "", state=p.get("region") or "", street=p.get("street") or "",
                     country_a=p.get("country_a") or "", source="Pelias",
                     house_number=p.get("housenumber") or "", postcode=p.get("postalcode") or "",
                     neighbourhood=p.get("neighbourhood") or ""), up


# what a maritime server answers when it has no water body for the point (its own "nothing found" -- not a place name)
_NO_WATER_NAME = {"unbekannt", "unknown", "unnamed", "n/a", "none", "null", "-", "inconnu", "desconocido", "sconosciuto"}


# water bodies of the own GeoNames database (class H): used for open water the maritime server does not know
_WATER_FCODES = ["SEA", "OCN", "GULF", "BAY", "STRT", "CHN", "SD", "SNDS"]
_WATER_RADIUS_KM = 300


def _gazetteer_water(s: Settings, lat: float, lon: float, lang: str):
    from . import gazetteer
    g = gazetteer.get(s.gazetteer)
    if not g.available():
        return None
    near = g.nearby(lat, lon, limit=1, radius_km=_WATER_RADIUS_KM, fclasses=["H"], fcodes=_WATER_FCODES, lang=(lang or "")[:2] or None)
    if not near or not near[0].get("name"):
        return None
    return GeoResult(country=near[0]["name"], locality=near[0]["name"], source="Maritim-Backup")


# layers whose names are zones or upper-case code lists, not the sea itself
_SKIP_WATER_LAYERS = {"seavox", "eez", "territorial_sea", "contiguous_zone", "internal_waters", "archipelagic_waters"}
_WATER_LAYER_ORDER = {"iho": 0, "ne_marine": 1}
_FEATURE_NEAR_KM = 10   # the maritime server's nearest named point only counts as "the place" within this distance


def _water_area(contained: list, lang: str) -> str:
    """The sea / gulf / ocean the point lies in, from the maritime server's polygon layers (contained_in): an entry in the
    asked language first (IHO, then Natural Earth), else the IHO name."""
    cand = [c for c in (contained or []) if isinstance(c, dict) and c.get("name") and c.get("layer") not in _SKIP_WATER_LAYERS]
    cand.sort(key=lambda c: _WATER_LAYER_ORDER.get(c.get("layer"), 9))
    want = (lang or "")[:2].lower()
    same = [c for c in cand if (c.get("lang") or "")[:2].lower() == want] if want else []
    pick = (same or cand or [None])[0]
    return (pick or {}).get("name", "").strip()


def _maritime(s: Settings, lat: float, lon: float, lang: str, gazetteer_water: bool = False):
    """The maritime server names the water. `contained_in` gives the sea the point lies in, `name` the nearest named feature
    (bay, strait ...), which counts only when it is close. Result: country = the sea, locality = the specific feature
    (state = the sea again when a land country is added later); nothing but the sea -> both fields carry the sea.
    With `gazetteer_water` (only where nothing else answered) the nearest sea of the own GeoNames data is the last resort."""
    data, up = _get_json(f"{s.maritime.rstrip('/')}/reverse?lat={lat}&lon={lon}&lang={lang}", s.http_timeout)
    data = data if isinstance(data, dict) else {}
    feature = (data.get("name") or "").strip()
    if feature.casefold() in _NO_WATER_NAME:
        feature = ""
    dist = data.get("distance_km")
    if feature and isinstance(dist, (int, float)) and dist > _FEATURE_NEAR_KM:
        feature = ""
    area = _water_area(data.get("contained_in"), lang)
    if feature or area:
        if feature and area and feature.casefold() != area.casefold():
            return GeoResult(country=area, locality=feature, state=area, source="Maritim-Backup"), up
        name = feature or area
        return GeoResult(country=name, locality=name, source="Maritim-Backup"), up
    if gazetteer_water:
        try:
            w = _gazetteer_water(s, lat, lon, lang)
        except Exception:                     # noqa: BLE001  (never let the fallback break a lookup)
            w = None
        if w:
            return w, True
    return None, up


_FAR_M = 500   # the matched object lies further away than this: the point itself may be open water


def _coarse(r: GeoResult) -> bool:
    """True when the answer reaches no further than country / state (no district, city, street; locality empty or just that
    country or state). Typical for water inside territorial limits: 'Spanien', 'England, Vereinigtes Koenigreich'."""
    return bool(r.country) and not (r.county or r.city or r.street) and r.locality in ("", r.country, r.state)


def _gazetteer(s: Settings, lat: float, lon: float, lang: str):
    """Last resort without any server: the nearest populated place of the own GeoNames database (within GN_GAZETTEER_REVERSE_KM,
    default 25 km) with its state, district and country. Coarse by nature (no streets) but never down."""
    from . import gazetteer
    g = gazetteer.get(s.gazetteer)
    if not g.available():
        return None, False
    near = g.nearby(lat, lon, limit=1, radius_km=s.gn_gazetteer_reverse_km, fclasses=["P"], lang=(lang or "")[:2] or None)
    if not near:
        return None, True
    p = near[0]
    cc = p["countryCode"]
    return GeoResult(country=g.country_local(cc, (lang or "")[:2]) if lang else p["countryName"], country_a=alpha3(cc),
                     state=p["adminName1"], county=p["adminName2"], city=p["name"], source="GeoNames"), True


def _maritime_open(s: Settings, lat: float, lon: float, lang: str):
    """Open water, nothing else answered: country = the sea, locality = the specific feature; no state."""
    res, up = _maritime(s, lat, lon, lang, True)
    if res:
        res.state = ""
    return res, up


_FUNCS = {"photon": _photon, "nominatim": _nominatim, "pelias": _pelias, "gazetteer": _gazetteer,
          "maritime": _maritime_open}


def _pelias_alpha3(s: Settings, lat: float, lon: float) -> str:
    data, _ = _get_json(f"{s.pelias.rstrip('/')}/v1/reverse?point.lat={lat}&point.lon={lon}", s.http_timeout)
    try:
        return data["features"][0]["properties"].get("country_a") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def reverse_chain(s: Settings, breaker: Breaker, lat: float, lon: float, lang: str):
    """Walks GEO_CHAIN, then the maritime backup. Returns (GeoResult | None, tried, reachable_any)
    where `tried` is a list of {backend, status} for the response, and reachable_any says whether
    at least one backend actually answered (False => don't cache a negative result)."""
    tried: list[dict] = []
    reachable_any = False
    result: Optional[GeoResult] = None
    for name in s.chain:
        if name not in _FUNCS or not getattr(s, name):
            continue
        if not breaker.allow(name):
            tried.append({"backend": name, "status": "skipped (circuit open)"})
            continue
        res, up = _FUNCS[name](s, lat, lon, lang)
        (breaker.ok if up else breaker.bad)(name)
        reachable_any = reachable_any or up
        tried.append({"backend": name, "status": "hit" if res else ("empty" if up else "unreachable")})
        if res:
            result = res
            if (_coarse(res) or res.dist_m > _FAR_M) and s.maritime and name != "maritime" and breaker.allow("maritime"):
                # Only country/state, or the nearest object is far away: typical for water (Strait of Gibraltar, Messina, Dover ...).
                # Ask the maritime server; if it knows the water body, name it and keep the country (not the state: water is not in a state).
                m, mup = _maritime(s, lat, lon, lang)
                (breaker.ok if mup else breaker.bad)("maritime")
                reachable_any = reachable_any or mup
                tried.append({"backend": "maritime", "status": "hit" if m else ("empty" if mup else "unreachable")})
                if m:
                    result = GeoResult(country=res.country, country_a=res.country_a, locality=m.locality,
                                       state=m.state if m.state != m.locality else "", source=f"{res.source} + Maritim-Backup")
            break
    if result is None and s.maritime and "maritime" not in s.chain and breaker.allow("maritime"):       # not in GEO_CHAIN: always last
        res, up = _maritime_open(s, lat, lon, lang)
        (breaker.ok if up else breaker.bad)("maritime")
        reachable_any = reachable_any or up
        tried.append({"backend": "maritime", "status": "hit" if res else ("empty" if up else "unreachable")})
        result = res
    if result is not None and not result.country_a and s.pelias and result.source not in ("Pelias", "Maritim-Backup") and breaker.allow("pelias"):
        # Photon/Nominatim only expose alpha-2; ask Pelias once for the alpha-3 code, like the suite does.
        result.country_a = _pelias_alpha3(s, lat, lon)
    return result, tried, reachable_any


_PROBE_PATHS = {"photon": "/reverse?lon=15.43&lat=47.07", "nominatim": "/reverse?lat=47.07&lon=15.43&format=jsonv2",
                "pelias": "/v1/reverse?point.lat=47.07&point.lon=15.43", "maritime": "/reverse?lat=47.07&lon=15.43"}


def probe(s: Settings, breaker: Breaker, name: str) -> dict:
    """Live check of ONE backend right now (not cached, not skipped by an open circuit). A successful answer also
    closes that backend's circuit again, a failure shows the real reason (connection refused, DNS, HTTP code ...)."""
    base = getattr(s, name, "")
    if not base:
        return {"configured": False}
    if name == "gazetteer":
        from . import gazetteer
        g = gazetteer.get(base)
        return {"configured": True, "url": base, "reachable": g.available(), "detail": f"{g.meta().get('n_place', '0')} places", "ms": 0}
    url = base.rstrip("/") + _PROBE_PATHS[name]
    t0 = time.time()
    try:
        r = requests.get(url, timeout=s.http_timeout, headers={"User-Agent": "geo-service/1.0"})
        up = r.status_code < 500
        detail = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        up, detail = False, f"{type(e).__name__}: {str(e)[:200]}"
    if up:
        breaker.ok(name)
    return {"configured": True, "url": base, "reachable": up, "detail": detail, "ms": int((time.time() - t0) * 1000)}
