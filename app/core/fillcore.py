"""
fillcore.py -- engine of the "GPS füllen" module: give photos/videos WITHOUT GPS a position from
the GPS track (Traccar/Dawarich, fetched through the geo-service), with interpolation between the
two surrounding track points, a confidence value per file and automatic camera-clock drift
correction.

Port of the standalone gps_fill.py (geo-service/clients/) onto the suite's own infrastructure:
pooled exiftool (core/exifpool), reverse geocoding via geocore (-> geo-service first), the same
sidecar tag mapping as the GPS Sidecar module. The maths itself is core/trackmath.py, which is
byte-identical to the service's copy (tests/test_fill_trackmath_identical.py).

Pipeline
  1. collect(): one batched exiftool pass over the media and one over the .xmp sidecars
  2. resolve_times(): capture time -> UTC epoch + where it came from (basis)
       sidecar (offset-qualified) > EXIF+offset > file name > EXIF without offset;
       a missing offset is taken from the folder's most common one ("dir-offset") or, last resort,
       the default zone ("tz-assumed", wrong for trips abroad -> flagged in the UI)
  3. drift: files that already carry GPS reveal the camera clock error per camera and day
       gps-time    camera clock minus EXIF GPSDateTime (satellite time -> exact)
       track-match the time shift that makes the track pass through the photo's own position
  4. plan: position at the drift-corrected time, confidence + error radius
  5. write: <file>.xmp only (XMP-exif GPS, place names, geo keywords, provenance in XMP-mon:*)
"""
from __future__ import annotations

from . import skipdirs
import os
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from . import exifpool, geocore
from .trackmath import Pt, Track, best_shift, locate

VERSION = "1.0"
DAY = 86400
NOGPS_MARKER = geocore.NOGPS_MARKER if hasattr(geocore, "NOGPS_MARKER") else "no-gps-found"
MON_CONFIG = geocore.MON_CONFIG
CHUNK = 100                          # files per exiftool call (arguments, not stdin: the pool speaks argfiles)

IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "dng", "tif", "tiff", "webp", "arw", "cr2", "cr3", "nef", "orf", "rw2"}
VIDEO_EXT = {"mp4", "mov", "m4v", "mts", "m2ts", "avi", "3gp", "mkv", "insv"}


# ---------------------------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------------------------
@dataclass
class FillOpts:
    drift: str = "auto"              # auto | off | <seconds>
    min_conf: float = 0.3
    gap1: int = 600
    gap2: int = 21600
    max_drift: int = 14 * 3600
    max_shift: int = 900
    drift_min: int = 3
    tz: str = "Europe/Vienna"
    force: bool = False              # also replace GPS already in a sidecar
    retry: bool = False              # also retry files marked "no-gps-found"
    sources: str = "traccar,dawarich"
    devices: str = "default"
    include_import: bool = False
    neighbor: bool = True            # last resort: position of the nearest file with GPS in the same folder on the same day
    neighbor_s: int = 6 * 3600       # ... at most this far away in time (0 = anywhere on that day)


# ---------------------------------------------------------------------------------------------
# exiftool (pooled)
# ---------------------------------------------------------------------------------------------
def _base() -> list[str]:
    return ["exiftool", "-m", "-q", "-api", "LargeFileSupport=1"]


def exiftool_json(tags: list[str], files: list[str], timeout: int = 300, cancel_event=None) -> list[dict]:
    import json
    out: list[dict] = []
    for i in range(0, len(files), CHUNK):
        part = files[i:i + CHUNK]
        res = exifpool.run(_base() + ["-j", "-n", *tags, *part], timeout=timeout, cancel_event=cancel_event)
        if res.stdout.strip():
            try:
                out.extend(json.loads(res.stdout))
            except ValueError:
                pass
    return out


exiftool_write_mon = geocore.exiftool_write_mon      # implementation lives in geocore (shared with the GPS Sidecar module)


# ---------------------------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------------------------
_DT = re.compile(r"^(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$")
_FNAME = re.compile(r"^(\d{4})\.(\d\d)\.(\d\d)-(\d\d)\.(\d\d)\.(\d\d)")


