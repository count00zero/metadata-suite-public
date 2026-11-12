"""
timecore.py — importable core logic for resolving the "true" local timestamp of a
photo/video from its metadata, filename, or folder name.

This is a library refactor of the standalone resolve_times.py CLI script (used by
Timestamp-Bilder-Videos.sh). Same resolution logic, but structured as functions that
return data instead of printing lines, so the web app can call it directly for live
previews without shelling out per click.

Per-file offset is determined like this (first match wins):
  1) GPS coordinates present:
     a) GPSDateTime present  -> real UTC, converted into the GPS-derived IANA zone.
     b) otherwise            -> filename/metadata wall clock is trusted as already-local,
                                 just labelled with the GPS-derived zone's offset.
  2) No GPS coordinates, but filename AND raw metadata (CreateDate/DateTimeOriginal) both
     parse to a timestamp: many action cams (Osmo Action, Insta360, ...) store true UTC in
     CreateDate/DateTimeOriginal while the filename already carries the correct LOCAL wall
     clock. The difference between the two IS the true UTC offset for that recording - no
     GPS, timezone database, or manual offset needed. Rounded to the nearest 15 minutes.
     (If the difference is ~0, metadata and filename already agree -> this method can't
     reveal an offset, falls through to DEFAULT_TZ below.)
  3) Otherwise: filename / folder digits / metadata / mtime, labelled with DEFAULT_TZ.
"""
from __future__ import annotations

from . import skipdirs
import os
import re
import json
import subprocess
from . import exifpool
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from typing import Optional

try:
    from zoneinfo import ZoneInfo, available_timezones
except ImportError:  # pragma: no cover
    ZoneInfo = None
    available_timezones = lambda: []

try:
    from timezonefinder import TimezoneFinder
except ImportError:
    TimezoneFinder = None

VIDEO_EXT = {".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".mpg", ".mpeg"}
IMAGE_EXT = {".png", ".gif", ".jpg", ".jpeg", ".psd"}
ALL_EXT = {".nfo"} | VIDEO_EXT | IMAGE_EXT

# Common/well-known zones checked first so a numeric offset (no GPS involved) gets a
# recognisable place name instead of an obscure one that happens to share the offset.
PREFERRED_ZONES = [
    "Europe/Vienna", "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "Asia/Dubai", "Asia/Colombo", "Indian/Maldives", "Asia/Kolkata", "Asia/Kathmandu",
    "Asia/Dhaka", "Asia/Bangkok", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Tokyo",
    "Asia/Shanghai", "Asia/Tehran", "Asia/Kabul", "Asia/Jakarta",
    "Australia/Sydney", "Australia/Perth", "Pacific/Auckland",
    "Africa/Cairo", "Africa/Johannesburg", "Africa/Nairobi",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "America/Anchorage", "Pacific/Honolulu",
]

_tf_instance = None


def _timezonefinder():
    """Lazily create a single shared TimezoneFinder instance (loading its offline
    coordinate data is relatively expensive, so do it once per process, not per file)."""
    global _tf_instance
    if TimezoneFinder is None:
        return None
    if _tf_instance is None:
        _tf_instance = TimezoneFinder()
    return _tf_instance


def timezonefinder_available() -> bool:
    return TimezoneFinder is not None


def tzdata_available(zone_name: str = "UTC") -> bool:
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(zone_name)
        return True
    except Exception:
        return False


def digits(s: Optional[str]) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def digits_to_dt(d: str, fy: str, fm: str = "01", fd: str = "01") -> datetime:
    ye, mo, da = d[0:4], d[4:6], d[6:8]
    hr, mi, se = d[8:10], d[10:12], d[12:14]
    if not re.fullmatch(r"(19|20)\d\d", ye):
        ye = fy
    if not re.fullmatch(r"0[1-9]|1[0-2]", mo):
        mo = fm
    if not re.fullmatch(r"0[1-9]|[12]\d|3[01]", da):
        da = fd
    if not re.fullmatch(r"[01]\d|2[0-3]", hr):
        hr = "00"
    if not re.fullmatch(r"[0-5]\d", mi):
        mi = "00"
    if not re.fullmatch(r"[0-5]\d", se):
        se = "01"
    try:
        return datetime(int(ye), int(mo), int(da), int(hr), int(mi), int(se))
    except ValueError:
        return datetime(int(ye), int(mo), 1, 0, 0, 1)


def parse_exif_dt(s) -> Optional[datetime]:
    if not s:
        return None
    s = str(s).strip()[:19]
    try:
        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


@lru_cache(maxsize=256)
def zone_name_for_offset(offset_seconds: int, date_key: str) -> Optional[str]:
    """Best-effort guess of a human-readable zone name matching a raw numeric offset on a
    given date. Only used when there's no GPS/authoritative zone (filename-vs-metadata diff
    path) - purely cosmetic labelling, several real zones can share the same offset."""
    if ZoneInfo is None:
        return None
    sample = datetime.strptime(date_key, "%Y-%m-%d").replace(hour=12)
    for name in PREFERRED_ZONES:
        try:
            off = sample.replace(tzinfo=ZoneInfo(name)).utcoffset()
        except Exception:
            continue
        if off is not None and off.total_seconds() == offset_seconds:
            return name
    for name in sorted(available_timezones()):
        if "/" not in name or name.startswith("Etc/"):
            continue
        try:
            off = sample.replace(tzinfo=ZoneInfo(name)).utcoffset()
        except Exception:
            continue
        if off is not None and off.total_seconds() == offset_seconds:
            return name
    return None


def _matches_type_filter(ext: str, type_filter: str) -> bool:
    """Shared VIDEO/IMAGE/ALL extension check -- same semantics _resolve_core already
    applies per-file right before its own exiftool call, factored out so find_files()
    can apply it at listing time too (see find_files' own docstring for why)."""
    if type_filter == "VIDEO":
        return ext in VIDEO_EXT
    if type_filter == "IMAGE":
        return ext in IMAGE_EXT
    return ext in ALL_EXT


MONTH_COLLAGE_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])\.jpe?g$", re.IGNORECASE)


def find_files(directory: str, tiefe: int, type_filter: str = "ALL") -> list[str]:
    """Collect candidate media files under `directory` up to `tiefe` levels deep
    (0 = the directory itself only). Also accepts a single file path directly.

    type_filter (VIDEO|IMAGE|ALL, default ALL) narrows the listing itself -- e.g. a
    VIDEOS Basis-Ordner only ever gets video files back, never every photo in the folder
    too. This matters beyond just saving a few cheap os.walk entries: every caller that
    turns this list into a job's progress total (GPS Sidecar's browse_start, Timestamp &
    Rename's scan_candidates) would otherwise show an inflated/misleading total counting
    files of a type that was never going to be processed -- resolve_batch/_resolve_core
    already skip them before any real (exiftool) work, but the candidate list and
    progress bar didn't know that. Callers that want every type regardless (Timestamp &
    Rename browsing a mixed ALL-type folder, say) just pass the default."""
    if os.path.isfile(directory):
        ext = os.path.splitext(directory)[1].lower()
        return [directory] if _matches_type_filter(ext, type_filter) else []
    if not os.path.isdir(directory):
        return []
    base_depth = directory.rstrip("/").count("/")
    out = []
    for root, dirs, files in os.walk(directory):
        depth = root.rstrip("/").count("/") - base_depth
        if depth >= tiefe:
            dirs[:] = []
        sibling_dirs = list(dirs)
        skipdirs.prune(dirs)
        for f in files:
            if skipdirs.is_generated_collage(f, sibling_dirs):
                continue      # folder.jpg / monthly collage ("2023.12.jpg") / trip collage ("<folder name>.jpg"): generated by the Collage module, never a capture
            ext = os.path.splitext(f)[1].lower()
            if _matches_type_filter(ext, type_filter):
                out.append(os.path.join(root, f))
    return sorted(out)


