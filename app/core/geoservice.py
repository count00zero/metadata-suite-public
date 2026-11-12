"""
geoservice.py -- optional bridge to the standalone geo-service container (see geo-service/README.md).

Nothing in the suite depends on it. With GEO_SERVICE_URL unset, enabled() is False and every
caller keeps using its own local code. With it set:

  * geocore.reverse_geocode() asks the service first (one shared cache + circuit breaker for
    Photon/Nominatim/Pelias, the same place names for every script) and only falls back to its
    own local chain when the service is DOWN -- "nothing found here" is a real answer and is not
    repeated against the same backends;
  * AI-Beschreibung can add time of day / season / weather / trip name to the prompt
    (AI_CONTEXT=true), see caption_extra().

A service that is down must never slow a batch to a crawl, so after two failures in a row it is
skipped for 60 seconds instead of costing GEO_SERVICE_TIMEOUT per file.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

from . import exifpool

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

FAIL_LIMIT = 2
COOLDOWN_S = 60

_lock = threading.Lock()
_fails = 0
_open_until = 0.0


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name, default)
    return (v or "").strip().strip('"').strip("'").strip()


def url() -> str:
    return _env("GEO_SERVICE_URL").rstrip("/")


def enabled() -> bool:
    return bool(url()) and requests is not None


def timeout() -> float:
    try:
        return float(_env("GEO_SERVICE_TIMEOUT", "8") or 8)
    except ValueError:
        return 8.0


def context_timeout() -> float:
    """GEO_CONTEXT_TIMEOUT (default 40 s): /context can be slow the FIRST time for a place and day (the service then asks
    Open-Meteo, sights and tracks itself); with the short GEO_SERVICE_TIMEOUT that first call failed although the service
    kept working and cached the result for the next call."""
    try:
        return max(timeout(), float(_env("GEO_CONTEXT_TIMEOUT", "40") or 40))
    except ValueError:
        return 40.0


def ai_context_enabled() -> bool:
    return _env("AI_CONTEXT").lower() in ("1", "true", "yes", "on")


def reset_breaker() -> None:
    global _fails, _open_until
    with _lock:
        _fails, _open_until = 0, 0.0


def _get(path: str, params: dict, timeout_s: Optional[float] = None) -> tuple[Optional[dict], str]:
    """(json | None, 'ok' | 'down'). 'down' = unreachable / 5xx / breaker open."""
    global _fails, _open_until
    if time.time() < _open_until:
        return None, "down"
    headers = {"Accept": "application/json"}
    token = _env("GEO_SERVICE_TOKEN")
    if token:
        headers["X-API-Key"] = token
    try:
        r = requests.get(url() + path, params=params, headers=headers, timeout=timeout_s or timeout())
        bad = r.status_code >= 500 or r.status_code in (401, 403)
        data = None if bad else r.json()
    except Exception:           # noqa: BLE001 - any failure just means 'service unusable'
        bad, data = True, None
    with _lock:
        if bad:
            _fails += 1
            if _fails >= FAIL_LIMIT:
                _open_until = time.time() + COOLDOWN_S
                _fails = 0
        else:
            _fails = 0
    return (None, "down") if bad else (data, "ok")


def reverse(lat: float, lon: float, lang: str = "de", nocache: bool = False) -> tuple[str, Optional[dict]]:
    """('hit', fields) | ('empty', None) | ('down', None). nocache=True: ask the backends again, ignore the service's cache."""
    params = {"lat": lat, "lon": lon, "lang": lang}
    if nocache:
        params["nocache"] = "true"
    data, state = _get("/reverse", params)
    if state == "down" or not isinstance(data, dict):
        return "down", None
    if not data.get("found"):
        return "empty", None
    return "hit", data


def elevation(lat: float, lon: float) -> Optional[float]:
    """Metres above sea level at the point from the service (override/cache/SRTM3/Open-Meteo); None if unknown/off/down."""
    if not enabled():
        return None
    data, state = _get("/elevation", {"lat": lat, "lon": lon})
    try:
        v = data.get("elevation") if state == "ok" and isinstance(data, dict) else None
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def timezone_id(lat: float, lon: float) -> str:
    """IANA zone at the point ('Asia/Colombo') from the service's /timezoneJSON; '' if unknown/off/down."""
    if not enabled():
        return ""
    data, state = _get("/timezoneJSON", {"lat": lat, "lng": lon})
    if state == "ok" and isinstance(data, dict):
        return str(data.get("timezoneId") or "")
    return ""


