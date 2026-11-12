"""geonames_ws2.py -- further GeoNames-compatible services (XML, and JSON with the `JSON` suffix):

  GET /countryCode?lat=…&lng=…[&type=text|xml|json][&radius=km]   country at the point (plain text like GeoNames by default)
  GET /countryCodeJSON?lat=…&lng=…[&radius=km]
  GET /countrySubdivisionJSON?lat=…&lng=…                        first-level subdivision (state), ISO 3166-2 code
  GET /geoCodeAddressJSON?q=Hauptplatz 1, Graz[&country=AT]      address text -> the best match as an address
  GET /streetNameLookupJSON?q=Beispiel[&country=AT][&postalcode=][&adminCode1=][&maxRows=10]   streets by name
  GET /findNearestAddressJSON?lat=…&lng=…                        same answer as /addressJSON
  GET /findNearbyStreetsOSMJSON?lat=…&lng=…[&radius=0.2][&maxRows=10]   named streets around the point (approximate, see below)

Sources: your Nominatim (reverse / search), your Photon (streets) and the country list of the GeoNames volume. Approximations:
* findNearbyStreetsOSM has no Overpass behind it. Photon names the streets near the point; each segment's `line` is the
  street's bounding-box diagonal (or just its point) -- NOT the real geometry; maxspeed / oneway are not available.
* countrySubdivision knows level 1 only.
* The shape of streetNameLookup is modelled on /address: {"address": [ {street, postalcode, placename, adminCode1 ... } ]}.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Query
from fastapi.responses import JSONResponse, PlainTextResponse

from . import geonames as G

RECORD_NOT_FOUND = 15


def _addr(gn, raw: dict) -> dict:
    """GeoNames 'address' object from a Nominatim result (same fields as /address)."""
    rec = gn.from_nominatim(raw)
    a = raw.get("address") or {}
    return {"street": a.get("road", ""), "houseNumber": a.get("house_number", ""), "postalcode": a.get("postcode", ""),
            "placename": rec.get("name", ""), "adminCode1": rec.get("adminCode1", ""), "adminName1": rec.get("adminName1", ""),
            "countryCode": rec.get("countryCode", ""), "lat": rec.get("lat", ""), "lng": rec.get("lng", ""),
            "distance": rec.get("distance", 0)}


def register(router, gn, st, deps, cached, remember, countries, address) -> None:
    # ------------------------------------------------------------------ countryCode
    def country_rec(lat, lng, radius, lang):
        """({countryCode, countryName, languages, distance} | None, reachable)."""
        key = f"{lat:.4f}|{lng:.4f}|{radius}|{lang or ''}"
        hit = cached("ccode", key)
        if hit is not None:
            return hit, True
        raw, up = gn.nom_reverse(lat, lng, zoom=3, lang=lang)
        rec = None
        a = (raw or {}).get("address") or {}
        cc = (a.get("country_code") or "").upper()
        if cc:
            rec = {"countryCode": cc, "countryName": a.get("country", ""), "distance": 0}
        elif radius > 0 and gn.gaz.available():           # open water: the nearest populated place within `radius` km
            near = gn.gaz.nearby(lat, lng, limit=1, radius_km=radius, fclasses=["P"])
            if near:
                rec = {"countryCode": near[0]["countryCode"], "countryName": near[0].get("countryName", ""),
                       "distance": near[0].get("distance", 0)}
        if rec:
            info = countries.by_code(rec["countryCode"]) or {}
            rec["countryName"] = info.get("countryName") or rec["countryName"]      # GeoNames names countries in English
            rec = {"languages": info.get("languages", ""), "distance": rec["distance"],
                   "countryCode": rec["countryCode"], "countryName": rec["countryName"]}
            remember("ccode", key, rec)
        return rec, up

    def country_code(lat, lng, radius, fmt, lang):
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return G.error_response("invalid lat/lng", 12, fmt == "xml") if fmt != "text" else PlainTextResponse("ERR:12:invalid lat/lng\n")
        rec, up = country_rec(lat, lng, radius, lang)
        if rec is None:
            msg, code = ("no country code found", RECORD_NOT_FOUND) if up else ("no geocoding backend reachable", G.OTHER_ERROR)
            return PlainTextResponse(f"ERR:{code}:{msg}\n") if fmt == "text" else G.error_response(msg, code, fmt == "xml")
        if fmt == "text":
            return PlainTextResponse(rec["countryCode"] + "\n")
        if fmt == "xml":
            root = G.Element("geonames")
            G._append(G.SubElement(root, "country"), rec)
            return G._xml(root)
        return JSONResponse(rec)

    def cc_ep_default(default):
        def ep(lat: float, lng: float, type: str = Query(default, pattern="^(text|xml|json)$"),
               radius: float = Query(0.0, ge=0, le=300), lang: Optional[str] = Query(None)):
            return country_code(lat, lng, radius, type, lang)
        return ep
    router.add_api_route("/countryCode", cc_ep_default("text"), methods=["GET"], dependencies=deps)
    router.add_api_route("/countryCodeJSON", cc_ep_default("json"), methods=["GET"], dependencies=deps)

    # ------------------------------------------------------------ countrySubdivision
    def subdivision(lat, lng, lang, as_xml):
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return G.error_response("invalid lat/lng", 12, as_xml)
        key = f"{lat:.4f}|{lng:.4f}|{lang or ''}"
        payload = cached("subdiv", key)
        up = True
        if payload is None:
            raw, up = gn.nom_reverse(lat, lng, zoom=5, lang=lang)
            if not raw or not (raw.get("address") or {}).get("country_code"):
                if not up:
                    return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
                return G.error_response("no country subdivision found", RECORD_NOT_FOUND, as_xml)
            rec = gn.from_nominatim(raw)
            info = countries.by_code(rec["countryCode"]) or {}
            adm = rec.get("adminCode1", "")
            payload = {"codes": ([{"code": adm, "level": "1", "type": "ISO3166-2"}] if adm else []),
                       "adminName1": rec.get("adminName1", ""), "countryName": info.get("countryName") or rec.get("countryName", ""),
                       "countryCode": rec.get("countryCode", ""), "distance": 0, "adminCode1": adm}
            remember("subdiv", key, payload)
        if as_xml:
            root = G.Element("geonames")
            el = G.SubElement(root, "countrySubdivision")
            for k in ("countryCode", "countryName", "adminCode1", "adminName1", "distance"):
                G.SubElement(el, k).text = str(payload.get(k, ""))
            for c in payload["codes"]:
                ce = G.SubElement(el, "code")
                ce.set("type", c["type"])
                ce.set("level", c["level"])
                ce.text = c["code"]
            return G._xml(root)
        return JSONResponse(payload)

    # ------------------------------------------------------------- geoCodeAddress
    def geocode_address(q, country, lang, as_xml):
        if not (q or "").strip():
            return G.error_response("parameter 'q' is required", 12, as_xml)
        key = f"{q.strip().lower()}|{(country or '').lower()}|{lang or ''}"
        payload = cached("geocodeaddr", key)
        if payload is None:
            raw, up = gn.nom_search(q=q.strip(), country=(country or "").lower() or None, limit=1, lang=lang)
            if not raw:
                if not up:
                    return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
                return G.error_response("no address found for the given query", RECORD_NOT_FOUND, as_xml)
            payload = {"address": _addr(gn, raw[0])}
            remember("geocodeaddr", key, payload)
        if as_xml:
            root = G.Element("geonames")
            G._append(root, payload)
            return G._xml(root)
        return JSONResponse(payload)

    # ------------------------------------------------------------ streetNameLookup
    def street_lookup(q, country, postalcode, admin1, max_rows, lang, as_xml):
        if not (q or "").strip():
            return G.error_response("parameter 'q' is required", 12, as_xml)
        n = gn.clamp(max_rows)
        key = f"{q.strip().lower()}|{(country or '').lower()}|{postalcode or ''}|{admin1 or ''}|{n}|{lang or ''}"
        rows = cached("streetlookup", key)
        if rows is None:
            ctry = (country or "").lower() or None
            raw, up = gn.nom_search(structured={"street": q.strip(), "postalcode": postalcode}, country=ctry, limit=max(n * 3, 10), lang=lang)
            if not raw:
                raw, up2 = gn.nom_search(q=f"{q.strip()} {postalcode or ''}".strip(), country=ctry, limit=max(n * 3, 10), lang=lang)
                up = up or up2
            rows, seen = [], set()
            for item in raw:
                if (item.get("category") or item.get("class")) != "highway":
                    continue
                a = item.get("address") or {}
                road = a.get("road") or item.get("name") or ""
                if not road:
                    continue
                rec = gn.from_nominatim(item)
                if admin1 and rec.get("adminCode1") != admin1 and rec.get("adminName1", "").lower() != admin1.lower():
                    continue
                town = next((a[k] for k in ("city", "town", "village", "hamlet", "municipality", "suburb") if a.get(k)), "")
                if (road.lower(), town.lower(), a.get("postcode", "")) in seen:
                    continue
                seen.add((road.lower(), town.lower(), a.get("postcode", "")))
                rows.append({"street": road, "postalcode": a.get("postcode", ""), "placename": town,
                             "adminCode1": rec.get("adminCode1", ""), "adminName1": rec.get("adminName1", ""),
                             "countryCode": rec.get("countryCode", ""), "lat": rec.get("lat", ""), "lng": rec.get("lng", "")})
            rows = rows[:n]
            if not rows and not up:
                return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
            if rows:
                remember("streetlookup", key, rows)
        return G.list_response(as_xml, rows, "address", "address")

    # --------------------------------------------------------- findNearbyStreetsOSM
    def nearby_streets(lat, lng, radius_km, max_rows, as_xml):
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return G.error_response("invalid lat/lng", 12, as_xml)
        n = gn.clamp(max_rows)
        key = f"{lat:.5f}|{lng:.5f}|{radius_km}|{n}"
        rows = cached("streets", key)
        if rows is None:
            feats, up = gn.photon_streets(lat, lng, radius_km, max(n * 4, 20))
            if not feats and not up:
                return G.error_response("no geocoding backend reachable (PHOTON_SERVER)", G.OTHER_ERROR, as_xml)
            best = {}
            for f in feats:
                p = f.get("properties") or {}
                name = p.get("name") or ""
                coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
                if not name or coords[0] is None or p.get("osm_key") != "highway":
                    continue
                d = G.haversine_km(lat, lng, float(coords[1]), float(coords[0]))
                ext = p.get("extent")
                if isinstance(ext, list) and len(ext) == 4:          # Photon: [minLon, maxLat, maxLon, minLat]
                    line = f"{ext[0]} {ext[1]},{ext[2]} {ext[3]}"
                else:
                    line = f"{coords[0]} {coords[1]},{coords[0]} {coords[1]}"
                row = {"line": line, "distance": f"{d:.2f}", "name": name, "highway": p.get("osm_value", ""),
                       "wayId": str(p.get("osm_id", "")), "lat": G.fmt_coord(coords[1]), "lng": G.fmt_coord(coords[0])}
                k = (name.lower(), row["highway"])
                if k not in best or d < float(best[k]["distance"]):
                    best[k] = row
            rows = sorted(best.values(), key=lambda r: float(r["distance"]))[:n]
            if rows:
                remember("streets", key, rows)
        return G.list_response(as_xml, rows, "streetSegment", "streetSegment")

    # ---------------------------------------------------------------- registration
    def make(as_xml: bool):
        def sub_ep(lat: float, lng: float, lang: Optional[str] = Query(None), level: int = Query(1, ge=1, le=5)):
            return subdivision(lat, lng, lang, as_xml)

        def geo_ep(q: Optional[str] = Query(None), country: Optional[str] = Query(None), lang: Optional[str] = Query(None)):
            return geocode_address(q, country, lang, as_xml)

        def street_ep(q: Optional[str] = Query(None), country: Optional[str] = Query(None), postalcode: Optional[str] = Query(None),
                      adminCode1: Optional[str] = Query(None), maxRows: int = Query(10, ge=1, le=1000),
                      lang: Optional[str] = Query(None)):
            return street_lookup(q, country, postalcode, adminCode1, maxRows, lang, as_xml)

        def streets_ep(lat: float, lng: float, radius: float = Query(0.2, gt=0, le=5), maxRows: int = Query(10, ge=1, le=1000)):
            return nearby_streets(lat, lng, radius, maxRows, as_xml)

        def nearest_ep(lat: float, lng: float):
            return address(lat, lng, as_xml)
        return (("countrySubdivision", sub_ep), ("geoCodeAddress", geo_ep), ("streetNameLookup", street_ep),
                ("findNearbyStreetsOSM", streets_ep), ("findNearestAddress", nearest_ep))

    for suffix, as_xml in (("", True), ("JSON", False)):
        for path, ep in make(as_xml):
            router.add_api_route(f"/{path}{suffix}", ep, methods=["GET"], dependencies=deps)