def get_exif(path: str, timeout_secs: int = 120, cancel_event=None) -> dict:
    """Single-file exiftool call (JSON). One call per file rather than one batch call over
    a whole tree: a slow/stuck file only costs its own timeout instead of blocking or
    aborting a whole run, and callers can show progress/results as they arrive.

    If a threading.Event is passed as cancel_event, the subprocess is polled in short
    slices instead of a single blocking wait, so a Cancel click actually kills a slow/stuck
    exiftool call right away (e.g. a flaky NAS mount) instead of waiting out the full
    timeout_secs before the cancellation takes effect."""
    cmd = ["exiftool", "-j", "-m", "-api", "LargeFileSupport=1",
           "-GPSLatitude#", "-GPSLongitude#", "-GPSDateTime",
           "-CreateDate", "-DateTimeOriginal", "-Make", "-Model",
           "-OffsetTimeOriginal", "-OffsetTime", "-OffsetTimeDigitized", path]
    # Runs on the persistent exiftool pool (core/exifpool.py): timeout and cancel_event both kill
    # just the one worker that is handling this file (it is replaced automatically).
    try:
        res = exifpool.run(cmd, timeout=timeout_secs, cancel_event=cancel_event)
        data = json.loads(res.stdout or "[]")
        return data[0] if data else {}
    except exifpool.Cancelled:
        return {"_cancelled": True}
    except subprocess.TimeoutExpired:
        return {"_error": f"exiftool-Timeout ({timeout_secs}s)"}
    except Exception as e:
        return {"_error": f"exiftool-Aufruf fehlgeschlagen: {e}"}


# Known action-cam signature strings that DON'T show up in a proper EXIF/QuickTime
# Make/Model tag at all -- ported from the old Set-GPS bash script's own detection:
#   GYROINFO=$({ head -c 20M "$Video"; tail -c 5M "$Video"; } | strings | grep -i -m1 ...)
# An Insta360 Luna Ultra video carries no Make/Model metadata exiftool can read (confirmed
# against the maintainer's real files -- a camera_model filter match rate of 0 despite "nearly all
# videos are from Luna Ultra" is what surfaced this), but the camera/app name is still
# findable as a plain ASCII string buried in the file's own binary data (encoder/firmware
# identifier), near the start and/or the end of the file -- same two chunks the bash script
# read, kept identical here so this matches already-verified real-world behavior rather
# than a fresh guess.
CAMERA_SIGNATURE_HEAD_BYTES = 20_000_000
CAMERA_SIGNATURE_TAIL_BYTES = 5_000_000

# (needle to search for, case-insensitive) -> (Make, Model) to report, same MODEL string
# the bash script itself used ("Insta360 Luna Ultra"). First match wins; add further
# action-cam signatures here as they're confirmed against real files the same way.
CAMERA_SIGNATURES: list[tuple[str, str, str]] = [
    ("insta360 luna ultra", "Insta360", "Insta360 Luna Ultra"),
    ("luna ultra", "Insta360", "Insta360 Luna Ultra"),
    ("insta360", "Insta360", "Insta360 Luna Ultra"),
]


def sniff_camera_from_bytes(path: str) -> tuple[str, str]:
    """Best-effort (Make, Model) guess for a file whose EXIF/QuickTime metadata carries no
    Make/Model at all -- reads only the first CAMERA_SIGNATURE_HEAD_BYTES and last
    CAMERA_SIGNATURE_TAIL_BYTES of the file (never the whole thing, which can be several GB
    for an action-cam video) and searches that binary data, case-insensitively, for a known
    camera/app signature string. Returns ("", "") if the file can't be read or nothing
    matches -- never raises, so a caller can call this unconditionally as a fallback."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(min(CAMERA_SIGNATURE_HEAD_BYTES, size))
            tail = b""
            if size > CAMERA_SIGNATURE_HEAD_BYTES:
                tail_start = max(CAMERA_SIGNATURE_HEAD_BYTES, size - CAMERA_SIGNATURE_TAIL_BYTES)
                f.seek(tail_start)
                tail = f.read()
        blob = (head + tail).lower()
    except OSError:
        return "", ""
    for needle, make, model in CAMERA_SIGNATURES:
        if needle.encode("ascii", "ignore") in blob:
            return make, model
    return "", ""


_SOURCE_LABELS = {
    "Dateiname": "Dateiname",
    "Ordner": "Ordnername",
    "Metadaten": "EXIF-Metadaten (CreateDate/DateTimeOriginal)",
    "mtime": "Datei-Änderungsdatum (mtime)",
}


def humanize_source(source: str) -> str:
    """Turns the compact internal source tag (e.g. "Dateiname(Europe/Vienna)",
    "Dateiname-UTC-Diff(Asia/Colombo, +05:30)", "GPS-UTC->Europe/Vienna") into a readable
    "Quelle: ... / Zeitzone: ..." label for the preview, instead of the raw programmer-ish
    string. Always names what the timestamp actually came FROM, never what it didn't come
    from. Falls back to the raw source unchanged if it doesn't match a known pattern."""
    m = re.match(r"^GPS-UTC->(.+)$", source)
    if m:
        return f"Quelle: GPS-Zeitstempel (UTC) · Zeitzone: {m.group(1)}"
    m = re.match(r"^GPS-Zone\+(Dateiname|Metadaten|Ordner|mtime)\((.+)\)$", source)
    if m:
        key, tz = m.groups()
        return f"Quelle: {_SOURCE_LABELS.get(key, key)} · Zeitzone (aus GPS-Position): {tz}"
    m = re.match(r"^Dateiname-UTC-Diff\((.+)\)$", source)
    if m:
        return f"Quelle: Differenz Dateiname ↔ Metadaten-UTC · Zeitzone/Offset: {m.group(1)}"
    m = re.match(r"^(Dateiname|Ordner|Metadaten|mtime)\+Nachbar\|(.+?)\|(.+?)\|(.+)$", source)
    if m:
        key, donor_tz, diff, donor_name = m.groups()
        return (f"Quelle: {_SOURCE_LABELS.get(key, key)}, Zeitzone von Nachbardatei "
                f"„{donor_name}“ übernommen (zeitlicher Abstand {diff}) "
                f"· Zeitzone: {donor_tz}")
    m = re.match(r"^(Dateiname|Ordner|Metadaten|mtime)\((.+)\)$", source)
    if m:
        key, tz = m.groups()
        return f"Quelle: {_SOURCE_LABELS.get(key, key)} · Zeitzone: {tz}"
    return source


@dataclass
class Resolution:
    path: str
    source: str
    local_str: str          # YYYY.MM.DD-HH.MM.SS, used for the new filename
    exif_dt: str             # YYYY:MM:DD HH:MM:SS, used for exiftool writes
    offset: str               # +HH:MM
    offset_short: str          # +H / -H, for -TimeZoneOffset
    epoch: int
    warn: str = ""
    skipped: bool = False
    skip_reason: str = ""
    # Raw GPS coordinates this resolution actually read from the file's own EXIF (None if
    # the file has none). Exposed so other modules (e.g. the GPS Sidecar module) can reuse
    # resolve_one()'s single exiftool call instead of re-reading the file themselves just to
    # check for existing GPS -- resolve_one() already parses these out internally for its own
    # timezone resolution, this just surfaces them instead of discarding them.
    lat: Optional[float] = None
    lon: Optional[float] = None
    # The resolved IANA zone name (e.g. "Europe/Vienna"), when the zone actually has one --
    # None for a raw numeric-offset resolution with no matched zone name (an unborrowed
    # Dateiname-UTC-Diff whose offset didn't match any known zone, or the plain default_tz
    # fallback when zoneinfo/tzdata isn't available at all). tz_confident mirrors whether
    # that zone came from independent evidence for this file (real GPS, the UTC-diff trick,
    # or a borrowed same-folder sibling) rather than just the configured Fallback-Zone --
    # together these let a caller suggest a better Fallback-Zone from what the folder's own
    # confidently-resolved files actually show, instead of a fixed default.
    tz_name: Optional[str] = None
    tz_confident: bool = False
    # Raw EXIF Make/Model, whatever the camera itself wrote (never normalized/matched
    # against a known-device table here -- that's NFO Maker's own CAMERA_SOURCE_RULES
    # job). Surfaced so a scan can show/filter by which camera actually shot a file --
    # e.g. re-checking only Insta360 Luna Ultra or DJI Osmo Action 6 footage after a
    # timezone-resolution fix, without having to re-touch the whole library.
    camera_make: str = ""
    camera_model: str = ""
    shift: str = ""          # "+01:30:00" when a manual Zeitkorrektur was applied to the wall clock of this file
    ext: str = field(init=False, default="")
    dir: str = field(init=False, default="")
    target_name: str = field(init=False, default="")
    # What follows the timestamp in the target name ("_02", "_01", ...). None = derive it from
    # the current filename; set explicitly by assign_unique_targets() / carried through the
    # preview's JSON so the executed name is exactly the previewed one.
    name_suffix: Optional[str] = None

    def __post_init__(self):
        self.dir = os.path.dirname(self.path)
        self.ext = os.path.splitext(self.path)[1]
        # A file that ALREADY starts with a timestamp keeps whatever follows it ("_02", "-1",
        # " Urlaub", ...) -- only the timestamp part is ever replaced, so "..._02.jpg" never
        # collapses to "....jpg" (and two numbered shots never collide on the same target).
        if self.name_suffix is None:
            stem = os.path.splitext(os.path.basename(self.path))[0]
            m = ALREADY_NAMED_RE.match(stem)
            self.name_suffix = stem[m.end():] if m else ""
        self.target_name = f"{self.local_str}{self.name_suffix}{self.ext}"

    @property
    def target_path(self) -> str:
        return os.path.join(self.dir, self.target_name)

    @property
    def zieldate(self) -> str:
        return f"{self.exif_dt}{self.offset}"

    @property
    def source_display(self) -> str:
        return humanize_source(self.source) + (f" · Zeitkorrektur {self.shift}" if self.shift else "")

    @property
    def has_gps(self) -> bool:
        """Same (0, 0)-null-island-aware check used throughout the GPS Sidecar module --
        duplicated here (not imported from geocore) to keep timecore dependency-free."""
        if self.lat is None or self.lon is None:
            return False
        return not (self.lat == 0 and self.lon == 0)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "filename": os.path.basename(self.path),
            "source": self.source,
            "source_display": self.source_display,
            "lat": self.lat,
            "lon": self.lon,
            "has_gps": self.has_gps,
            "tz_name": self.tz_name,
            "tz_confident": self.tz_confident,
            "camera_make": self.camera_make,
            "camera_model": self.camera_model,
            "shift": self.shift,
            "local_str": self.local_str,
            "exif_dt": self.exif_dt,
            "offset": self.offset,
            "offset_short": self.offset_short,
            "epoch": self.epoch,
            "warn": self.warn,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "ext": self.ext,
            "dir": self.dir,
            "name_suffix": self.name_suffix,
            "target_name": self.target_name,
            "target_path": self.target_path,
            "zieldate": self.zieldate,
            "will_rename": self.target_name != os.path.basename(self.path),
        }