def parse_offset(s) -> Optional[int]:
    """'+02:00' / '-0330' / 'Z' / numeric hours (TimeZoneOffset) -> seconds east of UTC."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return int(round(float(s) * 3600))
    s = str(s).strip()
    if s.upper() == "Z":
        return 0
    m = re.fullmatch(r"([+-])(\d\d):?(\d\d)", s)
    if m:
        v = int(m.group(2)) * 3600 + int(m.group(3)) * 60
        return v if m.group(1) == "+" else -v
    try:
        return int(round(float(s) * 3600))
    except ValueError:
        return None


def parse_dt(s) -> tuple[Optional[datetime], Optional[int]]:
    """'2026:10:03 09:14:27+02:00' -> (naive wall-clock datetime, offset seconds | None)."""
    if not s or not isinstance(s, str):
        return None, None
    m = _DT.match(s.strip())
    if not m or m.group(1) == "0000":
        return None, None
    try:
        naive = datetime(*(int(m.group(i)) for i in range(1, 7)))
    except ValueError:
        return None, None
    return naive, parse_offset(m.group(7)) if m.group(7) else None


def to_epoch(naive: datetime, offset_s: int) -> int:
    return int((naive - timedelta(seconds=offset_s)).replace(tzinfo=timezone.utc).timestamp())


def local_offset(naive: datetime, zone: ZoneInfo) -> int:
    return int(naive.replace(tzinfo=zone).utcoffset().total_seconds())


# ---------------------------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------------------------
@dataclass
class Sidecar:
    path: str
    dto: Optional[str] = None
    create: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    marker: str = ""
    make: str = ""
    model: str = ""
    subject: list = field(default_factory=list)


@dataclass
class Media:
    path: str
    is_video: bool
    make: str = ""
    model: str = ""
    dto: Optional[str] = None
    offset: Optional[int] = None
    lat: Optional[float] = None          # real EXIF GPS only
    lon: Optional[float] = None
    gps_epoch: Optional[int] = None      # EXIF GPSDateTime (UTC from the satellites)
    sidecar: Optional[Sidecar] = None
    epoch: Optional[int] = None          # set by resolve_times()
    basis: str = ""

    @property
    def camera(self) -> str:
        make = self.make or (self.sidecar.make if self.sidecar else "")
        model = self.model or (self.sidecar.model if self.sidecar else "")
        return f"{make} {model}".strip() or "unknown"

    def has_exif_gps(self) -> bool:
        return real_gps(self.lat, self.lon)


def real_gps(lat, lon) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return not (la == 0 and lo == 0) and abs(la) <= 90 and abs(lo) <= 180


def _first(v):
    return v[0] if isinstance(v, list) and v else v


def _num(v) -> Optional[float]:
    try:
        return float(_first(v))
    except (TypeError, ValueError):
        return None


MEDIA_TAGS = ["-DateTimeOriginal", "-OffsetTimeOriginal", "-OffsetTime", "-TimeZoneOffset", "-GPSLatitude",
              "-GPSLongitude", "-GPSDateTime", "-Make", "-Model"]
SIDECAR_TAGS = ["-XMP-exif:DateTimeOriginal", "-XMP-xmp:CreateDate", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude",
                "-XMP-photoshop:TransmissionReference", "-XMP-tiff:Make", "-XMP-tiff:Model", "-XMP-dc:Subject"]


def sidecar_for(path: str) -> Optional[str]:
    """The suite's convention first (<file>.xmp), then <stem>.xmp."""
    for cand in (path + ".xmp", os.path.splitext(path)[0] + ".xmp"):
        if os.path.isfile(cand):
            return cand
    return None


def sidecar_target(path: str) -> str:
    return sidecar_for(path) or path + ".xmp"


def is_media(path: str) -> bool:
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in IMAGE_EXT or ext in VIDEO_EXT


def find_media(root: str, depth: int, types: str = "all") -> list[str]:
    """Photos/videos under root (or the file itself), folder depth like the other modules."""
    want_img, want_vid = types in ("all", "image"), types in ("all", "video")

    def ok(f: str) -> bool:
        ext = os.path.splitext(f)[1].lstrip(".").lower()
        return (ext in IMAGE_EXT and want_img) or (ext in VIDEO_EXT and want_vid)

    def generated(f: str) -> bool:
        """A '<video>-thumb/-poster/-fanart.jpg' next to its video is a by-product of the NFO toolchain,
        never a capture of its own (same rule as Timestamp & Rename). Orphans stay candidates."""
        from . import timecore
        if os.path.splitext(f)[1].lstrip(".").lower() not in IMAGE_EXT:
            return False
        base = timecore.generated_companion_base_stem(f)
        return base is not None and timecore.has_sibling_media(f, base)

    if os.path.isfile(root):
        return [root] if ok(root) and not generated(root) else []
    out: list[str] = []
    base = root.rstrip(os.sep).count(os.sep)
    for d, dirs, names in os.walk(root):
        if d.count(os.sep) - base >= depth:
            dirs[:] = []
        sibling_dirs = list(dirs)
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and not skipdirs.is_skipped_dir(x))
        out.extend(os.path.join(d, n) for n in sorted(names)
                   if not n.startswith(".") and ok(n) and not generated(os.path.join(d, n))
                   and not skipdirs.is_generated_collage(n, sibling_dirs))
    return out


