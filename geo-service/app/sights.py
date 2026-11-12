"""sights.py -- sights near a position: ranking, de-duplication, bearing and details. ONE code path for /sights, /pois,
/context and findNearbyWikipedia.

Source: the own sights index (sightsindex.py, built from the planet file) when its database exists -- otherwise YOUR Photon
(reverse search with OSM tag filters, one request per tag, in parallel). Photon only returns the nearest objects per request,
so in a dense city a far-away famous sight can be missed; the index has no such limit. Photon stays the fallback.

Ranking: score = category weight * distance decay + notability bonus.
  decay  = 1 / (1 + (d / d0)^2),  d0 = max(120 m, 20 % of the radius)   (halves at d0, a quarter at 1.7 * d0 ...)
  bonus  = +0.30 wikidata/wikipedia, +0.05 website, +0.03 opening hours, +0.02 description,
           up to +0.15 for the number of Wikipedia languages (own index after the Wikidata run; 300+ languages = full bonus)
Own places ("Meine Orte" with the sight flag) count as category weight 0.95 and +0.10.
Raw Photon answers are cached around the 3rd decimal (~100 m); ranking happens after the cache, with the exact position.
"""
from __future__ import annotations

import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote

from . import backends

CACHE_VERSION = "v3"            # v3: more tags (zoo, theme park, nature reserve), details, categories; v2 lists are not reused

# Photon request per tag: (osm_tag filter, request limit). `historic` is crowded with minor objects -> bigger limit.
FILTERS = (("tourism:attraction", 30), ("tourism:museum", 30), ("historic", 50), ("tourism:viewpoint", 30),
           ("natural:peak", 30), ("waterway:waterfall", 30), ("tourism:zoo", 20), ("tourism:theme_park", 20),
           ("leisure:nature_reserve", 20))

# category id -> (German label, weight)
CATEGORIES = {
    "castle": ("Burg/Schloss", 1.00), "museum": ("Museum", 0.95), "attraction": ("Sehenswürdigkeit", 0.90),
    "zoo": ("Zoo", 0.90), "theme_park": ("Freizeitpark", 0.90), "waterfall": ("Wasserfall", 0.85),
    "viewpoint": ("Aussichtspunkt", 0.80), "religious": ("Kirche/Kloster", 0.75), "ruins": ("Ruine/Ausgrabung", 0.75),
    "nature_reserve": ("Naturschutzgebiet", 0.70), "monument": ("Denkmal/Bauwerk", 0.65), "peak": ("Gipfel", 0.65),
    "historic": ("Historisches Objekt", 0.50),
}
_HIST_CAT = {"castle": "castle", "manor": "castle", "fort": "castle", "palace": "castle", "city_gate": "castle",
             "ruins": "ruins", "archaeological_site": "ruins",
             "church": "religious", "monastery": "religious", "cathedral": "religious", "chapel": "religious", "abbey": "religious",
             "monument": "monument", "tower": "monument", "bridge": "monument", "ship": "monument", "cannon": "monument", "tank": "monument"}
_TOURISM_CAT = {"attraction": "attraction", "museum": "museum", "viewpoint": "viewpoint", "zoo": "zoo", "theme_park": "theme_park"}
_OTHER_CAT = {("natural", "peak"): "peak", ("waterway", "waterfall"): "waterfall", ("leisure", "nature_reserve"): "nature_reserve"}

KIND_DE = {"attraction": "Sehenswürdigkeit", "museum": "Museum", "viewpoint": "Aussichtspunkt", "zoo": "Zoo",
           "theme_park": "Freizeitpark", "peak": "Gipfel", "waterfall": "Wasserfall", "nature_reserve": "Naturschutzgebiet",
           "castle": "Burg/Schloss", "manor": "Schloss", "ruins": "Ruine", "monument": "Denkmal", "memorial": "Gedenkstätte",
           "archaeological_site": "Ausgrabungsstätte", "fort": "Festung", "city_gate": "Stadttor", "tower": "Turm",
           "church": "Kirche", "monastery": "Kloster", "wayside_cross": "Wegkreuz", "wayside_shrine": "Bildstock",
           "building": "Historisches Gebäude", "bridge": "Historische Brücke", "ship": "Museumsschiff", "cannon": "Kanone", "tank": "Panzer"}
# Not real sights: plaques/memorial stones (historic=memorial incl. Gedenktafel, Stolperstein), graves, small relics,
# wayside crosses, and generic "historic building" (any old house). Extend with GEO_POI_SKIP.
SKIP_HISTORIC = {"wayside_cross", "wayside_shrine", "boundary_stone", "milestone", "charcoal_pile",
                 "memorial", "plaque", "tomb", "grave_yard", "aircraft", "locomotive", "rolling_stock",
                 "vehicle", "wreck", "building", "heritage", "highwater_mark", "stone", "cross", "marker", "yes"}

