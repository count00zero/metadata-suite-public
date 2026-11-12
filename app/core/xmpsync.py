"""
xmpsync.py — the single place that embeds a file's CURRENT .xmp sidecar contents
directly into its image (images only; videos never get direct modification, same
convention as the rest of this app).

Why this exists: GPS Sidecar and AI Description each used to have their own
"zusätzlich direkt ins Bild einbetten" checkbox that wrote only ITS OWN slice of data
(geocore.write_location_to_image() / aicore.write_description_to_image()) straight to
the image the moment that module ran. Running both modules on the same photo (the
normal standard-flow case) meant the image file itself got a separate
exiftool -overwrite_original rewrite -- and therefore a separate mtime bump needing
its own "Datei-Zeitstempel wiederherstellen" -- once per module, even though nothing
about the IMAGE needs to change twice: both writes land in the same file and neither
erases what the other wrote. the maintainer's own simplification request: always write to
.xmp (unchanged, every module still always does that), and only actually touch the
image file once, with everything accumulated in the .xmp so far.

sync_xmp_to_image() replaces both of those per-module partial writers: it reads back
whatever is CURRENTLY in the .xmp sidecar -- GPS + resolved location (whichever
module wrote them, or neither yet) and the AI description (if generated yet, or not)
-- and writes the whole, current snapshot into the image in exactly one exiftool
call. Both modules' "ins Bild einbetten" checkboxes now call this SAME function, so
wherever a batch happens to get embedded (GPS Sidecar, AI Description, or both, if
the maintainer leaves both ticked for some reason -- harmless, just redundant) the image
always ends up with everything the .xmp currently holds, never just one module's own
fields. Defaults changed so this normally happens exactly once, matching "im
allerletzten Schritt": GPS Sidecar's checkbox now defaults OFF (module #2, usually NOT
the last step for a photo in the standard flow), AI Description's stays default ON
(module #3, the last step for a photo whenever AI descriptions are in use at all).
Deliberately not retroactive -- files already embedded under the old per-module
behavior are left exactly as they are; this only changes what happens on a future run.
"""
from __future__ import annotations

import json
import os
import subprocess
from . import exifpool

from . import aicore

EXIFTOOL_TIMEOUT = 60  # same value geocore.py/aicore.py each already use locally
MON_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exiftool_mon.config")


