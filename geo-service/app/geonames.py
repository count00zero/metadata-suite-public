"""geonames.py -- GeoNames-compatible  findNearbyPlaceName  and  findNearbyPostalCodes  (XML + JSON).

Replaces the separate "geonames-wrapper" container for these two endpoints: same URL paths, same
parameters (lat, lng, maxRows, style, lang, radius, country, postalcode, placename), same field names
and order as api.geonames.org, so a client only needs a new base URL.

  GET /findNearbyPlaceNameJSON?lat&lng[&maxRows=5&style=MEDIUM&lang]     (…without JSON = XML)
  GET /findNearbyPostalCodesJSON?lat&lng[&radius=10&maxRows=5]  |  ?postalcode=…|placename=…[&country]

How the answers are made (no GeoNames data involved -- your own Pelias / Nominatim / Photon):
  * findNearbyPlaceName   nearest *populated place*: Pelias reverse restricted to locality/localadmin
                          (a real nearest-neighbour search), else Nominatim reverse at town level, else Photon.
  * findNearbyPostalCodes Pelias reverse on the postalcode layer; where Pelias has none, a ring of Nominatim
                          reverse probes around the centre collects the distinct postcodes (concurrent).
Known approximations (also true for the old wrapper): `geonameId` is synthetic (stable via a small SQLite
registry, never equal to the real GeoNames id), `fcl`/`fcode` are guessed from OSM tags, `population` is 0
unless the source carries it, adminCode2/3 of the postal rows cannot be derived from OSM and are omitted.
Results are cached in the service's SQLite cache (table `geonames`).
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlencode
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import backends
from .iso import ALPHA2_TO_ALPHA3

AUTH_ERROR, OTHER_ERROR, INVALID_PARAMETER = 10, 13, 20
_ALPHA3_TO_2 = {v: k for k, v in ALPHA2_TO_ALPHA3.items()}

# --------------------------------------------------------------------------- GeoNames "style" parameter
SHORT_FIELDS = ["toponymName", "name", "lat", "lng", "geonameId", "countryCode", "fcl", "fcode"]
MEDIUM_FIELDS = ["toponymName", "name", "lat", "lng", "geonameId", "countryCode", "countryName", "fcl", "fcode"]
LONG_EXTRA = ["fclName", "fcodeName", "population", "adminCode1", "adminName1"]
FULL_EXTRA = ["adminCode2", "adminName2"]


def select_fields(rec: dict, style: str = "MEDIUM") -> dict:
    """Keeps exactly the keys GeoNames returns for `style`, in its order; `distance` always last."""
    st = (style or "MEDIUM").upper()
    keys = {"SHORT": SHORT_FIELDS, "LONG": MEDIUM_FIELDS + LONG_EXTRA,
            "FULL": MEDIUM_FIELDS + LONG_EXTRA + FULL_EXTRA}.get(st, MEDIUM_FIELDS)
    out = {}
    for k in keys:
        if k in rec and (rec[k] not in (None, "") or k in ("geonameId", "population")):
            out[k] = rec[k]
    if "distance" in rec:
        out["distance"] = rec["distance"]
    return out


# --------------------------------------------------------------------------- feature class / code (guessed)
FCL_NAMES = {"A": "country, state, region,...", "H": "stream, lake, ...", "L": "parks,area, ...",
             "P": "city, village,...", "R": "road, railroad", "S": "spot, building, farm",
             "T": "mountain,hill,rock,...", "U": "undersea", "V": "forest,heath,..."}
FEATURE_MAP = {
    ("place", "country"): ("A", "PCLI", "independent political entity"),
    ("boundary", "administrative"): ("A", "ADM1", "first-order administrative division"),
    ("place", "state"): ("A", "ADM1", "first-order administrative division"),
    ("place", "region"): ("A", "ADM1", "first-order administrative division"),
    ("place", "province"): ("A", "ADM1", "first-order administrative division"),
    ("place", "county"): ("A", "ADM2", "second-order administrative division"),
    ("place", "city"): ("P", "PPLA", "seat of a first-order administrative division"),
    ("place", "town"): ("P", "PPLA3", "seat of a third-order administrative division"),
    ("place", "village"): ("P", "PPL", "populated place"),
    ("place", "hamlet"): ("P", "PPLL", "populated locality"),
    ("place", "suburb"): ("P", "PPLX", "section of populated place"),
    ("place", "neighbourhood"): ("P", "PPLX", "section of populated place"),
    ("place", "borough"): ("P", "PPLX", "section of populated place"),
    ("place", "capital"): ("P", "PPLC", "capital of a political entity"),
    ("highway", None): ("R", "RD", "road"), ("railway", None): ("R", "RR", "railroad"),
    ("waterway", "river"): ("H", "STM", "stream"), ("waterway", "stream"): ("H", "STM", "stream"),
    ("natural", "water"): ("H", "LK", "lake"), ("natural", "bay"): ("H", "BAY", "bay"),
    ("natural", "peak"): ("T", "PK", "peak"), ("natural", "hill"): ("T", "HLL", "hill"),
    ("natural", "ridge"): ("T", "RDGE", "ridge(s)"), ("natural", "wood"): ("V", "FRST", "forest(s)"),
    ("landuse", "forest"): ("V", "FRST", "forest(s)"), ("leisure", "park"): ("L", "PRK", "park"),
    ("leisure", "nature_reserve"): ("L", "RESN", "nature reserve"),
    ("amenity", "school"): ("S", "SCH", "school"), ("amenity", "university"): ("S", "UNIV", "university"),
    ("amenity", "hospital"): ("S", "HSP", "hospital"), ("amenity", "place_of_worship"): ("S", "RLG", "religious site"),
    ("aeroway", "aerodrome"): ("S", "AIRP", "airport"), ("tourism", "hotel"): ("S", "HTL", "hotel"),
    ("building", None): ("S", "BLDG", "building(s)"), ("shop", None): ("S", "STORE", "store"),
}
PELIAS_LAYER_MAP = {
    "country": ("A", "PCLI", "independent political entity"), "macroregion": ("A", "ADM1", "first-order administrative division"),
    "region": ("A", "ADM1", "first-order administrative division"), "macrocounty": ("A", "ADM2", "second-order administrative division"),
    "county": ("A", "ADM2", "second-order administrative division"), "locality": ("P", "PPL", "populated place"),
    "localadmin": ("P", "PPLA2", "seat of a second-order administrative division"),
    "borough": ("P", "PPLX", "section of populated place"), "neighbourhood": ("P", "PPLX", "section of populated place"),
    "street": ("R", "RD", "road"), "address": ("S", "ADDR", "address"), "venue": ("S", "BLDG", "building(s)"),
    "postalcode": ("L", "PRSH", "postal code area"),
}
_UNK = ("S", "UNK", "unclassified / no mapping available")


def _feat(t: tuple) -> dict:
    return {"fcl": t[0], "fcode": t[1], "fclName": FCL_NAMES.get(t[0], ""), "fcodeName": t[2]}


def guess_feature_code(osm_class: Optional[str], osm_type: Optional[str], is_capital: bool = False) -> dict:
    c, t = (osm_class or "").lower() or None, (osm_type or "").lower() or None
    if is_capital and c == "place":
        return _feat(FEATURE_MAP[("place", "capital")])
    return _feat(FEATURE_MAP.get((c, t)) or FEATURE_MAP.get((c, None)) or _UNK)


# --------------------------------------------------------------------------- synthetic geonameId registry
class IdRegistry:
    """(source, native id) <-> small integer, persisted. Same table layout as the old wrapper's
    id_registry.sqlite3, so that file can simply be copied here and keeps every id you already handed out."""

    # real geonames.org ids stay below this; synthetic ones (OSM / Pelias / Photon places, admin units) are numbered above
    # it, so an id never means two things once the gazetteer (real ids) is in use. Ids handed out earlier keep their value.
    OFFSET = 1_000_000_000

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS id_registry (
                geoname_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, native_id TEXT NOT NULL,
                record_json TEXT, UNIQUE(source, native_id))""")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get_or_create(self, source: str, native_id: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT geoname_id FROM id_registry WHERE source=? AND native_id=?", (source, native_id)).fetchone()
            if row:
                return row[0]
            top = c.execute("SELECT COALESCE(MAX(geoname_id), 0) FROM id_registry").fetchone()[0]
            return c.execute("INSERT INTO id_registry (geoname_id, source, native_id) VALUES (?, ?, ?)",
                             (max(top + 1, self.OFFSET), source, native_id)).lastrowid

    def save_record(self, geoname_id: int, record: dict) -> None:
        """Snapshot of the place as last seen -- what /get returns later (Photon cannot look a place up by id)."""
        with self._lock, self._conn() as c:
            c.execute("UPDATE id_registry SET record_json=? WHERE geoname_id=?", (json.dumps(record, ensure_ascii=False), geoname_id))

    def get_record(self, geoname_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT record_json FROM id_registry WHERE geoname_id=?", (geoname_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None


# --------------------------------------------------------------------------- small helpers
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def fmt_coord(v) -> str:
    try:
        return f"{float(v):.5f}"
    except (TypeError, ValueError):
        return str(v) if v is not None else ""


def fmt_distance(v) -> float:
    try:
        return round(float(v), 5)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def ring_points(lat: float, lng: float, radius_km: float, rings: int, per_ring: int) -> list:
    pts = []
    for ring in range(1, rings + 1):
        r = radius_km * ring / rings
        for i in range(per_ring):
            ang = 2 * math.pi * i / per_ring
            pts.append((lat + (r / 111.32) * math.cos(ang),
                        lng + (r / (111.32 * max(math.cos(math.radians(lat)), 0.01))) * math.sin(ang)))
    return pts


def derive_admin_code1(address: dict) -> Optional[str]:
    """ISO 3166-2 suffix like GeoNames' adminCode1 (AT-3 -> '03', US-AL -> 'AL')."""
    iso = address.get("ISO3166-2-lvl4")
    if not iso or "-" not in iso:
        return None
    suffix = iso.split("-", 1)[1]
    return suffix.zfill(2) if suffix.isdigit() else suffix


_NAME_FALLBACK = ("city", "town", "village", "hamlet", "suburb", "municipality", "county", "state", "country")


def _derive_name(item: dict, address: dict) -> str:
    if item.get("name"):
        return item["name"]
    first = (item.get("display_name") or "").split(",")[0].strip()
    hn = (address.get("house_number") or "").strip()
    if first and not (first == hn or first.isdigit()):
        return first
    for k in _NAME_FALLBACK:
        if address.get(k):
            return address[k]
    return first


# --------------------------------------------------------------------------- the service
class GeoNames:
    def __init__(self, st):
        self.st = st
        s = st.s
        self.s = s
        db = s.gn_id_db or os.path.join(os.path.dirname(s.cache_db) or ".", "geonames-ids.sqlite")
        self.ids = IdRegistry(db)
        from . import gazetteer
        self.gaz = gazetteer.get(s.gn_gazetteer_db or os.path.join(os.path.dirname(s.cache_db) or ".", "gazetteer.sqlite"),
                                 lambda cc: (getattr(st.countries, "by_code", lambda _c: None)(cc) or {}).get("countryName", "") if st.countries else "")

    # ---- backend access: (data, reachable) -- nothing here raises
    def _get(self, base: str, path: str, params: dict, headers: Optional[dict] = None):
        return backends._get_json(f"{base.rstrip('/')}{path}?{urlencode({k: v for k, v in params.items() if v is not None})}",
                                  self.s.gn_http_timeout, headers)

    def nom_reverse(self, lat, lng, zoom=None, lang=None, addressdetails=True, extra=None):
        if not self.s.nominatim:
            return None, False
        data, up = self._get(self.s.nominatim, "/reverse", {
            "format": "jsonv2", "lat": lat, "lon": lng, "addressdetails": 1 if addressdetails else 0, "extratags": 1,
            "zoom": zoom, "accept-language": lang, **(extra or {})}, {"User-Agent": "geo-service/1.0"})
        return (data if isinstance(data, dict) and "error" not in data else None), up

    def nom_search(self, structured=None, q=None, country=None, limit=10, lang=None, bbox=None, extra=None):
        if not self.s.nominatim:
            return [], False
        params = {"format": "jsonv2", "addressdetails": 1, "extratags": 1, "limit": limit, "countrycodes": country,
                  "accept-language": lang}
        if extra:
            params.update(extra)
        if bbox:                                     # viewbox is x1,y1,x2,y2 = west,north,east,south
            params.update({"viewbox": f"{bbox[0]},{bbox[3]},{bbox[2]},{bbox[1]}", "bounded": 1})
        if structured:
            params.update({k: v for k, v in structured.items() if v})
        elif q:
            params["q"] = q
        data, up = self._get(self.s.nominatim, "/search", params, {"User-Agent": "geo-service/1.0"})
        return (data if isinstance(data, list) else []), up

    def pelias_reverse(self, lat, lng, size=10, layers=None, lang=None, radius_km=None):
        if not self.s.pelias:
            return [], False
        data, up = self._get(self.s.pelias, "/v1/reverse", {"point.lat": lat, "point.lon": lng, "size": size,
                                                            "layers": layers, "lang": lang, "boundary.circle.radius": radius_km})
        return ((data or {}).get("features") or [] if isinstance(data, dict) else []), up

    def pelias_search(self, text, size=10, lang=None, autocomplete=False, layers=None, countries=None, bbox=None):
        """bbox = (west, south, east, north); countries = ISO2 list (Pelias wants alpha-2 or alpha-3, comma separated)."""
        if not self.s.pelias:
            return [], False
        params = {"text": text, "size": size, "lang": lang, "layers": layers,
                  "boundary.country": ",".join(countries) if countries else None}
        if bbox:
            params.update({"boundary.rect.min_lon": bbox[0], "boundary.rect.min_lat": bbox[1],
                           "boundary.rect.max_lon": bbox[2], "boundary.rect.max_lat": bbox[3]})
        data, up = self._get(self.s.pelias, "/v1/autocomplete" if autocomplete else "/v1/search", params)
        return ((data or {}).get("features") or [] if isinstance(data, dict) else []), up

    def photon_search(self, q, limit=10, lang=None, bbox=None):
        if not self.s.photon:
            return [], False
        data, up = self._get(self.s.photon, "/api", {"q": q, "limit": limit, "lang": lang or "en",
                                                     "bbox": ",".join(str(v) for v in bbox) if bbox else None})
        return ((data or {}).get("features") or [] if isinstance(data, dict) else []), up

    def photon_reverse(self, lat, lng, limit=10):
        if not self.s.photon:
            return [], False
        data, up = self._get(self.s.photon, "/reverse", {"lat": lat, "lon": lng, "limit": limit})
        return ((data or {}).get("features") or [] if isinstance(data, dict) else []), up

    def photon_streets(self, lat, lng, radius_km=0.2, limit=20):
        """Named streets (osm_tag=highway) around the point from Photon, nearest first as Photon returns them."""
        if not self.s.photon:
            return [], False
        data, up = self._get(self.s.photon, "/reverse", {"lat": lat, "lon": lng, "radius": radius_km, "limit": limit,
                                                         "osm_tag": "highway"})
        return ((data or {}).get("features") or [] if isinstance(data, dict) else []), up

    # ---- normalising the three backends into one GeoNames-shaped record
    def from_nominatim(self, item: dict, distance_km=None, localized_name=None, localized_country=None) -> dict:
        address = item.get("address") or {}
        extra = item.get("extratags") or {}
        native = f"{(item.get('osm_type') or '')[:1].upper()}{item.get('osm_id')}"
        top = _derive_name(item, address)
        feat = guess_feature_code(item.get("class"), item.get("type"),
                                  is_capital=str(extra.get("capital", "")).lower() in ("yes", "national"))
        rec = {"geonameId": self.ids.get_or_create("nominatim", native), "name": localized_name or top, "toponymName": top,
               "lat": fmt_coord(item.get("lat")), "lng": fmt_coord(item.get("lon")),
               "countryCode": (address.get("country_code") or "").upper(),
               "countryName": localized_country or address.get("country", ""),
               "adminCode1": derive_admin_code1(address) or "", "adminName1": address.get("state") or address.get("region") or "",
               "adminCode2": "", "adminName2": address.get("county", ""),
               "population": _safe_int(extra.get("population")), **feat}
        if distance_km is not None:
            rec["distance"] = fmt_distance(distance_km)
        self.ids.save_record(rec["geonameId"], rec)
        return rec

    def from_maritime(self, name: str, lat: float, lng: float) -> dict:
        rec = {"geonameId": self.ids.get_or_create("maritime", name), "name": name, "toponymName": name,
               "lat": fmt_coord(lat), "lng": fmt_coord(lng), "countryCode": "", "countryName": "",
               "adminCode1": "", "adminName1": "", "adminCode2": "", "adminName2": "", "population": 0,
               "distance": fmt_distance(0), **_feat(("H", "SEA", "sea"))}
        self.ids.save_record(rec["geonameId"], rec)
        return rec

    def from_pelias(self, feature: dict, distance_km=None) -> dict:
        p = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates", [None, None])
        name = p.get("name") or p.get("label", "")
        gn = ((p.get("addendum") or {}).get("gn")) or {}
        cc = p.get("country_code") or ""
        if not cc:
            a = (p.get("country_a") or "").upper()
            cc = a if len(a) == 2 else _ALPHA3_TO_2.get(a, "")
        rec = {"geonameId": self.ids.get_or_create("pelias", p.get("gid", "")), "name": name, "toponymName": name,
               "lat": fmt_coord(coords[1]) if coords[1] is not None else "", "lng": fmt_coord(coords[0]) if coords[0] is not None else "",
               "countryCode": cc.upper(), "countryName": p.get("country", ""),
               "adminCode1": p.get("region_a", ""), "adminName1": p.get("region", ""),
               "adminCode2": "", "adminName2": p.get("county", ""),
               "population": _safe_int(gn.get("population")), **_feat(PELIAS_LAYER_MAP.get((p.get("layer") or "").lower(), _UNK))}
        if distance_km is not None:
            rec["distance"] = fmt_distance(distance_km)
        elif "distance" in p:
            rec["distance"] = fmt_distance(p["distance"])
        self.ids.save_record(rec["geonameId"], rec)
        return rec

    def from_photon(self, feature: dict, distance_km=None) -> dict:
        p = feature.get("properties") or {}
        coords = (feature.get("geometry") or {}).get("coordinates", [None, None])
        name = p.get("name", "")
        rec = {"geonameId": self.ids.get_or_create("photon", f"{p.get('osm_type', '')}{p.get('osm_id', '')}"),
               "name": name, "toponymName": name,
               "lat": fmt_coord(coords[1]) if coords[1] is not None else "", "lng": fmt_coord(coords[0]) if coords[0] is not None else "",
               "countryCode": (p.get("countrycode") or "").upper(), "countryName": p.get("country", ""),
               "adminCode1": "", "adminName1": p.get("state", ""), "adminCode2": "", "adminName2": p.get("county", ""),
               "population": 0, **guess_feature_code(p.get("osm_key"), p.get("osm_value"))}
        if distance_km is not None:
            rec["distance"] = fmt_distance(distance_km)
        self.ids.save_record(rec["geonameId"], rec)
        return rec

    # ---- findNearbyPlaceName
    def nearest_populated_place(self, lat: float, lng: float, max_rows: int, lang: Optional[str]):
        """(records, any_backend_reachable)."""
        reachable = False
        if self.gaz.available():                      # own GeoNames database: real nearest populated places, real ids
            recs = self.gaz.nearby(lat, lng, limit=max_rows, fclasses=["P"], lang=(lang or "")[:2] or None)
            if recs:
                return recs, True
            reachable = True
            if self.s.maritime:                       # nothing within 1000 km = open sea: the maritime server names it,
                res, up = backends._maritime_open(self.s, lat, lng, (lang or self.s.lang or "")[:2])      # before the slow geocoders
                if res and res.locality:
                    return [self.from_maritime(res.locality, lat, lng)], True
        feats, up = self.pelias_reverse(lat, lng, size=max_rows, layers="locality,localadmin", lang=lang)
        reachable |= up
        recs = []
        for f in feats:
            c = (f.get("geometry") or {}).get("coordinates", [None, None])
            d = haversine_km(lat, lng, c[1], c[0]) if c[1] is not None and c[0] is not None else None
            recs.append(self.from_pelias(f, distance_km=d))
        if recs:
            recs.sort(key=lambda r: r.get("distance", 0))
            return recs[:max_rows], True

        raw, up = self.nom_reverse(lat, lng, zoom=12)
        reachable |= up
        if raw:
            ln = lc = None
            if lang:
                loc, _ = self.nom_reverse(lat, lng, zoom=12, lang=lang)
                if loc:
                    ln, lc = loc.get("name") or None, (loc.get("address") or {}).get("country") or None
            rec = self.from_nominatim(raw, localized_name=ln, localized_country=lc)
            rec["distance"] = fmt_distance(haversine_km(lat, lng, float(rec["lat"] or lat), float(rec["lng"] or lng)))
            return [rec], True

        feats, up = self.photon_reverse(lat, lng, limit=max_rows)
        reachable |= up
        return [self.from_photon(f) for f in feats][:max_rows], reachable

    # ---- findNearby (any feature class)
    CLASS_LAYERS = {"A": "country,macroregion,region,macrocounty,county,localadmin", "P": "locality,localadmin,borough,neighbourhood",
                    "R": "street", "S": "venue,address", "T": "venue", "H": "venue", "L": "venue", "V": "venue", "U": "venue"}

    def find_nearby(self, lat: float, lng: float, radius_km: float, max_rows: int, fclasses=None, fcodes=None, lang=None):
        """GeoNames findNearby: nearest toponyms of any class within radius_km, nearest first.
        (records, any_backend_reachable). fcl/fcode are guessed, so featureClass/featureCode filter on the guess."""
        fclasses = [c.upper() for c in (fclasses or []) if c]
        fcodes = [c.upper() for c in (fcodes or []) if c]
        reachable = False
        if self.gaz.available():
            got = self.gaz.nearby(lat, lng, limit=max_rows, radius_km=radius_km, fclasses=fclasses, fcodes=fcodes, lang=(lang or "")[:2] or None)
            if got:
                return got, True
        recs, seen = [], set()

        def take(rec):
            if rec["geonameId"] in seen:
                return
            if fclasses and rec.get("fcl") not in fclasses:
                return
            if fcodes and rec.get("fcode") not in fcodes:
                return
            if rec.get("distance", 0) > radius_km:
                return
            seen.add(rec["geonameId"])
            recs.append(rec)

        def pelias(layers):
            feats, up = self.pelias_reverse(lat, lng, size=max(max_rows * 3, 10), layers=layers, lang=lang, radius_km=radius_km)
            for f in feats:
                c = (f.get("geometry") or {}).get("coordinates", [None, None])
                d = haversine_km(lat, lng, c[1], c[0]) if c[1] is not None and c[0] is not None else None
                if d is not None:
                    take(self.from_pelias(f, distance_km=d))
            return up

        if fclasses:
            layers = ",".join(dict.fromkeys(l for c in fclasses for l in self.CLASS_LAYERS.get(c, "venue").split(",")))
            reachable |= pelias(layers)
        else:
            reachable |= pelias(None)                                 # venues / addresses / streets nearby ...
            reachable |= pelias("locality,localadmin")                # ... and the populated place itself
        if not recs:
            for zoom in (18, 14, 10):
                raw, up = self.nom_reverse(lat, lng, zoom=zoom, lang=lang)
                reachable |= up
                if raw:
                    rec = self.from_nominatim(raw)
                    try:
                        rec["distance"] = fmt_distance(haversine_km(lat, lng, float(rec["lat"]), float(rec["lng"])))
                    except (TypeError, ValueError):
                        continue
                    take(rec)
        if not recs:
            feats, up = self.photon_reverse(lat, lng, limit=max(max_rows * 3, 10))
            reachable |= up
            for f in feats:
                c = (f.get("geometry") or {}).get("coordinates", [None, None])
                if c[1] is not None and c[0] is not None:
                    take(self.from_photon(f, distance_km=haversine_km(lat, lng, c[1], c[0])))
        recs.sort(key=lambda r: r.get("distance", 0))
        return recs[:max_rows], (reachable or bool(recs))

    # ---- findNearbyPostalCodes
    def _resolve_center(self, lat, lng, postalcode, placename, country):
        if lat is not None and lng is not None:
            return lat, lng
        raw = []
        if postalcode:
            raw, _ = self.nom_search(structured={"postalcode": postalcode}, country=country, limit=1)
        if not raw and placename:
            raw, _ = self.nom_search(q=placename, country=country, limit=1)
        if raw:
            r = self.from_nominatim(raw[0])
            return float(r["lat"]), float(r["lng"])
        return None

    def find_nearby_postal_codes(self, lat, lng, postalcode, placename, country, radius_km: float, max_rows: int):
        """(rows | None when no centre could be determined, any_backend_reachable)."""
        if self.gaz.has("postal"):                    # GeoNames' own postal code table
            if lat is None or lng is None:
                first = self.gaz.postal_search(code=postalcode or "", place=placename or "", country=country or "", limit=1)
                if first:
                    lat, lng = float(first[0]["lat"]), float(first[0]["lng"])
            if lat is not None and lng is not None:
                hits = self.gaz.postal_near(lat, lng, radius_km, max_rows)
                if hits:
                    return [{k: v for k, v in {
                        "postalcode": h["code"], "name": h["placeName"], "countryCode": h["countryCode"], "lat": h["lat"], "lng": h["lng"],
                        "adminCode1": h["adminCode1"], "adminName1": h["adminName1"], "adminName2": h["adminName2"],
                        "adminName3": h["adminName3"], "distance": fmt_distance(h["distance"])}.items() if v not in (None, "")}
                        for h in hits], True
        center = self._resolve_center(lat, lng, postalcode, placename, country)
        if center is None:
            return None, True
        clat, clng = center
        reachable = False

        adm = {}
        raw, up = self.nom_reverse(clat, clng, zoom=14)
        reachable |= up
        if raw:
            a = raw.get("address") or {}
            adm = {"admin_code1": derive_admin_code1(a), "admin_name1": a.get("state") or a.get("region"),
                   "admin_name2": a.get("county"), "admin_name3": a.get("municipality") or a.get("town") or a.get("city")}

        def row(code, name, cc, la, lo, dist):
            r = {"postalcode": code, "name": name, "countryCode": cc, "lat": la, "lng": lo,
                 "adminCode1": adm.get("admin_code1"), "adminName1": adm.get("admin_name1"),
                 "adminName2": adm.get("admin_name2"), "adminName3": adm.get("admin_name3"),
                 "distance": fmt_distance(dist)}
            return {k: v for k, v in r.items() if v not in (None, "")}

        out, seen = [], set()
        feats, up = self.pelias_reverse(clat, clng, size=max(max_rows * 3, 20), layers="postalcode")
        reachable |= up
        for f in feats:
            p = f.get("properties") or {}
            code = p.get("postalcode") or p.get("name")
            c = (f.get("geometry") or {}).get("coordinates", [None, None])
            if not code or c[1] is None or c[0] is None:
                continue
            dist = haversine_km(clat, clng, c[1], c[0])
            if dist > radius_km:
                continue
            loc = p.get("locality") or p.get("localadmin") or p.get("region") or ""
            key = (code, loc, round(dist, 3))
            if key in seen:
                continue
            seen.add(key)
            rec = self.from_pelias(f, distance_km=dist)
            out.append(row(code, loc or rec.get("name", ""), rec.get("countryCode", ""), rec.get("lat", ""), rec.get("lng", ""), dist))
        if out:
            out.sort(key=lambda r: r["distance"])
            return out[:max_rows], True

        # Pelias has no postalcode layer here: probe a ring of points with Nominatim, collect the distinct postcodes
        pts = [(clat, clng)] + ring_points(clat, clng, radius_km, self.s.gn_probe_rings, self.s.gn_probe_points)
        with ThreadPoolExecutor(max_workers=max(1, self.s.gn_probe_concurrency)) as ex:
            probes = list(ex.map(lambda p: self.nom_reverse(p[0], p[1], zoom=18), pts))
        codes, results = set(), []
        for raw, up in probes:
            reachable |= up
            if not raw:
                continue
            code = (raw.get("address") or {}).get("postcode")
            if not code or code in codes:
                continue
            rec = self.from_nominatim(raw)
            try:
                dist = haversine_km(clat, clng, float(rec["lat"]), float(rec["lng"]))
            except (TypeError, ValueError):
                continue
            if dist > radius_km:
                continue
            codes.add(code)
            results.append(row(code, rec.get("name", ""), rec.get("countryCode", ""), rec.get("lat", ""), rec.get("lng", ""), dist))
        results.sort(key=lambda r: r["distance"])
        return results[:max_rows], reachable

    def clamp(self, n) -> int:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(n, self.s.gn_max_rows))


# --------------------------------------------------------------------------- rendering (GeoNames JSON / XML shapes)
def _append(parent: Element, fields: dict) -> None:
    for k, v in fields.items():
        if v is None:
            continue
        if isinstance(v, dict):
            _append(SubElement(parent, k), v)
        elif isinstance(v, (list, tuple)):
            for item in v:
                ch = SubElement(parent, k)
                if isinstance(item, dict):
                    _append(ch, item)
                else:
                    ch.text = str(item)
        else:
            SubElement(parent, k).text = str(v)


def _xml(root: Element) -> Response:
    return Response(minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  "), media_type="application/xml")


def list_response(as_xml: bool, records: list, item_tag: str, list_key: str) -> Response:
    if as_xml:
        root = Element("geonames")
        for r in records:
            _append(SubElement(root, item_tag), r)
        return _xml(root)
    return JSONResponse({list_key: records})


def error_response(message: str, value: int, as_xml: bool) -> Response:
    """GeoNames answers errors with HTTP 200 and a status object."""
    payload = {"status": {"message": message, "value": value}}
    if as_xml:
        root = Element("geonames")
        _append(root, payload)
        return _xml(root)
    return JSONResponse(payload)


# --------------------------------------------------------------------------- routes
def register(app, st, auth) -> GeoNames:
    gn = GeoNames(st)
    router = APIRouter()

    def gn_auth(request: Request) -> None:
        """Same token as the rest of the API; GeoNames clients cannot set headers, so ?token= is accepted too."""
        if st.s.token and request.query_params.get("token") != st.s.token:
            auth(request)

    def cached(kind: str, key: str):
        return st.cache.get("geonames", f"{kind}|{key}")

    def remember(kind: str, key: str, value) -> None:
        st.cache.put("geonames", f"{kind}|{key}", value, st.s.cache_ttl_days * 86400)

    def place(lat: float, lng: float, max_rows: int, style: str, lang: Optional[str], as_xml: bool):
        n = gn.clamp(max_rows)
        key = f"{lat:.6f}|{lng:.6f}|{n}|{(style or 'MEDIUM').upper()}|{lang or ''}"
        hit = cached("place", key)
        if hit is not None:
            r = list_response(as_xml, hit, "geoname", "geonames")
            r.headers["X-Geo-Cache"] = "hit"
            return r
        recs, up = gn.nearest_populated_place(lat, lng, n, lang)
        if not recs and not up:
            return error_response("no geocoding backend reachable", OTHER_ERROR, as_xml)
        styled = [select_fields(r, style) for r in recs]
        if styled:
            remember("place", key, styled)
        return list_response(as_xml, styled, "geoname", "geonames")

    def postal(lat, lng, postalcode, placename, country, radius, max_rows, as_xml: bool):
        n = gn.clamp(max_rows)
        key = f"{lat}|{lng}|{postalcode or ''}|{placename or ''}|{country or ''}|{radius}|{n}"
        hit = cached("postal", key)
        if hit is not None:
            r = list_response(as_xml, hit, "code", "postalCodes")
            r.headers["X-Geo-Cache"] = "hit"
            return r
        rows, up = gn.find_nearby_postal_codes(lat, lng, postalcode, placename, country, radius, n)
        if rows is None:
            return error_response("parameter 'postalcode'/'placename' or 'lat'/'lng' is required", INVALID_PARAMETER, as_xml)
        if not rows and not up:
            return error_response("no geocoding backend reachable", OTHER_ERROR, as_xml)
        if rows:
            remember("postal", key, rows)
        return list_response(as_xml, rows, "code", "postalCodes")

    def make(as_xml: bool):
        def place_ep(lat: float, lng: float, maxRows: int = Query(5, ge=1, le=1000), style: str = Query("MEDIUM"),
                     lang: Optional[str] = Query(None)):
            return place(lat, lng, maxRows, style, lang, as_xml)

        def postal_ep(lat: Optional[float] = Query(None), lng: Optional[float] = Query(None),
                      postalcode: Optional[str] = Query(None), placename: Optional[str] = Query(None),
                      country: Optional[str] = Query(None), radius: float = Query(10.0, gt=0, le=300),
                      maxRows: int = Query(5, ge=1, le=1000)):
            return postal(lat, lng, postalcode, placename, country, radius, maxRows, as_xml)
        return place_ep, postal_ep

    for suffix, as_xml in (("", True), ("JSON", False)):
        place_ep, postal_ep = make(as_xml)
        router.add_api_route(f"/findNearbyPlaceName{suffix}", place_ep, methods=["GET"], dependencies=[Depends(gn_auth)])
        router.add_api_route(f"/findNearbyPostalCodes{suffix}", postal_ep, methods=["GET"], dependencies=[Depends(gn_auth)])

    from . import geonames_ws
    geonames_ws.register(router, gn, st, gn_auth, cached, remember)
    app.include_router(router)
    return gn