MAX_RADIUS_M = 20000
DIRECTIONS = ("N", "NO", "O", "SO", "S", "SW", "W", "NW")


def hav_m(la1, lo1, la2, lo2) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, a)))


def bearing(la1, lo1, la2, lo2) -> int:
    """Initial compass bearing from point 1 to point 2, 0..359 (0 = north, 90 = east)."""
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round(math.degrees(math.atan2(y, x)) % 360)) % 360


def direction_de(deg: int) -> str:
    return DIRECTIONS[int(((deg % 360) + 22.5) // 45) % 8]


def wanted(key: str, value: str, extra_skip=()) -> bool:
    if key == "historic":
        return value not in SKIP_HISTORIC and value not in extra_skip
    return (key, value) in {("tourism", "attraction"), ("tourism", "museum"), ("tourism", "viewpoint"), ("tourism", "zoo"),
                            ("tourism", "theme_park"), ("natural", "peak"), ("waterway", "waterfall"), ("leisure", "nature_reserve")}


def category_of(key: str, value: str) -> str:
    if key == "historic":
        return _HIST_CAT.get(value, "historic")
    if key == "tourism":
        return _TOURISM_CAT.get(value, "attraction")
    return _OTHER_CAT.get((key, value), "attraction")


def kind_de(key: str, value: str) -> str:
    return KIND_DE.get(value) or ("Historisches Objekt" if key == "historic" else value.replace("_", " ").title())


def _norm(name: str) -> str:
    return re.sub(r"[\W_]+", "", name.casefold())


def _details(pr: dict) -> dict:
    """Optional OSM details Photon carries in properties.extra (depends on how your Photon index was imported)."""
    ex = pr.get("extra") if isinstance(pr.get("extra"), dict) else {}

    def pick(*keys):
        for k in keys:
            v = ex.get(k) or pr.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    out = {"wikidata": pick("wikidata"), "wikipedia": pick("wikipedia"),
           "website": pick("website", "contact:website", "url"), "opening_hours": pick("opening_hours"),
           "description": pick("description", "description:de", "short_description"), "phone": pick("phone", "contact:phone")}
    return {k: v for k, v in out.items() if v}


def wikipedia_url(tag: str, lang: str = "de") -> str:
    """'de:Schloss Musterdorf' -> https://de.wikipedia.org/wiki/Schloss_Musterdorf"""
    m = re.match(r"^([a-z]{2,3}(?:-[a-z]+)?):(.+)$", tag.strip())
    lg, title = (m.group(1), m.group(2)) if m else (lang, tag.strip())
    return f"https://{lg}.wikipedia.org/wiki/{quote(title.strip().replace(' ', '_'), safe='_(),')}"


def wikidata_url(qid: str) -> str:
    return f"https://www.wikidata.org/wiki/{qid}"


def index(s):
    """The own sights index when its database exists and holds sights, else None (-> Photon)."""
    from . import sightsindex
    path = s.sights_index_path
    if not os.path.isfile(path):
        return None
    idx = sightsindex.get(path)
    return idx if idx.available() else None


def _bucket(radius_m: int) -> int:
    for b in (500, 1000, 2500, 5000, 10000, 20000):
        if radius_m <= b:
            return b
    return 20000


def _fetch_one(s, lat: float, lon: float, radius_km: float, flt: str, limit: int, lang: str):
    return backends._get_json(
        f"{s.photon.rstrip('/')}/reverse?lon={lon}&lat={lat}&radius={max(0.1, radius_km):.2f}&limit={limit}"
        f"&osm_tag={flt}&lang={lang}", s.http_timeout)


def collect(s, cache, lat: float, lon: float, radius_m: int, lang: str) -> Optional[list]:
    """Raw sights around the ~100 m cell of the position (cached). None = no source reachable and nothing cached."""
    idx = index(s)
    if idx is not None:                                     # own planet-based index: exact position, no Photon limit, no cache needed
        return idx.within(lat, lon, radius_m, lang)
    bucket = _bucket(radius_m)
    key = f"poi|{lat:.3f}|{lon:.3f}|{bucket}|{lang}|{CACHE_VERSION}"
    hit = cache.get("geonames", key)
    if hit is not None:
        return hit
    if not s.photon:
        return None
    center_lat, center_lon = round(lat, 3), round(lon, 3)               # the cache cell's centre: same answer for the whole cell
    km = (bucket + 150) / 1000
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda f: _fetch_one(s, center_lat, center_lon, km, f[0], f[1], lang), FILTERS))
    reachable = any(up for _d, up in results)
    found, seen = [], set()
    skip = getattr(s, "poi_skip", ())
    for data, _up in results:
        for ft in ((data or {}).get("features") or []) if isinstance(data, dict) else []:
            pr = ft.get("properties") or {}
            name = (pr.get("name") or "").strip()
            k, v = pr.get("osm_key", ""), pr.get("osm_value", "")
            co = (ft.get("geometry") or {}).get("coordinates") or []
            if not name or len(co) < 2 or not wanted(k, v, skip):
                continue
            ident = (name.lower(), k, v)
            if ident in seen:
                continue
            seen.add(ident)
            found.append({"name": name, "kind": kind_de(k, v), "category": category_of(k, v), "lat": co[1], "lon": co[0],
                          "osm": f"{(pr.get('osm_type') or '')[:1]}{pr.get('osm_id', '')}", **_details(pr)})
    if not reachable:
        return None
    cache.put("geonames", key, found, (s.cache_ttl_days if found else 1) * 86400)
    return found