def read_sidecars(media_paths: list[str], cancel_event=None) -> dict[str, Sidecar]:
    pairs = {p: sidecar_for(p) for p in media_paths}
    xmps = sorted({x for x in pairs.values() if x})
    recs = {r["SourceFile"]: r for r in exiftool_json(SIDECAR_TAGS, xmps, cancel_event=cancel_event)} if xmps else {}
    out: dict[str, Sidecar] = {}
    for p, x in pairs.items():
        if not x:
            continue
        r = recs.get(x, {})
        subj = r.get("Subject", [])
        out[p] = Sidecar(path=x, dto=_first(r.get("DateTimeOriginal")), create=_first(r.get("CreateDate")),
                         lat=_num(r.get("GPSLatitude")), lon=_num(r.get("GPSLongitude")),
                         marker=str(r.get("TransmissionReference") or ""), make=str(r.get("Make") or ""),
                         model=str(r.get("Model") or ""), subject=subj if isinstance(subj, list) else [subj])
    return out


def collect(files: list[str], camera: str = "", cancel_event=None,
            progress: Optional[Callable[[int, int], None]] = None) -> list[Media]:
    """Metadata + sidecar for every file. `camera` filters here and only here (Make/Model or the
    NFO device alias, same matching as AI-Beschreibung)."""
    from . import devicenames
    recs: dict[str, dict] = {}
    for i in range(0, len(files), CHUNK):
        part = files[i:i + CHUNK]
        for r in exiftool_json(MEDIA_TAGS, part, cancel_event=cancel_event):
            recs[r["SourceFile"]] = r
        if progress:
            progress(min(i + CHUNK, len(files)), len(files))
        if cancel_event is not None and cancel_event.is_set():
            return []
    sidecars = read_sidecars(files, cancel_event)
    media: list[Media] = []
    for f in files:
        r = recs.get(f, {})
        gps_epoch = None
        n, goff = parse_dt(_first(r.get("GPSDateTime")))
        if n is not None:
            gps_epoch = to_epoch(n, goff if goff is not None else 0)           # GPS time is UTC by definition
        off = None
        for tag in ("OffsetTimeOriginal", "OffsetTime", "TimeZoneOffset"):
            if off is None and r.get(tag) not in (None, ""):
                off = parse_offset(_first(r.get(tag)))
        ext = os.path.splitext(f)[1].lstrip(".").lower()
        m = Media(path=f, is_video=ext in VIDEO_EXT, make=str(r.get("Make") or "").strip(),
                  model=str(r.get("Model") or "").strip(), dto=_first(r.get("DateTimeOriginal")), offset=off,
                  lat=_num(r.get("GPSLatitude")), lon=_num(r.get("GPSLongitude")), gps_epoch=gps_epoch,
                  sidecar=sidecars.get(f))
        if camera and not devicenames.camera_matches(
                m.make or (m.sidecar.make if m.sidecar else ""), m.model or (m.sidecar.model if m.sidecar else ""), camera):
            continue
        media.append(m)
    return media


