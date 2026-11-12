"""compat.py -- Nominatim- and Photon-compatible facades (own ports), for clients that only take a host
(Dawarich) or parse exactly those formats (Traccar).

    port GEO_NOMINATIM_PORT (8001 in the container, 8431 on the host)   /reverse  /search  /status
    port GEO_PHOTON_PORT    (8002 in the container, 8432 on the host)   /reverse  /api     /status

Both answer from ONE internal place record, so they agree with each other and with the main API:
    compat cache -> your Nominatim / Photon / Pelias (order of GEO_CHAIN) -> maritime/chain fallback,
    then the hand-made overrides (overrides.json) are laid over the answer, exactly as for /reverse and /address.
The cache holds the raw backend answer (like everywhere else); overrides are applied after reading it.
"""
from __future__ import annotations

import re
import zlib
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import names as namesmod
from .geonames import _ALPHA3_TO_2

LICENCE = "Data © OpenStreetMap contributors, ODbL 1.0. http://osm.org/copyright"
CITY_KEYS = ("city", "town", "village", "municipality", "hamlet")
SUBURB_KEYS = ("suburb", "city_district", "borough", "neighbourhood", "quarter")
_OSM_LONG = {"N": "node", "W": "way", "R": "relation"}
_OSM_SHORT = {"node": "N", "way": "W", "relation": "R"}


# ----------------------------------------------------------------------------- the neutral place record
def blank(lat: float, lon: float) -> dict:
    return {"lat": lat, "lon": lon, "osm_type": "", "osm_id": 0, "osm_key": "", "osm_value": "", "name": "",
            "housenumber": "", "street": "", "postcode": "", "suburb": "", "city": "", "city_key": "city",
            "county": "", "state": "", "country": "", "cc": "", "iso": ""}


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def from_nominatim(item: dict) -> dict:
    a = item.get("address") or {}
    p = blank(_f(item.get("lat")), _f(item.get("lon")))
    ck = next((k for k in CITY_KEYS if a.get(k)), "city")
    p.update(osm_type=(item.get("osm_type") or "")[:1].upper(), osm_id=int(_f(item.get("osm_id"))),
             osm_key=item.get("category") or item.get("class") or "", osm_value=item.get("type") or "",
             name=item.get("name") or "", housenumber=a.get("house_number", ""),
             street=a.get("road") or a.get("pedestrian") or a.get("footway") or a.get("path") or "",
             postcode=a.get("postcode", ""), suburb=next((a[k] for k in SUBURB_KEYS if a.get(k)), ""),
             city=a.get(ck, ""), city_key=ck, county=a.get("county", ""), state=a.get("state") or a.get("region") or "",
             country=a.get("country", ""), cc=(a.get("country_code") or "").lower(), iso=a.get("ISO3166-2-lvl4", ""))
    return p


def from_photon(feature: dict) -> dict:
    pr = feature.get("properties") or {}
    co = (feature.get("geometry") or {}).get("coordinates") or [0, 0]
    p = blank(_f(co[1]), _f(co[0]))
    ck = "city" if pr.get("city") else "village"
    p.update(osm_type=(pr.get("osm_type") or "")[:1].upper(), osm_id=int(_f(pr.get("osm_id"))),
             osm_key=pr.get("osm_key", ""), osm_value=pr.get("osm_value", ""), name=pr.get("name", ""),
             housenumber=pr.get("housenumber", ""), street=pr.get("street", ""), postcode=pr.get("postcode", ""),
             suburb=pr.get("district", "") or pr.get("locality", ""), city=pr.get("city", ""), city_key=ck,
             county=pr.get("county", ""), state=pr.get("state", ""), country=pr.get("country", ""),
             cc=(pr.get("countrycode") or "").lower())
    return p


def from_pelias(feature: dict) -> dict:
    pr = feature.get("properties") or {}
    co = (feature.get("geometry") or {}).get("coordinates") or [0, 0]
    p = blank(_f(co[1]), _f(co[0]))
    a3 = (pr.get("country_a") or "").upper()
    p.update(name=pr.get("name", ""), housenumber=pr.get("housenumber", ""), street=pr.get("street", ""),
             postcode=pr.get("postalcode", ""), suburb=pr.get("neighbourhood", "") or pr.get("borough", ""),
             city=pr.get("locality", "") or pr.get("localadmin", ""), county=pr.get("county", ""),
             state=pr.get("region", ""), country=pr.get("country", ""),
             cc=(pr.get("country_code") or _ALPHA3_TO_2.get(a3, "")).lower())
    if p["name"] in (p["street"], p["city"]) or p["name"] == f"{p['housenumber']} {p['street']}".strip():
        p["name"] = ""
    return p