def _offset_for(zone: str, exif_dt: str) -> str:
    """'+05:30' that `zone` had at the naive local time 'YYYY:MM:DD HH:MM:SS'; '' if either is unusable."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.strptime(exif_dt.strip()[:19], "%Y:%m:%d %H:%M:%S").replace(tzinfo=ZoneInfo(zone))
        mins = int(dt.utcoffset().total_seconds() // 60)
    except Exception:           # noqa: BLE001
        return ""
    sign = "+" if mins >= 0 else "-"
    return f"{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def image_offset_from_zone_enabled() -> bool:
    """IMAGE_OFFSET_FROM_ZONE (default true): also write EXIF OffsetTimeOriginal derived from the sidecar's time zone
    when the image has none. Assumes the camera clock showed LOCAL time at the place of the shot (the maintainer's rule)."""
    return (os.environ.get("IMAGE_OFFSET_FROM_ZONE") or "true").strip().lower() not in ("0", "false", "no", "off")


def _image_extras_args(file: str, fields: dict, timeout: int) -> list[str]:
    """Altitude (and, opt-in, the UTC offset) for the IMAGE -- only where the image has none of its own."""
    try:                                    # an empty or unreadable altitude ('' in the sidecar) is "no altitude", not an error
        alt_val = abs(float(fields["GPSAltitude"])) if fields.get("GPSAltitude") not in (None, "") else None
    except (TypeError, ValueError):
        alt_val = None
    want_alt = alt_val is not None
    want_off = bool(fields.get("CtxTimezone")) and image_offset_from_zone_enabled()
    if not (want_alt or want_off):
        return []
    try:
        out = exifpool.run(["exiftool", "-j", "-m", "-n", "-EXIF:GPSAltitude", "-EXIF:DateTimeOriginal",
                            "-EXIF:OffsetTimeOriginal", file], capture_output=True, text=True, timeout=timeout).stdout
        cur = (json.loads(out or "[]") or [{}])[0]
    except Exception:           # noqa: BLE001
        return []
    args: list[str] = []
    if want_alt and cur.get("GPSAltitude") in (None, ""):
        ref = fields.get("GPSAltitudeRef")
        alt = alt_val
        neg = 1 if str(ref) == "1" else 0
        args += [f"-GPSAltitude={alt}", f"-GPSAltitudeRef#={neg}"]
    if want_off and cur.get("OffsetTimeOriginal") in (None, "") and cur.get("DateTimeOriginal"):
        off = _offset_for(fields["CtxTimezone"], str(cur["DateTimeOriginal"]))
        if off:
            args.append(f"-OffsetTimeOriginal={off}")
    return args


def _gps_args(lat, lon) -> list[str]:
    """exiftool writes the ABSOLUTE value for GPSLatitude/GPSLongitude without a Ref tag --
    a plain "-GPSLatitude=-33.5" silently lands in the northern/eastern hemisphere -- so the
    sign must go into explicit GPSLatitudeRef/GPSLongitudeRef tags."""
    la, lo = float(lat), float(lon)
    return [f"-GPSLatitude={abs(la)}", f"-GPSLatitudeRef={'S' if la < 0 else 'N'}",
            f"-GPSLongitude={abs(lo)}", f"-GPSLongitudeRef={'W' if lo < 0 else 'E'}"]


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [str(v)] if str(v).strip() else []


def _read_image_keywords(file: str, timeout: int) -> list[str]:
    try:
        out = exifpool.run(["exiftool", "-j", "-m", "-IPTC:Keywords", "-XMP-dc:Subject", file],
                             capture_output=True, text=True, timeout=timeout).stdout
        rec = (json.loads(out or "[]") or [{}])[0]
    except Exception:
        return []
    found = _as_list(rec.get("Keywords")) + _as_list(rec.get("Subject"))
    return found


def _merge_keywords(existing: list[str], generated: list[str]) -> list[str]:
    """Keeps the image's own keywords (from camera/Lightroom/...), drops a previous run's
    geo:lat=/geo:lon=/geotagged entries so they are refreshed rather than duplicated, then
    appends the sidecar's current keywords -- de-duplicated, order preserved."""
    kept = [k for k in existing if not (k.startswith("geo:lat=") or k.startswith("geo:lon=") or k == "geotagged")]
    out: list[str] = []
    for k in generated + kept:
        if k not in out:
            out.append(k)
    return out


def _read_current_xmp_fields(xmp_path: str, timeout: int) -> dict:
    """One exiftool call, JSON output -- everything sync_xmp_to_image() might need to
    carry over, whichever subset of it is actually present in the sidecar so far."""
    if not os.path.isfile(xmp_path):
        return {}
    cmd = [
        "exiftool", "-config", MON_CONFIG, "-j", "-m",
        "-XMP-exif:GPSLatitude#", "-XMP-exif:GPSLongitude#", "-XMP-exif:GPSAltitude#", "-XMP-exif:GPSAltitudeRef#",
        "-XMP-mon:CtxTimezone",
        "-XMP-photoshop:Country", "-XMP-photoshop:State", "-XMP-photoshop:City",
        "-XMP-iptcCore:CountryCode", "-XMP-iptcCore:Location", "-XMP-iptcExt:LocationCreatedSublocation",
        "-XMP-dc:Description", "-XMP-dc:Subject",
        xmp_path,
    ]
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(res.stdout or "[]")
        return data[0] if data else {}
    except Exception:
        return {}


def _sync_core(file: str, xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    """Reads back whatever GPS/location/description fields currently sit in the .xmp
    sidecar and writes all of them into the image directly, in one exiftool call.
    Never call this for videos -- same "direct video modification is too risky/slow to
    redo" convention as the rest of this app; callers already gate on `is_video` before
    offering the "ins Bild einbetten" checkbox at all, same as before this change.

    Returns (True, "nichts zu schreiben") when the .xmp has none of these fields yet
    (e.g. embedding is ticked on a module that ran before any geo/description data
    exists) -- not an error, just nothing to do yet."""
    fields = _read_current_xmp_fields(xmp_path, timeout)
    if not fields:
        return True, "nichts zu schreiben"

    args: list[str] = []

    lat, lon = fields.get("GPSLatitude"), fields.get("GPSLongitude")
    try:                                    # empty ('') or unreadable coordinates in the sidecar = no position
        if lat not in (None, "") and lon not in (None, ""):
            args += _gps_args(lat, lon)
    except (TypeError, ValueError):
        pass

    args += _image_extras_args(file, fields, timeout)

    country, state, city = fields.get("Country"), fields.get("State"), fields.get("City")
    country_code, location = fields.get("CountryCode"), fields.get("Location")
    if country:
        args += [f"-XMP-photoshop:Country={country}", f"-IPTC:Country-PrimaryLocationName={country}"]
    if country_code:
        args.append(f"-XMP-iptcCore:CountryCode={country_code}")
    if state:
        args += [f"-XMP-photoshop:State={state}", f"-IPTC:Province-State={state}"]
    if city:
        args += [f"-XMP-photoshop:City={city}", f"-IPTC:City={city}"]
    street = fields.get("LocationCreatedSublocation")
    if location or street:
        # Same as the original script's image branch: the combined "locality, street" goes
        # into both IPTC:Sub-location and XMP-iptcCore:Location of the IMAGE (the sidecar
        # itself keeps locality / street in separate fields, which NFO Maker reads).
        sub = f"{location}, {street}" if (location and street) else (location or street)
        args += [f"-XMP-iptcCore:Location={sub}", f"-IPTC:Sub-location={sub}"]

    description = fields.get("Description")
    if description:
        args += [f"-XMP-dc:Description={description}", f"-IPTC:Caption-Abstract={description}"]
        if aicore.image_description_needs_sync(file, description):
            args.append(f"-EXIF:ImageDescription={description}")

    keywords = _as_list(fields.get("Subject"))
    if keywords:
        merged = _merge_keywords(_read_image_keywords(file, timeout), keywords)
        # Cleared as its OWN exiftool call first (the original script found that a combined
        # clear + rebuild in one invocation duplicated entries), then rebuilt in the main write.
        ok_clear, msg_clear = aicore.exiftool_write_image(file, ["-Keywords=", "-XMP-dc:Subject="], timeout)
        if not ok_clear:
            return False, msg_clear
        for kw in merged:
            args += [f"-Keywords+={kw}", f"-XMP-dc:Subject+={kw}"]

    if not args:
        return True, "nichts zu schreiben"

    # Reuses aicore's own image-writer (not a fresh subprocess.run here) so this gets
    # the same MakerNotes-rebuild-failure retry every other direct-image write in this
    # app already has (some cameras' MakerNotes blocks make a plain exiftool write fail
    # outright) -- no need to duplicate that robustness a second time.
    return aicore.exiftool_write_image(file, args, timeout)


def sync_xmp_to_image(file: str, xmp_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple[bool, str]:
    """Embeds the sidecar's current snapshot (see _sync_core), then fills the IPTC Credits/Origin fields (Date created,
    Source = camera name from the sidecar, Credits) -- only where the image has none, see iptcfill.py."""
    ok, msg = _sync_core(file, xmp_path, timeout)
    if ok:
        from . import iptcfill
        ok_i, msg_i, written = iptcfill.fill_image(file, xmp=xmp_path)
        if not ok_i:
            return False, msg_i or "IPTC-Felder konnten nicht geschrieben werden."
    return ok, msg
