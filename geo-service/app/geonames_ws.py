"""geonames_ws.py -- the rest of the GeoNames-compatible services:  search  get  hierarchy  countryInfo  address
(XML, and JSON with the `JSON` suffix).  Registered by geonames.register() next to findNearbyPlaceName /
findNearbyPostalCodes; see geonames.py for how places are normalised and what is approximated.

  GET /searchJSON?q=Musterdorf[&name=][&country=AT][&maxRows=10][&startRow=0][&style=][&lang=]
  GET /getJSON?geonameId=…                       a place this service has handed out before (synthetic ids)
  GET /hierarchyJSON?geonameId=…                 country > state > county > place, built from one Nominatim reverse
  GET /countryInfoJSON[?country=AT]              GeoNames' countryInfo.txt (from the GeoNames volume) + bounding box from Nominatim
  GET /addressJSON?lat=…&lng=…                   street / house number / postcode at the point (Nominatim, zoom 18)
  GET /postalCodeSearchJSON?postalcode=2552[&country=AT][&placename=][&maxRows=10]      → postalCodes[]   (XML: <code>)
  GET /postalCodeLookupJSON?postalcode=2552[&country=AT][&maxRows=20]                   → postalcodes[]   (JSON only, like GeoNames)
"""
from __future__ import annotations

import csv
import os
import threading
from typing import List, Optional

from fastapi import Depends, Query
from fastapi.responses import JSONResponse, Response

from . import children as childmod
from . import geonames as G
from . import overrides as ovmod
from . import sights as sightsmod
from . import timezone as tzmod

RECORD_NOT_FOUND = 15

CONTINENTS = {"AF": "Africa", "AS": "Asia", "EU": "Europe", "NA": "North America",
              "OC": "Oceania", "SA": "South America", "AN": "Antarctica"}
COLUMNS = ["ISO", "ISO3", "ISO-Numeric", "fips", "Country", "Capital", "Area(in sq km)", "Population", "Continent",
           "tld", "CurrencyCode", "CurrencyName", "Phone", "Postal Code Format", "Postal Code Regex", "Languages",
           "geonameid", "neighbours", "EquivalentFipsCode"]


