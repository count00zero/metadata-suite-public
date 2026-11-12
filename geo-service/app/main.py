"""geo-service -- one place for reverse geocoding, GPS-track lookup and caption context.

    GET  /reverse?lat&lon[&lang]            place name via Photon -> Nominatim -> Pelias (-> maritime), cached
    POST /reverse-batch                     the same for up to 500 points
    GET  /track-at?time[&max_gap&devices]   position at a moment from Traccar/Dawarich (interpolated)
    GET  /track-range?from&to[&step]        raw points, for clients that do their own maths (gps_fill)
    GET  /context?lat&lon&time              place + local time of day + season + (opt-in) weather
    POST /weather/backfill  GET /weather/backfill  fill the weather archive (shadow DB) from tracks + client points
    GET  /weather/stats                     what the archive holds
    GET  /findNearbyPlaceName[JSON]  /findNearbyPostalCodes[JSON]   GeoNames-compatible (replaces the geonames-wrapper)
    GET  /search /get /hierarchy /countryInfo /address /postalCodeSearch [JSON] /postalCodeLookupJSON   …and these
    GET  /ui                                override editor (GUI: look up an address/coordinates, override parts, save)
    GET  /elevation?lat&lon                 metres above sea level (override -> cache -> local SRTM3 -> Open-Meteo)
    GET/POST /overrides  DELETE /overrides/{id}   hand-made corrections laid over cache and geocoder answers
    GET  /srtm3 /astergdem /gtopo30 [JSON]  GeoNames-compatible heights (same value, see README)
    GET  /timezone[JSON]?lat&lng[&date]  zone id, offsets, local time, sunrise/sunset (offline)
    GET  /health  /stats                    liveness (no secrets) / counters
"""
from __future__ import annotations

import re
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import clockcheck, countryinfo, daycal, names as namesmod, pois as poimod, sights as sightsmod, trips as tripmod
from . import water as watermod
from . import around as aroundmod
from . import context as ctxmod
from .backends import Breaker, GeoResult, reverse_chain, probe as backends_probe
from .backfill import Backfill
from .cache import Cache
from . import backends, geonames, sightsindex
from . import overrides as ovmod
from . import srtmfetch
from .config import Settings, load
from .elevation import Elevation
from .overrides import Overrides
from .tracks import TrackService
from .trackmath import Track, locate
from .weather import WeatherService

VERSION = "1.0.0"
MAX_RANGE_S = 7 * 86400


class Point(BaseModel):
    lat: float
    lon: float


class Batch(BaseModel):
    points: list[Point] = Field(max_length=500)
    lang: Optional[str] = None


class State:
    def __init__(self, s: Settings):
        self.s = s
        self.cache = Cache(s.cache_db)
        self.breaker = Breaker(s.breaker_failures, s.breaker_cooldown)
        self.tracks = TrackService(s, self.cache)
        self.weather = WeatherService(s, self.cache)
        self.overrides = Overrides(s.overrides_file or os.path.join(os.path.dirname(s.cache_db) or ".", "overrides.json"))
        self.elevation = Elevation(s, self.cache, self.overrides)
        self.backfill = Backfill(self)
        self.aliases = namesmod.Aliases(s.name_aliases_file or os.path.join(os.path.dirname(s.cache_db) or ".", "place-names.json"))
        self.countries = None                      # set by geonames.register (reads countryInfo.txt from /geonames)
        self.started = time.time()
        self.reverse_calls = 0