def _score(item: dict, dist_m: float, radius_m: int) -> float:
    """Category weight (flattened: 0.5 of its spread, so a palace is not worth more than a cathedral just because OSM calls it a
    castle) with a distance decay, plus notability bonuses. The Wikimedia importance (0.2 = ordinary article, 0.6 = world famous)
    carries the most weight, so a well-known sight 200 m away beats an unknown one next door."""
    if item.get("own"):
        w, bonus = 1.0, 0.50
    else:
        w = CATEGORIES.get(item.get("category") or "historic", ("", 0.5))[1]
        w = 0.8 + (w - 0.8) * 0.5
        bonus = (0.15 if (item.get("wikidata") or item.get("wikipedia")) else 0.0) + (0.05 if item.get("website") else 0.0) \
            + (0.03 if item.get("opening_hours") else 0.0) + (0.02 if item.get("description") else 0.0) \
            + 0.10 * min(1.0, math.log10(1 + (item.get("sitelinks") or 0)) / 2.5)         # Wikipedia languages: 300+ = world famous
        bonus += 0.60 * min(1.0, max(0.0, ((item.get("importance") or 0) - 0.2) / 0.4))
    d0 = max(120.0, 0.25 * radius_m)
    return round(w / (1.0 + (dist_m / d0) ** 2) + bonus, 3)


def merge_duplicates(items: list) -> list:
    """The same sight often appears twice (the node and the building outline, or two tags). Same wikidata, same OSM id,
    or the same normalised name within 250 m -> one entry that keeps the better category and all details."""
    out: list = []
    for it in items:
        twin = None
        for o in out:
            if (it.get("wikidata") and it.get("wikidata") == o.get("wikidata")) or (it.get("osm") and it.get("osm") == o.get("osm")) \
                    or (_norm(it["name"]) == _norm(o["name"]) and hav_m(it["lat"], it["lon"], o["lat"], o["lon"]) <= 250):
                twin = o
                break
        if twin is None:
            out.append(dict(it))
            continue
        if CATEGORIES.get(it.get("category") or "", ("", 0))[1] > CATEGORIES.get(twin.get("category") or "", ("", 0))[1]:
            for k in ("category", "kind"):
                twin[k] = it[k]
        for k, v in it.items():
            if k not in twin or not twin[k]:
                twin[k] = v
    return out


def search(s, cache, lat: float, lon: float, radius_m: int, limit: int, lang: str = "de", categories: Optional[list] = None,
           sort: str = "score", own_places: Optional[list] = None) -> Optional[list]:
    """Ranked sights around (lat, lon), best first (sort='score') or nearest first (sort='distance').
    None = no source (Photon unset/unreachable and nothing cached) and no own places."""
    radius_m = max(50, min(MAX_RADIUS_M, int(radius_m)))
    raw = collect(s, cache, lat, lon, radius_m, lang)
    own = list(own_places or [])
    if raw is None and not own:
        return None
    cats = set(categories or [])
    items = [dict(p) for p in (raw or []) if not cats or p.get("category") in cats]
    items += [dict(p, category="own") for p in own if not cats or "own" in cats]
    items = merge_duplicates(items)
    out = []
    for p in items:
        d = hav_m(lat, lon, p["lat"], p["lon"])
        if d > radius_m:
            continue
        b = bearing(lat, lon, p["lat"], p["lon"])
        row = {**p, "distance_m": int(round(d)), "bearing": b, "direction": direction_de(b), "score": _score(p, d, radius_m)}
        if row.get("wikipedia"):
            row["wikipedia_url"] = wikipedia_url(row["wikipedia"], lang)
        if row.get("wikidata"):
            row["wikidata_url"] = wikidata_url(row["wikidata"])
        out.append(row)
    if sort == "distance":
        out.sort(key=lambda p: (p["distance_m"], -p["score"]))
    else:
        out.sort(key=lambda p: (-p["score"], p["distance_m"]))
    return out[:limit]