def from_result(res: dict, lat: float, lon: float) -> dict:
    """The main /reverse answer (GeoResult fields) as a place -- the last resort, also covers 'maritime'."""
    p = blank(lat, lon)
    p.update(street=res.get("street", ""), city=res.get("city", ""), county=res.get("county", ""),
             state=res.get("state", ""), country=res.get("country", ""),
             cc=_ALPHA3_TO_2.get((res.get("country_a") or "").upper(), "").lower(), name=res.get("locality", ""))
    if p["name"] in (p["street"], p["city"]):
        p["name"] = ""
    return p


_STREET_NUM = re.compile(r"^(.*\D)\s+(\d+\s?[A-Za-z]?(?:\s?[/-]\s?\d+[A-Za-z]?)?)$")


def apply_overrides(p: dict, st, qlat: float, qlon: float) -> dict:
    """overrides.json 'reverse' (country, state, city, locality, street, ...) and 'address' (street, houseNumber,
    postalcode, placename, adminName1, countryCode, lat, lng) laid over a place."""
    p = dict(p)
    ov = st.overrides.find("reverse", qlat, qlon)
    if ov:
        patch = ov[1]
        for src, dst in (("country", "country"), ("state", "state"), ("county", "county"), ("city", "city"),
                         ("street", "street"), ("locality", "name")):
            if src in patch:
                p[dst] = str(patch[src])
        if "street" in patch:
            # the override's street is the whole line as /reverse reports it ("Beispielgasse 84"):
            # split the house number off, and never keep the one the geocoder found for the raw coordinates (86)
            m = _STREET_NUM.match(p["street"].strip())
            p["street"], p["housenumber"] = (m.group(1).strip(), m.group(2)) if m else (p["street"].strip(), "")
        if "country_a" in patch:
            p["cc"] = _ALPHA3_TO_2.get(str(patch["country_a"]).upper(), p["cc"]).lower()
    ov = st.overrides.find("address", qlat, qlon)
    if ov:
        patch = ov[1]
        for src, dst in (("street", "street"), ("houseNumber", "housenumber"), ("postalcode", "postcode"),
                         ("placename", "name"), ("adminName1", "state")):
            if src in patch:
                p[dst] = str(patch[src])
        if "countryCode" in patch:
            p["cc"] = str(patch["countryCode"]).lower()
        for src, dst in (("lat", "lat"), ("lng", "lon")):
            if src in patch:
                p[dst] = _f(patch[src], p[dst])
    return p


# ----------------------------------------------------------------------------- finding places
def _lang(request: Request, st) -> str:
    raw = request.query_params.get("accept-language") or request.query_params.get("lang") \
        or request.headers.get("accept-language", "")
    first = raw.split(",")[0].split(";")[0].strip().split("-")[0].lower()
    return first if first.isalpha() and 2 <= len(first) <= 3 else st.s.lang


def find_place(st, lat: float, lon: float, lang: str, zoom: int = 18) -> Optional[dict]:
    """Raw place for a point (no overrides): compat cache -> backends in GEO_CHAIN order -> main /reverse."""
    key = f"compat|{lang}|{zoom}|{lat:.5f}|{lon:.5f}"
    hit = st.cache.get("geonames", key)
    if hit is not None:
        return hit or None
    gn, place = st.gn, None
    for name in st.s.chain:
        if name == "nominatim":
            raw, _up = gn.nom_reverse(lat, lon, zoom=zoom, lang=lang)
            if raw and (raw.get("address") or {}).get("country"):
                place = from_nominatim(raw)
        elif name == "photon":
            feats, _up = gn.photon_reverse(lat, lon, limit=1)
            if feats and (feats[0].get("properties") or {}).get("country"):
                place = from_photon(feats[0])
        elif name == "pelias":
            feats, _up = gn.pelias_reverse(lat, lon, size=1, lang=lang)
            if feats and (feats[0].get("properties") or {}).get("country"):
                place = from_pelias(feats[0])
        elif name == "maritime" and st.s.maritime:                 # maritime sits mid-chain: the main chain walks the order from here
            break
        if place:
            break
    if place is None and st.s.maritime:                        # open sea etc.: the main chain knows 'maritime'
        try:
            res = st.reverse_raw(lat, lon, lang)
        except Exception:                                      # noqa: BLE001  nothing reachable
            res = None
        if res and res.get("found"):
            place = from_result(res, lat, lon)
    if place:
        place["lat"], place["lon"] = place["lat"] or lat, place["lon"] or lon
        st.cache.put("geonames", key, place, st.s.cache_ttl_days * 86400)
    return place


