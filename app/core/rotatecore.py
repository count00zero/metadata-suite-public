"""
rotatecore.py -- lossless JPEG rotation for the Datei-Zeitstempel module ("Bilder aufrecht drehen").

A photo shot in landscape but shown upright only because of its EXIF Orientation tag looks
rotated in viewers that ignore the tag (older image viewers, some thumbnails). exiftran -a rotates the
JPEG data itself in 8x8/16x16 blocks, without re-compressing, resets the Orientation tag and
rotates the EXIF thumbnail:

    exiftran -a -i -p [-b] file.jpg      -a  auto (from the Orientation tag)   -i in place
                                         -p keep the file's timestamps         -b backup "file.jpg~"

Edges: when width/height are not multiples of the JPEG block size (usually 16 px), the partial edge
blocks cannot be rotated losslessly. exiftran then pares them off ("-np" would disable that) -- at most
15 pixels at the edge, everything else stays bit-exact. This is what was chosen (alternative would be to skip
such files); the result tells how many pixels were cut.

Only JPEG: HEIC/PNG/WebP/TIFF/RAW are not touched (RAW/DNG never; their Orientation is metadata only).
Sidecar: a ".xmp" whose tiff:Orientation is not 1 is reset to 1 so viewers do not rotate twice.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from . import exifpool

JPEG_EXT = {".jpg", ".jpeg"}
EXIFTRAN_TIMEOUT = 180
LABELS = {2: "gespiegelt", 3: "um 180° gedreht", 4: "vertikal gespiegelt", 5: "transponiert",
          6: "um 90° im Uhrzeigersinn gedreht", 7: "transversal gespiegelt", 8: "um 90° gegen den Uhrzeigersinn gedreht"}


def available() -> bool:
    return shutil.which("exiftran") is not None


def is_jpeg(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in JPEG_EXT


def _exif(path: str, tags: list, timeout: int = 60) -> dict:
    import json
    try:
        res = exifpool.run(["exiftool", "-j", "-n", "-m", "-q", *tags, path], timeout=timeout)
        data = json.loads(res.stdout or "[]")
        return data[0] if data else {}
    except Exception:           # noqa: BLE001
        return {}


def orientation(path: str) -> int:
    """EXIF Orientation 1-8 (1 = normal / missing)."""
    try:
        v = int(_exif(path, ["-Orientation"]).get("Orientation") or 1)
    except (TypeError, ValueError):
        return 1
    return v if 1 <= v <= 8 else 1


def needs_rotation(path: str) -> Optional[dict]:
    """{'orientation': n, 'label': ...} for a JPEG whose Orientation is not 'normal', else None."""
    if not is_jpeg(path):
        return None
    o = orientation(path)
    return None if o == 1 else {"orientation": o, "label": LABELS.get(o, str(o))}


def _dims(path: str) -> tuple:
    d = _exif(path, ["-ImageWidth", "-ImageHeight"])
    return int(d.get("ImageWidth") or 0), int(d.get("ImageHeight") or 0)


def _fix_sidecar(path: str) -> None:
    for xmp in (path + ".xmp", os.path.splitext(path)[0] + ".xmp"):
        if os.path.isfile(xmp):
            d = _exif(xmp, ["-XMP-tiff:Orientation"])
            if d.get("Orientation") not in (None, "", 1):
                try:
                    exifpool.run(["exiftool", "-m", "-overwrite_original", "-n", "-XMP-tiff:Orientation=1", xmp], timeout=60)
                except Exception:       # noqa: BLE001
                    pass
            break


def rotate_lossless(path: str, backup: bool = True) -> dict:
    """Rotate one JPEG upright. Returns {ok, rotated, message, trimmed_px, backup}."""
    out = {"ok": False, "rotated": False, "message": "", "trimmed_px": 0, "backup": ""}
    if not is_jpeg(path):
        out["message"] = "kein JPEG -- wird nicht verlustfrei gedreht"
        return out
    if not available():
        out["message"] = "exiftran ist nicht installiert (Docker-Image neu bauen)"
        return out
    o = orientation(path)
    if o == 1:
        out.update(ok=True, message="bereits aufrecht (Orientation normal)")
        return out
    w0, h0 = _dims(path)
    cmd = ["exiftran", "-a", "-i", "-p"] + (["-b"] if backup else []) + [path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=EXIFTRAN_TIMEOUT)
    except subprocess.TimeoutExpired:
        out["message"] = f"exiftran-Timeout ({EXIFTRAN_TIMEOUT}s)"
        return out
    except OSError as e:
        out["message"] = f"exiftran konnte nicht gestartet werden: {e}"
        return out
    if res.returncode != 0:
        out["message"] = f"exiftran Fehler: {(res.stderr or res.stdout).strip()[:200]}"
        return out
    if orientation(path) != 1:
        out["message"] = "Orientation ist nach exiftran nicht zurückgesetzt -- Datei bitte prüfen"
        return out
    w1, h1 = _dims(path)
    ew, eh = (h0, w0) if o in (5, 6, 7, 8) else (w0, h0)
    out["trimmed_px"] = max(0, ew - w1) + max(0, eh - h1) if (w0 and h0 and w1 and h1) else 0
    _fix_sidecar(path)
    bak = path + "~"
    out.update(ok=True, rotated=True, backup=bak if (backup and os.path.exists(bak)) else "",
               message="verlustfrei gedreht" + (f" ({ew - w1} px rechts / {eh - h1} px unten am Rand abgeschnitten)"
                                                 if out["trimmed_px"] else " (nichts abgeschnitten)"))
    return out