def resolve_times(media: list[Media], default_tz: str = "Europe/Vienna") -> None:
    """Sets .epoch and .basis on every item (epoch stays None when nothing usable exists)."""
    try:
        zone = ZoneInfo(default_tz)
    except Exception:                # noqa: BLE001
        zone = ZoneInfo("Europe/Vienna")
    dir_offsets: dict[str, Counter] = defaultdict(Counter)
    for m in media:
        if m.offset is not None:
            dir_offsets[os.path.dirname(m.path)][m.offset] += 1

    def wall_clock_epoch(m: Media, naive: datetime, what: str) -> tuple[int, str]:
        if m.offset is not None:
            return to_epoch(naive, m.offset), f"{what}+offset"
        common = dir_offsets.get(os.path.dirname(m.path))
        if common:
            return to_epoch(naive, common.most_common(1)[0][0]), f"{what}+dir-offset"
        return to_epoch(naive, local_offset(naive, zone)), f"{what}+tz-assumed"

    for m in media:
        m.epoch, m.basis = None, ""
        sc = m.sidecar
        if sc:
            for raw in (sc.dto, sc.create):
                naive, off = parse_dt(raw)
                if naive is not None and off is not None:
                    m.epoch, m.basis = to_epoch(naive, off), "sidecar"
                    break
            if m.epoch is not None:
                continue
        naive, off = parse_dt(m.dto)
        if naive is not None and (off is not None or m.offset is not None):
            m.epoch, m.basis = to_epoch(naive, off if off is not None else m.offset), "exif+offset"
            continue
        fm = _FNAME.match(os.path.basename(m.path))
        if fm:
            try:
                nv = datetime(*(int(fm.group(i)) for i in range(1, 7)))
                m.epoch, m.basis = wall_clock_epoch(m, nv, "filename")
                continue
            except ValueError:
                pass
        if naive is not None:
            m.epoch, m.basis = wall_clock_epoch(m, naive, "exif")


# ---------------------------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------------------------
@dataclass
class Sample:
    camera: str
    day: int
    drift: int                 # camera clock minus true time, seconds
    method: str                # gps-time | track-match
    path: str = ""


@dataclass
class Drift:
    seconds: int = 0
    method: str = "none"
    n: int = 0
    scope: str = ""            # day | camera | manual | ""
    spread: float = 0.0


def _wmedian(samples: list[Sample]) -> tuple[float, float, int]:
    """(median, MAD, weight): satellite-time samples count double -- exact, while a track match
    is a (good) guess."""
    vals: list[float] = []
    for s in samples:
        vals += [s.drift] * (2 if s.method == "gps-time" else 1)
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    return med, mad, len(vals)


class DriftModel:
    def __init__(self, samples: list[Sample], drift_min: int):
        self.by_cam: dict[str, list[Sample]] = defaultdict(list)
        for s in samples:
            self.by_cam[s.camera].append(s)
        self.drift_min = drift_min

    @staticmethod
    def _tol(med: float) -> float:
        return max(5.0, 0.25 * abs(med))

    def for_file(self, camera: str, epoch: int) -> Drift:
        samples = self.by_cam.get(camera, [])
        day = epoch - epoch % DAY
        same_day = [s for s in samples if s.day == day]
        for scope, group, need in (("day", same_day, 2), ("camera", samples, 3)):
            if not group:
                continue
            med, mad, w = _wmedian(group)
            if w >= need and mad <= self._tol(med):
                sec = int(round(med))
                methods = {s.method for s in group}
                method = ("gps-time" if methods == {"gps-time"} else
                          "track-match" if methods == {"track-match"} else "gps-time+track-match")
                if abs(sec) < self.drift_min:
                    return Drift(0, method, len(group), scope, mad)
                return Drift(sec, method, len(group), scope, mad)
        return Drift()

    def table(self) -> list[dict]:
        rows = []
        for cam, ss in sorted(self.by_cam.items()):
            med, mad, w = _wmedian(ss)
            ok = w >= 3 and mad <= self._tol(med)
            rows.append({"camera": cam, "samples": len(ss), "gps_time": sum(s.method == "gps-time" for s in ss),
                         "track_match": sum(s.method == "track-match" for s in ss), "median_s": int(round(med)),
                         "mad_s": round(mad, 1), "stable": ok, "days": len({s.day for s in ss}),
                         "median_display": fmt_dur(int(round(med)))})
        return rows


def estimate_drift(media: list[Media], track: Track, o: FillOpts) -> DriftModel:
    samples: list[Sample] = []
    for m in media:
        if not (m.has_exif_gps() and m.epoch is not None):
            continue
        day = m.epoch - m.epoch % DAY
        if m.gps_epoch is not None:
            d = m.epoch - m.gps_epoch
            if abs(d) <= o.max_drift:
                samples.append(Sample(m.camera, day, d, "gps-time", m.path))
            continue
        if len(track):
            r = best_shift(track, m.lat, m.lon, m.epoch, max_shift=o.max_shift)
            if r and r["ok"]:
                samples.append(Sample(m.camera, day, -r["shift"], "track-match", m.path))
    return DriftModel(samples, o.drift_min)