def parse_shift(text: str) -> int:
    """Manual time correction ("Zeitkorrektur", the camera clock was off) as seconds.
    Accepts "+01:30", "-0:05:30", "1:30:15", "+2h", "-90m", "45s", "+1d", "+1d 02:00" (sign applies to the whole value).
    Empty = 0. ValueError for anything else or more than +-400 days."""
    t = (text or "").strip().lower().replace(",", ".")
    if not t:
        return 0
    sign = -1 if t[0] == "-" else 1
    t = t.lstrip("+-").strip()
    secs = 0
    m = re.match(r"^(\d+)\s*d(?:ays?|ay)?\s*(.*)$", t)
    if m:
        secs += int(m.group(1)) * 86400
        t = m.group(2).strip()
    if t:
        m = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", t)
        if m:
            secs += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)
        else:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hms])", t)
            if not m:
                raise ValueError(f"Zeitkorrektur nicht lesbar: {text!r} (z.B. +01:30, -0:05:30, +2h, -90m, +1d 02:00)")
            secs += int(float(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)])
    if secs > 400 * 86400:
        raise ValueError("Zeitkorrektur größer als 400 Tage")
    return sign * secs


def format_shift(secs: int) -> str:
    """+1 day 02:00:00 style: "+26:00:00" (hours may exceed 24, so it reads the same as what was typed)."""
    sign = "-" if secs < 0 else "+"
    h, rem = divmod(abs(int(secs)), 3600)
    return f"{sign}{h:02d}:{rem // 60:02d}:{rem % 60:02d}"