class Countries:
    """GeoNames' countryInfo.txt, read from YOUR GeoNames volume (GEONAMES_HOST_DIR -> /geonames; nothing is shipped in
    the image). Without the file the country names/info are simply empty and /countryInfo says so."""

    def __init__(self, path: str = ""):
        self.rows: dict = {}
        self._lock = threading.Lock()
        self.path = path
        self.error = ""
        try:
            with open(path, encoding="utf-8") as fh:
                for row in csv.reader(fh, delimiter="\t"):
                    if row and not row[0].startswith("#") and len(row) >= len(COLUMNS) - 1:
                        row = row + [""] * (len(COLUMNS) - len(row))
                        rec = dict(zip(COLUMNS, row))
                        self.rows[rec["ISO"].upper()] = rec
            if not self.rows:
                self.error = f"{path}: no country rows found (GeoNames countryInfo.txt expected)"
        except OSError as e:
            self.error = f"countryInfo.txt not readable ({path}: {type(e).__name__}) -- put GeoNames' countryInfo.txt into the GeoNames volume (GEONAMES_HOST_DIR)"

    @staticmethod
    def record(row: dict) -> dict:
        def num(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        cont = row.get("Continent", "")
        return {"continent": cont, "capital": row.get("Capital", ""), "languages": row.get("Languages", ""),
                "geonameId": num(row.get("geonameid")), "isoAlpha3": row.get("ISO3", ""), "fipsCode": row.get("fips", ""),
                "population": row.get("Population", ""), "isoNumeric": row.get("ISO-Numeric", ""),
                "areaInSqKm": row.get("Area(in sq km)", ""), "countryCode": row.get("ISO", ""),
                "countryName": row.get("Country", ""), "continentName": CONTINENTS.get(cont, ""),
                "currencyCode": row.get("CurrencyCode", "")}

    def by_code(self, iso2: str) -> Optional[dict]:
        row = self.rows.get((iso2 or "").upper())
        return self.record(row) if row else None

    def all(self) -> list:
        return [self.record(r) for r in self.rows.values()]


def register(router, gn: "G.GeoNames", st, gn_auth, cached, remember) -> None:
    s = st.s
    countries = Countries(s.gn_country_file)
    st.countries = countries
    deps = [Depends(gn_auth)]

    def style_list(recs, style):
        return [G.select_fields(r, style) for r in recs]

    # ------------------------------------------------------------------ search
    def forward(q: str, countries: list, n: int, lang: Optional[str], f: dict):
        """(records, reachable) -- first backend of GN_SEARCH_ORDER with an answer wins (no merging).
        Backend-side narrowing where the backend can do it (prefix, country, bbox, layers); the exact filtering
        happens afterwards in apply_filters() on the normalised records."""
        reachable = False
        bbox = f.get("bbox")
        for name in s.gn_search_order:
            if name == "pelias":
                layers = ",".join(dict.fromkeys(l for c in f.get("fclasses", []) for l in gn.CLASS_LAYERS.get(c, "").split(",") if l)) or None
                feats, up = gn.pelias_search(q, n, lang, autocomplete=bool(f.get("startswith")), layers=layers,
                                             countries=countries or None, bbox=bbox)
                recs = [gn.from_pelias(x) for x in feats]
            elif name == "nominatim":
                raw, up = gn.nom_search(q=q, country=",".join(countries).lower() or None, limit=n, lang=lang, bbox=bbox)
                recs = [gn.from_nominatim(i) for i in raw]
            elif name == "photon":
                feats, up = gn.photon_search(q, n, lang, bbox=bbox)
                recs = [gn.from_photon(x) for x in feats]
            else:
                continue
            reachable |= up
            recs = apply_filters(recs, countries, f)
            if recs:
                return recs, True
        return [], reachable

    CITY_MIN = {"cities15000": 15000, "cities5000": 5000, "cities1000": 1000}

    def apply_filters(recs, countries, f):
        up = {c.upper() for c in countries}
        out = []
        for r in recs:
            if up and r.get("countryCode", "").upper() not in up:
                continue
            nm = (r.get("name") or "").lower()
            if f.get("startswith") and not (nm.startswith(f["startswith"]) or (r.get("toponymName") or "").lower().startswith(f["startswith"])):
                continue
            if f.get("equals") and nm != f["equals"] and (r.get("toponymName") or "").lower() != f["equals"]:
                continue
            if f.get("name_contains") and f["name_contains"] not in nm:
                continue
            if f.get("fclasses") and r.get("fcl") not in f["fclasses"]:
                continue
            if f.get("fcodes") and r.get("fcode") not in f["fcodes"]:
                continue
            if f.get("bbox"):
                try:
                    w, so, e, no = f["bbox"]
                    if not (so <= float(r["lat"]) <= no and w <= float(r["lng"]) <= e):
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
            if f.get("cities") and (r.get("population") or 0) < CITY_MIN[f["cities"]]:
                continue
            out.append(r)
        if f.get("orderby") == "population":
            out.sort(key=lambda r: -(r.get("population") or 0))
        return out

    def search(q, name, countries, max_rows, start_row, style, lang, as_xml, f):
        n = gn.clamp(max_rows)
        term = (q or name or f.get("startswith_raw") or f.get("equals_raw") or "").strip()
        if not term:
            return G.error_response("parameter 'q', 'name', 'name_equals' or 'name_startsWith' is required", G.INVALID_PARAMETER, as_xml)
        key = "|".join([term.lower(), ",".join(sorted(c.upper() for c in countries)), str(n), str(start_row),
                        (style or "MEDIUM").upper(), lang or "", repr(sorted((k, v) for k, v in f.items() if k.endswith("_raw") is False))])
        hit = cached("search", key)
        if hit is None and "gazetteer" in s.gn_search_order and gn.gaz.available():
            recs, total = gn.gaz.search(term, limit=n, offset=start_row, countries=countries, fclasses=f.get("fclasses"), fcodes=f.get("fcodes"),
                                        startswith=f.get("startswith_raw", ""), equals=f.get("equals_raw", ""), bbox=f.get("bbox"),
                                        min_pop=CITY_MIN.get(f.get("cities"), 0), order_pop=f.get("orderby") == "population",
                                        lang=(lang or "")[:2] or None)
            if recs:
                hit = {"total": total, "rows": style_list(recs, style)}
                remember("search", key, hit)
        if hit is None:
            filtered = any(f.get(k) for k in ("startswith", "equals", "name_contains", "fclasses", "fcodes", "bbox", "cities", "orderby"))
            want = start_row + n
            recs, up = forward(term, countries, min(100, max(want * 3, 20)) if filtered else want, lang, f)
            if not recs and not up:
                return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
            hit = {"total": len(recs), "rows": style_list(recs[start_row:start_row + n], style)}
            if recs:
                remember("search", key, hit)
        return _list_with_total(as_xml, hit["rows"], hit["total"])

    def _list_with_total(as_xml, rows, total, extra=None):
        if as_xml:
            root = G.Element("geonames")
            G.SubElement(root, "totalResultsCount").text = str(total)
            for k, v in (extra or {}).items():
                G.SubElement(root, k).text = str(v).lower()
            for r in rows:
                G._append(G.SubElement(root, "geoname"), r)
            return G._xml(root)
        return JSONResponse({"totalResultsCount": total, **(extra or {}), "geonames": rows})

    # --------------------------------------------------------------------- get
    def real_id(geoname_id) -> bool:
        """Ids below the synthetic range are real geonames.org ids -- answered by the own GeoNames database when it has them."""
        return geoname_id < G.IdRegistry.OFFSET and gn.gaz.available()

    def get(geoname_id, style, as_xml):
        rec = gn.gaz.get(geoname_id) if real_id(geoname_id) else None
        rec = rec or gn.ids.get_record(geoname_id)
        if rec is None:
            return G.error_response(f"no record found for geonameId {geoname_id}", RECORD_NOT_FOUND, as_xml)
        out = G.select_fields(rec, style)
        if as_xml:
            root = G.Element("geoname")
            G._append(root, out)
            return G._xml(root)
        return JSONResponse(out)

    # --------------------------------------------------------------- hierarchy
    LEVEL = {"country": ("A", "PCLI"), "ADM1": ("A", "ADM1"), "ADM2": ("A", "ADM2"), "PPL": ("P", "PPL")}

    def admin_record(level, name, cc, key, lat=None, lng=None, ctx=None, fcode=None):
        """Synthetic record of an administrative unit, stable per key. `_ctx` (not shown in any style) is what
        childrenJSON needs to find its children later."""
        gid = gn.ids.get_or_create("admin-synthetic", key)
        fcl, code = LEVEL[level]
        old = gn.ids.get_record(gid) or {}
        rec = {"geonameId": gid, "name": name, "toponymName": name, "countryCode": cc, "fcl": fcl, "fcode": fcode or code,
               "fclName": G.FCL_NAMES.get(fcl, "")}
        for k, v in (("lat", G.fmt_coord(lat) if lat not in (None, "") else old.get("lat")),
                     ("lng", G.fmt_coord(lng) if lng not in (None, "") else old.get("lng")), ("_ctx", ctx or old.get("_ctx"))):
            if v not in (None, ""):
                rec[k] = v
        gn.ids.save_record(gid, rec)
        return rec

    def admin_chain(lat, lng, cc_hint="", leaf_name=""):
        """country > ADM1 > ADM2 > PPL above a point, one Nominatim reverse. Synthetic admin ids (stable)."""
        raw, _ = gn.nom_reverse(lat, lng)
        a = (raw or {}).get("address", {}) or {}
        chain = []
        cc = (a.get("country_code") or cc_hint or "").upper()
        ci = countries.by_code(cc) if cc else None
        cname = ci["countryName"] if ci else a.get("country", cc)
        if cname:
            chain.append(admin_record("country", cname, cc, f"country|{cc}", ctx={"level": "country", "cc": cc, "name": cname, "state": "", "county": ""}))
        state, county = a.get("state"), a.get("county")
        if state:
            chain.append(admin_record("ADM1", state, cc, f"adm1|{cc}|{state}", ctx={"level": "ADM1", "cc": cc, "name": state, "state": state, "county": ""}))
        if county:
            chain.append(admin_record("ADM2", county, cc, f"adm2|{cc}|{state}|{county}", ctx={"level": "ADM2", "cc": cc, "name": county, "state": state or "", "county": county}))
        city = a.get("city") or a.get("town") or a.get("village")
        if city and city != leaf_name:
            chain.append(admin_record("PPL", city, cc, f"ppl|{cc}|{city}"))
        return chain

    def hierarchy(geoname_id, style, as_xml):
        if real_id(geoname_id):
            real = gn.gaz.get(geoname_id)
            if real:
                return G.list_response(as_xml, style_list(gn.gaz.parents(geoname_id) + [real], style), "geoname", "geonames")
        leaf = gn.ids.get_record(geoname_id)
        if leaf is None:
            return G.error_response(f"no record found for geonameId {geoname_id}", RECORD_NOT_FOUND, as_xml)
        chain = []
        if leaf.get("lat") and leaf.get("lng"):
            chain = admin_chain(float(leaf["lat"]), float(leaf["lng"]), leaf.get("countryCode", ""), leaf.get("name"))
        chain.append(leaf)
        return G.list_response(as_xml, style_list(chain, style), "geoname", "geonames")

    # ----------------------------------------------------------------- children
    def children(geoname_id, max_rows, style, as_xml):
        if real_id(geoname_id) and gn.gaz.get(geoname_id):
            kids, total = gn.gaz.children(geoname_id, max(1, min(int(max_rows), 1000)))
            return _list_with_total(as_xml, style_list(kids, style), total)
        rec = gn.ids.get_record(geoname_id)
        if rec is None:
            return G.error_response(f"no record found for geonameId {geoname_id}", RECORD_NOT_FOUND, as_xml)
        ctx = childmod.parent_context(gn, rec)
        if not ctx:                                    # a populated place / spot has no administrative children
            return _list_with_total(as_xml, [], 0)
        hit = cached("children", str(geoname_id))
        if hit is None:
            cc = ctx["cc"]
            level = childmod.NEXT_LEVEL[ctx["level"]]
            bbox, polys, up = childmod.parent_outline(gn, ctx)
            if not bbox:
                if not up:
                    return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
                return G.error_response(f"no boundary found for '{ctx['name']}'", RECORD_NOT_FOUND, as_xml)
            kids, complete, up = childmod.scan(gn, ctx, level, bbox, polys, s.gn_children_max_probes, s.gn_probe_concurrency)
            if not kids and level == "ADM2":           # no counties here: the municipalities hang directly under the state
                level = "PPL"
                kids, complete, up2 = childmod.scan(gn, {**ctx, "county": ""}, level, bbox, polys, s.gn_children_max_probes, s.gn_probe_concurrency)
                up |= up2
            if not kids and not up:
                return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
            rows = []
            for k in kids:
                name = k["name"]
                if level == "ADM1":
                    rows.append(admin_record("ADM1", name, cc, f"adm1|{cc}|{name}", k["lat"], k["lng"],
                                             {"level": "ADM1", "cc": cc, "name": name, "state": name, "county": ""}))
                elif level == "ADM2":
                    rows.append(admin_record("ADM2", name, cc, f"adm2|{cc}|{ctx['state']}|{name}", k["lat"], k["lng"],
                                             {"level": "ADM2", "cc": cc, "name": name, "state": ctx["state"], "county": name}))
                else:
                    code = "PPLA3" if k.get("kind") in ("city", "town", "municipality") else "PPL"
                    rows.append(admin_record("PPL", name, cc, f"ppl|{cc}|{name}", k["lat"], k["lng"], None, code))
            hit = {"rows": rows, "complete": complete}
            if complete and rows:
                remember("children", str(geoname_id), hit)
        n = max(1, min(int(max_rows), 1000))
        out = style_list(hit["rows"][:n], style)
        return _list_with_total(as_xml, out, len(hit["rows"]), None if hit["complete"] else {"complete": False})

    # ------------------------------------------------- findNearby / extendedFindNearby
    def nearby(lat, lng, radius, max_rows, fclasses, fcodes, style, lang, as_xml):
        n = gn.clamp(max_rows)
        fc, fk = [c.upper() for c in fclasses or [] if c], [c.upper() for c in fcodes or [] if c]
        key = f"{lat:.6f}|{lng:.6f}|{radius}|{n}|{','.join(fc)}|{','.join(fk)}|{(style or 'MEDIUM').upper()}|{lang or ''}"
        hit = cached("nearby", key)
        if hit is None:
            recs, up = gn.find_nearby(lat, lng, radius, n, fc, fk, lang)
            if not recs and not up:
                return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
            hit = style_list(recs, style)
            if hit:
                remember("nearby", key, hit)
        return G.list_response(as_xml, hit, "geoname", "geonames")

    def extended_nearby(lat, lng, style, lang, as_xml):
        """GeoNames extendedFindNearby: the nearest spot feature (if any) followed by the places that contain the point,
        most specific first: populated place > ADM2 > ADM1 > country."""
        key = f"{lat:.6f}|{lng:.6f}|{(style or 'MEDIUM').upper()}|{lang or ''}"
        hit = cached("extnearby", key)
        if hit is None and gn.gaz.available():
            spots = gn.gaz.nearby(lat, lng, limit=1, radius_km=1.0, fclasses=["S", "T", "H", "L", "V", "R"], lang=(lang or "")[:2] or None)
            place = gn.gaz.nearby(lat, lng, limit=1, fclasses=["P"], lang=(lang or "")[:2] or None)
            above = list(reversed(gn.gaz.parents(place[0]["geonameId"]))) if place else []
            seen, uniq = set(), []
            for r in spots + place + above:
                if r["geonameId"] not in seen:
                    seen.add(r["geonameId"])
                    uniq.append(r)
            if uniq:
                hit = style_list(uniq, style)
                remember("extnearby", key, hit)
        if hit is None:
            spots, up = gn.find_nearby(lat, lng, 1.0, 1, ["S", "T", "H", "L", "V", "R"], None, lang)
            place, up2 = gn.nearest_populated_place(lat, lng, 1, lang)
            chain = admin_chain(lat, lng, "", (place[0].get("name") if place else ""))
            recs = list(spots) + list(place) + list(reversed(chain))
            seen, uniq = set(), []
            for r in recs:
                if r["geonameId"] not in seen:
                    seen.add(r["geonameId"])
                    uniq.append(r)
            if not uniq and not (up or up2):
                return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
            hit = style_list(uniq, style)
            if hit:
                remember("extnearby", key, hit)
        return G.list_response(as_xml, hit, "geoname", "geonames")

    # ------------------------------------------------------------- countryInfo
    def with_bbox(rec: dict) -> dict:
        key = rec["countryCode"]
        box = cached("bbox", key)
        if box is None:
            raw, _ = gn.nom_search(structured={"country": rec["countryName"]}, limit=1)
            try:
                south, north, west, east = (float(v) for v in raw[0]["boundingbox"])
                box = {"south": south, "north": north, "west": west, "east": east}
                remember("bbox", key, box)
            except (IndexError, KeyError, TypeError, ValueError):
                box = {}
        return {**rec, **box}

    def country_info(country, as_xml):
        if countries.error:
            return G.error_response(countries.error, G.OTHER_ERROR, as_xml)
        if country:
            r = countries.by_code(country)
            recs = [with_bbox(r)] if r else []
        else:
            recs = countries.all()                    # no bounding boxes for the full list (250 lookups)
        if as_xml:
            root = G.Element("geonames")
            G.SubElement(root, "totalResultsCount").text = str(len(recs))
            for r in recs:
                G._append(G.SubElement(root, "country"), r)
            return G._xml(root)
        return JSONResponse({"totalResultsCount": len(recs), "geonames": recs})

    # ----------------------------------------------------------------- address
    def address_payload(lat, lng, may_be_blank=False):
        """(payload | None, error message, error code): cache first, then Nominatim. The cache stays raw.
        may_be_blank: return an empty address instead of an error (override stands in / editor GUI)."""
        key = f"{lat:.6f}|{lng:.6f}"
        payload = cached("address", key)
        if payload is not None:
            return payload, "", 0
        raw, up = gn.nom_reverse(lat, lng, zoom=18)
        if not raw:
            if may_be_blank:
                return {"address": {"street": "", "houseNumber": "", "postalcode": "", "placename": "",
                                    "adminCode1": "", "adminName1": "", "countryCode": "",
                                    "lat": f"{lat:.5f}", "lng": f"{lng:.5f}", "distance": 0}}, "", 0
            if not up:
                return None, "no geocoding backend reachable", G.OTHER_ERROR
            return None, "no address found at given location", RECORD_NOT_FOUND
        rec = gn.from_nominatim(raw)
        a = raw.get("address") or {}
        payload = {"address": {"street": a.get("road", ""), "houseNumber": a.get("house_number", ""),
                               "postalcode": a.get("postcode", ""), "placename": rec.get("name", ""),
                               "adminCode1": rec.get("adminCode1", ""), "adminName1": rec.get("adminName1", ""),
                               "countryCode": rec.get("countryCode", ""), "lat": rec.get("lat", ""),
                               "lng": rec.get("lng", ""), "distance": rec.get("distance", 0)}}
        remember("address", key, payload)
        return payload, "", 0

    def address(lat, lng, as_xml):
        ov = st.overrides.find("address", lat, lng)
        payload, msg, code = address_payload(lat, lng, may_be_blank=bool(ov))
        if payload is None:
            return G.error_response(msg, code, as_xml)
        ov = st.overrides.find("address", lat, lng)
        if ov:                                  # cache stays raw; the correction is laid over it
            payload = ovmod.apply_address(payload, *ov)
        if as_xml:
            root = G.Element("geonames")
            G._append(root, payload)
            return G._xml(root)
        return JSONResponse(payload)

    # ------------------------------------------------- postalCodeSearch / Lookup
    TOWN_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "locality")

    def postal_rows(postalcode, placename, country, n, lowercase_key):
        """Nominatim structured postcode search (or a free-text placename search) -> GeoNames postal rows.
        Quality depends on the postcodes in YOUR Nominatim import (OSM addr:postcode / postal_code boundaries)."""
        key = f"{postalcode or ''}|{(placename or '').lower()}|{(country or '').upper()}|{n}"
        rows = cached("postalsearch", key)
        reachable = True
        if rows is None and gn.gaz.has("postal"):
            got = gn.gaz.postal_search(code=postalcode or "", place=placename or "", country=country or "", limit=n)
            rows = [{k: h[k] for k in ("code", "placeName", "countryCode", "lat", "lng", "adminCode1", "adminName1", "adminCode2", "adminName2")} for h in got]
            if rows:
                remember("postalsearch", key, rows)
            else:
                rows = None
        if rows is None:
            if postalcode:
                raw, reachable = gn.nom_search(structured={"postalcode": postalcode}, country=country, limit=max(n * 3, 10))
            else:
                raw, reachable = gn.nom_search(q=placename, country=country, limit=max(n * 3, 10))
            rows, seen = [], set()
            for item in raw:
                a = item.get("address") or {}
                code = a.get("postcode") or (postalcode or "")
                if postalcode and not str(code).lower().startswith(postalcode.lower()):
                    continue
                if not code:
                    continue
                rec = gn.from_nominatim(item)
                town = next((a[k] for k in TOWN_KEYS if a.get(k)), "") or (rec.get("name") if rec.get("name") != code else "")
                if placename and postalcode and placename.lower() not in town.lower():
                    continue
                dedup = (code, town, rec.get("lat", "")[:7], rec.get("lng", "")[:7])
                if dedup in seen:
                    continue
                seen.add(dedup)
                rows.append({"code": code, "placeName": town, "countryCode": rec.get("countryCode", ""),
                             "lat": rec.get("lat", ""), "lng": rec.get("lng", ""),
                             "adminCode1": rec.get("adminCode1", ""), "adminName1": rec.get("adminName1", ""),
                             "adminCode2": rec.get("adminCode2", ""), "adminName2": rec.get("adminName2", "")})
            rows = rows[:n]
            if rows:
                remember("postalsearch", key, rows)
        out = []
        for r in rows:
            r = dict(r)
            code = r.pop("code")
            out.append(({"postalcode": code, **r} if lowercase_key else {"postalCode": code, **r}))
        return out, reachable

    def postal_search(postalcode, placename, country, max_rows, as_xml):
        if not (postalcode or placename):
            return G.error_response("parameter 'postalcode' or 'placename' is required", G.INVALID_PARAMETER, as_xml)
        rows, up = postal_rows(postalcode, placename, country, gn.clamp(max_rows), False)
        if not rows and not up:
            return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, as_xml)
        return G.list_response(as_xml, rows, "code", "postalCodes")

    def postal_lookup(postalcode, country, max_rows):
        if not postalcode:
            return G.error_response("parameter 'postalcode' is required", G.INVALID_PARAMETER, False)
        rows, up = postal_rows(postalcode, None, country, gn.clamp(max_rows), True)
        if not rows and not up:
            return G.error_response("no geocoding backend reachable", G.OTHER_ERROR, False)
        return JSONResponse({"postalcodes": rows})

    # ------------------------------------------------------------------ routes
    def wikipedia_nearby(lat, lng, radius_km, max_rows, lang, as_xml: bool):
        """findNearbyWikipedia: sights with a Wikipedia article / Wikidata item around the point (same data as /sights)."""
        lg = (lang or s.lang or "en")[:2].lower()
        lst = sightsmod.search(s, st.cache, lat, lng, int(radius_km * 1000), 50, lg, None, "distance", None)
        if lst is None:
            return G.error_response("no sights source reachable (PHOTON_SERVER)", G.OTHER_ERROR, as_xml)
        return G.list_response(as_xml, sightsmod.wikipedia_entries(lst, lg, max_rows), "entry", "geonames")

    def make(as_xml: bool):
        def wikipedia_ep(lat: float, lng: float, radius: float = Query(1.0, gt=0, le=20), maxRows: int = Query(5, ge=1, le=50),
                         lang: Optional[str] = Query(None), country: Optional[str] = Query(None)):
            return wikipedia_nearby(lat, lng, radius, maxRows, lang, as_xml)

        def search_ep(q: Optional[str] = Query(None), name: Optional[str] = Query(None), country: Optional[List[str]] = Query(None),
                      maxRows: int = Query(10, ge=1, le=1000), startRow: int = Query(0, ge=0),
                      style: str = Query("MEDIUM"), lang: Optional[str] = Query(None),
                      name_startsWith: Optional[str] = Query(None), name_equals: Optional[str] = Query(None),
                      featureClass: Optional[List[str]] = Query(None), featureCode: Optional[List[str]] = Query(None),
                      east: Optional[float] = Query(None), west: Optional[float] = Query(None),
                      north: Optional[float] = Query(None), south: Optional[float] = Query(None),
                      cities: Optional[str] = Query(None), orderby: Optional[str] = Query(None),
                      isNameRequired: Optional[bool] = Query(False), searchlang: Optional[str] = Query(None)):
            countries = [c.strip() for v in (country or []) for c in v.split(",") if c.strip()]
            f = {"startswith": (name_startsWith or "").strip().lower(), "startswith_raw": (name_startsWith or "").strip(),
                 "equals": (name_equals or "").strip().lower(), "equals_raw": (name_equals or "").strip(),
                 "fclasses": [c.upper() for c in (featureClass or []) if c], "fcodes": [c.upper() for c in (featureCode or []) if c],
                 "cities": cities if cities in CITY_MIN else "", "orderby": "population" if (orderby or "").lower() == "population" else ""}
            if None not in (east, west, north, south):
                f["bbox"] = (west, south, east, north)
            if isNameRequired and (q or "").strip():
                f["name_contains"] = (q or "").strip().lower()
            return search(q, name, countries, maxRows, startRow, style, lang or searchlang, as_xml, f)

        def get_ep(geonameId: int, style: str = Query("MEDIUM")):
            return get(geonameId, style, as_xml)

        def hierarchy_ep(geonameId: int, style: str = Query("MEDIUM")):
            return hierarchy(geonameId, style, as_xml)

        def nearby_ep(lat: float, lng: float, radius: float = Query(1.0, gt=0, le=300), maxRows: int = Query(10, ge=1, le=1000),
                      featureClass: Optional[List[str]] = Query(None), featureCode: Optional[List[str]] = Query(None),
                      style: str = Query("MEDIUM"), lang: Optional[str] = Query(None)):
            return nearby(lat, lng, radius, maxRows, featureClass, featureCode, style, lang, as_xml)

        def children_ep(geonameId: int, maxRows: int = Query(200, ge=1, le=1000), style: str = Query("MEDIUM")):
            return children(geonameId, maxRows, style, as_xml)

        def extended_ep(lat: float, lng: float, style: str = Query("MEDIUM"), lang: Optional[str] = Query(None)):
            return extended_nearby(lat, lng, style, lang, as_xml)

        def country_ep(country: Optional[str] = Query(None)):
            return country_info(country, as_xml)

        def address_ep(lat: float, lng: float):
            return address(lat, lng, as_xml)
        def postal_search_ep(postalcode: Optional[str] = Query(None), placename: Optional[str] = Query(None),
                             country: Optional[str] = Query(None), maxRows: int = Query(10, ge=1, le=1000)):
            return postal_search(postalcode, placename, country, maxRows, as_xml)
        return search_ep, get_ep, hierarchy_ep, country_ep, address_ep, postal_search_ep, nearby_ep, extended_ep, children_ep, wikipedia_ep

    from . import geonames_ws2
    geonames_ws2.register(router, gn, st, deps, cached, remember, countries, address)

    for suffix, as_xml in (("", True), ("JSON", False)):
        eps = make(as_xml)
        for path, ep in zip(("search", "get", "hierarchy", "countryInfo", "address", "postalCodeSearch", "findNearby", "extendedFindNearby", "children", "findNearbyWikipedia"), eps):
            router.add_api_route(f"/{path}{suffix}", ep, methods=["GET"], dependencies=deps)

    def lookup_ep(postalcode: Optional[str] = Query(None), country: Optional[str] = Query(None),
                  maxRows: int = Query(20, ge=1, le=1000)):
        return postal_lookup(postalcode, country, maxRows)
    router.add_api_route("/postalCodeLookupJSON", lookup_ep, methods=["GET"], dependencies=deps)

    # ---------------------------------------------------------- override editor lookup
    def editor_lookup(lat: float = Query(...), lng: float = Query(...), lang: Optional[str] = Query(None)):
        """Everything the editor GUI needs for one point, WITHOUT overrides applied (the 'original' values), plus the
        override that currently covers the point. Uses the same code paths (and caches) as the normal endpoints."""
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return JSONResponse({"error": "lat/lng out of range"}, status_code=422)
        out = {"lat": lat, "lng": lng, "raw": {}, "override": st.overrides.at(lat, lng), "errors": {}}
        try:
            out["raw"]["reverse"] = st.reverse_raw(lat, lng, lang or st.s.lang)
        except Exception as e:          # noqa: BLE001  HTTPException(503) when no geocoder answers
            out["errors"]["reverse"] = str(getattr(e, "detail", e))
            out["raw"]["reverse"] = {"found": False}
        payload, msg, _code = address_payload(lat, lng, may_be_blank=True)
        out["raw"]["address"] = payload["address"]
        el = st.elevation.lookup(lat, lng, use_override=False)
        out["raw"]["elevation"], out["raw"]["elevation_source"] = el["elevation"], el["source"]
        out["raw"]["timezone"] = tzmod.info(lat, lng, countries=countries)
        return out
    router.add_api_route("/overrides/lookup", editor_lookup, methods=["GET"], dependencies=deps)

    # ------------------------------------------------- srtm3 / astergdem / gtopo30
    MAX_POINTS = 20

    def points(lat, lng, lats, lngs):
        """[(lat, lng)] from lat/lng or the comma lists lats/lngs (GeoNames: up to 20), or None if invalid."""
        try:
            if lats is not None or lngs is not None:
                la = [float(x) for x in (lats or "").split(",") if x.strip()]
                lo = [float(x) for x in (lngs or "").split(",") if x.strip()]
                pts = list(zip(la, lo)) if la and len(la) == len(lo) else None
            elif lat is not None and lng is not None:
                pts = [(float(lat), float(lng))]
            else:
                pts = None
        except ValueError:
            return None
        if not pts or len(pts) > MAX_POINTS or any(not (-90 <= a <= 90 and -180 <= b <= 180) for a, b in pts):
            return None
        return pts

    def height(key: str, void: int, as_json: bool):
        """GeoNames height services. One source (local SRTM3 tiles / Open-Meteo), so the three names return the same
        value. Plain flavour = text like the original (one number per line); JSON = {"srtm3": 215, "lng": .., "lat": ..}.
        Several points: lats=a,b&lngs=c,d  ->  one line each / {"geonames": [...]}."""
        def ep(lat: Optional[float] = Query(None), lng: Optional[float] = Query(None),
               lats: Optional[str] = Query(None), lngs: Optional[str] = Query(None)):
            pts = points(lat, lng, lats, lngs)
            if pts is None:
                return G.error_response("lat/lng (or lats/lngs, max %d valid pairs) required" % MAX_POINTS, 12, False)
            rows = []
            for a, b in pts:
                e = st.elevation.lookup(a, b)
                rows.append((void if e["elevation"] is None else int(round(e["elevation"])), a, b))
            if as_json:
                recs = [{key: v, "lng": b, "lat": a} for v, a, b in rows]
                return JSONResponse(recs[0] if lats is None and lngs is None else {"geonames": recs})
            return Response("\n".join(str(v) for v, _, _ in rows), media_type="text/plain")
        return ep

    for key, void in (("srtm3", -32768), ("astergdem", -9999), ("gtopo30", -9999)):
        router.add_api_route(f"/{key}", height(key, void, False), methods=["GET"], dependencies=deps)
        router.add_api_route(f"/{key}JSON", height(key, void, True), methods=["GET"], dependencies=deps)

    # ------------------------------------------------------------------ timezone
    def timezone_ep(as_xml: bool):
        def ep(lat: Optional[float] = Query(None), lng: Optional[float] = Query(None), date: Optional[str] = Query(None)):
            from datetime import datetime as _dt
            if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return G.error_response("parameters 'lat' and 'lng' are required", 12, as_xml)
            on = None
            if date:
                try:
                    on = _dt.strptime(date, "%Y-%m-%d").date()
                except ValueError:
                    return G.error_response("date must be yyyy-MM-dd", 12, as_xml)
            rec = tzmod.info(lat, lng, on, countries=countries)
            if rec is None:
                return G.error_response("no timezone information found for lat/lng", RECORD_NOT_FOUND, as_xml)
            if as_xml:
                root = G.Element("geonames")
                el = G.SubElement(root, "timezone")
                el.set("tzversion", "tzdata")
                G._append(el, rec)
                return G._xml(root)
            return JSONResponse(rec)
        return ep
    router.add_api_route("/timezone", timezone_ep(True), methods=["GET"], dependencies=deps)
    router.add_api_route("/timezoneJSON", timezone_ep(False), methods=["GET"], dependencies=deps)