def fmt_dur(s: float) -> str:
    s = int(round(s))
    sign = "-" if s < 0 else "+"
    s = abs(s)
    if s >= 3600:
        return f"{sign}{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"
    if s >= 60:
        return f"{sign}{s // 60}m{s % 60:02d}s"
    return f"{sign}{s}s"


# ---------------------------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------------------------
@dataclass
class Plan:
    media: Media
    status: str                    # fill | has-gps | sidecar-has-gps | marked-no-gps | no-time | no-track-point | low-confidence
    reason: str = ""
    drift: Drift = field(default_factory=Drift)
    t_true: Optional[int] = None
    loc: Optional[dict] = None
    geo: Optional[dict] = None     # place fields (GeoResult.to_dict()) once looked up


def track_windows(epochs: list[int], pad: int) -> list[tuple[int, int]]:
    """Merged [e - pad, e + pad] intervals: a trip photographed on 14 days of a year costs a few
    small fetches, not 365 days of track."""
    out: list[list[int]] = []
    for e in sorted(set(epochs)):
        lo, hi = e - pad, e + pad
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def parse_manual_drift(raw: str) -> Optional[int]:
    """None for auto/off; seconds for a number; ValueError otherwise."""
    raw = (raw or "auto").strip().lower()
    if raw in ("auto", "off", ""):
        return None
    return int(float(raw.replace(",", ".")))


def plan_files(media: list[Media], track: Track, model: Optional[DriftModel], o: FillOpts) -> list[Plan]:
    manual = parse_manual_drift(o.drift)
    plans: list[Plan] = []
    for m in media:
        sc = m.sidecar
        if m.has_exif_gps():
            plans.append(Plan(m, "has-gps")); continue
        if sc and real_gps(sc.lat, sc.lon) and not o.force:
            plans.append(Plan(m, "sidecar-has-gps")); continue
        if sc and sc.marker == NOGPS_MARKER and not o.retry:
            plans.append(Plan(m, "marked-no-gps")); continue
        if m.epoch is None:
            plans.append(Plan(m, "no-time", "keine verwertbare Aufnahmezeit")); continue
        if manual is not None:
            drift = Drift(manual, "manual", 0, "manual") if abs(manual) >= o.drift_min else Drift()
        elif model is not None:
            drift = model.for_file(m.camera, m.epoch)
        else:
            drift = Drift()
        t_true = m.epoch - drift.seconds
        loc = locate(track, t_true, o.gap1, o.gap2) if len(track) else None
        p = Plan(m, "fill", drift=drift, t_true=t_true, loc=loc)
        if loc is None:
            p.status, p.reason = "no-track-point", f"kein Track-Punkt innerhalb von {o.gap2 // 60} min"
        elif loc["confidence"] < o.min_conf:
            p.status, p.reason = "low-confidence", f"Konfidenz {loc['confidence']} < {o.min_conf}"
        plans.append(p)
    return plans


def _own_gps(m: Media):
    """The real position a file already carries (EXIF first, then sidecar) or None."""
    if m.has_exif_gps():
        return m.lat, m.lon
    sc = m.sidecar
    if sc and real_gps(sc.lat, sc.lon):
        return sc.lat, sc.lon
    return None