def _resolve_core(path: str, mode: str, default_tz: str, gps_filter: str = "ALL",
                   type_filter: str = "ALL", timeout_secs: int = 120,
                   cancel_event=None, force_zone: bool = False, time_shift_s: int = 0) -> Optional[dict]:
    """Does everything resolve_one() does EXCEPT attach a tzinfo-naive consistency check
    and finalize into a Resolution -- it stops right after picking local_naive/tzinfo_obj/
    source, so a batch caller (resolve_many/scan_folder, via resolve_batch below) can
    still override tzinfo_obj with a same-folder sibling's before finalizing. Returns None
    if the file is filtered out by gps_filter/type_filter (same semantics as resolve_one).

    cancel_event (a threading.Event), if given, is passed straight through to get_exif()
    so a Cancel click can kill a slow/stuck exiftool call for THIS file right away.

    The returned dict's "is_confident" flag marks whether tzinfo_obj came from something
    independently trustworthy for THIS file alone (real GPS, or the filename/metadata
    UTC-diff trick) vs. the plain default_tz fallback (Dateiname/Ordner/Metadaten/mtime
    wallclock with no real zone info of its own) -- only non-confident entries get a
    chance to borrow a confident sibling's zone instead of jumping straight to
    default_tz. "wallclock_tag" / "tz_label" are the pieces resolve_batch needs to build
    the "borrowed from a sibling" source tag without re-deriving them."""
    ext = os.path.splitext(path)[1].lower()

    if type_filter == "VIDEO" and ext not in VIDEO_EXT:
        return None
    if type_filter == "IMAGE" and ext not in IMAGE_EXT:
        return None

    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    parent = os.path.basename(os.path.dirname(path))
    try:
        mtime_epoch = int(os.path.getmtime(path))
    except OSError:
        mtime_epoch = int(datetime.now().timestamp())
    fy = str(datetime.fromtimestamp(mtime_epoch).year)

    rec = get_exif(path, timeout_secs, cancel_event) if mode == "EXIF" else {}
    if rec.get("_cancelled"):
        return None
    exif_warn = rec.pop("_error", "") if isinstance(rec, dict) else ""

    lat, lon = rec.get("GPSLatitude"), rec.get("GPSLongitude")
    try:                                    # an empty ('') or garbled GPS tag is "no GPS", not a crash further down (float(''))
        lat, lon = (float(lat), float(lon)) if lat not in (None, "") and lon not in (None, "") else (None, None)
    except (TypeError, ValueError):
        lat = lon = None
    has_gps = lat is not None and lon is not None
    camera_make = (rec.get("Make") or "").strip()
    camera_model = (rec.get("Model") or "").strip()
    # Fallback for a camera that writes no Make/Model metadata exiftool can read at all --
    # confirmed true for Insta360 Luna Ultra VIDEOS specifically (the maintainer's real files: the
    # camera_model filter matched 0 of ~90 known-Luna-Ultra videos). Only costs a ~25MB
    # read, and only for a file that would otherwise report no camera at all.
    if not camera_make and not camera_model and ext in VIDEO_EXT:
        camera_make, camera_model = sniff_camera_from_bytes(path)

    if gps_filter == "WITH-GPS" and not has_gps:
        return None
    if gps_filter == "NO-GPS" and has_gps:
        return None

    gps_dt = parse_exif_dt(rec.get("GPSDateTime"))
    # CreateDate is read here for EVERY file type, not just videos -- the UTC-diff trick
    # this feeds (module docstring, point 2) is explicitly for action cams like the
    # Insta360 Luna Ultra, which can store true UTC in CreateDate for PHOTOS too, not
    # only for videos. A previous version of this line gated create_dt to
    # `ext in VIDEO_EXT`, silently discarding CreateDate for every image -- for an Insta360
    # Luna Ultra photo whose DateTimeOriginal is missing/empty (CreateDate is its only
    # populated capture-time tag), that made meta_dt None, so the UTC-diff trick could
    # never fire for that file. With nothing "is_confident" for it to borrow from a
    # same-folder sibling either (when every photo from that batch has the same gap),
    # it fell all the way through to the plain Fallback-Zone with no offset at all --
    # exactly the maintainer's "neither fallback nor from neighbouring images" report. Fixed by
    # reading CreateDate for every type, same priority as before (CreateDate wins over
    # DateTimeOriginal when both are present, matching what videos already did).
    create_dt = parse_exif_dt(rec.get("CreateDate"))
    dto = parse_exif_dt(rec.get("DateTimeOriginal")) if ext != ".nfo" else None
    meta_dt = create_dt if create_dt is not None else dto

    fn_digits = digits(stem)
    fn_dt = digits_to_dt(fn_digits, fy) if re.fullmatch(r"[0-9]{4,}", fn_digits) else None
    parent_digits = digits(parent)
    folder_dt = digits_to_dt(parent_digits, fy) if re.fullmatch(r"[0-9]{4,}", parent_digits) else None
    # A camera counter ("D85_1872" -> 851872) is not a date: without a plausible year in front the old
    # fallback invented "<file year>-01-01 00:00:01". A name needs a real year (19xx/20xx) to count.
    if fn_dt is not None and not re.match(r"(19|20)\d\d", fn_digits):
        fn_dt = None
    # A date-only / year-only name ("2026", "202604") must not override a folder that names the full day
    # ("2023.04.29 - Hochzeit ...", a single-day event): the folder date is the better reading then.
    if fn_dt is not None and len(fn_digits) < 8 and folder_dt is not None and len(parent_digits) >= 8:
        fn_dt = None
    # A folder gives a DAY, not a time: start at 13:00 so a zone shift (GPS zone e.g. Dubai +4h, or any
    # offset) can never move the photo onto the previous/next day.
    if folder_dt is not None and len(parent_digits) < 10:
        folder_dt = folder_dt.replace(hour=13, minute=0, second=0)

    # The file's OWN camera-/earlier-run-written UTC offset (OffsetTimeOriginal/OffsetTime/
    # OffsetTimeDigitized) -- real per-file info, trusted outright like the original Set-GPS
    # script's has_embedded_tz(). Photos only: videos never carry this kind of offset.
    embedded_offset = None
    if ext in IMAGE_EXT:
        for k in ("OffsetTimeOriginal", "OffsetTime", "OffsetTimeDigitized"):
            v = str(rec.get(k) or "").strip()
            if re.fullmatch(r"[+-]\d{2}:\d{2}", v):
                embedded_offset = v
                break

    warn = exif_warn
    default_zone = ZoneInfo(default_tz) if ZoneInfo else timezone.utc

    # is_confident marks whether tzinfo_obj below came from something independently
    # trustworthy for THIS file alone (real GPS, or the filename/metadata UTC-diff
    # trick) -- the only sources a sibling file is allowed to *borrow* from. The four
    # default_zone fallback branches at the bottom are not confident: wallclock_tag
    # there records which wallclock reading was used (Dateiname/Ordner/Metadaten/mtime),
    # so a sibling-borrow (done afterwards, in _resolve_batch) can still say where this
    # file's own TIME reading came from even while its ZONE got borrowed.
    is_confident = False
    wallclock_tag = None
    tz_label = None

    if has_gps:
        tzname = default_tz
        # force_zone: the GPS position is wrong / irrelevant (e.g. a wrongly geotagged event): the
        # Fallback-Zone is the zone, whatever the coordinates say. The GPS UTC time (if any) still counts.
        tf = _timezonefinder() if not force_zone else None
        if tf is not None:
            try:
                found = tf.timezone_at(lat=float(lat), lng=float(lon))
                if found:
                    tzname = found
            except (TypeError, ValueError):
                pass
        tz = ZoneInfo(tzname) if ZoneInfo else timezone.utc
        is_confident = True
        tz_label = tzname
        gps_local = (gps_dt.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
                     if gps_dt is not None else None)
        # A phone whose clock was set by hand (or whose time zone setting was left on the
        # home zone) writes a GPSTimeStamp that is "UTC" only by its own belief -- e.g. a
        # Xiaomi in Abu Dhabi on Vienna time wrote 17:55:13Z for a shot taken 19:55:13 local,
        # so trusting it blindly moved the photo 2 h. When the GPS-derived local time
        # contradicts the file's own wall clock (filename / DateTimeOriginal) by a zone-like
        # amount (1h..14h, multiple of 15 min), the wall clock wins and GPS only supplies the
        # zone. Small gaps (clock drift) and day-sized gaps keep the GPS time. Set
        # GPS_UTC_TRUST=always to restore the old "GPS time always wins" behaviour.
        wall_ref, wall_ref_tag = ((fn_dt, "Dateiname") if fn_dt is not None
                                  else (meta_dt, "Metadaten") if meta_dt is not None else (None, None))
        gps_time_distrusted = False
        if (gps_local is not None and wall_ref is not None
                and os.environ.get("GPS_UTC_TRUST", "").strip().lower() != "always"):
            gap = (wall_ref - gps_local).total_seconds()
            if (3600 <= abs(gap) <= 14 * 3600 and abs(gap - round(gap / 900) * 900) <= 90):
                gps_time_distrusted = True
            # Corroboration: the file carries its own UTC offset and DateTimeOriginal - offset
            # equals the GPS UTC (phone in flight mode: clock stayed on the home zone, e.g.
            # 16:07:44 +02:00 == 14:07:44Z, but the shot was at 18:07:44 local in Abu Dhabi).
            # Then clock and GPS agree on the instant, only the zone label is stale -> GPS wins.
            if gps_time_distrusted and embedded_offset is not None and meta_dt is not None:
                sg = -1 if embedded_offset[0] == "-" else 1
                off = sg * (int(embedded_offset[1:3]) * 3600 + int(embedded_offset[4:6]) * 60)
                meta_utc = meta_dt - timedelta(seconds=off)
                if abs((meta_utc - gps_dt.replace(tzinfo=None)).total_seconds()) <= 120:
                    gps_time_distrusted = False
        if gps_local is not None and not gps_time_distrusted:
            local_naive = gps_local
            source = f"GPS-UTC->{tzname}"
        elif gps_time_distrusted:
            local_naive = wall_ref
            source = f"GPS-Zone+{wall_ref_tag}({tzname})"
            # Filename AND metadata clock agree with each other (two independent readings)
            # while only the GPS stamp deviates -- e.g. a DJI drone without a GPS fix
            # (GPSStatus=V) writing its own clock as "UTC". That is the normal, well-supported
            # case: no warning, the source line already says the time comes from the filename.
            wall_agree = (fn_dt is not None and meta_dt is not None
                          and abs((fn_dt - meta_dt).total_seconds()) <= 2)
            if not wall_agree:
                warn = (warn + " " if warn else "") + (
                    f"GPS-Zeitstempel (UTC) widerspricht der Aufnahmezeit um {abs(gap)/3600:.2f}h "
                    f"(Telefonuhr/Zeitzone evtl. falsch eingestellt) – {wall_ref_tag}-Uhrzeit mit "
                    f"GPS-Zone verwendet")
        else:
            # GPS gives us the right IANA zone (tzname), but no authoritative GPS
            # timestamp -- so the wall-clock reading itself still has to come from
            # somewhere else. Tag the source with WHICH one actually supplied it
            # (not just "no GPS timestamp"), so the preview says where the time came
            # from rather than only what it didn't come from.
            if fn_dt is not None:
                wallclock, wallclock_tag = fn_dt, "Dateiname"
            elif meta_dt is not None:
                wallclock, wallclock_tag = meta_dt, "Metadaten"
            elif folder_dt is not None:
                wallclock, wallclock_tag = folder_dt, "Ordner"
            else:
                wallclock, wallclock_tag = datetime.fromtimestamp(mtime_epoch), "mtime"
            source = f"GPS-Zone+{wallclock_tag}({tzname})"
            local_naive = wallclock
        tzinfo_obj = tz
    elif (not force_zone and embedded_offset is not None and dto is not None
          and (fn_dt is None or abs((fn_dt - dto).total_seconds()) < 120)):
        sign = -1 if embedded_offset[0] == "-" else 1
        off_secs = sign * (int(embedded_offset[1:3]) * 3600 + int(embedded_offset[4:6]) * 60)
        tzinfo_obj = timezone(timedelta(seconds=off_secs))
        local_naive = fn_dt if fn_dt is not None else dto
        zone_guess = zone_name_for_offset(off_secs, local_naive.strftime("%Y-%m-%d"))
        label = f"{zone_guess}, {embedded_offset}" if zone_guess else embedded_offset
        source = f"EXIF-Offset({label})"
        is_confident = True
        tz_label = label
        wallclock_tag = "Dateiname" if fn_dt is not None else "Metadaten"
    # Only a plausible zone gap (< 15h) is a UTC-diff: a date-only/year-only name or a copied
    # file differs by days/months and used to crash timezone() with an out-of-range offset.
    elif (not force_zone and fn_dt is not None and meta_dt is not None
          and 120 <= abs((fn_dt - meta_dt).total_seconds()) < 15 * 3600):
        diff_sec = (fn_dt - meta_dt).total_seconds()
        rounded = round(diff_sec / 900) * 900
        if abs(diff_sec - rounded) > 90:
            warn = (warn + " " if warn else "") + (
                f"Dateiname-Metadaten-Differenz {diff_sec/3600:.2f}h liegt nicht nahe "
                f"einer 15-Minuten-Grenze - bitte pruefen")
        tzinfo_obj = timezone(timedelta(seconds=rounded))
        local_naive = fn_dt
        off_label_sign = "+" if rounded >= 0 else "-"
        off_h, off_rem = divmod(abs(int(rounded)), 3600)
        off_m = off_rem // 60
        off_label = f"{off_label_sign}{off_h:02d}:{off_m:02d}"
        zone_guess = zone_name_for_offset(rounded, fn_dt.strftime("%Y-%m-%d"))
        label = f"{zone_guess}, {off_label}" if zone_guess else off_label
        source = f"Dateiname-UTC-Diff({label})"
        is_confident = True
        tz_label = label
        wallclock_tag = "Dateiname"
    elif fn_dt is not None:
        local_naive = fn_dt
        tzinfo_obj = default_zone
        source = f"Dateiname({default_tz})"
        wallclock_tag = "Dateiname"
    elif folder_dt is not None:
        local_naive = folder_dt
        tzinfo_obj = default_zone
        source = f"Ordner({default_tz})"
        wallclock_tag = "Ordner"
    elif meta_dt is not None:
        local_naive = meta_dt
        tzinfo_obj = default_zone
        source = f"Metadaten({default_tz})"
        wallclock_tag = "Metadaten"
    else:
        local_naive = datetime.fromtimestamp(mtime_epoch)
        tzinfo_obj = default_zone
        source = f"mtime({default_tz})"
        wallclock_tag = "mtime"

    # Zeitkorrektur: the camera clock was off by a known amount -> shift the wall clock of every file by it. A time that
    # comes from the GPS UTC stamp is already right (the GPS clock is not the camera's) and is left alone.
    shift_note = ""
    if time_shift_s and not source.startswith("GPS-UTC->"):
        local_naive = local_naive + timedelta(seconds=time_shift_s)
        shift_note = format_shift(time_shift_s)

    return {
        "path": path,
        "shift": shift_note,
        "lat": lat, "lon": lon, "has_gps": has_gps,
        "local_naive": local_naive,
        "tzinfo_obj": tzinfo_obj,
        "source": source,
        "warn": warn,
        "is_confident": is_confident,
        "fn_dt": fn_dt,
        "wallclock_tag": wallclock_tag,
        "tz_label": tz_label,
        "camera_make": camera_make, "camera_model": camera_model,
    }