def place_extras_enabled() -> bool:
    """XMP_PLACE_EXTRAS (default true): GPS Sidecar / GPS Fill add altitude + time zone to the sidecar when missing."""
    return _env("XMP_PLACE_EXTRAS", "true").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# AI caption context
# ---------------------------------------------------------------------------

_TRIP = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:-\d{4}\.\d{2}\.\d{2})?\s+(?P<name>\S.*)$")
_DT = re.compile(r"^(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")


def trip_name(path: str) -> str:
    """'…/2026.08.05-2026.08.22 Sri Lanka & Malediven/x.jpg' -> 'Sri Lanka & Malediven' ('' for date-only folders)."""
    d = os.path.dirname(os.path.abspath(path))
    while d and d != os.path.dirname(d):
        m = _TRIP.match(os.path.basename(d))
        if m:
            return m.group("name").strip()
        d = os.path.dirname(d)
    return ""


def _offset_seconds(s: str) -> Optional[int]:
    s = (s or "").strip()
    if s.upper() == "Z":
        return 0
    m = re.fullmatch(r"([+-])(\d\d):?(\d\d)", s)
    if not m:
        return None
    v = int(m.group(2)) * 3600 + int(m.group(3)) * 60
    return v if m.group(1) == "+" else -v


def _epoch(raw: str, offset: Optional[int]) -> Optional[int]:
    from datetime import datetime, timedelta, timezone
    m = _DT.match((raw or "").strip())
    if not m or m.group(1) == "0000":
        return None
    off = _offset_seconds(m.group(7)) if m.group(7) else offset
    if off is None:
        return None
    try:
        naive = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None
    return int((naive - timedelta(seconds=off)).replace(tzinfo=timezone.utc).timestamp())


def capture_epoch(file: str) -> Optional[int]:
    """UTC epoch of the shot, only when it is certain: an offset-qualified .xmp date (what Timestamp
    & Rename writes), else EXIF DateTimeOriginal + OffsetTimeOriginal/OffsetTime. Never guesses a zone."""
    xmp = file + ".xmp"
    try:
        if os.path.isfile(xmp):
            res = exifpool.run(["exiftool", "-m", "-T", "-XMP-exif:DateTimeOriginal", "-XMP-xmp:CreateDate", xmp],
                               capture_output=True, text=True, timeout=30)
            for raw in (res.stdout or "").rstrip("\n").split("\t"):
                e = _epoch(raw, None) if raw and raw != "-" else None
                if e is not None:
                    return e
        res = exifpool.run(["exiftool", "-m", "-T", "-DateTimeOriginal", "-OffsetTimeOriginal", "-OffsetTime", file],
                           capture_output=True, text=True, timeout=30)
        parts = (res.stdout or "").rstrip("\n").split("\t") + ["-", "-", "-"]
        off = next((o for o in (_offset_seconds(p) for p in parts[1:3] if p != "-") if o is not None), None)
        return _epoch(parts[0], off) if parts[0] != "-" else None
    except Exception:           # noqa: BLE001
        return None


_FNAME_TIME = re.compile(r"^(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d)")