def plan_neighbors(plans: list, o: FillOpts, load_dir: Callable[[str], list]) -> int:
    """Last resort for files the track could not place: the nearest file (by capture time) in the same folder OR ANY SUBFOLDER
    of it, on the SAME local day, that has a real position of its own (same folder wins a tie). `load_dir(folder)` returns the
    media of the folder and its subfolders with times resolved.
    The position is copied as is, so the confidence is capped at 0.5 and falls with the time distance; files that were
    only just filled by the track in this scan are NOT used as references (no chains). Returns how many were placed."""
    try:
        zone = ZoneInfo(o.tz)
    except Exception:                # noqa: BLE001
        zone = ZoneInfo("Europe/Vienna")
    day = lambda e: datetime.fromtimestamp(e, zone).date()
    window = o.neighbor_s if o.neighbor_s > 0 else 86400
    refs_cache: dict = {}
    placed = 0
    for p in plans:
        if p.status != "no-track-point" or p.media.epoch is None:
            continue
        folder = os.path.dirname(p.media.path)
        if folder not in refs_cache:
            refs_cache[folder] = [(m, g) for m in load_dir(folder) if m.epoch is not None and (g := _own_gps(m)) is not None]
        d0 = day(p.media.epoch)
        best = None
        for m, g in refs_cache[folder]:
            if m.path == p.media.path or day(m.epoch) != d0:
                continue
            gap = abs(m.epoch - p.media.epoch)
            if o.neighbor_s > 0 and gap > o.neighbor_s:
                continue
            key = (gap, os.path.dirname(m.path) != folder)
            if best is None or key < best[0]:
                best = (key, m, g)
        if best is None:
            p.reason += " · keine Nachbardatei mit GPS am selben Tag im Ordner/in Unterordnern"
            continue
        (gap, _sub), m, (lat, lon) = best
        label = os.path.relpath(m.path, folder)          # "near.jpg" or "sub/near.jpg"
        conf = round(max(0.1, min(0.5, 0.5 * (1 - gap / window))), 2)
        ref = {"epoch": int(m.epoch), "lat": float(lat), "lon": float(lon), "source": "neighbor:" + label}
        p.loc = {"lat": round(float(lat), 6), "lon": round(float(lon), 6), "method": "neighbor", "gap_s": int(gap), "span_s": 0,
                 "dist_m": 0, "tier": 2, "confidence": conf, "uncertainty_m": 5000, "before": ref, "after": None,
                 "neighbor": label}
        if conf < o.min_conf:
            p.status, p.reason = "low-confidence", f"Nachbardatei {label} ({int(gap // 60)} min entfernt): Konfidenz {conf} < {o.min_conf}"
        else:
            p.status, p.reason = "fill", ""
            placed += 1
    return placed


def weather_points(media: list) -> list:
    """[epoch, lat, lon] of every photo/video that has a time and a REAL position (EXIF or sidecar GPS) --
    what the weather archive needs besides the tracks. Files without a position are skipped."""
    out = []
    for m in media:
        if m.epoch is None:
            continue
        lat, lon = m.lat, m.lon
        if not geocore.has_real_gps(lat, lon) and m.sidecar is not None:
            lat, lon = m.sidecar.lat, m.sidecar.lon
        if geocore.has_real_gps(lat, lon):
            out.append([int(m.epoch), float(lat), float(lon)])
    return out


CTX_TAGS = {"daypart": "CtxDaypart", "season": "CtxSeason", "month": "CtxMonth", "year": "CtxYear",
            "weekday": "CtxWeekday", "local_time": "CtxLocalTime", "weather": "CtxWeather",
            "temp_c": "CtxTempC", "elevation": "CtxElevation", "timezone": "CtxTimezone", "trip": "CtxTrip",
            "trip_source": "CtxTripSource", "daylight": "CtxDaylight", "sunrise": "CtxSunrise", "sunset": "CtxSunset",
            "moon": "CtxMoon", "holiday": "CtxHoliday", "wind_kmh": "CtxWindKmh", "wind_text": "CtxWindText",
            "precip_mm": "CtxPrecipMm", "cloud_pct": "CtxCloudPct", "humidity_pct": "CtxHumidityPct",
            "pois": "CtxPois", "sight": "CtxSight", "place_version": "CtxPlaceVersion", "country": "CtxCountry", "country_info": "CtxCountryInfo",
            "clock": "CtxClock", "clock_status": "CtxClockStatus", "time_basis": "CtxTimeBasis",
            "ctx_schema": "CtxSchema", "ai_stale": "AiStale", "nfo_stale": "NfoStale"}      # stamps/flags, see ctxfill.CTX_SCHEMA


def context_args(fields: dict) -> list[str]:
    m = CTX_TAGS
    a = [f"-XMP-mon:{tag}={fields[k]}" for k, tag in m.items() if fields.get(k) not in (None, "")]
    return a + [f"-XMP-mon:CtxFilledBy=suite-context/{VERSION}"] if a else []


def write_context_fields(xmp: str, fields: dict) -> tuple[bool, str]:
    """Time-of-day / season / weather / trip name as XMP-mon:Ctx* fields (independent of any AI text)."""
    a = context_args(fields)
    return exiftool_write_mon(a, xmp) if a else (True, "")