def _finalize_resolution(core: dict) -> Resolution:
    """Second half of what resolve_one() used to do in one go: attach tzinfo_obj to
    local_naive, run the filename-consistency cross-check, format the output strings, and
    build the Resolution. Split out of _resolve_core() so a batch caller (_resolve_batch)
    can first look at every file's is_confident/tzinfo_obj across a whole folder and
    possibly swap in a borrowed sibling tzinfo_obj/source before this step ever runs."""
    path = core["path"]
    lat, lon, has_gps = core["lat"], core["lon"], core["has_gps"]
    local_naive = core["local_naive"]
    tzinfo_obj = core["tzinfo_obj"]
    source = core["source"]
    warn = core["warn"]
    fn_dt = core["fn_dt"]

    local_dt = local_naive.replace(tzinfo=tzinfo_obj)

    # Consistency cross-check (web-UI only addition, doesn't affect the resolved value
    # itself -- resolve_times.py's naming/exiftool-write behavior above is untouched):
    # if the filename itself encodes a date/time, flag it when the final resolved
    # timestamp disagrees with it by more than 2 minutes, so a bad GPS fix, a copied/
    # re-encoded file, or a wrong camera clock gets surfaced for review instead of
    # silently producing a rename the filename itself contradicts. Branches that already
    # set local_naive = fn_dt (Dateiname, Dateiname-UTC-Diff, ...) always agree here by
    # construction -- this only ever fires for a timestamp resolved independently of the
    # filename (GPS-UTC, or GPS-Zone/Metadaten wallclock picked without a usable fn_dt).
    if fn_dt is not None and not core.get("shift"):
        naive_diff = abs((local_dt.replace(tzinfo=None) - fn_dt).total_seconds())
        if naive_diff >= 120:
            warn = (warn + " " if warn else "") + (
                f"Dateiname deutet auf {fn_dt.strftime('%Y-%m-%d %H:%M:%S')} hin, ermittelter "
                f"Zeitstempel ist {local_dt.strftime('%Y-%m-%d %H:%M:%S')} ({source}) - bitte pruefen"
            )

    local_str = local_dt.strftime("%Y.%m.%d-%H.%M.%S")
    exif_dt = local_dt.strftime("%Y:%m:%d %H:%M:%S")
    offset = local_dt.strftime("%z")
    offset = offset[:3] + ":" + offset[3:]
    off_sign, off_hh, off_mm = offset[0], int(offset[1:3]), int(offset[4:6])
    # EXIF TimeZoneOffset is an INTEGER number of hours (exiftool rounds 5.5 to 6): a
    # half-/quarter-hour zone (India +05:30, Nepal +05:45, ...) cannot be stored there at
    # all, and the old truncation wrote a plainly wrong +5. Leave it empty in that case --
    # the exact value goes into OffsetTimeOriginal/OffsetTime (written next to it) and into
    # the offset-qualified dates.
    offset_short = f"{off_sign}{off_hh}" if off_mm == 0 else ""
    epoch = int(local_dt.timestamp())
    tz_name = tzinfo_obj.key if (ZoneInfo is not None and isinstance(tzinfo_obj, ZoneInfo)) else None

    return Resolution(
        path=path, source=source, local_str=local_str, exif_dt=exif_dt,
        offset=offset, offset_short=offset_short, epoch=epoch, warn=warn,
        lat=float(lat) if has_gps else None, lon=float(lon) if has_gps else None,
        tz_name=tz_name, tz_confident=bool(core.get("is_confident")),
        camera_make=core.get("camera_make") or "", camera_model=core.get("camera_model") or "",
        shift=core.get("shift") or "",
    )


def row_with_zone(row: dict, tz_name: str) -> dict:
    """Copy of a preview row (Resolution.to_dict()) with the zone REPLACED by `tz_name`: the wall clock
    (exif_dt, and with it the target name) stays, offset / offset_short / epoch / tz_name are recomputed.
    This is the write-time "Fallback-Zone erzwingen": e.g. photos whose GPS said Asia/Dubai (+04:00) but were
    shot in Austria get Europe/Vienna's +02:00 on the same wall clock."""
    if ZoneInfo is None or not tz_name or not row.get("exif_dt"):
        return row
    naive = datetime.strptime(row["exif_dt"], "%Y:%m:%d %H:%M:%S")
    dt = naive.replace(tzinfo=ZoneInfo(tz_name))
    off = dt.strftime("%z")
    off = off[:3] + ":" + off[3:]
    out = dict(row)
    out.update(offset=off, offset_short=(f"{off[0]}{int(off[1:3])}" if off[4:6] == "00" else ""),
               epoch=int(dt.timestamp()), tz_name=tz_name)
    return out


def resolve_one(path: str, mode: str, default_tz: str, gps_filter: str = "ALL",
                 type_filter: str = "ALL", timeout_secs: int = 120) -> Optional[Resolution]:
    """Resolve a single file in isolation (no same-folder sibling borrowing -- that only
    happens for a batch of files processed together, via resolve_many/scan_folder, since
    it needs to see other files in the same folder). Returns None if the file is filtered
    out by gps_filter/type_filter."""
    core = _resolve_core(path, mode, default_tz, gps_filter, type_filter, timeout_secs)
    if core is None:
        return None
    return _finalize_resolution(core)


def scan_candidates(directory: str, tiefe: int, type_filter: str = "ALL",
                     include_already_named: bool = False) -> tuple[list[str], int]:
    """Cheap first pass over a folder (just os.walk + a filename regex -- no exiftool/GPS
    calls at all): finds every candidate file already narrowed to type_filter (so e.g. a
    VIDEOS Basis-Ordner's candidate list -- and therefore the job's progress total --
    never includes photos it was never going to process) and, unless
    include_already_named is set, filters out ones already matching the target naming
    format. Returns (candidate_paths, skipped_count). Split out of scan_folder() so a
    caller that wants to show real progress (e.g. the web UI's Cancel/progress-bar job)
    can get an accurate "total files to process" count up front, before the slow
    per-file work starts."""
    skipped_count = 0
    candidate_paths = []
    for path in find_files(directory, tiefe, type_filter):
        if not include_already_named and is_already_named(path):
            skipped_count += 1
            continue
        candidate_paths.append(path)
    return candidate_paths, skipped_count