class TimingLog:
    """Pure ASGI middleware: one line per request that took at least `slow_ms` (0 = every request), with the arrival time to the
    millisecond, so it can be lined up with `docker compose logs -t` and with a client's own timing."""

    def __init__(self, app, slow_ms: int = 500):
        self.app, self.slow_ms = app, slow_ms

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0, status = time.time(), [0]

        async def snd(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, snd)
        finally:
            ms = (time.time() - t0) * 1000
            if ms >= self.slow_ms:
                qs = scope.get("query_string", b"").decode("latin-1")
                print(f"{datetime.fromtimestamp(t0).isoformat(timespec='milliseconds')} {scope.get('method', '')} "
                      f"{scope.get('path', '')}{'?' + qs if qs else ''} -> {status[0]} {ms:.0f} ms", flush=True)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    st = State(settings or load())
    app = FastAPI(title="geo-service", version=VERSION)
    app.state.st = st
    if st.s.slow_log_ms >= 0:
        app.add_middleware(TimingLog, slow_ms=st.s.slow_log_ms)

    def auth(request: Request) -> None:
        if not st.s.token:
            return
        given = request.headers.get("x-api-key", "")
        if not given and request.headers.get("authorization", "").lower().startswith("bearer "):
            given = request.headers["authorization"][7:].strip()
        if given != st.s.token:
            raise HTTPException(401, "missing or wrong token")

    # ---------------------------------------------------------------- helpers
    def parse_time(value: str) -> int:
        """Epoch seconds or ISO 8601. A time without zone/offset is taken as UTC."""
        v = value.strip()
        try:
            return int(float(v))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, f"cannot parse time {value!r} (use epoch seconds or ISO 8601)")
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())

    def check_coords(lat: float, lon: float) -> None:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(422, "lat/lon out of range")

    def do_reverse(lat: float, lon: float, lang: str, nocache: bool = False) -> dict:
        """Cache first, then the geocoders; a matching override (overrides.json) is laid over the answer."""
        out = namesmod.normalize(do_reverse_raw(lat, lon, lang, nocache), st.aliases, st.s.normalize_names)
        ov = st.overrides.find("reverse", lat, lon)
        return ovmod.apply_reverse(out, *ov) if ov else out

    def do_reverse_raw(lat: float, lon: float, lang: str, nocache: bool = False) -> dict:
        check_coords(lat, lon)
        p = st.s.cache_precision
        key = f"{lang}|{lat:.{p}f}|{lon:.{p}f}"
        if not nocache:
            hit, hit_ts = st.cache.get_with_ts("reverse", key)
            if hit is not None:
                # `tried` inside a cached row describes the lookup that FILLED the cache (it may say "unreachable"
                # from a time the backend was down) -- not the current state, so it is not repeated here.
                return {"lat": lat, "lon": lon, **{k: v for k, v in hit.items() if k != "tried"}, "cached": True,
                        **cache_info(hit_ts, hit.get("source", "")), "tried": []}
        st.reverse_calls += 1
        res, tried, reachable = reverse_chain(st.s, st.breaker, round(lat, p), round(lon, p), lang)
        if res is None and not reachable:
            if st.overrides.find("reverse", lat, lon):          # an override can stand in for a missing answer
                return {"lat": lat, "lon": lon, "found": False, "tried": tried, "cached": False}
            raise HTTPException(503, {"error": "no geocoding backend reachable or configured", "tried": tried})
        payload = {"found": res is not None, **(res.to_dict() if res else {}), "tried": tried}
        st.cache.put("reverse", key, payload, st.s.cache_ttl_days * 86400 if res else st.s.cache_neg_ttl)
        return {"lat": lat, "lon": lon, **payload, "cached": False, **cache_info(int(time.time()), payload.get("source", ""))}

    def cache_info(ts, upstream: str) -> dict:
        """Where the (raw) answer came from and when it entered the cache -- stays visible when an override replaces `source`."""
        tz = ZoneInfo(st.s.default_tz) if st.s.default_tz else timezone.utc
        return {"upstream_source": upstream, "cached_at": datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds"),
                "cached_age_s": max(0, int(time.time() - ts))}

    def window_points(t0: int, t1: int, sources: str, devices: str, include_import: bool):
        wanted = tuple(x.strip() for x in sources.split(",") if x.strip())
        pts, status = st.tracks.points(t0, t1, wanted, devices, include_import)
        return pts, status

    # ---------------------------------------------------------------- endpoints
    @app.get("/health")
    def health(probe: bool = False):
        """probe=true additionally asks every configured geocoder RIGHT NOW (live, with the real error text)
        and closes the circuit of those that answer. Backends are never checked 'only at startup': every
        uncached lookup tries them again, except while a circuit is open (GEO_BREAKER_* cooldown)."""
        s = st.s
        geo = {n: {"configured": bool(getattr(s, n)), **st.breaker.state(n)}
               for n in ("photon", "nominatim", "pelias", "maritime", "gazetteer")}
        if probe:
            for n in geo:
                if geo[n]["configured"]:
                    geo[n]["probe"] = backends_probe(s, st.breaker, n)
                    geo[n].update(st.breaker.state(n))
        return {"status": "ok", "version": VERSION, "uptime_s": int(time.time() - st.started),
                "geocoders": geo,
                "tracks": st.tracks.available(), "weather": s.weather_provider,
                "env": {k: v for k, v in os.environ.items()
                        if re.match(r"^(GEO_|GN_|WEATHER_|ELEVATION_)", k) and not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k)}}

    @app.get("/stats", dependencies=[Depends(auth)])
    def stats():
        return {"cache_rows": st.cache.counts(), "cache_bytes": st.cache.size_bytes(),
                "cache_hits": st.cache.hits, "cache_misses": st.cache.misses,
                "upstream_reverse_lookups": st.reverse_calls,
                "overrides": len(st.overrides.items()), "overrides_error": st.overrides.error,
                "elevation": {"providers": st.elevation.providers, "local_tiles": len(st.elevation.local_tiles())},
                "gazetteer": {"available": st.gn.gaz.available() if st.gn else False,
                              **{k: v for k, v in (st.gn.gaz.meta().items() if st.gn else []) if k.startswith("n_") or k in ("built", "langs")}}}

    @app.get("/reverse", dependencies=[Depends(auth)])
    def reverse(lat: float, lon: float, lang: Optional[str] = None, nocache: bool = False):
        return do_reverse(lat, lon, lang or st.s.lang, nocache)

    @app.post("/reverse-batch", dependencies=[Depends(auth)])
    def reverse_batch(b: Batch):
        lang = b.lang or st.s.lang

        def one(p: Point):
            try:
                return do_reverse(p.lat, p.lon, lang)
            except HTTPException as e:
                return {"lat": p.lat, "lon": p.lon, "found": False, "error": e.status_code}
        with ThreadPoolExecutor(max_workers=4) as ex:
            return {"results": list(ex.map(one, b.points))}

    @app.get("/track-at", dependencies=[Depends(auth)])
    def track_at(time_: str = Query(alias="time"), max_gap: Optional[int] = None, devices: str = "default",
                 sources: str = "traccar,dawarich", include_import: bool = False):
        t = parse_time(time_)
        gap2 = max_gap or st.s.max_gap2
        pts, status = window_points(t - gap2, t + gap2, sources, devices, include_import)
        res = locate(Track(pts), t, st.s.max_gap1, gap2)
        if res is None:
            if status and not any(status.values()):
                raise HTTPException(503, {"error": "track sources unreachable", "sources": status})
            return {"found": False, "epoch": t, "points_in_window": len(pts), "sources": status}
        return {"found": True, "epoch": t, **res, "sources": status}

    @app.get("/track-range", dependencies=[Depends(auth)])
    def track_range(from_: str = Query(alias="from"), to: str = Query(), step: int = 0, devices: str = "default",
                    sources: str = "traccar,dawarich", include_import: bool = False):
        t0, t1 = parse_time(from_), parse_time(to)
        if t1 < t0:
            raise HTTPException(422, "'to' is before 'from'")
        if t1 - t0 > MAX_RANGE_S:
            raise HTTPException(422, f"range too large (max {MAX_RANGE_S // 86400} days per request)")
        pts, status = window_points(t0, t1, sources, devices, include_import)
        if status and not any(status.values()):
            raise HTTPException(503, {"error": "track sources unreachable", "sources": status})
        if step > 0:                       # thin out: at most one point per `step` seconds
            thinned, last = [], None
            for p in pts:
                if last is None or p.epoch - last >= step:
                    thinned.append(p)
                    last = p.epoch
            pts = thinned
        short = {"Traccar": "T", "Dawarich": "D", "Dawarich-Import": "I"}
        return {"count": len(pts), "sources": status,
                "points": [[p.epoch, p.lat, p.lon, short.get(p.source, p.source)] for p in pts]}

    # ---------------------------------------------------------------- Meine Orte, sights, trips
    def own_pois() -> list:
        return [{"name": it["place"]["name"], "kind": it["place"].get("kind") or "Eigener Ort", "lat": it["lat"], "lon": it["lng"],
                 "own": True} for it in st.overrides.places() if it["place"].get("poi")]

    def home_pos(points=None) -> Optional[tuple]:
        if st.s.home_lat or st.s.home_lon:
            return (st.s.home_lat, st.s.home_lon)
        for it in st.overrides.places():
            if (it["place"].get("kind") or "").strip().lower() == "zuhause":
                return (it["lat"], it["lng"])
        return None

    def trips_between(first: date, last: date, lang: str) -> dict:
        """{'trips': [...], 'home': [lat, lon] | None, 'points': n}. One computation serves a whole 30-day bucket of
        photos (cached), so a folder of 500 pictures costs a handful of track fetches, not 500."""
        tz = ZoneInfo(st.s.default_tz) if st.s.default_tz else timezone.utc
        today = datetime.now(timezone.utc).astimezone(tz).date()
        last = min(last, today)
        hp = home_pos()
        key = f"trips|v1|{first}|{last}|{hp}|{st.s.trip_away_km}|{st.s.trip_min_days}|{st.s.trip_max_gap_days}|{st.s.home_country}|{lang}"
        hit = st.cache.get("geonames", key)
        if hit is not None:
            return hit
        t0 = int(datetime(first.year, first.month, first.day, tzinfo=tz).timestamp())
        t1 = int(datetime(last.year, last.month, last.day, tzinfo=tz).timestamp()) + 86399
        pts, status = st.tracks.points(t0, t1, ("traccar", "dawarich"), "default", False)
        if not pts:
            res = {"trips": [], "home": None, "points": 0, "sources": status}
            if status and any(status.values()):              # sources answered: really nothing recorded
                st.cache.put("geonames", key, res, 600)
            return res
        day_pos = tripmod.daily(pts, tz)
        home = hp or tripmod.home_from_nights(pts, tz)
        if home is None and day_pos:                         # no night data: the most visited day-position cell
            cells: dict = {}
            for dd, v in day_pos.items():
                cells.setdefault((round(v["lat"], 1), round(v["lon"], 1)), []).append(v)
            best = max(cells.values(), key=len)
            home = (best[0]["lat"], best[0]["lon"])

        def rev(la, lo):
            try:
                r = do_reverse(la, lo, lang)
            except HTTPException:
                return None
            return r if r and r.get("found") else None
        found = tripmod.detect(day_pos, home, first, last, st.s.trip_away_km, st.s.trip_min_days, st.s.trip_max_gap_days)
        out = [tripmod.summary(t, tripmod.name_trip(t, day_pos, rev, st.s.home_country)) for t in found]
        res = {"trips": out, "home": [round(home[0], 5), round(home[1], 5)], "points": len(pts), "sources": status}
        st.cache.put("geonames", key, res, 3600 if last >= today - timedelta(days=14) else st.s.cache_ttl_days * 86400)
        return res

    def trip_for(t: int, lang: str) -> Optional[dict]:
        tz = ZoneInfo(st.s.default_tz) if st.s.default_tz else timezone.utc
        d = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(tz).date()
        base = date.fromordinal(d.toordinal() // 30 * 30)
        lb = timedelta(days=st.s.trip_lookback_days)
        res = trips_between(base - lb, base + timedelta(days=29) + lb, lang)
        for tr in res["trips"]:
            a, b = date.fromisoformat(tr["from"]), date.fromisoformat(tr["to"])
            if a <= d <= b:
                return {**tr, "day": (d - a).days + 1}
        return None

    def trip_available() -> bool:
        return any(st.tracks.available().get(k) for k in ("traccar", "dawarich"))

    def extras(out: dict, place: Optional[dict], lat: float, lon: float, t: int, lang: str, want_poi: bool, want_trip: bool) -> None:
        parts = out.setdefault("parts_de", {})
        w = out.get("weather") or {}
        if w.get("wind_kmh") is not None and w["wind_kmh"] >= 20:
            parts["wind"] = f"{w['wind_de']} ({w['wind_kmh']} km/h)"
        if w.get("precip_mm") is not None and w["precip_mm"] >= 0.1:
            parts["precip"] = f"{w['precip_mm']} mm Niederschlag"
        cc = daycal.country_code(place)
        if cc and st.countries is not None and cc != st.s.home_country.upper() and cc in st.countries.rows:
            ci = countryinfo.describe(st.countries.rows[cc], (place or {}).get("country", ""))
            out["country_info"] = ci
            if ci["text_de"]:
                parts["country"] = f"{ci['name'] or cc}: {ci['text_de']}"
        if want_poi and st.s.poi_enabled:
            try:
                lst = poimod.nearby(st.s, st.cache, lat, lon, st.s.poi_radius_m, st.s.poi_max, lang, own_pois())
            except Exception:                     # noqa: BLE001  a sight list must never break a caption
                lst = None
            if lst:
                out["pois"] = lst
                parts["poi"] = poimod.phrase_de(lst)
        if want_poi and st.s.poi_enabled and st.s.top_sight_m > 0:
            try:
                near = sightsmod.search(st.s, st.cache, lat, lon, st.s.top_sight_m, 5, lang, sort="score", own_places=own_pois())
                top = sightsmod.top_sight(near, st.s.top_sight_min_importance)
            except Exception:                     # noqa: BLE001
                top = None
            if top:
                out["top_sight"] = top
        if want_trip:
            tr = None
            if trip_available():
                try:
                    tr = trip_for(t, lang)
                except Exception:                 # noqa: BLE001
                    tr = None
            if tr:
                out["trip"] = tr
                parts["trip"] = f"auf Reise: {tr['name'] or tr['folder']} (Tag {tr['day']} von {tr['days']})"
            else:                                  # no recorded trip: name it after the place when far from home
                pt = tripmod.place_title(place, lat, lon, home_pos(), st.s.place_trip_km)
                if pt:
                    out["trip"] = pt
                    parts["trip"] = f"unterwegs: {pt['name']}"

    @app.get("/context", dependencies=[Depends(auth)])
    def context(lat: float, lon: float, time_: str = Query(alias="time"), lang: Optional[str] = None,
                weather: bool = True, elevation: bool = True, poi: bool = True, trip: bool = True):
        check_coords(lat, lon)
        t = parse_time(time_)
        lg = lang or st.s.lang
        try:
            place = do_reverse(lat, lon, lg)
        except HTTPException:
            place = None                   # a context without a place name is still useful
        place = place if place and place.get("found") else None
        w, wstatus = st.weather.at_status(lat, lon, t) if weather else (None, "not-requested")
        out = ctxmod.build(place, w, lat, lon, t, st.s.default_tz)
        out["weather_status"] = wstatus          # ok | off | limited | failed | empty | no-hour | not-requested
        if elevation and st.elevation.enabled or st.overrides.find("elevation", lat, lon):
            e = st.elevation.lookup(lat, lon)
            if e["elevation"] is not None:
                out["elevation_m"] = round(e["elevation"])
        extras(out, place, lat, lon, t, lg, poi, trip)
        return out

    @app.get("/places", dependencies=[Depends(auth)])
    def places():
        """'Meine Orte': named places from the override editor (kind 'Zuhause' = home), for clients that offer a
        place list (GPS sidecar)."""
        return {"places": [{"id": it["id"], "name": it["place"]["name"], "kind": it["place"].get("kind", ""),
                            "poi": bool(it["place"].get("poi")), "lat": it["lat"], "lon": it["lng"], "radius_m": it["radius_m"],
                            "note": it.get("note", "")} for it in st.overrides.places()]}

    @app.get("/pois", dependencies=[Depends(auth)])
    def pois(lat: float, lon: float, radius: Optional[int] = None, limit: Optional[int] = None, lang: Optional[str] = None):
        check_coords(lat, lon)
        r = max(50, min(5000, radius or st.s.poi_radius_m))
        lst = poimod.nearby(st.s, st.cache, lat, lon, r, max(1, min(30, limit or st.s.poi_max)), lang or st.s.lang, own_pois())
        if lst is None:
            raise HTTPException(503, {"error": "no sights source: PHOTON_SERVER not set or unreachable"})
        return {"count": len(lst), "radius_m": r, "pois": lst, "text_de": poimod.phrase_de(lst)}

    @app.get("/sights", dependencies=[Depends(auth)])
    def sights(lat: Optional[float] = None, lon: Optional[float] = None, q: Optional[str] = None, address: Optional[str] = None,
               radius: int = Query(2000, ge=50, le=sightsmod.MAX_RADIUS_M), limit: int = Query(10, ge=1, le=50),
               categories: Optional[str] = None, lang: Optional[str] = None, sort: str = Query("score", pattern="^(score|distance)$")):
        """Sights around a position (lat+lon), a place name (q) or an address (address): ranked by category and distance,
        duplicates merged, with bearing and details (description, opening hours, website, wikidata/wikipedia)."""
        lg = lang or st.s.lang
        center = None
        if lat is not None and lon is not None:
            check_coords(lat, lon)
        elif (q or address or "").strip():
            hit = sightsmod.resolve(st.gn, q or address, lg, address=bool(address and not q))
            if hit is None:
                raise HTTPException(404, {"error": f"'{q or address}' not found"})
            lat, lon, label = hit
            center = {"label": label}
        else:
            raise HTTPException(422, "give lat+lon, q or address")
        cats = [c.strip().lower() for c in (categories or "").split(",") if c.strip()]
        unknown = [c for c in cats if c not in sightsmod.CATEGORIES and c not in ("own", watermod.CATEGORY)]
        if unknown:
            raise HTTPException(422, {"error": f"unknown categories: {', '.join(unknown)}", "allowed": list(sightsmod.CATEGORIES) + ["own", watermod.CATEGORY]})
        want_water = watermod.CATEGORY in cats
        cats = [c for c in cats if c != watermod.CATEGORY]
        if want_water and not cats:
            lst = []                                            # only water asked for: no sights lookup
        else:
            lst = sightsmod.search(st.s, st.cache, lat, lon, radius, limit, lg, cats, sort, own_pois())
        if lst is None and not want_water:
            raise HTTPException(503, {"error": "no sights source: no sights index and PHOTON_SERVER not set or unreachable"})
        if want_water:                                         # rivers / lake from the maritime server, merged by sort order
            lst = (lst or []) + watermod.near(st.s, lat, lon, radius, limit, lg)
            lst.sort(key=(lambda p: (p["distance_m"], -p["score"])) if sort == "distance" else (lambda p: (-p["score"], p["distance_m"])))
            lst = lst[:limit]
            cats = cats + [watermod.CATEGORY]
        return {"center": {"lat": lat, "lon": lon, **(center or {})}, "radius_m": radius, "sort": sort, "categories": cats or "all",
                "count": len(lst), "sights": lst, "text_de": sightsmod.phrase_de(lst),
                "source": "Sights-Index" if sightsmod.index(st.s) is not None else "Photon"}

    @app.get("/around", dependencies=[Depends(auth)])
    def around(lat: float, lon: float, radius: int = Query(2000, ge=50, le=sightsmod.MAX_RADIUS_M), limit: int = Query(10, ge=1, le=30),
               lang: Optional[str] = None, include: Optional[str] = None):
        """Everything about one coordinate in ONE answer: the place (street, house number, postcode ...), the water (sea, bay,
        lake, rivers nearby), the best sights, elevation and time zone. `include=place,water,sights,elevation,timezone` picks parts;
        a part that fails is listed under `errors`, the others still arrive."""
        check_coords(lat, lon)
        parts, unknown = aroundmod.parse_include(include)
        if unknown:
            raise HTTPException(422, {"error": f"unknown parts: {', '.join(unknown)}", "allowed": list(aroundmod.PARTS)})
        return aroundmod.build(st, lat, lon, radius, limit, lang or st.s.lang, parts, do_reverse, own_pois)

    @app.get("/sights/status", dependencies=[Depends(auth)])
    def sights_status():
        """State of the sights index: counts, build / Wikidata / importance dates, translated descriptions, the data version
        written into sidecars (place_version) and the last automatic refresh (see sightsindex refresh)."""
        idx = sightsmod.index(st.s)
        if idx is None:
            return {"available": False, "place_version": sightsmod.place_version(None), "refresh": sightsindex.read_status(st.s.sights_index_path)}
        return {**idx.stats(), "place_version": sightsmod.place_version(idx), "refresh": sightsindex.read_status(st.s.sights_index_path)}

    @app.get("/trips", dependencies=[Depends(auth)])
    def trips(from_: str = Query(alias="from"), to: str = Query(), lang: Optional[str] = None):
        try:
            a, b = date.fromisoformat(from_[:10]), date.fromisoformat(to[:10])
        except ValueError:
            raise HTTPException(422, "from/to: YYYY-MM-DD expected")
        if b < a or (b - a).days > 800:
            raise HTTPException(422, "range invalid (max 800 days)")
        if not trip_available():
            raise HTTPException(503, {"error": "no track source (Traccar/Dawarich) configured"})
        return trips_between(a, b, lang or st.s.lang)

    @app.get("/trip-at", dependencies=[Depends(auth)])
    def trip_at(time_: str = Query(alias="time"), lang: Optional[str] = None):
        t = parse_time(time_)
        if not trip_available():
            raise HTTPException(503, {"error": "no track source (Traccar/Dawarich) configured"})
        tr = trip_for(t, lang or st.s.lang)
        return {"found": bool(tr), "trip": tr}

    @app.get("/clock-check", dependencies=[Depends(auth)])
    def clock_check(time_: str = Query(alias="time"), lat: Optional[float] = None, lon: Optional[float] = None,
                    gps_time: Optional[str] = None, devices: str = "default", sources: str = "traccar,dawarich"):
        """Is the camera clock plausible? `time` = capture time (UTC/offset), lat/lon = the camera's OWN position (not one
        filled in from a track), gps_time = the file's GPS satellite time. Every part is optional."""
        t = parse_time(time_)
        track = None
        if lat is not None and lon is not None:
            check_coords(lat, lon)
            pts, _status = window_points(t - 45000, t + 45000, sources, devices, False)
            track = Track(pts) if pts else None
        g = parse_time(gps_time) if gps_time else None
        return {"epoch": t, **clockcheck.check(t, lat, lon, g, track, int(time.time()))}

    def tile_url_for_browser() -> str:
        """GEO_TILE_PROXY=true: the browser asks this service (same origin, so HTTPS pages get no mixed-content block and the
        tileserver stays on the LAN); otherwise GEO_TILE_URL as written."""
        return "/tiles/{z}/{x}/{y}" if (st.s.tile_proxy and st.s.tile_url) else st.s.tile_url

    @app.get("/tiles/{z}/{x}/{y}", include_in_schema=False)
    def tile_proxy(z: int, x: int, y: int):
        """Forwards one raster tile from GEO_TILE_URL (map data only, no token: <img> tiles cannot send headers)."""
        if not (st.s.tile_proxy and st.s.tile_url):
            raise HTTPException(404, "tile proxy is off (GEO_TILE_PROXY=true and GEO_TILE_URL needed)")
        if not (0 <= z <= 22 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise HTTPException(404, "no such tile")
        url = st.s.tile_url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y)).replace("{r}", "")
        code, body, ctype = backends.fetch_tile(url, st.s.http_timeout)
        if code == 404:
            raise HTTPException(404, "no such tile")
        if code != 200:
            raise HTTPException(502, "tile server not reachable" if code == 0 else f"tile server answered {code}")
        return Response(body, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})

    UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "ui.html")

    @app.get("/ui", include_in_schema=False)
    def ui():
        """Override editor (one page; the API calls it makes carry the token you type in). The map settings
        (GEO_TILE_URL ...) are injected here; nothing secret is in them."""
        cfg = {"tileUrl": tile_url_for_browser(), "attribution": st.s.tile_attribution, "maxZoom": st.s.tile_maxzoom}
        try:
            h = home_pos()                    # GEO_HOME_LAT/LON, else the place of kind 'Zuhause' -> the map opens there
        except Exception:                     # noqa: BLE001
            h = None
        if h:
            cfg["home"] = [round(h[0], 6), round(h[1], 6)]
        with open(UI_FILE, encoding="utf-8") as f:
            html = f.read().replace("/*GEO_UI_CONFIG*/{}", json.dumps(cfg).replace("</", "<\\/"), 1)
        return HTMLResponse(html)

    RADAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "radar.html")

    @app.get("/radar", include_in_schema=False)
    def radar_page():
        """Rain radar page: your own map style (tileserver-GL) + radar tiles of a RainViewer-compatible API. Settings are
        injected (nothing secret); the map opens at home (GEO_HOME_LAT/LON or the place of kind 'Zuhause')."""
        style = st.s.radar_style_url
        if not style and st.s.tile_url:                    # http://host:8777/styles/x/{z}/{x}/{y}.png -> .../styles/x/style.json
            style = re.sub(r"/\{z\}/\{x\}/\{y\}[^/]*$", "/style.json", st.s.tile_url)
        cfg = {"styleUrl": style, "radarApi": st.s.radar_api, "radarColor": st.s.radar_color, "radarOptions": st.s.radar_options,
               "radarMaxZoom": st.s.radar_maxzoom, "attribution": st.s.tile_attribution}
        try:
            h = home_pos()
        except Exception:                                  # noqa: BLE001
            h = None
        if h:
            cfg["center"] = [round(h[1], 6), round(h[0], 6)]       # MapLibre wants [lng, lat]
        with open(RADAR_FILE, encoding="utf-8") as f:
            html = f.read().replace("/*GEO_RADAR_CONFIG*/{}", json.dumps(cfg).replace("</", "<\\/"), 1)
        return HTMLResponse(html)

    NEARBY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "nearby.html")
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @app.get("/nearby", include_in_schema=False)
    def nearby_page():
        """PWA 'in der Naehe': location button / place input -> /sights, list + map (your tileserver-GL)."""
        cfg = {"tileUrl": tile_url_for_browser(), "attribution": st.s.tile_attribution, "maxZoom": st.s.tile_maxzoom}
        with open(NEARBY_FILE, encoding="utf-8") as f:
            html = f.read().replace("/*GEO_UI_CONFIG*/{}", json.dumps(cfg).replace("</", "<\\/"), 1)
        return HTMLResponse(html)

    @app.get("/nearby.webmanifest", include_in_schema=False)
    def nearby_manifest():
        return JSONResponse({"name": "In der Nähe", "short_name": "Nearby", "start_url": "/nearby", "scope": "/", "display": "standalone",
                             "background_color": "#14171b", "theme_color": "#2563eb", "lang": "de",
                             "icons": [{"src": "/nearby-icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
                                       {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                                       {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]},
                            media_type="application/manifest+json")

    @app.get("/nearby-sw.js", include_in_schema=False)
    def nearby_sw():
        return FileResponse(os.path.join(STATIC_DIR, "nearby-sw.js"), media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/"})

    # browser tab icon for every page of this host (also /docs): asked for at the root by browsers and iOS
    for _path, _file, _mt in (("/favicon.ico", "favicon.ico", "image/x-icon"), ("/favicon.svg", "favicon.svg", "image/svg+xml"),
                              ("/apple-touch-icon.png", "apple-touch-icon.png", "image/png"),
                              ("/icon-192.png", "icon-192.png", "image/png"), ("/icon-512.png", "icon-512.png", "image/png")):
        def _icon(f=_file, m=_mt):
            return FileResponse(os.path.join(STATIC_DIR, f), media_type=m, headers={"Cache-Control": "public, max-age=86400"})
        app.get(_path, include_in_schema=False)(_icon)

    @app.get("/nearby-icon.svg", include_in_schema=False)
    def nearby_icon():
        return FileResponse(os.path.join(STATIC_DIR, "nearby-icon.svg"), media_type="image/svg+xml")

    # Leaflet (vendored, BSD-2) for the editor's map: /ui-assets/leaflet/leaflet.js ...
    app.mount("/ui-assets", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "vendor")),
              name="ui-assets")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    @app.get("/elevation", dependencies=[Depends(auth)])
    def elevation_ep(lat: float, lon: float):
        """Metres above sea level: override -> cache -> local SRTM3 tiles -> Open-Meteo (ELEVATION_PROVIDER)."""
        check_coords(lat, lon)
        return {"lat": lat, "lon": lon, **st.elevation.lookup(lat, lon)}

    @app.get("/overrides", dependencies=[Depends(auth)])
    def overrides_list():
        return {"file": st.overrides.path, "error": st.overrides.error, "overrides": st.overrides.items()}

    @app.post("/overrides", dependencies=[Depends(auth)])
    def overrides_set(item: dict = Body(...)):
        """Add or replace one override (same id = replace). Body: see overrides.example.json."""
        try:
            return st.overrides.upsert(item)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/overrides/{oid}", dependencies=[Depends(auth)])
    def overrides_delete(oid: str):
        if not st.overrides.delete(oid):
            raise HTTPException(404, "no such override")
        return {"deleted": oid}

    @app.on_event("startup")
    def srtm_preload():
        """GEO_SRTM_PRELOAD=austria,germany,...: fetch missing tiles in the background (a few MB each, once)."""
        tiles, unknown = srtmfetch.resolve_regions(st.s.srtm_preload)
        if unknown:
            print(f"srtm preload: unknown region(s) {unknown} ignored; known: {', '.join(sorted(srtmfetch.REGIONS))}",
                  file=sys.stderr, flush=True)
        if not tiles:
            return
        def log(m):
            print(m, file=sys.stderr, flush=True)

        def run():
            # the container runs several app processes: only one of them downloads
            import fcntl
            os.makedirs(st.s.srtm_dir, exist_ok=True)
            lock = open(os.path.join(st.s.srtm_dir, ".preload.lock"), "w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            log(f"srtm preload {st.s.srtm_preload}: {len(tiles)} tiles to check, missing ones are downloaded (~3 MB each)")
            res = srtmfetch.fetch_many(st.s.srtm_source_url, tiles, st.s.srtm_dir, log=log)
            log(f"srtm preload {st.s.srtm_preload}: done {res}")
            lock.close()
        threading.Thread(target=run, name="srtm-preload", daemon=True).start()

    @app.post("/weather/backfill", dependencies=[Depends(auth)])
    def weather_backfill(b: dict = Body(default_factory=dict)):
        """Body: {points: [[epoch, lat, lon], ...], from, to (epoch or ISO), tracks: true, sources, devices,
        include_import, dry_run, refresh: none|bad|all}.  refresh=bad fetches stored days that are empty/incomplete again,
        refresh=all every requested day again.  Runs in the background; poll GET /weather/backfill."""
        refresh = str(b.get("refresh") or "none").lower()
        if refresh not in ("none", "bad", "all"):
            raise HTTPException(422, "refresh must be none, bad or all")
        points = b.get("points") or []
        if not isinstance(points, list) or len(points) > 200000:
            raise HTTPException(422, "points must be a list of [epoch, lat, lon] (max 200000)")
        use_tracks, dry = bool(b.get("tracks", True)), bool(b.get("dry_run", False))
        if not st.weather.enabled:
            raise HTTPException(409, "weather is off (WEATHER_PROVIDER=open-meteo to enable)")
        t0 = parse_time(str(b["from"])) if b.get("from") not in (None, "") else None
        t1 = parse_time(str(b["to"])) if b.get("to") not in (None, "") else None
        if use_tracks and (t0 is None) != (t1 is None):
            raise HTTPException(422, "give both 'from' and 'to' (or neither, to use only the points)")
        if not points and not (use_tracks and t0 is not None):
            raise HTTPException(422, "nothing to do: send points and/or from+to with tracks=true")
        try:
            job = st.backfill.start(points, t0, t1, use_tracks, str(b.get("sources") or "traccar,dawarich"),
                                    str(b.get("devices") or "default"), bool(b.get("include_import", False)), dry, refresh)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return job.to_dict()

    @app.get("/weather/backfill", dependencies=[Depends(auth)])
    def weather_backfill_status():
        j = st.backfill.current()
        return j.to_dict() if j else {"status": "idle"}

    @app.post("/weather/backfill/cancel", dependencies=[Depends(auth)])
    def weather_backfill_cancel():
        j = st.backfill.current()
        if j and j.status == "running":
            j.cancel.set()
        return j.to_dict() if j else {"status": "idle"}

    @app.get("/weather/stats", dependencies=[Depends(auth)])
    def weather_stats():
        return {"enabled": st.weather.enabled, "archive": st.cache.weather_span(),
                "backfill": (st.backfill.current().to_dict() if st.backfill.current() else None)}

    st.reverse_raw = do_reverse_raw
    st.gn = geonames.register(app, st, auth)
    return app


# Run with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
# (a factory, so importing this module never opens the cache file -- tests build their own app)
