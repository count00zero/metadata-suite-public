"""
executor.py — applies a resolved timestamp decision to disk: rename, exiftool write,
mtime touch. Python port of the execution loop in Timestamp-Bilder-Videos.sh, so the web
app can call it in-process instead of shelling out to the bash script.

Companion files (a .xmp sidecar, a companion audio track, a generated -thumb/-poster/
-fanart image, or any other same-stem sibling -- see timecore.find_companions()) are no
longer renamed as an automatic, invisible side effect of apply_resolution() the way
companion audio alone used to be: they're now their own selectable rows in the preview
(modules/timestamp_tool/router.py's _rows_from_resolutions), applied via apply_companion()
below, so a person can see and choose exactly which companions get renamed alongside their
primary file instead of it happening silently (or, for everything but audio, not at all --
a .xmp sidecar or a video's own thumbnail used to simply go stale, left behind under the
OLD filename after a rename, since nothing here ever looked for them before).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from . import exifpool
from dataclasses import dataclass, field

from . import timecore
from .timecore import Resolution


@dataclass
class ExecutionResult:
    path: str
    renamed_to: str = ""
    exif_written: bool = False
    exif_error: str = ""
    exif_note: str = ""
    touched: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "renamed_to": self.renamed_to,
            "exif_written": self.exif_written,
            "exif_error": self.exif_error,
            "exif_note": self.exif_note,
            "touched": self.touched,
            "error": self.error,
        }


def _make_model_args(res: Resolution, path: str) -> list[str]:
    """exiftool args that add the camera's Make/Model to the file itself, but ONLY when the
    file doesn't already carry them (a camera that wrote real Make/Model tags is left
    exactly as it was). The values come from this scan's own detection -- the real EXIF
    tags, or for a camera like the Insta360 Luna Ultra (whose videos carry NO Make/Model
    metadata at all) the byte-signature sniff (timecore.sniff_camera_from_bytes) -- so a
    write-back turns that sniffed identity into real tags every later scan, viewer and
    .xmp refresh can read directly. Generic -Make/-Model lets exiftool pick the right
    place per container (EXIF for photos; UserData + XMP-tiff for MP4/MOV)."""
    make, model = (res.camera_make or "").strip(), (res.camera_model or "").strip()
    if not make and not model:
        return []
    try:
        out = exifpool.run(["exiftool", "-s3", "-Make", "-Model", path],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return []
    if out.strip():
        return []  # already has Make and/or Model -- leave the camera's own tags alone
    args = []
    if make:
        args.append(f"-Make={make}")
    if model:
        args.append(f"-Model={model}")
    return args


def apply_resolution(res: Resolution, do_rename: bool, do_exifwrite: bool,
                      exiftool_timeout_secs: int = 120) -> ExecutionResult:
    """Apply one resolved file: rename, exiftool write, mtime touch. Mirrors the bash
    script's loop body exactly, including skipping exiftool writes for .nfo files.
    Companion files (see module docstring) are applied separately via apply_companion(),
    one call per selected companion row -- not from in here."""
    result = ExecutionResult(path=res.path)
    current_path = res.path

    if do_rename and res.target_path != current_path:
        try:
            if os.path.exists(res.target_path):
                result.error = f"Ziel existiert bereits, Rename übersprungen: {res.target_path}"
            else:
                shutil.move(current_path, res.target_path)
                current_path = res.target_path
                result.renamed_to = res.target_path
        except OSError as e:
            result.error = f"Rename fehlgeschlagen: {e}"

    if res.ext.lower() == ".nfo" or not do_exifwrite:
        pass
    elif res.ext.lower() in timecore.VIDEO_EXT:
        # Videos are never rewritten (decision: too slow/risky for large files, and Immich
        # reads the date from the .xmp sidecar anyway -- see "xmp-Sidecar-Datum setzen").
        result.exif_note = "Video: kein EXIF-Rückschreiben – Datum nur über .xmp-Sidecar"
    else:
        base = [
            f"-alldates={res.zieldate}", f"-filemodifydate={res.zieldate}",
            f"-MediaCreateDate={res.zieldate}",
            f"-xmp:alldates={res.zieldate}", f"-XMP-exif:DateTimeDigitized={res.zieldate}",
            f"-quicktime:time:all={res.zieldate}",
            *([f"-TimeZoneOffset={res.offset_short}"] if res.offset_short else []),
            f"-exif:gpstimestamp={res.zieldate}", f"-exif:gpsdatestamp={res.zieldate}",
        ]
        # Photos: also write the standard EXIF offset tags (what most cameras/phones write and
        # what the original Set-GPS script wrote), but only when the zone is real information
        # (GPS / embedded offset / UTC-diff / borrowed sibling) -- never a Fallback-Zone guess.
        offset_args = []
        if res.ext.lower() in timecore.IMAGE_EXT and res.tz_confident:
            offset_args = [f"-OffsetTimeOriginal={res.offset}", f"-OffsetTimeDigitized={res.offset}",
                           f"-OffsetTime={res.offset}"]
            base = base + offset_args
        mm = _make_model_args(res, current_path)
        # Some real-world files make exiftool's QuickTime writer fail outright (e.g.
        # "Not a HASH reference at WriteQuickTime.pl line 1732" on Insta360 videos) -- a
        # failed write leaves the file untouched (-overwrite_original is atomic), so retry
        # with progressively less: first without the optional Make/Model, then also without
        # the explicit QuickTime time block. The first error is kept and reported either way.
        attempts = []
        if mm:
            attempts.append(("", base + mm))
        attempts.append(("ohne Make/Model" if mm else "", base))
        attempts.append(("ohne Make/Model und ohne -quicktime:time:all",
                         [a for a in base if not a.startswith("-quicktime:time:all")]))
        first_error = ""
        for label, args in attempts:
            cmd = ["timeout", str(exiftool_timeout_secs), "exiftool", "-m", "-q", "-api", "LargeFileSupport=1",
                   *args, "-P", "-overwrite_original", current_path]
            try:
                proc = exifpool.run(cmd, capture_output=True, text=True)
            except Exception as e:
                first_error = first_error or str(e)
                break
            if proc.returncode == 0:
                result.exif_written = True
                if first_error:
                    result.exif_note = (f"Erster Versuch fehlgeschlagen ({first_error}) -- "
                                        f"geschrieben {label}.")
                break
            first_error = first_error or proc.stderr.strip() or "exiftool-Write fehlgeschlagen/Timeout"
        if not result.exif_written and first_error:
            # Last resort: write each tag group on its own, so whatever the file's QuickTime
            # structure CAN take still gets written and we learn exactly which group breaks it
            # (reported in exif_note) instead of one opaque all-or-nothing failure.
            groups = [
                ("alldates", [f"-alldates={res.zieldate}"]),
                ("MediaCreateDate", [f"-MediaCreateDate={res.zieldate}"]),
                ("xmp:alldates", [f"-xmp:alldates={res.zieldate}", f"-XMP-exif:DateTimeDigitized={res.zieldate}"]),
                ("quicktime:time:all", [f"-quicktime:time:all={res.zieldate}"]),
                *([("TimeZoneOffset", [f"-TimeZoneOffset={res.offset_short}"])] if res.offset_short else []),
                *([("OffsetTime*", offset_args)] if offset_args else []),
                ("GPS-Zeit", [f"-exif:gpstimestamp={res.zieldate}", f"-exif:gpsdatestamp={res.zieldate}"]),
            ]
            ok_groups, bad_groups = [], []
            for gname, gargs in groups:
                cmd = ["timeout", str(exiftool_timeout_secs), "exiftool", "-m", "-q", "-api", "LargeFileSupport=1",
                       *gargs, "-P", "-overwrite_original", current_path]
                try:
                    p = exifpool.run(cmd, capture_output=True, text=True)
                    (ok_groups if p.returncode == 0 else bad_groups).append(gname)
                except Exception:
                    bad_groups.append(gname)
            if ok_groups:
                result.exif_written = True
                result.exif_note = (f"Gesamt-Write fehlgeschlagen ({first_error}) -- einzeln geschrieben: "
                                    f"{', '.join(ok_groups)}"
                                    + (f"; nicht schreibbar: {', '.join(bad_groups)}" if bad_groups else "") + ".")
            else:
                result.exif_error = (f"{first_error} (auch einzelne Tag-Gruppen nicht schreibbar -- "
                                     f"die Datei-Struktur verträgt keine exiftool-Schreibzugriffe; "
                                     f".xmp-Sidecar und Dateizeit sind trotzdem gesetzt)")
        elif not result.exif_written:
            result.exif_error = first_error

    if result.exif_written and res.ext.lower() in timecore.IMAGE_EXT:
        # IPTC "Credits/Origin" (Date created, Source = camera name, Credits) -- only where empty, never overwritten
        try:
            from . import iptcfill
            ok_i, msg_i, _w = iptcfill.fill_image(current_path)
            if not ok_i and msg_i:
                result.exif_note = (result.exif_note + " " if result.exif_note else "") + f"IPTC-Felder nicht geschrieben: {msg_i}"
        except Exception:           # noqa: BLE001 - an extra must never break the main write
            pass

    try:
        os.utime(current_path, (res.epoch, res.epoch))
        result.touched = True
    except OSError as e:
        result.error = (result.error + " " if result.error else "") + f"touch fehlgeschlagen: {e}"

    return result


def apply_companion(row: dict, do_rename: bool) -> ExecutionResult:
    """Applies one companion row (see timecore.find_companions()/module docstring): a
    rename to the primary's new target name with the companion's own suffix/extension
    preserved (row["target_path"], already computed by the preview step), plus an mtime
    touch to the primary's own resolved epoch (row["epoch"]) -- never an exiftool write
    (a .xmp sidecar, a thumbnail, or an audio track has no "alldates"/GPS tags of its own
    worth writing the same way a real media file does; its .xmp CONTENT, if any, is
    GPS Sidecar's/AI Description's job, not this module's). Returns an ExecutionResult so
    the same _results.html template renders a companion's outcome identically to a
    primary file's (exif_written/exif_error simply stay at their defaults)."""
    result = ExecutionResult(path=row["path"])
    current_path = row["path"]

    if do_rename and row["target_path"] != current_path:
        try:
            if os.path.exists(row["target_path"]):
                result.error = f"Ziel existiert bereits, Rename übersprungen: {row['target_path']}"
            else:
                shutil.move(current_path, row["target_path"])
                current_path = row["target_path"]
                result.renamed_to = row["target_path"]
        except OSError as e:
            result.error = f"Rename fehlgeschlagen: {e}"

    try:
        os.utime(current_path, (row["epoch"], row["epoch"]))
        result.touched = True
    except OSError as e:
        result.error = (result.error + " " if result.error else "") + f"touch fehlgeschlagen: {e}"

    return result