def _naive_epoch(dt: datetime) -> float:
    """Treats a naive local datetime as if it were UTC, purely so two local wallclock
    readings (possibly in different/unknown real zones) can be compared by a consistent,
    symmetric "distance in seconds" -- mirrors the CLI's own `date -u -d ...` epoch-diff
    trick for finding the nearest sibling in time."""
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _format_seconds_diff(secs: float) -> str:
    """Compact human-readable duration for a time difference, used only inside the
    "+Nachbar|..." source tag (purely informational -- never parsed by the CLI)."""
    secs = abs(int(round(secs)))
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{s:02d}s"
    hours, m = divmod(mins, 60)
    return f"{hours}h{m:02d}m"


def resolve_batch(paths: list[str], mode: str, default_tz: str, gps_filter: str = "ALL",
                   type_filter: str = "ALL", timeout_secs: int = 120,
                   progress_cb=None, cancel_event=None, force_zone: bool = False, time_shift_s: int = 0) -> list[Resolution]:
    """Resolves every path in `paths`, then -- before finalizing any of them -- lets each
    non-confident file (one that would otherwise simply fall back to the configured
    Fallback-Zone/default_tz: Dateiname/Ordner/Metadaten/mtime wallclock with no real zone
    info of its own) borrow the timezone of the nearest-in-time file in the SAME FOLDER
    that DOES have a confidently-resolved zone (real GPS, or the Dateiname-UTC-Diff trick).
    This mirrors the CLI's find_nearest_sibling_tz_offset(), scoped to one folder (not
    recursive) and working off resolve_one()'s full resolution chain rather than only a
    real embedded camera offset. If no confident sibling exists in the folder, the file is
    finalized exactly as resolve_one() would do it on its own (falls back to default_tz).

    progress_cb(done, total, current_filename), if given, is called right before each
    file's exiftool work starts (done = how many are already finished, so the UI can show
    "done / total - processing <name>"). cancel_event (a threading.Event), if given, is
    checked before each file; a set event stops the whole batch right there (the file whose
    exiftool call was in flight when cancelled is discarded, not partially included) --
    everything resolved before the cancellation is still returned."""
    cores = []
    total = len(paths)
    for i, path in enumerate(paths):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb is not None:
            progress_cb(i, total, os.path.basename(path))
        core = _resolve_core(path, mode, default_tz, gps_filter, type_filter, timeout_secs,
                              cancel_event, force_zone, time_shift_s)
        if cancel_event is not None and cancel_event.is_set():
            break  # this file's exiftool call was killed mid-flight -- don't keep its core
        if core is not None:
            cores.append(core)
    else:
        # Loop ran to completion (no break -> not cancelled) -- report the final tick so
        # the UI can show a clean 100% before the job flips to "done".
        if progress_cb is not None:
            progress_cb(total, total, "")

    donors_by_folder: dict[str, list[dict]] = {}
    for core in cores:
        if core["is_confident"]:
            donors_by_folder.setdefault(os.path.dirname(core["path"]), []).append(core)

    for core in cores:
        if core["is_confident"]:
            continue
        candidates = donors_by_folder.get(os.path.dirname(core["path"]))
        if not candidates:
            continue
        this_epoch = _naive_epoch(core["local_naive"])
        donor = min(candidates, key=lambda d: abs(_naive_epoch(d["local_naive"]) - this_epoch))
        diff = abs(_naive_epoch(donor["local_naive"]) - this_epoch)
        core["tzinfo_obj"] = donor["tzinfo_obj"]
        core["source"] = (f"{core['wallclock_tag']}+Nachbar|{donor['tz_label']}|"
                           f"{_format_seconds_diff(diff)}|{os.path.basename(donor['path'])}")
        # The zone now came from a trusted neighbor rather than the plain Fallback-Zone --
        # count it as confident too (e.g. for a folder-wide Fallback-Zone suggestion).
        core["is_confident"] = True

    return [_finalize_resolution(core) for core in cores]


def resolve_many(directory: str, tiefe: int, mode: str, default_tz: str,
                  gps_filter: str = "ALL", type_filter: str = "ALL",
                  timeout_secs: int = 120, progress_cb=None, cancel_event=None):
    """Generator: yields Resolution objects for every file under `directory` that passes
    the filters. Resolves the whole folder as one batch via resolve_batch (not true
    per-file streaming any more) so that same-folder sibling timezone-borrowing can see
    every file's confidence/zone before any one of them is finalized."""
    results = resolve_batch(find_files(directory, tiefe, type_filter), mode, default_tz,
                             gps_filter, type_filter, timeout_secs, progress_cb, cancel_event)
    for res in results:
        yield res


# Target naming format produced by this tool: YYYY.MM.DD-HH.MM.SS. Matched as a PREFIX
# (re.match, not fullmatch) so any trailing filename addition -- a disambiguation suffix
# like "-1", a camera/app suffix like "_05", "_HDR", "(1)", etc. -- is ignored: once the
# timestamp portion itself is already correct, the file counts as already named no matter
# what got appended after it.
ALREADY_NAMED_RE = re.compile(
    r"^(19|20)\d\d\.(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])-"
    r"([01]\d|2[0-3])\.[0-5]\d\.[0-5]\d"
)


