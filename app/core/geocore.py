"""
geocore.py — reverse-geocoding and GPS-track lookup core, ported from
Set-GPS-Data-via-traccar-and-dawarich.sh.

Scope of this first port (deliberately narrower than the full CLI script): given a
file's existing GPS (if any) or a capture time to look up against a merged Traccar +
Dawarich track, resolve a complete, human-readable location (country/state/county/city/
locality/street + ISO alpha-3 country code) via a Photon -> Nominatim -> Pelias ->
maritime-backup reverse-geocoding chain, exactly like the CLI script's
reverse_geocode_lookup(). AI descriptions, NFO generation, DJI .srt extraction,
interactive prompts, sibling-offset time-zone detection, and exiftool's own -geotag/GPX
matching engine (with its MakerNotes-retry and tiered-radius logic) are NOT ported here --
those remain CLI-only for now. The track-lookup here uses a straightforward Python
nearest-in-time match instead (same idea as the CLI's resolve_geo_fields()/
get_lookup_position() awk-based nearest-row search, just in Python).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from . import exifpool
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import timecore

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

HTTP_TIMEOUT = 5
FETCH_TIMEOUT = 60
EXIFTOOL_TIMEOUT = 60

# Tiered search radius for matching a capture time to the nearest track point, same two
# tiers as the CLI script's EXT_SECS (600s) / EXT_SECS_TIER2 (EXT_HOURS*3600, default 6h):
# try the tight window first, only widen if nothing was found within it.
DEFAULT_MAX_GAP_SECS = 600
DEFAULT_MAX_GAP_SECS_TIER2 = 6 * 3600


# ---------------------------------------------------------------------------
# GPS presence / sanity
# ---------------------------------------------------------------------------

def has_real_gps(lat, lon) -> bool:
    """Same check as the CLI's has_real_gps(): both coordinates present and not the
    (0, 0) null-island sentinel some cameras/phones write when they have no fix."""
    if lat is None or lon is None:
        return False
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if la == 0 and lo == 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Reverse geocoding chain: Photon -> Nominatim -> Pelias -> maritime backup
# ---------------------------------------------------------------------------

@dataclass
class GeoResult:
    country: str = ""
    country_a: str = ""   # ISO 3166-1 alpha-3
    state: str = ""
    county: str = ""
    city: str = ""
    locality: str = ""
    street: str = ""
    source: str = ""      # which backend actually answered (for the preview/logs)

    @property
    def sublocation(self) -> str:
        """Mirrors the CLI's write_rg_location_fields() sublocation composition:
        "<locality>, <street>" when both are present, else whichever one is."""
        if self.locality and self.street:
            return f"{self.locality}, {self.street}"
        return self.locality or self.street

    @property
    def is_empty(self) -> bool:
        return not (self.country or self.city)

    def to_dict(self) -> dict:
        d = {f: getattr(self, f) for f in
             ("country", "country_a", "state", "county", "city", "locality", "street", "source")}
        d["sublocation"] = self.sublocation
        return d


def _get_json(url: str, headers: Optional[dict] = None) -> Optional[dict]:
    if requests is None:
        return None
    try:
        resp = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _try_photon(lat: float, lon: float, server: str, lang: str) -> Optional[GeoResult]:
    url = f"{server.rstrip('/')}/reverse?lon={lon}&lat={lat}&lang={lang}"
    data = _get_json(url)
    if not data:
        return None
    try:
        props = data["features"][0]["properties"]
    except (KeyError, IndexError, TypeError):
        return None
    country = props.get("country") or ""
    city = props.get("city") or ""
    if not (country or city):
        return None
    return GeoResult(
        country=country, city=city,
        locality=props.get("locality") or props.get("name") or "",
        county=props.get("county") or "",
        state=props.get("state") or "",
        street=props.get("street") or "",
        country_a=_alpha3_from_code(props.get("countrycode")),
        source="Photon",
    )


def _try_nominatim(lat: float, lon: float, server: str, lang: str) -> Optional[GeoResult]:
    url = f"{server.rstrip('/')}/reverse?lat={lat}&lon={lon}&format=jsonv2&accept-language={lang}"
    data = _get_json(url, headers={"User-Agent": "metadata-suite/gps-sidecar"})
    if not data:
        return None
    addr = data.get("address") or {}
    country = addr.get("country") or ""
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("suburb") or ""
    if not (country or city):
        return None
    return GeoResult(
        country=country, city=city,
        locality=addr.get("locality") or data.get("name") or addr.get("road") or "",
        county=addr.get("county") or "",
        state=addr.get("state") or "",
        street=addr.get("road") or "",
        country_a=_alpha3_from_code(addr.get("country_code")),
        source="Nominatim",
    )


def _try_pelias(lat: float, lon: float, server: str, lang: str) -> Optional[GeoResult]:
    url = f"{server.rstrip('/')}/v1/reverse?point.lat={lat}&point.lon={lon}&lang={lang}"
    data = _get_json(url)
    if not data:
        return None
    try:
        props = data["features"][0]["properties"]
    except (KeyError, IndexError, TypeError):
        return None
    country = props.get("country") or ""
    city = props.get("locality") or props.get("county") or ""
    if not (country or city):
        return None
    return GeoResult(
        country=country, city=city,
        locality=props.get("name") or "",
        county=props.get("county") or "",
        state=props.get("region") or "",
        street=props.get("street") or "",
        country_a=props.get("country_a") or "",
        source="Pelias",
    )


def _try_pelias_country_a_only(lat: float, lon: float, server: str) -> str:
    """Mirrors the CLI's final fallback: if no country_a was found yet and no
    ALPHA3_VIA_PYTHON mode is forced, ask Pelias one more time just for country_a."""
    url = f"{server.rstrip('/')}/v1/reverse?point.lat={lat}&point.lon={lon}"
    data = _get_json(url)
    if not data:
        return ""
    try:
        return data["features"][0]["properties"].get("country_a") or ""
    except (KeyError, IndexError, TypeError):
        return ""


_NO_WATER_NAME = {"unbekannt", "unknown", "unnamed", "n/a", "none", "null", "-", "inconnu", "desconocido", "sconosciuto"}
_SKIP_WATER_LAYERS = {"seavox", "eez", "territorial_sea", "contiguous_zone", "internal_waters", "archipelagic_waters"}
_FEATURE_NEAR_KM = 10


def _water_area(contained, lang: str) -> str:
    cand = [c for c in (contained or []) if isinstance(c, dict) and c.get("name") and c.get("layer") not in _SKIP_WATER_LAYERS]
    cand.sort(key=lambda c: {"iho": 0, "ne_marine": 1}.get(c.get("layer"), 9))
    want = (lang or "")[:2].lower()
    same = [c for c in cand if (c.get("lang") or "")[:2].lower() == want] if want else []
    return str(((same or cand or [None])[0] or {}).get("name", "")).strip()


def _try_maritime(lat: float, lon: float, server: str, lang: str) -> Optional[GeoResult]:
    """Self-hosted maritime reverse-geocoding backup for coordinates out at sea, where
    Photon/Nominatim/Pelias usually have nothing (named bodies of water, e.g. a cruise).
    Country = the sea the point lies in (contained_in, asked language), Location/locality = the nearest named
    feature (bay, strait) if within 10 km, else the sea again -- same mapping as the geo-service."""
    url = f"{server.rstrip('/')}/reverse?lat={lat}&lon={lon}&lang={lang}"
    data = _get_json(url)
    if not isinstance(data, dict):
        return None
    feature = str(data.get("name") or "").strip()
    if feature.casefold() in _NO_WATER_NAME:
        feature = ""
    dist = data.get("distance_km")
    if feature and isinstance(dist, (int, float)) and dist > _FEATURE_NEAR_KM:
        feature = ""
    area = _water_area(data.get("contained_in"), lang)
    if not (feature or area):
        return None
    return GeoResult(country=area or feature, locality=feature or area, source="Maritim-Backup")


def service_source_label(data: dict) -> str:
    """Where a geo-service answer really came from, for the 'via ...' badge: its own cache (and which geocoder filled it
    originally), a fresh lookup, or a hand-made override."""
    src = (data.get("source") or "").strip()
    if data.get("override"):
        return f"Override \u201e{data['override']}\u201c (geo-service)"
    if data.get("cached"):
        return f"geo-service-Cache, urspr\u00fcnglich {src}" if src else "geo-service-Cache"
    return f"{src} frisch, \u00fcber geo-service" if src else "geo-service"


def reverse_geocode(lat: float, lon: float, *, photon_server: str = "", nominatim_server: str = "",
                     pelias_server: str = "", maritime_server: str = "", lang: str = "de",
                     nocache: bool = False) -> Optional[GeoResult]:
    """Same priority chain as the CLI's reverse_geocode_lookup(): Photon first, then
    Nominatim, then Pelias, then (only if all three came back empty -- the normal case
    out on open water) the maritime backup. Returns None if nothing configured/reachable
    had anything for these coordinates.

    With GEO_SERVICE_URL set, the standalone geo-service is asked first (shared cache + circuit
    breaker, same chain). Only when that service is DOWN does the local chain below run;
    "nothing found" from the service is a real answer and is not repeated against the same
    backends -- except the maritime backup, which is always asked as the last resort (open sea,
    coastal waters) when MARITIME_SERVER is set here."""
    from . import geoservice
    svc_down = False
    if geoservice.enabled():
        status, data = geoservice.reverse(lat, lon, lang, nocache)
        svc_down = status == "down"
        if status == "hit":
            res = GeoResult(**{k: data.get(k) or "" for k in
                               ("country", "country_a", "state", "county", "city", "locality", "street", "source")})
            res.source = service_source_label(data)
            return res
        if status == "empty":
            # Final for Photon/Nominatim/Pelias, but the service may run without MARITIME_SERVER (e.g. a
            # standalone instance elsewhere): at sea / near the coast the maritime backup is still asked.
            return _try_maritime(lat, lon, maritime_server, lang) if maritime_server else None
    result = None
    if photon_server:
        result = _try_photon(lat, lon, photon_server, lang)
    if result is None and nominatim_server:
        result = _try_nominatim(lat, lon, nominatim_server, lang)
    if result is None and pelias_server:
        result = _try_pelias(lat, lon, pelias_server, lang)
    if result is None and maritime_server:
        result = _try_maritime(lat, lon, maritime_server, lang)

    if result is not None and svc_down:
        result.source = f"{result.source} direkt (geo-service nicht erreichbar)"
    if result is not None and not result.country_a and pelias_server and not result.source.startswith("Pelias"):
        # Same supplemental lookup the CLI does when a non-Pelias backend answered but
        # didn't give us an alpha-3 code (Photon/Nominatim only expose alpha-2).
        result.country_a = _try_pelias_country_a_only(lat, lon, pelias_server)

    return result


def _alpha3_from_code(alpha2: Optional[str]) -> str:
    if not alpha2:
        return ""
    return ALPHA2_TO_ALPHA3.get(alpha2.strip().upper(), "")


def alpha2_to_alpha3(alpha2: str) -> str:
    """Standalone lookup (equivalent to the CLI's alpha2_to_alpha3.py helper), exposed
    for reuse -- a static ISO 3166-1 table, so no external script/process needed."""
    return _alpha3_from_code(alpha2)


# ---------------------------------------------------------------------------
# GPS track sources: Traccar + Dawarich, merged and searched for the nearest point
# in time to a file's capture time (same intent as the CLI's GEO_LOOKUP_FILE/
# resolve_geo_fields()/get_lookup_position(), done here with a plain Python list
# instead of a temp-file-plus-awk pipeline).
# ---------------------------------------------------------------------------

@dataclass
class TrackPoint:
    epoch: int
    lat: float
    lon: float
    source: str = ""  # "Traccar" or "Dawarich", for the preview


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_traccar_track(traccar_url: str, token: str, device_ids: list[str],
                         from_dt: datetime, to_dt: datetime, timeout: int = FETCH_TIMEOUT) -> list[TrackPoint]:
    """GET {traccar_url}/api/positions/gpx?deviceId=...&from=...&to=... per device
    (Bearer token auth), same endpoint/params as the CLI script. Parses the returned
    GPX <trkpt lat lon><time> points. A device that errors or returns nothing is
    skipped, same as the CLI (errors are collected, not raised)."""
    if requests is None or not traccar_url or not token:
        return []
    points: list[TrackPoint] = []
    from_iso, to_iso = _iso(from_dt), _iso(to_dt)
    for dev in device_ids:
        dev = dev.strip()
        if not dev:
            continue
        url = f"{traccar_url.rstrip('/')}/api/positions/gpx?deviceId={dev}&from={from_iso}&to={to_iso}"
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
            if resp.status_code != 200 or not resp.content:
                continue
            points.extend(_parse_gpx(resp.content, source="Traccar"))
        except Exception:
            continue
    return points


_GPX_NS_STRIP = True  # the CLI strips the default xmlns before parsing with xmlstarlet


def _parse_gpx(content: bytes, source: str) -> list[TrackPoint]:
    try:
        text = content.decode("utf-8", errors="replace")
        # Strip the default namespace declaration so plain tag names (trkpt/time) work
        # without namespace-qualified lookups -- same effect as the CLI's
        # `sed 's/ xmlns="[^"]*"//'` before piping into xmlstarlet.
        text = text.replace('xmlns="http://www.topografix.com/GPX/1/1"', "")
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for trkpt in root.iter("trkpt"):
        lat_s, lon_s = trkpt.get("lat"), trkpt.get("lon")
        time_el = trkpt.find("time")
        if lat_s is None or lon_s is None or time_el is None or not time_el.text:
            continue
        try:
            lat, lon = float(lat_s), float(lon_s)
            dt = _parse_iso(time_el.text)
        except (ValueError, TypeError):
            continue
        if dt is None:
            continue
        out.append(TrackPoint(epoch=int(dt.timestamp()), lat=lat, lon=lon, source=source))
    return out


def _parse_iso(s: str) -> Optional[datetime]:
    s = s.strip()
    try:
        if s.endswith("Z"):
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_dawarich_track(server: str, api_key: str, from_dt: datetime, to_dt: datetime,
                          timeout: int = FETCH_TIMEOUT, label: str = "Dawarich") -> list[TrackPoint]:
    """Pages through {server}/api/v1/points?api_key=...&start_at=...&end_at=...&
    order=asc&page=N, same endpoint/pagination (X-Total-Pages header) as the CLI's
    fetch_dawarich_points_into(). Returns an empty list on any error."""
    if requests is None or not server or not api_key:
        return []
    from_iso, to_iso = _iso(from_dt), _iso(to_dt)
    points: list[TrackPoint] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        url = (f"{server.rstrip('/')}/api/v1/points?api_key={api_key}"
               f"&start_at={from_iso}&end_at={to_iso}&order=asc&page={page}")
        try:
            resp = requests.get(url, timeout=timeout)
        except Exception:
            break
        if resp.status_code != 200:
            break
        if page == 1:
            try:
                total_pages = max(1, int(resp.headers.get("X-Total-Pages", "1")))
            except ValueError:
                total_pages = 1
        try:
            data = resp.json()
        except ValueError:
            break
        for item in data or []:
            lat, lon, ts = item.get("latitude"), item.get("longitude"), item.get("timestamp")
            if lat is None or lon is None or ts is None:
                continue
            try:
                points.append(TrackPoint(epoch=int(ts), lat=float(lat), lon=float(lon), source=label))
            except (TypeError, ValueError):
                continue
        page += 1
    return points


class SortedTrack(list):
    """A time-sorted list of TrackPoint that remembers its epoch column, so nearest_point() can
    bisect (O(log n)) instead of scanning the whole track for every file -- matters once a track
    spans weeks (100k+ points) and a folder has thousands of files."""
    _epochs: Optional[list] = None

    def epochs(self) -> list:
        if self._epochs is None or len(self._epochs) != len(self):
            self._epochs = [p.epoch for p in self]
        return self._epochs


def merge_tracks(*tracks: list[TrackPoint]) -> list[TrackPoint]:
    """Merge several tracks into one time-sorted track; identical points (same time, position and
    source, e.g. from overlapping fetch windows) are kept once."""
    merged = SortedTrack()
    seen = set()
    for t in tracks:
        for p in t:
            key = (p.epoch, p.lat, p.lon, p.source)
            if key in seen:
                continue
            seen.add(key)
            merged.append(p)
    merged.sort(key=lambda p: p.epoch)
    return merged


DEFAULT_TRACK_PAD_SECS = 12 * 3600
DEFAULT_TRACK_BRIDGE_DAYS = 3
DEFAULT_TRACK_MAX_SPAN_DAYS = 31


def _env_days(name: str, default: int) -> int:
    try:
        return max(1, int((os.environ.get(name) or "").strip().strip("\"'") or default))
    except ValueError:
        return default


def plan_track_windows(epochs: list[int], pad_secs: int = DEFAULT_TRACK_PAD_SECS,
                       bridge_days: Optional[int] = None, max_span_days: Optional[int] = None
                       ) -> list[tuple[datetime, datetime]]:
    """How many track requests does a set of capture times need, and for which time windows?

    Instead of one request per day (many small GPX files, each cut at midnight -- a photo at 23:58
    can't see the point at 00:05) the capture times are grouped:
      * days that are at most `bridge_days` apart share ONE window (a trip / month folder = one request),
      * a bigger gap starts a new window (no weeks of unneeded points between two separate trips),
      * a window longer than `max_span_days` is cut into several requests (keeps each response a size
        the Traccar/Dawarich API answers within the timeout).
    Every window is padded by `pad_secs` on both sides so edge photos still find neighbours.
    Env: GPS_TRACK_BRIDGE_DAYS (default 3), GPS_TRACK_MAX_SPAN_DAYS (default 31)."""
    if not epochs:
        return []
    bridge = (bridge_days if bridge_days is not None else _env_days("GPS_TRACK_BRIDGE_DAYS", DEFAULT_TRACK_BRIDGE_DAYS)) * 86400
    span = (max_span_days if max_span_days is not None else _env_days("GPS_TRACK_MAX_SPAN_DAYS", DEFAULT_TRACK_MAX_SPAN_DAYS)) * 86400
    pts = sorted(set(int(e) for e in epochs))
    clusters: list[list[int]] = [[pts[0], pts[0]]]
    for e in pts[1:]:
        cur = clusters[-1]
        if e - cur[1] > bridge:
            clusters.append([e, e])
        else:
            cur[1] = e
    windows: list[tuple[int, int]] = []
    for lo, hi in clusters:
        start = lo
        while hi - start > span:
            windows.append((start, start + span))
            start += span
        windows.append((start, hi))
    return [(datetime.fromtimestamp(a - pad_secs, tz=timezone.utc), datetime.fromtimestamp(b + pad_secs, tz=timezone.utc))
            for a, b in windows]


def nearest_point(track: list[TrackPoint], capture_epoch: int,
                   max_gap_secs: int = DEFAULT_MAX_GAP_SECS_TIER2) -> Optional[tuple[TrackPoint, int]]:
    """Nearest-in-time match against a (pre-sorted) merged track, same intent as the
    CLI's get_lookup_position()/resolve_geo_fields() awk-based nearest-row search.
    Returns (point, diff_secs) or None if the track is empty or the closest point is
    further away than max_gap_secs."""
    if not track:
        return None
    if isinstance(track, SortedTrack):
        ep = track.epochs()
        i = bisect_left(ep, capture_epoch)
        cands = [track[j] for j in (i - 1, i) if 0 <= j < len(track)]
        best = min(cands, key=lambda p: abs(p.epoch - capture_epoch))
    else:
        best = min(track, key=lambda p: abs(p.epoch - capture_epoch))
    diff = abs(best.epoch - capture_epoch)
    if diff > max_gap_secs:
        return None
    return best, diff


# ---------------------------------------------------------------------------
# Writing: XMP sidecar (always) + direct image embedding (optional, images only), same
# tag mapping as the CLI's write_rg_location_fields()/write_geo_xmp_for_file(). Videos
# only ever get the sidecar -- never direct file modification -- same convention as the
# rest of the CLI toolchain.
# ---------------------------------------------------------------------------

def _exiftool_write(args: list[str], path: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    if not args:
        return False, "nichts zu schreiben"
    cmd = ["exiftool", "-m", "-overwrite_original", "-api", "LargeFileSupport=1", *args, path]
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=timeout)
        stderr = (res.stderr or "").strip()
        ok = res.returncode == 0 and not _has_hard_error(stderr)
        return ok, stderr or (res.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"exiftool-Timeout ({timeout}s)"
    except Exception as e:
        return False, str(e)


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _sidecar_attempts(is_video: bool, size_mb: float) -> list:
    """[(extract_embedded, fast), ...] to try in order. `-ee` (read every sample of embedded timed-metadata
    streams) is what makes exiftool crawl through a multi-hundred-MB video, over a NAS mount often for
    minutes -- it is only used for files up to XMP_EE_MAX_MB (default 100). Big videos get `-fast2`
    (no trailer scan); a failed first attempt falls back to the plain light command."""
    if not is_video:
        return [(False, False)]
    first = (size_mb <= _env_num("XMP_EE_MAX_MB", 100), size_mb > 50)
    return [first] if first == (False, True) else [first, (False, True)]


def _has_hard_error(stderr: str) -> bool:
    """True only for a real exiftool error ("Error: ..."). A "Warning: Error rebuilding maker notes (may be
    corrupt)" is a harmless warning about one vendor block -- the sidecar written next to it is fine."""
    return any(ln.strip().lower().startswith("error") for ln in (stderr or "").splitlines())


def ensure_xmp_sidecar_ex(file: str, xmp_path: str, is_video: bool, timeout: int = EXIFTOOL_TIMEOUT) -> tuple:
    """(ok, message). Creates (or refreshes, carrying over the file's own tags) the .xmp sidecar, same as the
    CLI's ensure_video_xmp_sidecar()/ensure_image_xmp_sidecar() -- but fast for huge videos: no -ee above
    XMP_EE_MAX_MB, a timeout that grows with the file size (60 s + 0.25 s/MB, at most XMP_TIMEOUT_MAX = 1800 s)
    and a light fallback attempt, so a 2 GB video no longer ends in "Konnte XMP-Sidecar nicht erstellen"."""
    try:
        size_mb = os.path.getsize(file) / 1e6
    except OSError:
        size_mb = 0.0
    t_eff = int(min(max(timeout, 60 + size_mb * 0.25), max(timeout, _env_num("XMP_TIMEOUT_MAX", 1800))))
    existed = os.path.exists(xmp_path)
    last_err = ""
    for use_ee, fast in _sidecar_attempts(is_video, size_mb):
        cmd = ["exiftool", "-m", "-q", "-api", "LargeFileSupport=1"]
        if is_video:
            cmd += ["-api", "QuickTimeUTC=0"]
            if use_ee:
                cmd += ["-ee"]
            if fast:
                cmd += ["-fast2"]
        if existed:
            cmd += ["-tagsFromFile", file, "-all:all", "--XMP-dc:Subject", "--Keywords", "-overwrite_original", xmp_path]
        else:
            cmd += ["-o", xmp_path, file]
        try:
            res = exifpool.run(cmd, capture_output=True, text=True, timeout=t_eff)
            last_err = (res.stderr or "").strip()
            if res.returncode == 124:
                last_err = last_err or f"Timeout nach {t_eff}s"
        except Exception as e:           # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        if os.path.exists(xmp_path) and (existed or not _has_hard_error(last_err)):
            _ensure_xmp_make_model(file, xmp_path, is_video, timeout)
            return True, ""
        if not existed and os.path.exists(xmp_path):
            try:
                os.remove(xmp_path)          # half-written leftover of a failed attempt
            except OSError:
                pass
    return False, (f"{last_err[:200]} (Datei {size_mb:.0f} MB)" if last_err else f"exiftool lieferte kein .xmp (Datei {size_mb:.0f} MB)")


def ensure_xmp_sidecar(file: str, xmp_path: str, is_video: bool, timeout: int = EXIFTOOL_TIMEOUT) -> bool:
    return ensure_xmp_sidecar_ex(file, xmp_path, is_video, timeout)[0]


def write_dates_to_xmp(xmp_path: str, zieldate: str, timeout: int = EXIFTOOL_TIMEOUT) -> bool:
    """Sets the sidecar's capture dates to the resolved local time + offset
    ("YYYY:MM:DD HH:MM:SS+HH:MM") directly, independent of whatever the video's own tags
    say -- so Immich & co. (which read xmp:CreateDate) are right even when the video file
    itself could not be rewritten, or when a raw-UTC QuickTime tag would win the tag copy."""
    ok, _msg = _exiftool_write([f"-XMP-xmp:CreateDate={zieldate}", f"-XMP-xmp:ModifyDate={zieldate}",
                                f"-XMP-exif:DateTimeOriginal={zieldate}", f"-XMP-exif:DateTimeDigitized={zieldate}"],
                               xmp_path, timeout)
    return ok


_OFFSET_RE = re.compile(r"(Z|[+-]\d\d:?\d\d)\s*$")


def xmp_dates_lack_offset(xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> bool:
    """True when the sidecar's capture date is missing or has no UTC offset (e.g. the raw UTC wall clock
    of a QuickTime CreateDate copied over by ensure_xmp_sidecar: '2026-10-04T10:27:29'). Such a date is
    ambiguous: Immich & co. read it as LOCAL time (2 h off in Austria) and the weather/AI context can't use it."""
    try:
        res = exifpool.run(["exiftool", "-m", "-s3", "-XMP-xmp:CreateDate", "-XMP-exif:DateTimeOriginal", xmp_path],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:           # noqa: BLE001
        return False
    vals = [v.strip() for v in (res.stdout or "").splitlines() if v.strip()]
    return not vals or any(not _OFFSET_RE.search(v) for v in vals)


def ensure_xmp_dates_with_offset(xmp_path: str, zieldate: str, timeout: int = EXIFTOOL_TIMEOUT) -> bool:
    """Writes the resolved local capture time WITH offset into the sidecar -- but only when its date has
    no offset yet (an offset-qualified date, e.g. from Timestamp & Rename, is never touched).
    Returns True when it wrote."""
    if not zieldate or not _OFFSET_RE.search(str(zieldate)):
        return False
    if not xmp_dates_lack_offset(xmp_path, timeout):
        # dates are fine; only a missing XMP-exif:DateTimeDigitized (older sidecars) is added, same value as DateTimeOriginal
        try:
            have = exifpool.run(["exiftool", "-m", "-s3", "-XMP-exif:DateTimeDigitized", xmp_path],
                                capture_output=True, text=True, timeout=timeout).stdout.strip()
        except Exception:           # noqa: BLE001
            return False
        if have:
            return False
        return _exiftool_write([f"-XMP-exif:DateTimeDigitized={zieldate}"], xmp_path, timeout)[0]
    return write_dates_to_xmp(xmp_path, str(zieldate), timeout)


_VENDOR_MODEL_RE = re.compile(r"model_name:([A-Za-z0-9_.-]+)")


def derive_make_model_from_vendor_tags(xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT, file: str = "") -> tuple:
    """(make, model) for cameras whose files carry NO Make/Model at all but name themselves in a vendor tag
    that ends up in the sidecar: DJI Neo 2 writes XMP-photoshop:Category =
    'pb_file:dvtm_NEO2.proto;model_name:FC9470;pb_version:...' -> ('DJI', 'FC9470'). The raw model code is
    what camera_names.json then maps to a readable name. Without that tag the Encoder of the file itself is
    used ('DJI NEO2' -> ('DJI', 'NEO2')). ('', '') when nothing is recognised."""
    try:
        res = exifpool.run(["exiftool", "-m", "-s3", "-XMP-photoshop:Category", xmp_path],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:           # noqa: BLE001
        return "", ""
    cat = (res.stdout or "").strip()
    m = _VENDOR_MODEL_RE.search(cat)
    if m:
        return ("DJI" if "dvtm_" in cat.lower() else ""), m.group(1)
    # second hint: the container's Encoder / Encoded_Application ("DJI NEO2") -> ('DJI', 'NEO2')
    if file:
        try:
            enc = exifpool.run(["exiftool", "-m", "-s3", "-Encoder", file], capture_output=True, text=True,
                               timeout=timeout).stdout.strip().splitlines()
        except Exception:           # noqa: BLE001
            enc = []
        for e in enc:
            mm = re.match(r"^(DJI)\s+(\S.*)$", e.strip(), re.I)
            if mm:
                return "DJI", mm.group(2).strip()
    return "", ""


MON_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exiftool_mon.config")


def exiftool_write_mon(args: list, xmp: str, timeout: int = 120) -> tuple:
    """Write to a sidecar with the XMP-mon namespace loaded (-config must come first -> one-shot)."""
    cmd = ["exiftool", "-config", MON_CONFIG, "-m", "-overwrite_original", "-api", "LargeFileSupport=1", *args, xmp]
    try:
        res = exifpool.run(cmd, timeout=timeout)
    except Exception as e:           # noqa: BLE001
        return False, str(e)
    err = (res.stderr or "").strip()
    return res.returncode == 0 and not _has_hard_error(err), err


def write_place_extras(xmp_path: str, lat: Optional[float], lon: Optional[float]) -> dict:
    """Altitude + time zone for the position, added to the sidecar ONLY where missing (never overwrites):
      XMP-exif:GPSAltitude / GPSAltitudeRef   from the file's own EXIF/QuickTime altitude if it has one (camera),
                                              else the geo-service's height (SRTM3 / Open-Meteo)   + XMP-mon:AltitudeSource
      XMP-mon:CtxTimezone                     IANA zone id from the geo-service
    Best effort: no service / no value / exiftool trouble just means nothing is added.
    Returns {"altitude": metres|None, "altitude_source": "camera"|"dem"|"", "timezone": id|""} (only what was ADDED)."""
    out = {"altitude": None, "altitude_source": "", "timezone": ""}
    from . import geoservice
    if lat is None or lon is None or not geoservice.place_extras_enabled() or not os.path.isfile(xmp_path):
        return out
    try:
        cur = _read_json([ "exiftool", "-config", MON_CONFIG, "-j", "-m", "-n", "-XMP-exif:GPSAltitude", "-XMP-mon:CtxTimezone", xmp_path])
        args = []
        if cur.get("GPSAltitude") in (None, ""):
            alt, src = None, ""
            media = xmp_path[:-4] if xmp_path.lower().endswith(".xmp") else ""
            if media and os.path.isfile(media):
                own = _read_json(["exiftool", "-j", "-m", "-n", "-Composite:GPSAltitude", media]).get("GPSAltitude")
                if isinstance(own, (int, float)):
                    alt, src = float(own), "camera"
            if alt is None:
                alt, src = geoservice.elevation(lat, lon), "dem"
            if alt is not None:
                args += [f"-XMP-exif:GPSAltitude={abs(alt):.1f}", f"-XMP-exif:GPSAltitudeRef#={1 if alt < 0 else 0}",
                         f"-XMP-mon:AltitudeSource={src}"]
                out["altitude"], out["altitude_source"] = round(alt, 1), src
        if cur.get("CtxTimezone") in (None, ""):
            tz = geoservice.timezone_id(lat, lon)
            if tz:
                args.append(f"-XMP-mon:CtxTimezone={tz}")
                out["timezone"] = tz
        if args:
            ok, _msg = exiftool_write_mon(args, xmp_path)
            if not ok:
                return {"altitude": None, "altitude_source": "", "timezone": ""}
    except Exception:           # noqa: BLE001 - never let an extra break the main write
        return {"altitude": None, "altitude_source": "", "timezone": ""}
    return out


def _read_json(cmd: list) -> dict:
    res = exifpool.run(cmd, capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    data = json.loads(res.stdout or "[]")
    return data[0] if data else {}


def write_camera_name(xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> dict:
    """Stores the READABLE camera name (own camera_names.json rule > NFO Maker rule > Google device list > raw)
    as XMP-mon:CameraName (+ CameraNameSource) next to the raw, standard XMP-tiff:Make/Model that
    ensure_xmp_sidecar() already put there. Returns {"ok", "name", "source", "make", "model", "error"}."""
    from . import devicenames
    out = {"ok": False, "name": "", "source": "", "make": "", "model": "", "error": ""}
    try:
        res = exifpool.run(["exiftool", "-m", "-j", "-XMP-tiff:Make", "-XMP-tiff:Model", xmp_path],
                           capture_output=True, text=True, timeout=timeout)
        rec = (json.loads(res.stdout or "[]") or [{}])[0]
    except Exception as e:           # noqa: BLE001
        out["error"] = str(e)
        return out
    make, model = str(rec.get("Make") or "").strip(), str(rec.get("Model") or "").strip()
    out["make"], out["model"] = make, model
    if not make and not model:
        out["error"] = "keine Kamera im Sidecar (weder Make noch Model)"
        return out
    t = devicenames.translate(make, model)
    out["name"], out["source"] = t["label"], t["source"]
    ok, err = exiftool_write_mon([f"-XMP-mon:CameraName={t['label']}", f"-XMP-mon:CameraNameSource={t['source']}"],
                                 xmp_path, timeout)
    out["ok"], out["error"] = ok, "" if ok else err
    return out


def _ensure_xmp_make_model(file: str, xmp_path: str, is_video: bool, timeout: int) -> None:
    """Makes sure the sidecar carries the camera's Make/Model (XMP-tiff:Make/Model, the
    fields Immich and other viewers read), whenever the tag copy above didn't already bring
    them along. A camera with no Make/Model metadata at all (Insta360 Luna Ultra videos) is
    identified by timecore.sniff_camera_from_bytes -- the same byte-signature check the
    Timestamp & Rename camera filter uses. Never overwrites a Make/Model already in the
    sidecar."""
    try:
        have = exifpool.run(["exiftool", "-s3", "-XMP-tiff:Make", "-XMP-tiff:Model", xmp_path],
                              capture_output=True, text=True, timeout=timeout).stdout.strip()
        if have:
            return
        tags = exifpool.run(["exiftool", "-j", "-Make", "-Model", file],
                              capture_output=True, text=True, timeout=timeout).stdout
        rec = (json.loads(tags or "[]") or [{}])[0]
        make, model = (rec.get("Make") or "").strip(), (rec.get("Model") or "").strip()
        if not make and not model and is_video:
            make, model = timecore.sniff_camera_from_bytes(file)
        if not make and not model:
            make, model = derive_make_model_from_vendor_tags(xmp_path, timeout, file)
        args = []
        if make:
            args.append(f"-XMP-tiff:Make={make}")
        if model:
            args.append(f"-XMP-tiff:Model={model}")
        if args:
            _exiftool_write(args, xmp_path, timeout)
    except Exception:
        pass


def _location_args(prefix_photoshop: str, prefix_iptc_core: str, geo: Optional[GeoResult],
                    extra_iptc: bool = False) -> list[str]:
    """Builds the -TAG=value exiftool args for a GeoResult, shared by the XMP-only and
    XMP+direct-image writers below so the two never drift apart in which fields they set."""
    args: list[str] = []
    if geo is None:
        return args
    if geo.country:
        args.append(f"-{prefix_photoshop}:Country={geo.country}")
        if extra_iptc:
            args.append(f"-IPTC:Country-PrimaryLocationName={geo.country}")
    if geo.country_a:
        args.append(f"-{prefix_iptc_core}:CountryCode={geo.country_a}")
    state_val = geo.state or geo.county
    if state_val:
        args.append(f"-{prefix_photoshop}:State={state_val}")
        if extra_iptc:
            args.append(f"-IPTC:Province-State={state_val}")
    if geo.city:
        args.append(f"-{prefix_photoshop}:City={geo.city}")
        if extra_iptc:
            args.append(f"-IPTC:City={geo.city}")
    sub = geo.sublocation
    if sub:
        args.append(f"-{prefix_iptc_core}:Location={sub}")
        if extra_iptc:
            args.append(f"-IPTC:Sub-location={sub}")
    return args


def write_location_to_xmp(xmp_path: str, lat: Optional[float], lon: Optional[float],
                           geo: Optional[GeoResult], timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    """Writes GPS + resolved location into the .xmp sidecar (XMP-exif:GPSLatitude/
    Longitude, XMP-photoshop:Country/State/City, XMP-iptcCore:CountryCode/Location) --
    same tag set as the CLI's write_geo_xmp_for_file()/write_rg_location_fields()."""
    args = []
    if lat is not None and lon is not None:
        args += [f"-XMP-exif:GPSLatitude={lat}", f"-XMP-exif:GPSLongitude={lon}"]
    args += _sidecar_location_args(geo)
    kw = _geo_keyword_args(lat, lon, geo)
    if kw:
        # Cleared in its own exiftool call first -- the original script found a combined
        # clear + rebuild in a single invocation duplicated keywords.
        _exiftool_write(["-XMP-dc:Subject="], xmp_path, timeout)
        args += kw[1:]
    if lat is not None and lon is not None:
        args.append("-XMP-photoshop:TransmissionReference=")   # clears a "no GPS found" marker
    return _exiftool_write(args, xmp_path, timeout)


def _sidecar_location_args(geo: Optional[GeoResult]) -> list[str]:
    """Sidecar location fields exactly as the original script writes them (and as NFO Maker
    reads them back): State, County -> iptcExt:LocationCreatedProvinceState, City,
    Locality -> iptcCore:Location, Street -> iptcExt:LocationCreatedSublocation."""
    args: list[str] = []
    if geo is None:
        return args
    if geo.country:
        args.append(f"-XMP-photoshop:Country={geo.country}")
    if geo.country_a:
        args.append(f"-XMP-iptcCore:CountryCode={geo.country_a}")
    if geo.state:
        args.append(f"-XMP-photoshop:State={geo.state}")
    if geo.county:
        args.append(f"-XMP-iptcExt:LocationCreatedProvinceState={geo.county}")
    if geo.city:
        args.append(f"-XMP-photoshop:City={geo.city}")
    if geo.locality:
        args.append(f"-XMP-iptcCore:Location={geo.locality}")
    if geo.street:
        args.append(f"-XMP-iptcExt:LocationCreatedSublocation={geo.street}")
    return args


def _geo_keyword_args(lat, lon, geo: Optional[GeoResult]) -> list[str]:
    """XMP-dc:Subject geo keywords as in the original script: geo:lat=, geo:lon=, geotagged,
    then country code/country/county/city/locality/street, de-duplicated. The list is reset
    first so a re-run never accumulates stale values."""
    if lat is None or lon is None:
        return []
    values = [f"geo:lat={lat}", f"geo:lon={lon}", "geotagged"]
    if geo is not None:
        # the state joins only where there is no city (at sea it holds the sea next to a land country: "Mittelmeer")
        for cand in (geo.country_a, geo.country, geo.county, geo.city, geo.locality, geo.street, "" if geo.city else geo.state):
            if cand and cand not in values:
                values.append(cand)
    return ["-XMP-dc:Subject="] + [f"-XMP-dc:Subject+={v}" for v in values]


def write_location_to_image(file: str, lat: Optional[float], lon: Optional[float],
                             geo: Optional[GeoResult], timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    """Writes GPS + resolved location directly into an image file (IPTC + XMP mirrored,
    same as write_rg_location_fields()'s non-sidecar-only branch). Never call this for
    videos -- they only ever get the .xmp sidecar, same convention as the rest of the
    CLI toolchain (direct video modification is considered too risky/slow to redo).

    NOT used by gps_sidecar/router.py's own execute() anymore -- that now calls
    xmpsync.sync_xmp_to_image() instead, which embeds everything currently in the .xmp
    sidecar (this function's own GPS/location fields included) in one call, rather than
    only this one module's slice. Kept here as the focused, single-purpose primitive
    xmpsync.py's full sync is built from conceptually; no current caller uses it
    directly."""
    args = []
    if lat is not None and lon is not None:
        la, lo = float(lat), float(lon)
        args += [f"-GPSLatitude={abs(la)}", f"-GPSLatitudeRef={'S' if la < 0 else 'N'}",
                 f"-GPSLongitude={abs(lo)}", f"-GPSLongitudeRef={'W' if lo < 0 else 'E'}"]
    args += _location_args("XMP-photoshop", "XMP-iptcCore", geo, extra_iptc=True)
    return _exiftool_write(args, file, timeout)


def read_xmp_location(xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[str, str]:
    """Reads back (Country, City) from an existing .xmp sidecar, if any -- used to skip
    re-reverse-geocoding a file that's already been fully processed on an earlier run
    (same idea as the CLI's -LM/--location-fill-missing "already has a location -> skip"
    check), so repeat browses of the same folder don't keep hitting the geocoding servers
    for files that are already done."""
    if not os.path.isfile(xmp_path):
        return "", ""
    cmd = ["exiftool", "-m", "-T", "-XMP-photoshop:Country", "-XMP-photoshop:City", xmp_path]
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=timeout)
        parts = (res.stdout or "").rstrip("\n").split("\t")
        country = parts[0] if len(parts) > 0 and parts[0] != "-" else ""
        city = parts[1] if len(parts) > 1 and parts[1] != "-" else ""
        return country, city
    except Exception:
        return "", ""


# ---------------------------------------------------------------------------
# ISO 3166-1 alpha-2 -> alpha-3 country code table (standard, static -- replaces the
# CLI's external alpha2_to_alpha3.py helper script/process with a plain lookup).
# ---------------------------------------------------------------------------
ALPHA2_TO_ALPHA3 = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AG": "ATG", "AI": "AIA", "AL": "ALB", "AM": "ARM",
    "AO": "AGO", "AQ": "ATA", "AR": "ARG", "AS": "ASM", "AT": "AUT", "AU": "AUS", "AW": "ABW",
    "AX": "ALA", "AZ": "AZE", "BA": "BIH", "BB": "BRB", "BD": "BGD", "BE": "BEL", "BF": "BFA",
    "BG": "BGR", "BH": "BHR", "BI": "BDI", "BJ": "BEN", "BL": "BLM", "BM": "BMU", "BN": "BRN",
    "BO": "BOL", "BQ": "BES", "BR": "BRA", "BS": "BHS", "BT": "BTN", "BV": "BVT", "BW": "BWA",
    "BY": "BLR", "BZ": "BLZ", "CA": "CAN", "CC": "CCK", "CD": "COD", "CF": "CAF", "CG": "COG",
    "CH": "CHE", "CI": "CIV", "CK": "COK", "CL": "CHL", "CM": "CMR", "CN": "CHN", "CO": "COL",
    "CR": "CRI", "CU": "CUB", "CV": "CPV", "CW": "CUW", "CX": "CXR", "CY": "CYP", "CZ": "CZE",
    "DE": "DEU", "DJ": "DJI", "DK": "DNK", "DM": "DMA", "DO": "DOM", "DZ": "DZA", "EC": "ECU",
    "EE": "EST", "EG": "EGY", "EH": "ESH", "ER": "ERI", "ES": "ESP", "ET": "ETH", "FI": "FIN",
    "FJ": "FJI", "FK": "FLK", "FM": "FSM", "FO": "FRO", "FR": "FRA", "GA": "GAB", "GB": "GBR",
    "GD": "GRD", "GE": "GEO", "GF": "GUF", "GG": "GGY", "GH": "GHA", "GI": "GIB", "GL": "GRL",
    "GM": "GMB", "GN": "GIN", "GP": "GLP", "GQ": "GNQ", "GR": "GRC", "GS": "SGS", "GT": "GTM",
    "GU": "GUM", "GW": "GNB", "GY": "GUY", "HK": "HKG", "HM": "HMD", "HN": "HND", "HR": "HRV",
    "HT": "HTI", "HU": "HUN", "ID": "IDN", "IE": "IRL", "IL": "ISR", "IM": "IMN", "IN": "IND",
    "IO": "IOT", "IQ": "IRQ", "IR": "IRN", "IS": "ISL", "IT": "ITA", "JE": "JEY", "JM": "JAM",
    "JO": "JOR", "JP": "JPN", "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KI": "KIR", "KM": "COM",
    "KN": "KNA", "KP": "PRK", "KR": "KOR", "KW": "KWT", "KY": "CYM", "KZ": "KAZ", "LA": "LAO",
    "LB": "LBN", "LC": "LCA", "LI": "LIE", "LK": "LKA", "LR": "LBR", "LS": "LSO", "LT": "LTU",
    "LU": "LUX", "LV": "LVA", "LY": "LBY", "MA": "MAR", "MC": "MCO", "MD": "MDA", "ME": "MNE",
    "MF": "MAF", "MG": "MDG", "MH": "MHL", "MK": "MKD", "ML": "MLI", "MM": "MMR", "MN": "MNG",
    "MO": "MAC", "MP": "MNP", "MQ": "MTQ", "MR": "MRT", "MS": "MSR", "MT": "MLT", "MU": "MUS",
    "MV": "MDV", "MW": "MWI", "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM", "NC": "NCL",
    "NE": "NER", "NF": "NFK", "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR", "NP": "NPL",
    "NR": "NRU", "NU": "NIU", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER", "PF": "PYF",
    "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PM": "SPM", "PN": "PCN", "PR": "PRI",
    "PS": "PSE", "PT": "PRT", "PW": "PLW", "PY": "PRY", "QA": "QAT", "RE": "REU", "RO": "ROU",
    "RS": "SRB", "RU": "RUS", "RW": "RWA", "SA": "SAU", "SB": "SLB", "SC": "SYC", "SD": "SDN",
    "SE": "SWE", "SG": "SGP", "SH": "SHN", "SI": "SVN", "SJ": "SJM", "SK": "SVK", "SL": "SLE",
    "SM": "SMR", "SN": "SEN", "SO": "SOM", "SR": "SUR", "SS": "SSD", "ST": "STP", "SV": "SLV",
    "SX": "SXM", "SY": "SYR", "SZ": "SWZ", "TC": "TCA", "TD": "TCD", "TF": "ATF", "TG": "TGO",
    "TH": "THA", "TJ": "TJK", "TK": "TKL", "TL": "TLS", "TM": "TKM", "TN": "TUN", "TO": "TON",
    "TR": "TUR", "TT": "TTO", "TV": "TUV", "TW": "TWN", "TZ": "TZA", "UA": "UKR", "UG": "UGA",
    "UM": "UMI", "US": "USA", "UY": "URY", "UZ": "UZB", "VA": "VAT", "VC": "VCT", "VE": "VEN",
    "VG": "VGB", "VI": "VIR", "VN": "VNM", "VU": "VUT", "WF": "WLF", "WS": "WSM", "YE": "YEM",
    "YT": "MYT", "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
}


# ---------------------------------------------------------------------------
# Sidecar GPS read-back, "no GPS found" marker, DJI .srt, predefined (fixed) locations
# ---------------------------------------------------------------------------

NOGPS_MARKER = "no-gps-found"


def read_xmp_gps(xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[Optional[float], Optional[float], bool]:
    """(lat, lon, nogps_marker) from an existing .xmp sidecar. Numeric (-n) so the sign is
    kept. lat/lon None when absent or (0,0); marker True when an earlier run recorded
    "no GPS found" for this file (see mark_no_gps)."""
    if not os.path.isfile(xmp_path):
        return None, None, False
    cmd = ["exiftool", "-m", "-n", "-T", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude",
           "-XMP-photoshop:TransmissionReference", xmp_path]
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=timeout)
        parts = (res.stdout or "").rstrip("\n").split("\t")
        parts += ["-"] * (3 - len(parts))
        lat = lon = None
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            pass
        if not has_real_gps(lat, lon):
            lat = lon = None
        return lat, lon, parts[2].strip() == NOGPS_MARKER
    except Exception:
        return None, None, False


def mark_no_gps(file: str, xmp_path: str, is_video: bool) -> bool:
    """Remembers "no GPS position could be found" in the sidecar (creating it when needed),
    so the next run skips the file unless 'retry' is requested -- same idea as the original
    script's empty sidecar for no-GPS files, but explicit and self-describing."""
    if not ensure_xmp_sidecar(file, xmp_path, is_video):
        return False
    ok, _ = _exiftool_write([f"-XMP-photoshop:TransmissionReference={NOGPS_MARKER}"], xmp_path)
    return ok


_SRT_COORD_RES = [
    # "[latitude: 48.12345] [longitude: 16.12345]"  (Mavic/Mini/Air older firmware)
    re.compile(r"\[\s*latitude\s*:\s*(-?\d+(?:\.\d+)?)\s*\]\s*\[\s*longitude?\s*:\s*(-?\d+(?:\.\d+)?)\s*\]", re.I),
    # "GPS (16.12345, 48.12345, 22)" -> lon, lat, sat (newer firmware)
    re.compile(r"GPS\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*(?:,\s*-?\d+)?\s*\)", re.I),
]


def parse_dji_srt(srt_path: str) -> Optional[tuple[float, float]]:
    """First valid GPS coordinate in a DJI .srt telemetry file, or None. Handles both
    '[latitude: x] [longitude: y]' and 'GPS (lon, lat, sats)' layouts."""
    try:
        with open(srt_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(512 * 1024)
    except OSError:
        return None
    m = _SRT_COORD_RES[0].search(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if has_real_gps(lat, lon) else None
    for m in _SRT_COORD_RES[1].finditer(text):
        lon, lat = float(m.group(1)), float(m.group(2))
        if has_real_gps(lat, lon) and abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    return None


def find_dji_srt(file: str) -> Optional[str]:
    """The .srt that sits next to a DJI video (same basename, any case)."""
    base = os.path.splitext(file)[0]
    for ext in (".srt", ".SRT", ".Srt"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def find_dji_lrf(file: str) -> Optional[str]:
    """The .lrf low-resolution proxy that DJI cameras write next to a video (same basename, any case)."""
    base = os.path.splitext(file)[0]
    for ext in (".lrf", ".LRF", ".Lrf"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


@dataclass
class FixedPlace:
    name: str        # the place's name ("Meine Orte" of the geo-service; selection key)
    label: str       # human-readable "kind, street, city" for the dropdown
    lat: float
    lon: float
    geo: GeoResult


def all_places() -> list[FixedPlace]:
    """Every fixed place ('-GEO'): the geo-service's 'Meine Orte' (override editor, with map); [] without the service."""
    out: list[FixedPlace] = []
    try:
        from . import geoservice
        svc = geoservice.places()
    except Exception:           # noqa: BLE001
        return out
    names: set = set()
    for p in svc:
        status, rec = geoservice.reverse(p["lat"], p["lon"]) if geoservice.enabled() else ("down", None)
        g = GeoResult(country=(rec or {}).get("country", ""), country_a=(rec or {}).get("country_a", ""),
                      state=(rec or {}).get("state", ""), county=(rec or {}).get("county", ""),
                      city=(rec or {}).get("city", ""), locality=(rec or {}).get("locality", ""),
                      street=(rec or {}).get("street", ""), source=f"Mein Ort: {p['name']}")
        name = p["name"] if p["name"] not in names else f"{p['name']} [{p['id']}]"
        names.add(name)
        label = ", ".join(x for x in (p.get("kind"), g.street, g.city or g.locality) if x) or p["name"]
        out.append(FixedPlace(name, label, float(p["lat"]), float(p["lon"]), g))
    return out


def write_gps_to_image(file: str, lat: float, lon: float, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    """Writes just the GPS position into an image's EXIF (absolute value + explicit Ref
    tags, so southern/western coordinates keep their sign)."""
    la, lo = float(lat), float(lon)
    return _exiftool_write([f"-GPSLatitude={abs(la)}", f"-GPSLatitudeRef={'S' if la < 0 else 'N'}",
                            f"-GPSLongitude={abs(lo)}", f"-GPSLongitudeRef={'W' if lo < 0 else 'E'}"],
                           file, timeout)
