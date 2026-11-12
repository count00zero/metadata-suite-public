"""
devicenames.py — turns a camera's raw EXIF Make/Model ("Samsung SM-S921B", "Xiaomi
22101316UG", ...) into something a person can read, in three stages (first match wins):

  1. Your own overrides file (config/camera_names.json, path via CAMERA_NAMES_FILE) --
     lets you name individual devices ("SM-S921B" -> "Samsung Galaxy S24").
  2. NFO Maker's CAMERA_SOURCE_RULES table (core/nfocore.py) -- the same fnmatch patterns
     and personal labels NFO Maker already uses, so both modules call a device the same
     thing without maintaining two tables.
  3. Google's Play "Supported Devices" list, kept OUTSIDE the image next to camera_names.json
     (/config/android_devices.json, or ANDROID_DEVICES_FILE; model code -> brand + marketing name,
     ~43k Android devices). Fetch/refresh it once with
         docker compose exec metadata-suite python -m app.core.devicenames update
     The app itself never goes online; without the file the raw "Make Model" is shown.
  Otherwise the raw "Make Model" is returned unchanged.

Display/filter only: the raw Make/Model is what gets written into EXIF/XMP, never the
translated name, so the real device code stays recoverable.

Overrides file format (patterns are case-insensitive fnmatch, matched against the Model and
against "Make Model"; first matching rule wins):
    {"rules": [{"match": ["SM-S921B", "SM-S92*"], "name": "Samsung Galaxy S24"}]}
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
from typing import Optional

from . import nfocore

OVERRIDES_PATH = os.environ.get(
    "CAMERA_NAMES_FILE",
    os.path.join(
        os.path.dirname(os.path.abspath(os.environ.get(
            "TIMESTAMP_PRESETS_FILE",
            os.path.join(os.path.dirname(__file__), "..", "..", "config", "presets.json")))),
        "camera_names.json"),   # same folder as presets.json (= /config in Docker)
)
ANDROID_DB_PATH = os.environ.get("ANDROID_DEVICES_FILE") or os.path.join(os.path.dirname(OVERRIDES_PATH), "android_devices.json")
ANDROID_CSV_URL = "https://storage.googleapis.com/play_public/supported_devices.csv"

SOURCE_OVERRIDE = "override"
SOURCE_NFO_RULE = "nfo-rule"
SOURCE_GOOGLE = "google"
SOURCE_RAW = "raw"
SOURCE_LABELS = {
    SOURCE_OVERRIDE: "eigene Regel",
    SOURCE_NFO_RULE: "NFO-Maker-Regel",
    SOURCE_GOOGLE: "Google-Geräteliste",
    SOURCE_RAW: "Rohwert",
}

# NFO Maker's table also holds SOFTWARE/encoder rules (it scans every string in a video's
# metadata, not just the camera model) -- e.g. `*Google*` -> "Google Movie" would otherwise
# swallow a "Google Pixel 9". These labels never name a device, so they are skipped here.
_NFO_NON_DEVICE_LABELS = {"Google Movie", "Adobe Premiere Pro", "Video Converter",
                          "Videoediting Software"}

_lock = threading.Lock()
_android_db: Optional[dict] = None
_android_mtime: Optional[float] = None
_overrides_cache: tuple = (None, [])  # (mtime, rules)


def _load_android_db() -> dict:
    """Re-read whenever the file changes (so `update` takes effect without a restart)."""
    global _android_db, _android_mtime
    with _lock:
        try:
            mtime = os.path.getmtime(ANDROID_DB_PATH)
        except OSError:
            mtime = None
        if _android_db is None or mtime != _android_mtime:
            try:
                with open(ANDROID_DB_PATH, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
            # case-insensitive lookup; on a collision keep the first entry
            db: dict = {}
            for code, val in raw.items():
                db.setdefault(code.strip().lower(), val)
            _android_db, _android_mtime = db, mtime
        return _android_db


def parse_supported_devices(raw: bytes) -> dict:
    """Google's CSV (UTF-16 with BOM; UTF-8 fallback) -> {model_code: [brand, marketing_name]}."""
    import csv
    import io
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    next(reader, None)          # header: Retail Branding, Marketing Name, Device, Model
    mapping = {}
    for row in reader:
        if len(row) >= 4 and row[3].strip() and row[1].strip():
            mapping[row[3].strip()] = [row[0].strip(), row[1].strip()]
    return mapping