def _override_only(st, lat: float, lon: float) -> Optional[dict]:
    """No backend knows the spot but a hand-made override does: answer from the override alone."""
    if st.overrides.find("reverse", lat, lon) or st.overrides.find("address", lat, lon):
        return apply_overrides(blank(lat, lon), st, lat, lon)
    return None


def reverse_place(st, lat: float, lon: float, lang: str, zoom: int = 18) -> Optional[dict]:
    p = find_place(st, lat, lon, lang, zoom)
    if p:
        p = namesmod.normalize_place(p, st.aliases, st.s.normalize_names)
    p = p or _override_only(st, lat, lon)
    return apply_overrides(p, st, lat, lon) if p else None


def search_places(st, q: str, limit: int, lang: str, country: str = "", structured: Optional[dict] = None) -> list:
    gn, out = st.gn, []
    key = f"compat-search|{lang}|{limit}|{country}|{q}|{sorted((structured or {}).items())}"
    hit = st.cache.get("geonames", key)
    if hit is not None:
        return hit
    for name in st.s.gn_search_order:
        if name == "nominatim":
            items, _ = gn.nom_search(structured=structured, q=None if structured else q, country=country or None,
                                     limit=limit, lang=lang)
            out = [from_nominatim(i) for i in items]
        elif name == "photon":
            feats, _ = gn.photon_search(q, limit=limit, lang=lang)
            out = [from_photon(f) for f in feats]
        elif name == "pelias":
            feats, _ = gn.pelias_search(q, size=limit, lang=lang)
            out = [from_pelias(f) for f in feats]
        if out:
            break
    if out:
        st.cache.put("geonames", key, out, st.s.cache_ttl_days * 86400)
    return out


# ----------------------------------------------------------------------------- rendering
def _unique(parts):
    seen, out = set(), []
    for x in parts:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def display_name(p: dict) -> str:
    return ", ".join(_unique([p["name"] if p["name"] not in (p["street"], p["city"]) else "", p["housenumber"],
                              p["street"], p["suburb"], p["city"], p["county"], p["state"], p["postcode"],
                              p["country"]]))


def nominatim_address(p: dict) -> dict:
    a = {}
    if p["name"] and p["osm_key"] and p["name"] not in (p["street"], p["city"]):
        a[p["osm_key"] if p["osm_key"] not in ("place", "highway", "boundary") else "amenity"] = p["name"]
    for k, v in (("house_number", p["housenumber"]), ("road", p["street"]), ("suburb", p["suburb"]),
                 (p["city_key"], p["city"]), ("county", p["county"]), ("state", p["state"]),
                 ("ISO3166-2-lvl4", p["iso"]), ("postcode", p["postcode"]), ("country", p["country"]),
                 ("country_code", p["cc"])):
        if v:
            a[k] = v
    return a


def _place_id(p: dict) -> int:
    return zlib.crc32(f"{p['osm_type']}{p['osm_id']}|{p['lat']:.5f}|{p['lon']:.5f}".encode())


def nominatim_item(p: dict, jsonv2: bool, details: bool = True) -> dict:
    long_type = _OSM_LONG.get(p["osm_type"], "node")
    d = {"place_id": _place_id(p), "licence": LICENCE, "osm_type": long_type, "osm_id": p["osm_id"],
         "lat": f"{p['lat']:.7f}", "lon": f"{p['lon']:.7f}"}
    if jsonv2:
        d.update(category=p["osm_key"] or "place", type=p["osm_value"] or "yes", place_rank=30, importance=0,
                 addresstype=p["osm_value"] or ("building" if p["housenumber"] else "road" if p["street"] else "place"),
                 name=p["name"])
    else:
        d.update({"class": p["osm_key"] or "place", "type": p["osm_value"] or "yes"})
    d["display_name"] = display_name(p)
    if details:
        d["address"] = nominatim_address(p)
    e = 0.0005
    d["boundingbox"] = [f"{p['lat'] - e:.7f}", f"{p['lat'] + e:.7f}", f"{p['lon'] - e:.7f}", f"{p['lon'] + e:.7f}"]
    return d


