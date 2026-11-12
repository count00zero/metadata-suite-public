"""iptcfill.py -- the IPTC "Credits/Origin" fields of a photo (what IrfanView's IPTC dialog shows), filled where EMPTY:

  Date created  IPTC:DateCreated + IPTC:TimeCreated   from EXIF DateTimeOriginal (+ OffsetTimeOriginal)
  Source        IPTC:Source                           the mapped, readable camera name (camera_names.json / NFO rules / Google list)
  Credits       IPTC:Credit                           IPTC_CREDIT (default: empty = no credit)
and their XMP twins (XMP-photoshop:DateCreated / Source / Credit), which is all a sidecar (videos) can carry.

Never overwrites: exiftool runs in create-only mode (-wm cg), so a value the camera or an earlier edit put there stays
(Samsung phones, for instance, fill IPTC:DateCreated themselves).  Switch: IPTC_FILL=false.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from . import exifpool

TIMEOUT = 60
DEFAULT_CREDIT = ""


def enabled() -> bool:
    return (os.environ.get("IPTC_FILL") or "true").strip().lower() not in ("0", "false", "no", "off")


def credit() -> str:
    """IPTC_CREDIT; unset -> no credit; set but empty -> no credit is written."""
    v = os.environ.get("IPTC_CREDIT")
    return DEFAULT_CREDIT if v is None else v.strip()


def _read(cmd: list) -> dict:
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        return (json.loads(res.stdout or "[]") or [{}])[0]
    except Exception:           # noqa: BLE001
        return {}


def _s(v) -> str:
    return "" if v is None else str(v).strip()


_DT = re.compile(r"^(\d{4})[:\-](\d\d)[:\-](\d\d)[ T](\d\d):(\d\d):(\d\d)")
_OFF = re.compile(r"(Z|[+-]\d\d:\d\d)\s*$")


def camera_name(file: str, xmp: str = "") -> str:
    """The readable camera name: the sidecar's XMP-mon:CameraName if there is one, else mapped from the file's Make/Model."""
    from . import devicenames
    if xmp and os.path.isfile(xmp):
        from .geocore import MON_CONFIG
        n = _s(_read(["exiftool", "-config", MON_CONFIG, "-j", "-m", "-XMP-mon:CameraName", xmp]).get("CameraName"))
        if n:
            return n
    rec = _read(["exiftool", "-j", "-m", "-Make", "-Model", file])
    return devicenames.translate(_s(rec.get("Make")), _s(rec.get("Model")))["label"]


def _stamp(dt: str, off: str):
    """('YYYY:MM:DD', 'HH:MM:SS[+HH:MM]', 'YYYY-MM-DDTHH:MM:SS[+HH:MM]') from an EXIF-style date, or None."""
    m = _DT.match(dt or "")
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}:{mo}:{d}", f"{h}:{mi}:{s}{off}", f"{y}-{mo}-{d}T{h}:{mi}:{s}{off}"


def image_args(file: str, camera: Optional[str] = None, xmp: str = "") -> list:
    """Args for the fields that are EMPTY in the image (IPTC and its XMP twin); [] when nothing is missing / nothing known."""
    rec = _read(["exiftool", "-j", "-m", "-G1", "-EXIF:DateTimeOriginal", "-EXIF:OffsetTimeOriginal", "-IPTC:DateCreated",
                 "-IPTC:TimeCreated", "-IPTC:Source", "-IPTC:Credit", "-XMP-photoshop:DateCreated", "-XMP-photoshop:Source",
                 "-XMP-photoshop:Credit", file])
    g = lambda k: _s(rec.get(k))
    args: list = []
    off = g("ExifIFD:OffsetTimeOriginal")
    off = off if _OFF.search(off) else ""
    st = _stamp(g("ExifIFD:DateTimeOriginal"), off)
    if st:
        if not g("IPTC:DateCreated"):
            args.append(f"-IPTC:DateCreated={st[0]}")
        if not g("IPTC:TimeCreated") and off:      # exiftool would append the SERVER's zone to an offset-less time -> only with a known offset
            args.append(f"-IPTC:TimeCreated={st[1]}")
        if not g("XMP-photoshop:DateCreated"):
            args.append(f"-XMP-photoshop:DateCreated={st[2]}")
    cam = camera if camera is not None else (camera_name(file, xmp) if not (g("IPTC:Source") and g("XMP-photoshop:Source")) else "")
    if cam:
        if not g("IPTC:Source"):
            args.append(f"-IPTC:Source={cam}")
        if not g("XMP-photoshop:Source"):
            args.append(f"-XMP-photoshop:Source={cam}")
    cr = credit()
    if cr:
        if not g("IPTC:Credit"):
            args.append(f"-IPTC:Credit={cr}")
        if not g("XMP-photoshop:Credit"):
            args.append(f"-XMP-photoshop:Credit={cr}")
    return args


def fill_image(file: str, camera: Optional[str] = None, xmp: str = "") -> tuple:
    """(ok, message, args_written). Create-only write (-wm cg), so existing values are never touched."""
    if not enabled():
        return True, "", []
    from . import aicore
    args = image_args(file, camera, xmp)
    if not args:
        return True, "", []
    ok, msg = aicore.exiftool_write_image(file, ["-wm", "cg", *args], TIMEOUT)
    return ok, msg, args if ok else []


def fill_sidecar(xmp: str, camera: str = "") -> tuple:
    """XMP twins in a sidecar (videos too): Credit, Source (camera name), DateCreated from XMP-exif:DateTimeOriginal."""
    if not enabled() or not os.path.isfile(xmp):
        return True, ""
    rec = _read(["exiftool", "-j", "-m", "-XMP-exif:DateTimeOriginal", "-XMP-photoshop:DateCreated", "-XMP-photoshop:Source",
                 "-XMP-photoshop:Credit", xmp])
    args = []
    dt = _DT.match(_s(rec.get("DateTimeOriginal")))
    if dt and not _s(rec.get("DateCreated")):
        y, mo, d, h, mi, s = dt.groups()
        off = _OFF.search(_s(rec.get("DateTimeOriginal")))
        args.append(f"-XMP-photoshop:DateCreated={y}-{mo}-{d}T{h}:{mi}:{s}{off.group(1) if off else ''}")
    if camera and not _s(rec.get("Source")):
        args.append(f"-XMP-photoshop:Source={camera}")
    cr = credit()
    if cr and not _s(rec.get("Credit")):
        args.append(f"-XMP-photoshop:Credit={cr}")
    if not args:
        return True, ""
    res = exifpool.run(["exiftool", "-m", "-overwrite_original", "-wm", "cg", *args, xmp], capture_output=True, text=True, timeout=TIMEOUT)
    return res.returncode == 0, (res.stderr or "").strip()
