"""around.py -- everything about one coordinate in ONE answer:  GET /around?lat&lon

  place      the reverse-geocoded place (country, state, city, street, house number, postcode ...)
  water      the sea / gulf the point lies in, the nearest named bay or strait, the lake it lies in, rivers nearby
             (needs MARITIME_SERVER; empty without it)
  sights     the best sights around (the same ranking as /sights) and a German summary sentence
  elevation  metres above sea level
  timezone   zone id, offsets, local time, sunrise / sunset

Every part is looked up on its own: one that fails shows up under `errors` and the rest still arrives. `include` picks
parts (comma list of place,water,sights,elevation,timezone; default all) so a client can skip the expensive ones."""
from __future__ import annotations

from typing import Callable, Optional

from . import backends
from . import sights as sightsmod
from . import timezone as tzmod
from . import water as watermod

PARTS = ("place", "water", "sights", "elevation", "timezone")
PLACE_FIELDS = ("found", "country", "country_a", "state", "county", "city", "locality", "neighbourhood", "street", "house_number",
                "postcode", "sublocation", "label", "source")


def parse_include(include: Optional[str]) -> tuple:
    """(parts, unknown) from a comma list; empty = all parts."""
    want = [p.strip().lower() for p in (include or "").split(",") if p.strip()]
    unknown = [p for p in want if p not in PARTS]
    return tuple(p for p in PARTS if (not want or p in want)), unknown


def water_part(s, lat: float, lon: float, radius_m: int, limit: int, lang: str) -> dict:
    """sea / feature (bay, strait ...) from the maritime server, lake and rivers like /sights?categories=water."""
    out = {"available": bool(getattr(s, "maritime", "")), "sea": "", "feature": "", "lake": "", "rivers": []}
    if not out["available"]:
        return out
    res, _up = backends._maritime(s, lat, lon, lang)
    if res is not None:
        out["sea"] = res.country or ""
        out["feature"] = res.locality if res.locality and res.locality != res.country else ""
    for w in watermod.near(s, lat, lon, radius_m, limit, lang):
        if w["kind"] == "See":
            out["lake"] = w["name"]
        else:
            out["rivers"].append({k: w[k] for k in ("name", "lat", "lon", "distance_m", "bearing", "direction")})
    out["rivers"].sort(key=lambda r: r["distance_m"])
    return out


def build(st, lat: float, lon: float, radius_m: int, limit: int, lang: str, parts: tuple,
          reverse: Callable[[float, float, str], dict], own_pois: Callable[[], list]) -> dict:
    out: dict = {"center": {"lat": lat, "lon": lon}, "radius_m": radius_m, "lang": lang, "errors": {}}
    s = st.s

    def part(name: str, fn: Callable):
        if name not in parts:
            return
        try:
            out[name] = fn()
        except Exception as e:          # noqa: BLE001  one failing part must not cost the others
            out["errors"][name] = str(getattr(e, "detail", e))[:300]

    def place():
        r = reverse(lat, lon, lang)
        return {k: r[k] for k in PLACE_FIELDS if k in r and r[k] not in ("", None)}

    def sights():
        lst = sightsmod.search(s, st.cache, lat, lon, radius_m, limit, lang, [], "score", own_pois())
        if lst is None:
            raise RuntimeError("no sights source (index or PHOTON_SERVER)")
        return {"count": len(lst), "items": lst, "text_de": sightsmod.phrase_de(lst),
                "source": "Sights-Index" if sightsmod.index(s) is not None else "Photon"}

    def elevation():
        e = st.elevation.lookup(lat, lon)
        return {"metres": e["elevation"], "source": e["source"]}

    def timezone():
        return tzmod.info(lat, lon, countries=getattr(st, "countries", None), default_tz=getattr(s, "default_tz", "")) or {}

    part("place", place)
    part("water", lambda: water_part(s, lat, lon, radius_m, limit, lang))
    part("sights", sights)
    part("elevation", elevation)
    part("timezone", timezone)
    if not out["errors"]:
        del out["errors"]
    return out