def update_android_db(url: str = ANDROID_CSV_URL, path: Optional[str] = None) -> int:
    """Download Google's list and write it atomically to ANDROID_DB_PATH. Returns the number of devices;
    the existing file is kept when the download looks wrong."""
    import urllib.request
    path = path or ANDROID_DB_PATH
    with urllib.request.urlopen(url, timeout=60) as resp:
        mapping = parse_supported_devices(resp.read())
    if len(mapping) < 10000:
        raise RuntimeError(f"nur {len(mapping)} Zeilen gelesen -- bestehende Datei bleibt unverändert")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)
    return len(mapping)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["update"]:
        try:
            print(f"{update_android_db()} Geräte nach {os.path.abspath(ANDROID_DB_PATH)} geschrieben")
        except Exception as e:      # noqa: BLE001
            print(f"Fehler: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Aufruf: python -m app.core.devicenames update", file=sys.stderr)
        sys.exit(2)


def _load_overrides() -> list:
    """Re-read only when the file's mtime changes, so edits apply without a restart."""
    global _overrides_cache
    path = os.path.normpath(OVERRIDES_PATH)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    with _lock:
        if _overrides_cache[0] == mtime:
            return _overrides_cache[1]
        rules = []
        try:
            with open(path, encoding="utf-8") as f:
                for r in (json.load(f).get("rules") or []):
                    pats = r.get("match") or []
                    if isinstance(pats, str):
                        pats = [pats]
                    name = (r.get("name") or "").strip()
                    if pats and name:
                        rules.append(([str(p).lower() for p in pats], name))
        except (OSError, ValueError, AttributeError):
            rules = []
        _overrides_cache = (mtime, rules)
        return rules


def override_for(values) -> Optional[str]:
    """Name of the first own camera_names.json rule that matches any of `values` (case-insensitive fnmatch),
    else None. Lets NFO Maker apply the user's rules to every string it finds in a video."""
    rules = _load_overrides()
    if not rules:
        return None
    vals = [str(v).strip().lower() for v in values if v and str(v).strip()]
    for pats, name in rules:
        if any(fnmatch.fnmatchcase(v, p) for v in vals for p in pats):
            return name
    return None


def overrides_status() -> dict:
    """{"path", "exists", "rules", "error"} -- why a name was NOT translated: no file at that path, a file that does
    not parse (one stray comma silently disables every rule), or simply no matching rule."""
    path = os.path.normpath(OVERRIDES_PATH)
    st = {"path": path, "exists": os.path.isfile(path), "rules": 0, "error": ""}
    if not st["exists"]:
        return st
    try:
        with open(path, encoding="utf-8") as f:
            st["rules"] = len(json.load(f).get("rules") or [])
    except (OSError, ValueError, AttributeError) as e:
        st["error"] = f"{type(e).__name__}: {e}"
    return st


def _raw_name(make: str, model: str) -> str:
    make, model = (make or "").strip(), (model or "").strip()
    if make and model and model.lower().startswith(make.lower()):
        return model  # "Insta360 Luna Ultra" with make "Insta360" -- don't double it
    return f"{make} {model}".strip()


def _with_brand(brand: str, name: str) -> str:
    brand, name = (brand or "").strip(), (name or "").strip()
    if not brand or name.lower().startswith(brand.lower()):
        return name
    return f"{brand} {name}"


def translate(make: str, model: str) -> dict:
    """Returns {"label", "source", "raw"} -- label is the human-readable name (== raw when
    nothing better is known), source one of SOURCE_*, raw the untouched "Make Model"."""
    make, model = (make or "").strip(), (model or "").strip()
    raw = _raw_name(make, model)
    if not raw:
        return {"label": "", "source": "", "raw": ""}
    candidates = [c for c in dict.fromkeys([model, raw]) if c]

    for pats, name in _load_overrides():
        if any(fnmatch.fnmatchcase(c.lower(), p) for c in candidates for p in pats):
            return {"label": name, "source": SOURCE_OVERRIDE, "raw": raw}

    for c in candidates:
        vsource, _helper = nfocore.match_camera_source(c)
        if vsource and vsource not in _NFO_NON_DEVICE_LABELS:
            return {"label": vsource, "source": SOURCE_NFO_RULE, "raw": raw}

    db = _load_android_db()
    for c in candidates:
        hit = db.get(c.lower())
        if hit:
            return {"label": _with_brand(hit[0], hit[1]), "source": SOURCE_GOOGLE, "raw": raw}

    return {"label": raw, "source": SOURCE_RAW, "raw": raw}


def camera_matches(make: str, model: str, needle: str) -> bool:
    """Case-insensitive substring match of `needle` against the raw Make/Model AND the
    translated device name (own rule / NFO Maker rule / Google list) -- so "Luna Ultra",
    "SM-S921B", "S24" and "Galaxy" all find the same phone. Empty needle matches everything."""
    needle = (needle or "").strip().lower()
    if not needle:
        return True
    haystack = f"{make or ''} {model or ''} {translate(make, model)['label']}".strip().lower()
    return needle in haystack