# ---------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------
def sidecar_args(p: Plan, existing_subject: list) -> list[str]:
    """Same tag mapping as geocore._sidecar_location_args / _geo_keyword_args, plus provenance."""
    loc, g, d = p.loc, p.geo or {}, p.drift
    lat, lon = loc["lat"], loc["lon"]
    a = [f"-XMP-exif:GPSLatitude={lat}", f"-XMP-exif:GPSLongitude={lon}", "-XMP-photoshop:TransmissionReference="]
    for tag, key in (("XMP-photoshop:Country", "country"), ("XMP-iptcCore:CountryCode", "country_a"),
                     ("XMP-photoshop:State", "state"), ("XMP-iptcExt:LocationCreatedProvinceState", "county"),
                     ("XMP-photoshop:City", "city"), ("XMP-iptcCore:Location", "locality"),
                     ("XMP-iptcExt:LocationCreatedSublocation", "street")):
        if g.get(key):
            a.append(f"-{tag}={g[key]}")
    wanted = [f"geo:lat={lat}", f"geo:lon={lon}", "geotagged"]
    for k in ("country_a", "country", "county", "city", "locality", "street"):
        if g.get(k) and g[k] not in wanted:
            wanted.append(g[k])
    for old in existing_subject:                       # a re-fill must not leave the old coordinates behind
        if isinstance(old, str) and old.startswith(("geo:lat=", "geo:lon=")) and old not in wanted:
            a.append(f"-XMP-dc:Subject-={old}")
    for v in wanted:                                   # += would duplicate, so only add what is missing
        if v not in existing_subject:
            a.append(f"-XMP-dc:Subject+={v}")
    srcs = sorted({x["source"] for x in (loc["before"], loc["after"]) if x})
    a += [f"-XMP-mon:GpsSource={'neighbor-file' if loc['method'] == 'neighbor' else 'track-' + loc['method']}", f"-XMP-mon:GpsConfidence={loc['confidence']}",
          f"-XMP-mon:GpsGapSeconds={loc['gap_s']}", f"-XMP-mon:GpsUncertaintyM={loc['uncertainty_m']}",
          f"-XMP-mon:TimeBasis={p.media.basis}", f"-XMP-mon:ClockDriftSeconds={d.seconds}",
          f"-XMP-mon:ClockDriftMethod={d.method}", f"-XMP-mon:GpsFilledBy=suite-gps-fill/{VERSION}"]
    if srcs:
        a.append(f"-XMP-mon:GpsTrackSources={','.join(srcs)}")
    return a


def write_plan(p: Plan) -> tuple[bool, str]:
    """Writes the sidecar (media files are never touched)."""
    m = p.media
    xmp = sidecar_target(m.path)
    if not os.path.exists(xmp):
        geocore.ensure_xmp_sidecar(m.path, xmp, m.is_video)
        if not os.path.exists(xmp):
            return False, "die .xmp-Sidecar-Datei konnte nicht angelegt werden"
    ok, msg = exiftool_write_mon(sidecar_args(p, m.sidecar.subject if m.sidecar else []), xmp)
    if ok:
        geocore.write_place_extras(xmp, p.loc["lat"], p.loc["lon"])        # altitude + time zone, only where missing
    return ok, msg


def mark_nogps(p: Plan) -> bool:
    xmp = sidecar_target(p.media.path)
    if not os.path.exists(xmp):
        return False
    ok, _ = exiftool_write_mon([f"-XMP-photoshop:TransmissionReference={NOGPS_MARKER}"], xmp)
    return ok


# ---------------------------------------------------------------------------------------------
# track + drift for a whole file set
# ---------------------------------------------------------------------------------------------
def load_track(media: list[Media], o: FillOpts, fetch: Callable[..., list],
               cancel_event=None) -> tuple[Track, int]:
    """Track points covering every file that needs them (fill candidates, plus the GPS reference
    files when drift is 'auto'). Returns (Track, number of fetch windows)."""
    timed = [m for m in media if m.epoch is not None]
    needs = [m for m in timed if not m.has_exif_gps() or o.drift == "auto"]
    if not needs:
        return Track([]), 0
    pad = o.gap2 + (o.max_shift if o.drift == "auto" else 0) + 3600
    windows = track_windows([m.epoch for m in needs], pad)
    pts: list[Pt] = []
    for lo, hi in windows:
        if cancel_event is not None and cancel_event.is_set():
            break
        pts += [Pt(e, la, lo_, s) for e, la, lo_, s in fetch(lo, hi, o.sources, o.devices, o.include_import)]
    return Track(pts), len(windows)
