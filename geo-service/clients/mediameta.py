"""mediameta.py -- reading what gps_fill.py and caption_context.py need from photos/videos.

Shared by both CLIs: one batched exiftool pass over the media, one over the .xmp sidecars,
a single capture-time resolver (UTC epoch + where the time came from), the camera key, and
the camera filter. The filter is applied ONCE, in collect(), so no later code path can forget it.

Time resolution, most trustworthy first (the files are expected to have been through the
suite's "Timestamp & Rename" step, which writes offset-qualified dates into the sidecar):
  sidecar             XMP-exif:DateTimeOriginal / xmp:CreateDate with an explicit offset
  exif+offset         EXIF DateTimeOriginal + OffsetTimeOriginal/OffsetTime/TimeZoneOffset
  filename+...        YYYY.MM.DD-HH.MM.SS in the file name (local time) + an offset
  exif+...            EXIF DateTimeOriginal without offset
where "..." says which offset was used: the folder's most common explicit offset ("dir-offset")
or the default zone ("tz-assumed", least trustworthy: wrong for photos taken abroad).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
EXIFTOOL = os.environ.get("EXIFTOOL_BIN", "exiftool")
CONFIG = os.environ.get("EXIFTOOL_MON_CONFIG", os.path.join(HERE, "exiftool_mon.config"))

IMAGE_EXT = {"jpg", "jpeg", "png", "heic", "heif", "dng", "tif", "tiff", "webp", "arw", "cr2", "cr3", "nef", "orf", "rw2"}
VIDEO_EXT = {"mp4", "mov", "m4v", "mts", "m2ts", "avi", "3gp", "mkv", "insv"}
NOGPS_MARKER = "no-gps-found"          # same marker the metadata-suite's GPS Sidecar writes
CHUNK = 400


# ---------------------------------------------------------------------------------------------
# exiftool
# ---------------------------------------------------------------------------------------------
def exiftool_base() -> list[str]:
    cmd = [EXIFTOOL]
    if os.path.isfile(CONFIG):
        cmd += ["-config", CONFIG]
    return cmd + ["-charset", "utf8", "-charset", "filename=utf8", "-m", "-q", "-api", "LargeFileSupport=1"]


def exiftool_json(tags: list[str], files: list[str], timeout: int = 600) -> list[dict]:
    """One exiftool call per CHUNK files; file names go in via stdin (-@ -), so a folder with
    thousands of files never hits the command-line length limit."""
    out: list[dict] = []
    for i in range(0, len(files), CHUNK):
        part = files[i:i + CHUNK]
        res = subprocess.run(exiftool_base() + ["-j", "-n", *tags, "-@", "-"], input="\n".join(part),
                             capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        if res.stdout.strip():
            out.extend(json.loads(res.stdout))
    return out


def exiftool_write(args: list[str], path: str, timeout: int = 120) -> tuple[bool, str]:
    res = subprocess.run(exiftool_base() + ["-overwrite_original", *args, path], capture_output=True,
                         text=True, encoding="utf-8", timeout=timeout)
    err = (res.stderr or "").strip()
    return res.returncode == 0 and "error" not in err.lower(), err


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
    dto: Optional[str] = None            # XMP-exif:DateTimeOriginal
    create: Optional[str] = None         # XMP-xmp:CreateDate
    lat: Optional[float] = None
    lon: Optional[float] = None
    marker: str = ""                     # photoshop:TransmissionReference
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
    offset: Optional[int] = None         # seconds, from OffsetTimeOriginal/OffsetTime/TimeZoneOffset
    lat: Optional[float] = None          # real EXIF GPS only
    lon: Optional[float] = None
    gps_epoch: Optional[int] = None      # EXIF GPSDateTime (UTC from the satellites), if any
    sidecar: Optional[Sidecar] = None
    # filled by resolve_times():
    epoch: Optional[int] = None
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


# ---------------------------------------------------------------------------------------------
# collecting
# ---------------------------------------------------------------------------------------------
def walk(paths: Iterable[str], depth: int = 7) -> list[str]:
    files: list[str] = []
    for root in paths:
        root = os.path.abspath(root)
        if os.path.isfile(root):
            files.append(root)
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for d, dirs, names in os.walk(root):
            if d.count(os.sep) - base >= depth:
                dirs[:] = []
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            files.extend(os.path.join(d, n) for n in sorted(names))
    return files


def sidecar_for(path: str) -> Optional[str]:
    """The suite's convention first (<file>.xmp, e.g. IMG.jpg.xmp), then <stem>.xmp."""
    for cand in (path + ".xmp", os.path.splitext(path)[0] + ".xmp"):
        if os.path.isfile(cand):
            return cand
    return None


def sidecar_target(path: str) -> str:
    """Where a NEW sidecar goes: an existing one, else <file>.xmp like the suite."""
    return sidecar_for(path) or path + ".xmp"


MEDIA_TAGS = ["-DateTimeOriginal", "-OffsetTimeOriginal", "-OffsetTime", "-TimeZoneOffset", "-GPSLatitude",
              "-GPSLongitude", "-GPSDateTime", "-Make", "-Model"]
SIDECAR_TAGS = ["-XMP-exif:DateTimeOriginal", "-XMP-xmp:CreateDate", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude",
                "-XMP-photoshop:TransmissionReference", "-XMP-tiff:Make", "-XMP-tiff:Model", "-XMP-dc:Subject"]


def read_sidecars(media_paths: list[str]) -> dict[str, Sidecar]:
    pairs = {p: sidecar_for(p) for p in media_paths}
    xmps = sorted({x for x in pairs.values() if x})
    recs = {r["SourceFile"]: r for r in exiftool_json(SIDECAR_TAGS, xmps)} if xmps else {}
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


def collect(paths: list[str], depth: int = 7, camera: str = "", types: str = "all") -> list[Media]:
    """Every photo/video under `paths` with its metadata and sidecar. `camera` (case-insensitive
    substring of 'Make Model') filters here and only here."""
    want_img, want_vid = types in ("all", "image"), types in ("all", "video")
    files = []
    for f in walk(paths, depth):
        ext = os.path.splitext(f)[1].lstrip(".").lower()
        if (ext in IMAGE_EXT and want_img) or (ext in VIDEO_EXT and want_vid):
            files.append(f)
    recs = {r["SourceFile"]: r for r in exiftool_json(MEDIA_TAGS, files)} if files else {}
    sidecars = read_sidecars(files)
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
        if camera and camera.lower() not in m.camera.lower():
            continue
        media.append(m)
    return media


# ---------------------------------------------------------------------------------------------
# capture time -> UTC epoch
# ---------------------------------------------------------------------------------------------
def resolve_times(media: list[Media], default_tz: str = "Europe/Vienna") -> None:
    """Sets .epoch and .basis on every item (epoch stays None when nothing usable exists)."""
    zone = ZoneInfo(default_tz)
    dir_offsets: dict[str, Counter] = defaultdict(Counter)
    for m in media:                                   # explicit offsets seen per folder
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