def nominatim_geojson(places: list, geocodejson: bool = False) -> dict:
    feats = []
    for p in places:
        item = nominatim_item(p, True)
        props = ({"geocoding": {"place_id": item["place_id"], "osm_type": item["osm_type"], "osm_id": item["osm_id"],
                                "type": item["addresstype"], "label": item["display_name"], "name": item["name"],
                                **{k: v for k, v in item["address"].items()}}}
                 if geocodejson else {k: v for k, v in item.items() if k not in ("lat", "lon", "boundingbox")})
        feats.append({"type": "Feature", "properties": props,
                      "bbox": [float(item["boundingbox"][2]), float(item["boundingbox"][0]),
                               float(item["boundingbox"][3]), float(item["boundingbox"][1])],
                      "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}})
    out = {"type": "FeatureCollection", "features": feats}
    if geocodejson:
        out = {"type": "FeatureCollection", "geocoding": {"version": "0.1.0", "licence": "ODbL"}, **out}
    else:
        out["licence"] = LICENCE
    return out


def nominatim_xml(places: list, reverse: bool) -> str:
    def addr(p):
        return "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in nominatim_address(p).items() if k != "ISO3166-2-lvl4")
    if reverse:
        p = places[0]
        return ('<?xml version="1.0" encoding="UTF-8" ?>\n<reversegeocode timestamp="" attribution="OpenStreetMap" '
                f'querystring=""><result place_id="{_place_id(p)}" osm_type="{_OSM_LONG.get(p["osm_type"], "node")}" '
                f'osm_id="{p["osm_id"]}" lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">{escape(display_name(p))}</result>'
                f'<addressparts>{addr(p)}</addressparts></reversegeocode>')
    rows = "".join(f'<place place_id="{_place_id(p)}" osm_type="{_OSM_LONG.get(p["osm_type"], "node")}" '
                   f'osm_id="{p["osm_id"]}" lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}" '
                   f'display_name="{escape(display_name(p), {chr(34): "&quot;"})}"/>' for p in places)
    return f'<?xml version="1.0" encoding="UTF-8" ?>\n<searchresults>{rows}</searchresults>'


def photon_type(p: dict) -> str:
    if p["housenumber"]:
        return "house"
    if p["osm_key"] == "highway" or (p["street"] and not p["name"]):
        return "street"
    if p["osm_key"] == "place":
        return {"city": "city", "town": "city", "village": "city", "suburb": "district", "hamlet": "locality"}.get(
            p["osm_value"], "locality")
    return "house" if p["name"] else "city"


def photon_feature(p: dict) -> dict:
    props = {"osm_id": p["osm_id"], "osm_type": p["osm_type"] or "N", "osm_key": p["osm_key"] or "place",
             "osm_value": p["osm_value"] or "locality", "type": photon_type(p)}
    for k, v in (("name", p["name"]), ("housenumber", p["housenumber"]), ("street", p["street"]),
                 ("postcode", p["postcode"]), ("district", p["suburb"]), ("city", p["city"]), ("county", p["county"]),
                 ("state", p["state"]), ("country", p["country"]), ("countrycode", p["cc"].upper())):
        if v:
            props[k] = v
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]}, "properties": props}


# ----------------------------------------------------------------------------- the two apps
def _guard(st, request: Request):
    """None = allowed. Open by default (location lookups only -- no tracks, no secrets); GEO_COMPAT_AUTH=true asks
    for GEO_SERVICE_TOKEN as ?token= / ?key= / ?api_key= / X-Api-Key / Bearer."""
    if not (st.s.compat_auth and st.s.token):
        return None
    q = request.query_params
    given = q.get("token") or q.get("key") or q.get("api_key") or request.headers.get("x-api-key", "")
    if not given and request.headers.get("authorization", "").lower().startswith("bearer "):
        given = request.headers["authorization"][7:].strip()
    return None if given == st.s.token else JSONResponse({"error": {"code": 401, "message": "missing or wrong token"}}, 401)