def filename_epoch(file: str, lat: float, lon: float) -> Optional[int]:
    """Fallback when capture_epoch() finds no certain time (e.g. a video whose sidecar date has no zone and that never went through
    Timestamp & Rename's pinning): the suite's own file name 'YYYY.MM.DD-HH.MM.SS' IS the local capture time -- read in the zone at the
    shot's position. None when the name has no such time or the zone is unknown. Weaker than a pinned date, so callers mark it."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    m = _FNAME_TIME.match(os.path.basename(file))
    if not m:
        return None
    tz = timezone_id(lat, lon)
    if not tz:
        return None
    try:
        y, mo, d, h, mi, sec = (int(x) for x in m.groups())
        return int(datetime(y, mo, d, h, mi, sec, tzinfo=ZoneInfo(tz)).timestamp())
    except (ValueError, KeyError, OSError):
        return None


AI_EXTRAS_DEFAULT = "daylight,moon,holiday,trip,poi"
AI_EXTRAS_ALL = ("daylight", "moon", "holiday", "trip", "poi", "wind", "precip", "country")


def ai_context_extras() -> set:
    """AI_CONTEXT_EXTRAS (default daylight,moon,holiday,trip,poi): which of the service's extra phrases go into the AI
    prompt as well -- daylight (golden hour ...), moon (notable phases), holiday, trip (auto-detected), poi (sights
    nearby), wind, precip, country (capital/currency/languages abroad). 'none' = no extras. The XMP fields
    (XMP_CONTEXT) are written for all of them either way."""
    raw = _env("AI_CONTEXT_EXTRAS", AI_EXTRAS_DEFAULT).strip().lower()
    if raw in ("none", "off", "false", "0"):
        return set()
    if raw in ("all", "*"):
        return set(AI_EXTRAS_ALL)
    return {x.strip() for x in raw.split(",") if x.strip()}


def _gps_epoch(raw: str) -> Optional[int]:
    return _epoch(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw, 0)


def clock_info(file: str, epoch: int) -> dict:
    """Plausibility of the camera clock via the service's /clock-check: GPS satellite time of the file and, for files with
    their OWN position (camera GPS), the recorded track. {} when the file carries neither or the service is down.
    Result: {'clock_status': 'ok'|'warn', 'clock': text}."""
    try:
        res = exifpool.run(["exiftool", "-m", "-T", "-n", "-GPSLatitude", "-GPSLongitude", "-GPSDateTime", file],
                           capture_output=True, text=True, timeout=30)
        parts = (res.stdout or "").rstrip("\n").split("\t") + ["-", "-", "-"]
    except Exception:           # noqa: BLE001
        return {}
    params: dict = {"time": epoch}
    filled = False                 # a position that was filled in FROM the track proves nothing about the clock
    try:
        with open(file + ".xmp", encoding="utf-8", errors="replace") as fh:
            filled = "GpsSource" in fh.read()
    except OSError:
        pass
    try:
        if parts[0] != "-" and parts[1] != "-" and not filled:
            la, lo = float(parts[0]), float(parts[1])
            if abs(la) > 1e-6 or abs(lo) > 1e-6:
                params["lat"], params["lon"] = la, lo
    except ValueError:
        pass
    g = _gps_epoch(parts[2]) if parts[2] != "-" else None
    if g is not None:
        params["gps_time"] = g
    data, state = _get("/clock-check", params)
    if state != "ok" or not isinstance(data, dict) or data.get("status") not in ("ok", "warn"):
        return {}
    if len(params) == 1 and data["status"] == "ok":          # only the date sanity ran: say nothing
        return {}
    return {"clock_status": data["status"], "clock": data.get("text_de", "") if data["status"] == "warn" else "ok"}


_PV: dict = {"at": 0.0, "v": ""}


def place_version() -> str:
    """Data version of the geo-service's places / sights (GET /sights/status); '' when the service is off or old. Cached 5 min."""
    import time as _t
    if _t.time() - _PV["at"] < 300 and _PV["v"]:
        return _PV["v"]
    if not enabled():
        return ""
    data, state = _get("/sights/status", {})
    v = data.get("place_version", "") if state == "ok" and isinstance(data, dict) else ""
    _PV["at"], _PV["v"] = _t.time(), v
    return v