PLACE_REVISION = 3      # bump when the way a place / sea / sight is resolved changes: photos resolved earlier are then offered again


def place_version(idx) -> str:
    """Data version of what the place of a photo is made from: index build + resolver revision. Written into the sidecar
    (XMP-mon:CtxPlaceVersion); a different value later means "resolved with older data"."""
    built = ""
    try:
        built = (idx.meta().get("built") or "") if idx is not None and idx.available() else ""
    except Exception:                         # noqa: BLE001
        built = ""
    return f"{built or 'no-index'}|r{PLACE_REVISION}"


def top_sight(items: list, min_importance: float = 0.30) -> Optional[dict]:
    """The one sight a photo at this spot is most likely OF: the best-scored of `items` (sights within a few metres) that is
    notable -- one of your own places, or an Wikimedia-importance >= min_importance -- else None."""
    best = None
    for p in items or []:
        if not (p.get("own") or (p.get("importance") or 0) >= min_importance):
            continue
        if best is None or p["score"] > best["score"]:
            best = p
    if best is None:
        return None
    return {k: best[k] for k in ("name", "kind", "distance_m", "wikidata", "wikipedia_url") if best.get(k) not in (None, "")}


def phrase_de(sights: list) -> str:
    """'in der Nähe: Schloss Musterdorf (Schloss, 120 m), Pfarrkirche (Kirche, 300 m)'"""
    if not sights:
        return ""
    return "in der Nähe: " + ", ".join(f"{p['name']} ({p['kind']}, {p['distance_m']} m)" for p in sights)


def resolve(gn, text: str, lang: str, address: bool = False):
    """Free text (place name or address) -> (lat, lon, label) or None, with the services you run:
    address: Nominatim, Photon, Pelias -- place name: own GeoNames database first, then the same."""
    text = (text or "").strip()
    if not text:
        return None
    if not address and gn.gaz.available():
        recs, _n = gn.gaz.search(text, limit=1, order_pop=True, lang=(lang or "")[:2] or None)
        if recs:
            r = recs[0]
            return float(r["lat"]), float(r["lng"]), r.get("name") or text
    raw, _up = gn.nom_search(q=text, limit=1, lang=lang)
    if raw:
        i = raw[0]
        return float(i["lat"]), float(i["lon"]), i.get("display_name") or text
    feats, _up = gn.photon_search(text, 1, lang)
    if feats:
        co = (feats[0].get("geometry") or {}).get("coordinates") or []
        if len(co) >= 2:
            return float(co[1]), float(co[0]), (feats[0].get("properties") or {}).get("name") or text
    feats, _up = gn.pelias_search(text, 1, lang)
    if feats:
        co = (feats[0].get("geometry") or {}).get("coordinates") or []
        if len(co) >= 2:
            return float(co[1]), float(co[0]), (feats[0].get("properties") or {}).get("label") or text
    return None


def wikipedia_entries(sights: list, lang: str, limit: int) -> list:
    """GeoNames findNearbyWikipedia shape: only sights that have a Wikipedia article (or at least a Wikidata item; its
    Special:GoToLinkedPage URL redirects to the article in `lang`), nearest first."""
    out = []
    for p in sorted(sights, key=lambda x: x["distance_m"]):
        if p.get("wikipedia"):
            url = wikipedia_url(p["wikipedia"], lang)
            lg = (p["wikipedia"].split(":", 1)[0] if re.match(r"^[a-z]{2,3}(?:-[a-z]+)?:", p["wikipedia"]) else lang)
        elif p.get("wikidata"):
            url = f"https://www.wikidata.org/wiki/Special:GoToLinkedPage/{lang}wiki/{p['wikidata']}"
            lg = lang
        else:
            continue
        out.append({"summary": p.get("description", ""), "distance": f"{p['distance_m'] / 1000:.4f}", "rank": int(round(p["score"] * 100)),
                    "title": p["name"], "wikipediaUrl": url.replace("https://", ""), "elevation": 0, "lat": round(p["lat"], 6),
                    "lng": round(p["lon"], 6), "lang": lg, "feature": p.get("category", ""), "thumbnailImg": "",
                    "wikidata": p.get("wikidata", "")})
        if len(out) >= limit:
            break
    return out