def _coords(request: Request):
    try:
        lat, lon = float(request.query_params["lat"]), float(request.query_params["lon"])
    except (KeyError, ValueError):
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def create_nominatim_app(st) -> FastAPI:
    app = FastAPI(title="geo-service (Nominatim API)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/status")
    @app.get("/status.php")
    def status(request: Request, format: str = "text"):
        return JSONResponse({"status": 0, "message": "OK"}) if format == "json" else Response("OK", media_type="text/plain")

    @app.get("/reverse")
    @app.get("/reverse.php")
    def reverse(request: Request):
        bad = _guard(st, request)
        if bad:
            return bad
        c = _coords(request)
        fmt = request.query_params.get("format", "xml").lower()
        if c is None:
            return JSONResponse({"error": {"code": 400, "message": "Need coordinates or OSM object to look up."}}, 400)
        try:
            zoom = max(0, min(18, int(request.query_params.get("zoom", 18))))
        except ValueError:
            zoom = 18
        details = request.query_params.get("addressdetails", "1") != "0"
        p = reverse_place(st, c[0], c[1], _lang(request, st), zoom)
        if p is None:
            if fmt == "xml":
                return Response('<?xml version="1.0" encoding="UTF-8" ?>\n<reversegeocode><error>Unable to geocode</error>'
                                '</reversegeocode>', media_type="text/xml")
            return JSONResponse({"error": "Unable to geocode"})
        if fmt in ("json", "jsonv2"):
            return JSONResponse(nominatim_item(p, fmt == "jsonv2", details))
        if fmt in ("geojson", "geocodejson"):
            return JSONResponse(nominatim_geojson([p], fmt == "geocodejson"))
        return Response(nominatim_xml([p], True), media_type="text/xml")

    @app.get("/search")
    @app.get("/search.php")
    def search(request: Request):
        bad = _guard(st, request)
        if bad:
            return bad
        q = request.query_params
        fmt = q.get("format", "xml").lower()
        structured = {k: q[k] for k in ("street", "city", "county", "state", "country", "postalcode") if q.get(k)}
        text = q.get("q") or " ".join(structured.get(k, "") for k in ("street", "postalcode", "city", "county", "state", "country")).strip()
        if not text:
            return JSONResponse({"error": {"code": 400, "message": "Need q or structured parameters."}}, 400)
        try:
            limit = max(1, min(40, int(q.get("limit", 10))))
        except ValueError:
            limit = 10
        places = search_places(st, text, limit, _lang(request, st), q.get("countrycodes", ""), structured or None)
        if fmt in ("json", "jsonv2"):
            return JSONResponse([nominatim_item(p, fmt == "jsonv2", q.get("addressdetails", "0") == "1") for p in places])
        if fmt in ("geojson", "geocodejson"):
            return JSONResponse(nominatim_geojson(places, fmt == "geocodejson"))
        return Response(nominatim_xml(places, False), media_type="text/xml")

    return app


def create_photon_app(st) -> FastAPI:
    app = FastAPI(title="geo-service (Photon API)", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/status")
    def status():
        return {"status": "Ok", "import_date": None}

    @app.get("/reverse")
    def reverse(request: Request):
        bad = _guard(st, request)
        if bad:
            return bad
        c = _coords(request)
        if c is None:
            return JSONResponse({"message": "missing search term 'lat' and 'lon': /?lat=51.5&lon=8.0"}, 400)
        p = reverse_place(st, c[0], c[1], _lang(request, st))
        return {"features": [photon_feature(p)] if p else [], "type": "FeatureCollection"}

    @app.get("/api")
    @app.get("/api/")
    def api(request: Request):
        bad = _guard(st, request)
        if bad:
            return bad
        q = request.query_params
        if not q.get("q"):
            return JSONResponse({"message": "missing search term 'q': /?q=berlin"}, 400)
        try:
            limit = max(1, min(40, int(q.get("limit", 15))))
        except ValueError:
            limit = 15
        places = search_places(st, q["q"], limit, _lang(request, st))
        return {"features": [photon_feature(p) for p in places], "type": "FeatureCollection"}

    return app