def context_info(file: str, lat: Optional[float] = None, lon: Optional[float] = None, force: bool = False,
                 clock: bool = True) -> tuple:
    """(extra text for the AI prompt, dict of structured fields for the sidecar).
    Fields (all optional): daypart, season, month, year, weekday, local_time, weather, temp_c, elevation, timezone,
    trip (+ trip_source folder|track), daylight, sunrise, sunset, moon, holiday, wind_kmh, wind_text, precip_mm,
    cloud_pct, humidity_pct, pois, sight (the sight the photo is most likely of), country, country_info, clock, clock_status.
    force=True ignores the AI_CONTEXT switch (module "Kontext nachfüllen" fills the XMP fields regardless of the AI text);
    clock=False skips the camera-clock check (an exiftool call per file)."""
    if not (enabled() and (force or ai_context_enabled())):
        return "", {}
    # local import: geocore imports this module lazily too, never at module load
    from . import geocore
    if lat is None or lon is None:
        lat, lon, _marker = geocore.read_xmp_gps(file + ".xmp") if os.path.isfile(file + ".xmp") else (None, None, False)
    parts: list[str] = []
    fields: dict = {}
    want = ai_context_extras()
    epoch = capture_epoch(file)
    time_basis = ""
    if epoch is None and geocore.has_real_gps(lat, lon):
        epoch = filename_epoch(file, lat, lon)
        time_basis = "Dateiname" if epoch is not None else ""
    folder_trip = trip_name(file)
    auto_trip = ""
    trip_src = "track"
    if epoch is not None and geocore.has_real_gps(lat, lon):
        data, state = _get("/context", {"lat": lat, "lon": lon, "time": epoch, "trip": "false" if folder_trip else "true"},
                          timeout_s=context_timeout())
        if state == "ok" and isinstance(data, dict):
            if time_basis:
                fields["time_basis"] = time_basis          # CtxTimeBasis: the time came from the file name, not from a pinned date
            if data.get("extra_de"):
                parts.append(data["extra_de"])
            if data.get("elevation_m") is not None and ai_context_elevation_enabled():
                parts.append(f"auf ca. {int(data['elevation_m'])} m Seeh\u00f6he")
            w = data.get("weather") or {}
            if not w and data.get("weather_status"):
                fields["weather_status"] = data["weather_status"]
            dl, moon, hol = data.get("daylight") or {}, data.get("moon") or {}, data.get("holiday") or {}
            ci, pois = data.get("country_info") or {}, data.get("pois") or []
            for k, v in (("daypart", data.get("daypart_de")), ("season", data.get("season_de")),
                         ("month", data.get("month_de")), ("year", data.get("year")),
                         ("weekday", data.get("weekday_de")), ("local_time", data.get("local_time")),
                         ("weather", w.get("summary_de")), ("temp_c", w.get("temp_c")),
                         ("elevation", data.get("elevation_m")), ("timezone", data.get("tz")),
                         ("wind_kmh", w.get("wind_kmh")), ("wind_text", w.get("wind_de")), ("precip_mm", w.get("precip_mm")),
                         ("cloud_pct", w.get("cloud_pct")), ("humidity_pct", w.get("humidity_pct")),
                         ("daylight", dl.get("label")), ("sunrise", dl.get("sunrise")), ("sunset", dl.get("sunset")),
                         ("moon", moon.get("label")), ("holiday", hol.get("name")),
                         ("country", ci.get("name")), ("country_info", ci.get("text_de")), ("sight", (data.get("top_sight") or {}).get("name")),
                         ("pois", "; ".join(f"{p['name']} ({p['kind']}, {p['distance_m']} m)" for p in pois))):
                if v not in (None, ""):
                    fields[k] = v
            pd = data.get("parts_de") or {}
            for key in ("daylight", "moon", "holiday", "wind", "precip", "country", "poi"):
                if key in want and pd.get(key):
                    parts.append(pd[key])
            tr = None if folder_trip else data.get("trip")
            if tr:
                auto_trip = tr.get("name") or tr.get("folder") or ""
                if auto_trip and "trip" in want:
                    parts.append(pd.get("trip") or f"auf Reise: {auto_trip}")
                if auto_trip and tr.get("source") == "place":
                    trip_src = "place"
        if epoch is not None and clock and not time_basis:        # a time taken from the name says nothing about the camera clock
            fields.update(clock_info(file, epoch))
    if folder_trip:
        parts.append(f"Wo wars: \u201e{folder_trip}\u201c")
        fields["trip"], fields["trip_source"] = folder_trip, "folder"
    elif auto_trip:
        fields["trip"], fields["trip_source"] = auto_trip, trip_src
    return "; ".join(parts), fields


def caption_extra(file: str, lat: Optional[float] = None, lon: Optional[float] = None) -> str:
    """'vormittags im Oktober 2026; Regen bei 11 \u00b0C; Wo wars: \u201eTestreise\u201c' -- or '' when
    AI_CONTEXT is off, the service is off/down, or the file has no certain time and no trip name.
    The place is NOT part of it: the prompt already carries it as geo_context."""
    return context_info(file, lat, lon)[0]


def ai_context_elevation_enabled() -> bool:
    """AI_CONTEXT_ELEVATION (default false): also tell the model the height above sea level ('auf ca. 1200 m Seehöhe').
    The XMP field CtxElevation is written either way (XMP_CONTEXT)."""
    return _env("AI_CONTEXT_ELEVATION").strip().lower() in ("1", "true", "yes", "on")


def xmp_context_enabled() -> bool:
    """XMP_CONTEXT (default true): also store the context as XMP-mon fields, independent of the AI text."""
    return _env("XMP_CONTEXT", "true").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# GPS tracks (module "GPS füllen"): raw points from the service's /track-range
# ---------------------------------------------------------------------------

class ServiceError(RuntimeError):
    pass


_TRACK_CHUNK_S = 5 * 86400          # the service caps /track-range at 7 days
_SOURCE_NAMES = {"T": "Traccar", "D": "Dawarich", "I": "Dawarich-Import"}


