"""children.py -- the administrative children of a place (GeoNames childrenJSON), worked out from boundaries.

Neither Nominatim, Photon nor Pelias can list "everything inside this area". What they CAN do reliably is answer
"what is at this point" (reverse) including the outline of the object found. So the children are found by scanning:

  1. the parent's outline (Nominatim search, polygon_geojson) gives its bounding box and exact shape;
  2. a grid of points is laid over it; points outside the shape are dropped (point-in-polygon);
  3. a point not yet covered is reversed at the zoom of the child level (state / county / municipality), the answer
     is accepted only if its address chain really lies inside the parent (country, state, county must match) and
     it is of the wanted kind -- membership is therefore decided by the data, never by a radius;
  4. the outline of every accepted child is remembered, so all later points inside it are skipped -- the number of
     requests is about "children + boundary slop", not the grid size.

Limits (stated, not hidden): a child smaller than the grid spacing that lies completely inside a bigger neighbour's
shape can be missed; the number of requests per call is capped (GN_CHILDREN_MAX_PROBES) -- when the cap stops the
scan the answer is marked "complete": false and is not cached. Levels: country > ADM1 (state) > ADM2 (county/Bezirk)
> PPL (municipality). A country without counties lists its municipalities straight under the state.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# child level -> (Nominatim reverse zoom, accepted addresstypes, base grid spacing in degrees)
CHILD = {
    "ADM1": (5, {"state", "region", "province", "territory", "state_district"}, 0.25),
    "ADM2": (8, {"county", "state_district", "district", "region"}, 0.06),
    "PPL": (10, {"city", "town", "village", "municipality", "hamlet", "borough"}, 0.012),
}
NEXT_LEVEL = {"country": "ADM1", "ADM1": "ADM2", "ADM2": "PPL"}


# ------------------------------------------------------------------ geometry
def _rings(geom: Optional[dict]) -> list:
    """GeoJSON Polygon/MultiPolygon -> list of polygons, each a list of rings [(lon,lat),...] (first = outer)."""
    if not isinstance(geom, dict):
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon" and c:
        return [c]
    if t == "MultiPolygon" and c:
        return list(c)
    return []


def _in_ring(x: float, y: float, ring) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def point_in(polys: list, lon: float, lat: float) -> bool:
    """True when the point lies in any polygon (outer ring in, holes out)."""
    for rings in polys:
        if rings and _in_ring(lon, lat, rings[0]) and not any(_in_ring(lon, lat, h) for h in rings[1:]):
            return True
    return False


def _bbox_of(polys: list, fallback) -> tuple:
    xs = [p[0] for rings in polys for p in (rings[0] if rings else [])]
    ys = [p[1] for rings in polys for p in (rings[0] if rings else [])]
    if xs and ys:
        return min(xs), min(ys), max(xs), max(ys)
    return fallback


def grid(bbox: tuple, polys: list, spacing: float) -> list:
    """(lat, lon) grid over bbox=(west,south,east,north), only points inside the shape; scanned row by row from the middle
    of the cell so a thin strip is not missed."""
    w, s, e, n = bbox
    step = max(spacing, max(e - w, n - s) / 60.0)          # never more than ~60 x 60 cells
    out = []
    lat = s + step / 2
    while lat < n:
        lon = w + step / 2
        while lon < e:
            if not polys or point_in(polys, lon, lat):
                out.append((lat, lon))
            lon += step
        lat += step
    return out


# ------------------------------------------------------------------ context of a parent
def level_of(rec: dict) -> str:
    code = (rec.get("fcode") or "").upper()
    if code in ("PCLI", "PCL", "PCLD", "PCLF", "PCLS", "PCLIX"):
        return "country"
    if code == "ADM1":
        return "ADM1"
    if code == "ADM2":
        return "ADM2"
    return ""


def parent_context(gn, rec: dict) -> Optional[dict]:
    """{'level','cc','state','county','name'} of a parent record, or None when it cannot have children.
    Synthetic admin records carry it in `_ctx`; a record from a search result is resolved with one reverse."""
    ctx = rec.get("_ctx")
    if ctx and ctx.get("level"):
        return ctx
    level = level_of(rec)
    if not level:
        return None
    ctx = {"level": level, "cc": (rec.get("countryCode") or "").upper(), "name": rec.get("name", ""), "state": "", "county": ""}
    if level == "ADM1":
        ctx["state"] = rec.get("name", "")
    elif level == "ADM2":
        ctx["county"] = rec.get("name", "")
        ctx["state"] = rec.get("adminName1", "")
        if not ctx["state"] and rec.get("lat") and rec.get("lng"):
            raw, _ = gn.nom_reverse(float(rec["lat"]), float(rec["lng"]), zoom=5)
            ctx["state"] = ((raw or {}).get("address") or {}).get("state", "")
    return ctx


def parent_outline(gn, ctx: dict) -> tuple:
    """(bbox(west,south,east,north) | None, polygons, reachable) of the parent from a structured Nominatim search."""
    cc = ctx["cc"].lower()
    if ctx["level"] == "country":
        structured = {"country": ctx["cc"]}
    elif ctx["level"] == "ADM1":
        structured = {"state": ctx["state"], "country": ctx["cc"]}
    else:
        structured = {"county": ctx["county"], "state": ctx["state"] or None, "country": ctx["cc"]}
    raw, up = gn.nom_search(structured=structured, country=cc or None, limit=3, extra={"polygon_geojson": 1, "polygon_threshold": 0.005})
    for item in raw:
        polys = _rings(item.get("geojson"))
        if not polys:
            continue
        try:
            s, n, w, e = (float(v) for v in item["boundingbox"])
        except (KeyError, TypeError, ValueError):
            w, s, e, n = _bbox_of(polys, (0, 0, 0, 0))
        return (w, s, e, n), polys, True
    return None, [], up


# ------------------------------------------------------------------ the scan
def _is_member(addr: dict, ctx: dict, child_level: str) -> bool:
    if ctx["cc"] and (addr.get("country_code") or "").upper() != ctx["cc"]:
        return False
    if child_level in ("ADM2", "PPL") and ctx["state"]:
        if (addr.get("state") or addr.get("region") or "") != ctx["state"]:
            return False
    if child_level == "PPL" and ctx["county"]:
        if (addr.get("county") or addr.get("state_district") or "") != ctx["county"]:
            return False
    return True


def scan(gn, ctx: dict, child_level: str, bbox: tuple, polys: list, max_probes: int, concurrency: int):
    """-> (children, complete, reachable). children: [{'name','lat','lng','osm','polys'}]"""
    zoom, kinds, base = CHILD[child_level]
    queue = grid(bbox, polys, base)
    covered: list = []                 # polygon lists of children already found
    found: dict = {}
    probes, reachable = 0, False

    def covered_by_known(pt) -> bool:
        return any(point_in(c, pt[1], pt[0]) for c in covered)

    def probe(pt):
        return gn.nom_reverse(pt[0], pt[1], zoom=zoom, extra={"polygon_geojson": 1, "polygon_threshold": 0.005})

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        while queue and probes < max_probes:
            batch = []
            while queue and len(batch) < max(1, concurrency) and probes + len(batch) < max_probes:
                pt = queue.pop(0)
                if not covered_by_known(pt):
                    batch.append(pt)
            if not batch:
                continue
            probes += len(batch)
            for pt, (raw, up) in zip(batch, ex.map(probe, batch)):
                reachable |= up
                if not raw:
                    continue
                addr = raw.get("address") or {}
                if (raw.get("addresstype") or "") not in kinds or not _is_member(addr, ctx, child_level):
                    continue
                name = raw.get("name") or addr.get(raw.get("addresstype", ""), "")
                if not name or name in found:
                    continue
                cp = _rings(raw.get("geojson"))
                found[name] = {"name": name, "lat": raw.get("lat"), "lng": raw.get("lon"), "osm": raw.get("osm_id"),
                               "kind": raw.get("addresstype", "")}
                if cp:
                    covered.append(cp)
    complete = not queue
    return sorted(found.values(), key=lambda c: c["name"].casefold()), complete, reachable