def is_already_named(path: str) -> bool:
    """Fast, metadata-free check: does the filename already start with the target naming
    format? No exiftool/GPS call involved -- just a filename regex, so this is cheap
    enough to run over an entire folder before doing any real resolution work. Anything
    appended after the timestamp (a disambiguation suffix, a camera/app tag, ...) is
    ignored -- only the timestamp prefix itself has to already be correct."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return bool(ALREADY_NAMED_RE.match(stem))


def scan_folder(directory: str, tiefe: int, mode: str, default_tz: str,
                 gps_filter: str = "ALL", type_filter: str = "ALL",
                 timeout_secs: int = 120, include_already_named: bool = False,
                 progress_cb=None, cancel_event=None):
    """Like resolve_many, but first applies the cheap is_already_named() filename check
    (via scan_candidates). By default, files already matching the target naming format are
    skipped entirely -- no exiftool/GPS call is made for them, and they're not returned, so
    the preview only shows files that actually need something done. Returns
    (resolutions, skipped_count), where skipped_count is how many already-correctly-named
    files were found (and, when include_already_named is False, left out of `resolutions`)
    -- the UI uses that count to decide whether to show an "Umbenannte einblenden" button.

    When include_already_named is True, every file is resolved and returned as normal
    (so the full folder content can be reviewed) and skipped_count stays 0, since nothing
    was actually skipped on that call.

    progress_cb/cancel_event are passed straight through to resolve_batch (see there).

    Resolution (including same-folder sibling timezone-borrowing, via resolve_batch) only
    ever considers the candidate files actually being processed on this call -- an
    already-named file that got skipped here contributes no exiftool data and so can't act
    as a donor for a sibling either; that's an accepted side effect of skipping it
    entirely, not a special case to work around."""
    candidate_paths, skipped_count = scan_candidates(directory, tiefe, type_filter,
                                                      include_already_named)
    resolutions = resolve_batch(candidate_paths, mode, default_tz, gps_filter, type_filter,
                                 timeout_secs, progress_cb, cancel_event)
    return resolutions, skipped_count


_DT_VALUE_RE = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def _check_date_value(value, res) -> tuple[str, str]:
    """Compares one raw exiftool date string against what this scan resolved for the file
    (res.exif_dt = local wall clock, res.epoch = the real instant). Returns (state, shown):
    state is "missing" (no value), "ok" (same instant, or -- for a value with no offset --
    same local wall clock, within 2s) or "mismatch" (e.g. a camera's raw UTC CreateDate
    that was never rewritten to local time: 2h off in CEST)."""
    if not value:
        return "missing", ""
    shown = str(value).strip()
    m = _DT_VALUE_RE.match(shown)
    if not m:
        return "mismatch", shown
    y, mo, d, h, mi, se, tz = m.groups()
    try:
        naive = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    except ValueError:
        return "mismatch", shown
    if tz:
        if tz == "Z":
            off = timedelta(0)
        else:
            t = tz.replace(":", "")
            off = timedelta(hours=int(t[1:3]), minutes=int(t[3:5]))
            if t[0] == "-":
                off = -off
        epoch = int(naive.replace(tzinfo=timezone(off)).timestamp())
        return ("ok" if abs(epoch - res.epoch) <= 2 else "mismatch"), shown
    target = parse_exif_dt(res.exif_dt)
    if target is not None and abs((naive - target).total_seconds()) <= 2:
        return "ok", shown
    return "mismatch", shown


def date_diagnostics(res: "Resolution", timeout_secs: int = 60) -> dict:
    """Per-file "is the local time actually stored where viewers read it?" check, for the
    Timestamp & Rename preview when a camera filter is active. One exiftool call over the
    file itself plus its .xmp sidecar (if there is one), reading ONLY the capture-date tags
    -- no QuickTime UTC conversion (-api QuickTimeUTC=0), so a video's raw stored value is
    shown as-is. Compared against what this scan resolved (see _check_date_value):

      file_state/file_value     video: QuickTime CreateDate; photo: EXIF DateTimeOriginal
                                (CreateDate if that's all there is)
      embedded_xmp_*            video only: XMP CreateDate embedded in the video itself
      sidecar_state/_value      xmp:CreateDate in "<file>.xmp" ("nofile" if no sidecar)
      offset_missing            photo only: no EXIF offset info at all (OffsetTimeOriginal/
                                OffsetTime/TimeZoneOffset or an offset-qualified XMP date)
      needs_backwrite           the file itself doesn't carry the resolved local time
      sidecar_stale             a sidecar exists but doesn't carry it either
    """
    is_video = res.ext.lower() in VIDEO_EXT
    xmp_path = res.path + ".xmp"
    has_sidecar = os.path.isfile(xmp_path)
    cmd = ["exiftool", "-j", "-G1", "-a", "-api", "QuickTimeUTC=0", "-api", "LargeFileSupport=1",
           "-QuickTime:CreateDate", "-XMP-xmp:CreateDate", "-ExifIFD:DateTimeOriginal",
           "-ExifIFD:CreateDate", "-ExifIFD:OffsetTimeOriginal", "-ExifIFD:OffsetTime",
           "-ExifIFD:TimeZoneOffset", "-XMP-exif:DateTimeOriginal", res.path]
    if has_sidecar:
        cmd.append(xmp_path)
    recs = {}
    try:
        out = exifpool.run(cmd, timeout=timeout_secs).stdout
        for r in json.loads(out or "[]"):
            recs[r.get("SourceFile")] = r
    except Exception:
        pass
    file_rec = recs.get(res.path, {})
    side_rec = recs.get(xmp_path, {}) if has_sidecar else {}

    if is_video:
        file_value = file_rec.get("QuickTime:CreateDate")
        emb_value = file_rec.get("XMP-xmp:CreateDate")
    else:
        file_value = file_rec.get("ExifIFD:DateTimeOriginal") or file_rec.get("ExifIFD:CreateDate")
        emb_value = None
    file_state, file_shown = _check_date_value(file_value, res)
    emb_state, emb_shown = _check_date_value(emb_value, res) if is_video else ("", "")
    if has_sidecar:
        # Immich checks the sidecar's DateTimeOriginal first, then xmp:CreateDate, and takes the
        # timezone from the same place -- a value WITHOUT offset is read as UTC. So the sidecar
        # only counts as fine if every date it carries is offset-qualified and the right instant.
        side_state, side_shown = "missing", ""
        for key in ("XMP-exif:DateTimeOriginal", "XMP-xmp:CreateDate"):
            v = side_rec.get(key)
            if not v:
                continue
            st, shown = _check_date_value(v, res)
            if st == "ok" and not re.search(r"(Z|[+-]\d{2}:?\d{2})$", shown):
                st = "nooffset"
            if side_state == "missing" or (side_state == "ok" and st != "ok"):
                side_state, side_shown = st, shown
    else:
        side_state, side_shown = "nofile", ""
    # TimeZoneOffset is what executor.apply_resolution itself writes (OffsetTimeOriginal is
    # what many cameras/phones write) -- either one, or an offset-qualified embedded XMP
    # date, counts as "the file knows its UTC offset".
    offset_missing = (not is_video) and not (
        file_rec.get("ExifIFD:OffsetTimeOriginal") or file_rec.get("ExifIFD:OffsetTime")
        or re.search(r"(Z|[+-]\d{2}:?\d{2})$", str(file_rec.get("XMP-xmp:CreateDate") or "")))
    # A video counts as carrying the local time if EITHER its QuickTime value matches (what
    # executor.apply_resolution writes) -- the embedded XMP is shown for information only.
    return {
        "is_video": is_video,
        "file_state": file_state, "file_value": file_shown,
        "embedded_xmp_state": emb_state, "embedded_xmp_value": emb_shown,
        "sidecar_state": side_state, "sidecar_value": side_shown,
        "offset_missing": offset_missing,
        "needs_backwrite": (not is_video) and (file_state != "ok" or offset_missing),  # videos are never rewritten
        "sidecar_stale": has_sidecar and side_state != "ok",
        # What actually decides Immich's date/sort order (sidecar wins over the file whenever
        # it carries a date). Videos: the sidecar must be right. Photos: right sidecar, or no
        # sidecar and a file that carries local time + offset itself.
        "needs_fix": (side_state != "ok") if (is_video or has_sidecar)
                     else (file_state != "ok" or offset_missing),
    }


def read_camera(path: str, timeout_secs: int = 60) -> tuple[str, str]:
    """(make, model) of one file for camera filters in modules that don't run the full
    timestamp resolution (AI-Beschreibung): exiftool Make/Model, and for a video without
    either tag the byte-signature sniff (Insta360 Luna Ultra)."""
    rec = get_exif(path, timeout_secs)
    make, model = (rec.get("Make") or "").strip(), (rec.get("Model") or "").strip()
    if not make and not model and os.path.splitext(path)[1].lower() in VIDEO_EXT:
        make, model = sniff_camera_from_bytes(path)
    return make, model


def suggest_fallback_zone(resolutions: list) -> Optional[tuple]:
    """Looks at a batch of already-resolved files (Resolution objects, e.g. straight from
    resolve_many/scan_folder) and suggests a better Fallback-Zone than whatever was
    configured, based on which IANA zone shows up most often among files whose zone was
    resolved CONFIDENTLY (real GPS, the filename/metadata UTC-diff trick, or a borrowed
    confident same-folder sibling) -- never from the Fallback-Zone itself, so the
    suggestion reflects independent evidence in the folder, not an echo of the current
    setting. Returns (zone_name, count, confident_total) -- count is how many files
    actually showed that zone, confident_total is how many confidently-resolved files
    there were in all (for a "7 of 9" style display) -- or None if none of the files in
    this batch resolved a zone confidently at all (nothing to suggest from)."""
    counts = Counter(r.tz_name for r in resolutions if r.tz_confident and r.tz_name)
    if not counts:
        return None
    zone, count = counts.most_common(1)[0]
    return zone, count, sum(counts.values())


# NOTE: .mp3 added provisionally (quick addition only) — not finalized, since the real
# resolve_times.py is still pending and may define companion handling differently.
COMPANION_AUDIO_EXT = {".aac", ".wav", ".mp3"}


def find_companion_audio(path: str) -> Optional[str]:
    """Companion audio track (DJI-style .AAC, .WAV, or .MP3 from other action cams): same
    base name, different extension, sitting next to the video file."""
    d = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    for fname in os.listdir(d) if os.path.isdir(d) else []:
        name, ext = os.path.splitext(fname)
        if name == stem and ext.lower() in COMPANION_AUDIO_EXT:
            return os.path.join(d, fname)
    return None


# Backward-compat alias (was AAC-only).
find_aac_companion = find_companion_audio


# ---------------------------------------------------------------------------
# General companion-file discovery for Timestamp & Rename's preview: ANY other
# file in the same folder that belongs to the same capture as a given primary
# file -- its .xmp sidecar, a companion audio track, a generated "-thumb"/
# "-poster"/"-fanart" image, or simply another file sharing the exact same
# stem under a different extension (e.g. a .jpg still next to an .avi video).
# These are shown as their own selectable rows in the rename preview (see
# modules/timestamp_tool/router.py) instead of being silently auto-renamed
# (the old behavior, for companion audio only) or left behind with a now-
# mismatched filename after the primary gets renamed (the old behavior for
# everything else, including .xmp sidecars and thumbnails, which this module
# never even looked for before).
# ---------------------------------------------------------------------------

# "<stem><suffix>.jpg" generated by the AI Description module / the NAS
# toolchain's NFO writer -- same three suffixes as core/mtimecore.py's own
# GENERATED_IMAGE_SUFFIXES (kept as a separate copy here rather than an
# import, since mtimecore.py and timecore.py are otherwise independent of
# each other and neither needs the other for anything but this one constant).
GENERATED_IMAGE_SUFFIXES = ("-fanart", "-poster", "-thumb")


def find_companions(path: str) -> list[str]:
    """Every other file in `path`'s own folder that belongs to the same capture:
    its ".xmp" sidecar (named "<full original filename>.xmp"), a "-thumb"/
    "-poster"/"-fanart" generated image, or any other file sharing the exact
    same stem (filename without its OWN extension) under a different
    extension -- a companion audio track (.aac/.wav/.mp3), a plain sibling
    file (e.g. "clip.jpg" next to "clip.avi"), or anything else a NAS
    toolchain left behind under the same base name.

    Deliberately NOT a bare filename-prefix match: a disambiguation suffix
    this app itself might add to a target name (e.g. a camera's own
    "IMG_0001.jpg" vs. "IMG_00011.jpg", or an already-timestamp-named
    "2026.08.09-17.17.49-1.MP4" sitting next to "2026.08.09-17.17.49.MP4") is
    its own, independently-timestamped file, not a companion -- requiring the
    EXACT stem (or stem+known-suffix) to match, not just a shared prefix,
    is what tells those two cases apart."""
    d = os.path.dirname(path) or "."
    own_name = os.path.basename(path)
    stem = os.path.splitext(own_name)[0]
    try:
        names = os.listdir(d)
    except OSError:
        return []
    matches = []
    for name in names:
        if name == own_name:
            continue
        if name == own_name + ".xmp":
            matches.append(name)
            continue
        name_stem = os.path.splitext(name)[0]
        if name_stem == stem:
            matches.append(name)
            continue
        for suffix in GENERATED_IMAGE_SUFFIXES:
            if name_stem == stem + suffix:
                matches.append(name)
                break
    return sorted(os.path.join(d, n) for n in matches)


def generated_companion_base_stem(path: str) -> Optional[str]:
    """If `path`'s own filename looks like a "-thumb"/"-poster"/"-fanart"
    generated image (by naming convention, not content), returns the stem of
    the media file it would belong to (e.g. "clip" for "clip-thumb.jpg");
    otherwise None. Used to keep a generated thumbnail from being resolved
    and renamed as if it were its own independent capture (see
    has_sibling_media below and modules/timestamp_tool/router.py's
    browse_start) -- it should only ever move in lockstep with its own video/
    photo, as one of find_companions()'s rows above."""
    stem = os.path.splitext(os.path.basename(path))[0]
    for suffix in GENERATED_IMAGE_SUFFIXES:
        if stem.lower().endswith(suffix):
            return stem[: -len(suffix)]
    return None


def has_sibling_media(path: str, base_stem: str) -> bool:
    """True if some OTHER file in `path`'s own folder has `base_stem` as its
    OWN stem and a real video/image extension -- i.e. a plausible primary
    capture file that a "-thumb"/"-poster"/"-fanart" image (generated_
    companion_base_stem) actually belongs to. False for an orphaned
    thumbnail (its video/photo was deleted, moved, or never existed) -- that
    case is deliberately left as its own independent candidate rather than
    silently dropped from the preview."""
    d = os.path.dirname(path) or "."
    own_name = os.path.basename(path)
    try:
        names = os.listdir(d)
    except OSError:
        return False
    for name in names:
        if name == own_name:
            continue
        n_stem, n_ext = os.path.splitext(name)
        if n_stem == base_stem and n_ext.lower() in (VIDEO_EXT | IMAGE_EXT):
            return True
    return False


# ---------------------------------------------------------------------------
# Track-matching time (GPS Sidecar): filename special cases + video CreateDate as UTC
# ---------------------------------------------------------------------------

_DT_PAT = (r"(\d{4})[.\-_]?(\d{2})[.\-_]?(\d{2})"
           r"(?:[-_ T]?(\d{2})[.\-_:]?(\d{2})(?:[.\-_:]?(\d{2}))?)?")
_RANGE_RE = re.compile(r"^\D*?" + _DT_PAT + r"\s*(?:–|—|-|_|\sbis\s)\s*" + _DT_PAT + r"\D*$")
_DATEONLY_RE = re.compile(r"^\D*(\d{4})[.\-_]?(\d{2})[.\-_]?(\d{2})\D*$")
_YEARONLY_RE = re.compile(r"^\D*((?:19|20)\d{2})\D*$")


def _dt_from_groups(g, default_time=(12, 0, 0)) -> Optional[datetime]:
    try:
        y, mo, d = int(g[0]), int(g[1]), int(g[2])
        if g[3] is None:
            h, mi, s = default_time
        else:
            h, mi, s = int(g[3]), int(g[4]), int(g[5] or 0)
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def filename_time_hint(stem: str):
    """The original script's special filename shapes for the track lookup:
      ("range", naive_dt)   'A-B' / 'A bis B' -> midpoint of the two (date without time = 12:00)
      ("dateonly", naive_dt) 'YYYY[.-_]MM[.-_]DD' -> 12:00 that day
      ("yearonly", None)    'YYYY' -> no position lookup at all
    or None for an ordinary (or non-date) name."""
    m = _RANGE_RE.match(stem)
    if m:
        a = _dt_from_groups(m.groups()[0:6])
        b = _dt_from_groups(m.groups()[6:12])
        if a and b:
            return "range", a + (b - a) / 2
    m = _DATEONLY_RE.match(stem)
    if m:
        d = _dt_from_groups(m.groups() + (None, None, None))
        if d:
            return "dateonly", d
    if _YEARONLY_RE.match(stem):
        return "yearonly", None
    return None


def track_match_time(res, path: str) -> tuple[Optional[int], str]:
    """(epoch, note) used to find the nearest track point; epoch None = do not look up.
    Order: filename special case (range midpoint / date-only noon / year-only none) ->
    for a VIDEO whose time did not come from the filename: raw QuickTime CreateDate read as
    UTC (as the original's `-geotime<${CreateDate}Z`) -> the resolved capture epoch.
    Files named with a real timestamp are never changed by this."""
    stem = os.path.splitext(os.path.basename(path))[0]
    hint = filename_time_hint(stem)
    if hint:
        kind, dt = hint
        if kind == "yearonly":
            return None, "Nur-Jahr-Dateiname: keine Positionssuche"
        sign = -1 if res.offset.startswith("-") else 1
        off = sign * (int(res.offset[1:3]) * 3600 + int(res.offset[4:6]) * 60)
        epoch = int(dt.replace(tzinfo=timezone.utc).timestamp()) - off
        return epoch, ("Track-Zeit: Mitte des Datumsbereichs" if kind == "range"
                       else "Track-Zeit: 12:00 (nur Datum im Dateinamen)")
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT and not res.source.startswith("Dateiname"):
        cd = parse_exif_dt(get_exif(path).get("CreateDate"))
        if cd is not None and cd.year > 1970:
            return int(cd.replace(tzinfo=timezone.utc).timestamp()), "Track-Zeit: Video-CreateDate (UTC)"
    return int(res.epoch), ""


def assign_unique_targets(resolutions: list) -> int:
    """Avoids renames that would fail or overwrite: when two files resolve to the same
    target name (same second, e.g. milliseconds apart or two cameras) or the target already
    exists on disk as another file, the affected files get a running "_01", "_02", ... after
    the timestamp. Files whose name is already exactly their target keep it; files without
    any collision are never touched. Returns how many files got a number."""
    import collections
    groups = collections.defaultdict(list)
    for r in resolutions:
        groups[(r.dir, r.target_name.lower())].append(r)
    listing = {}

    def names_in(d):
        if d not in listing:
            try:
                listing[d] = {n.lower() for n in os.listdir(d or ".")}
            except OSError:
                listing[d] = set()
        return listing[d]

    numbered = 0
    for (d, tname), members in groups.items():
        on_disk = names_in(d)
        own_names = {os.path.basename(m.path).lower() for m in members}
        taken_elsewhere = tname in on_disk and tname not in own_names
        if len(members) == 1 and not taken_elsewhere:
            continue
        taken = set(on_disk)
        todo = []
        for m in sorted(members, key=lambda x: x.path):
            if os.path.basename(m.path).lower() == tname:
                continue            # already holds exactly this name
            todo.append(m)
        for m in todo:
            base = m.name_suffix or ""
            n = 1
            while True:
                cand_suffix = f"{base}_{n:02d}"
                cand = f"{m.local_str}{cand_suffix}{m.ext}".lower()
                if cand not in taken or cand == os.path.basename(m.path).lower():
                    break
                n += 1
            taken.add(cand)
            m.name_suffix = cand_suffix
            m.target_name = f"{m.local_str}{cand_suffix}{m.ext}"
            numbered += 1
    return numbered