def _call(path: str, params: dict, timeout_s: Optional[float] = None):
    """Strict variant of _get(): raises ServiceError instead of returning 'down' (no breaker --
    a user-started fill run should say what is wrong, not silently do nothing)."""
    if requests is None:
        raise ServiceError("python-requests fehlt")
    headers = {"Accept": "application/json"}
    token = _env("GEO_SERVICE_TOKEN")
    if token:
        headers["X-API-Key"] = token
    try:
        r = requests.get(url() + path, params=params, headers=headers, timeout=timeout_s or max(timeout(), 30.0))
    except Exception as e:           # noqa: BLE001
        raise ServiceError(f"{path}: {e}") from None
    if r.status_code != 200:
        raise ServiceError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise ServiceError(f"{path}: keine JSON-Antwort") from None


def health() -> dict:
    return _call("/health", {}, timeout())


def track_range(t0: int, t1: int, sources: str = "traccar,dawarich", devices: str = "default",
                include_import: bool = False) -> list:
    """All points in [t0, t1] as (epoch, lat, lon, source), fetched in <= 5-day chunks."""
    out: list = []
    t = int(t0)
    while t <= t1:
        e = min(int(t1), t + _TRACK_CHUNK_S)
        r = _call("/track-range", {"from": t, "to": e, "sources": sources, "devices": devices,
                                   "include_import": str(include_import).lower()})
        out.extend((p[0], p[1], p[2], _SOURCE_NAMES.get(p[3], p[3])) for p in r["points"])
        t = e + 1
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Weather archive ("shadow database"): backfill + status on the service
# ---------------------------------------------------------------------------

def _json_call(method: str, path: str, body: Optional[dict] = None, timeout_s: float = 60.0):
    headers = {"Accept": "application/json"}
    token = _env("GEO_SERVICE_TOKEN")
    if token:
        headers["X-API-Key"] = token
    try:
        r = requests.request(method, url() + path, json=body, headers=headers, timeout=timeout_s)
    except Exception as e:           # noqa: BLE001
        raise ServiceError(f"{path}: {e}") from None
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:            # noqa: BLE001
            detail = r.text
        raise ServiceError(f"{path}: HTTP {r.status_code} {str(detail)[:300]}")
    return r.json()


def weather_backfill(body: dict) -> dict:
    return _json_call("POST", "/weather/backfill", body, 120.0)      # may carry many points


def weather_status() -> dict:
    return _json_call("GET", "/weather/backfill")


def weather_cancel() -> dict:
    return _json_call("POST", "/weather/backfill/cancel", {})


def weather_stats() -> dict:
    return _json_call("GET", "/weather/stats")


# ---------------------------------------------------------------------------
# Meine Orte (named places of the geo-service's override editor)
# ---------------------------------------------------------------------------

def places() -> list:
    """[{'id','name','kind','poi','lat','lon','radius_m','note'}] -- the service's 'Meine Orte'; [] when off/down."""
    if not enabled():
        return []
    data, state = _get("/places", {})
    return list(data.get("places") or []) if state == "ok" and isinstance(data, dict) else []


def overrides_list() -> list:
    """The service's overrides.json items ([] when off/down) -- used for the sample points of the endpoint page."""
    if not enabled():
        return []
    data, state = _get("/overrides", {})
    return list(data.get("overrides") or []) if state == "ok" and isinstance(data, dict) else []


_ids_cache: dict = {}


def sample_ids(lat: float, lng: float) -> dict:
    """Real (synthetic) geonameIds for an example point, so the endpoint page can offer clickable get/hierarchy/children links:
    {'id': nearest place, 'state': its ADM1, 'county': its ADM2}; {} when the service is off/down. Cached per point."""
    if not enabled():
        return {}
    key = (round(lat, 4), round(lng, 4))
    if key in _ids_cache:
        return _ids_cache[key]
    out: dict = {}
    data, state = _get("/findNearbyPlaceNameJSON", {"lat": lat, "lng": lng, "maxRows": 1})
    rows = (data or {}).get("geonames") if state == "ok" and isinstance(data, dict) else None
    if rows:
        out["id"] = rows[0].get("geonameId")
        h, st = _get("/hierarchyJSON", {"geonameId": out["id"]})
        for r in ((h or {}).get("geonames") or []) if st == "ok" and isinstance(h, dict) else []:
            if r.get("fcode") == "ADM1":
                out["state"] = r.get("geonameId")
            elif r.get("fcode") == "ADM2":
                out["county"] = r.get("geonameId")
        if out.get("id") is not None and len(_ids_cache) < 200:
            _ids_cache[key] = out
    return out
