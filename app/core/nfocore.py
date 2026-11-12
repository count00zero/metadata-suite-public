"""
nfocore.py — Module #6 (NFO Maker): generates Kodi .nfo files + poster/fanart/thumb/
folder.jpg images for home videos, ported from HomeVideo_NFO_Maker_new.sh.

Deliberate scope decision (confirmed with the maintainer before building this): this module is
READ-ONLY for GPS/location and AI-description data. The CLI script fetches a missing .xmp
geo sidecar itself (shells out to Set-GPS-Data-via-traccar-and-dawarich.sh in single-file
mode) and can call an AI provider itself for the plot description if the sidecar has none.
Both of those are already separate modules here (#2 GPS Sidecar, #3 AI Description) that
write exactly those .xmp fields -- so this module only ever READS whatever's already in a
video's .xmp sidecar (read_geo_sidecar()) and never fetches or generates anything itself.
A video with no sidecar yet, or a sidecar with no AI description yet, simply gets an NFO
without that piece -- run GPS Sidecar/AI Description first for that. This also means
--force-geo (the CLI's "re-fetch geo from the GPS script" flag) has no equivalent here at
all, since there's no fetch step left to force.

Everything else (camera/software source detection, mediainfo/exiftool metadata extraction,
thumbnail extraction via ffmpeg, the NFO XML itself, the repair-ai/reintegrate-ai/
repair-italic scan modes, -O/--older protection, -T/--force-timestamp) is ported as
faithfully as possible -- including quirks that look like bugs but are the CLI's actual
existing behavior (e.g. match_camera_source() being called over every candidate field
with no early exit, so the LAST matching field in VSourceArray order wins, not the first).
"""
from __future__ import annotations

from . import skipdirs
import fnmatch
from html import unescape as html_unescape
import json
import os
import re
import subprocess
from . import exifpool
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

from . import mtimecore, settings
from .timecore import VIDEO_EXT

EXIFTOOL_TIMEOUT = 60
MEDIAINFO_TIMEOUT = 60
FFMPEG_TIMEOUT = 120

# Same extension set find_files()/VIDEO_EXT already covers, plus the three the CLI's own
# find -iregex lists that aren't in VIDEO_EXT (.wmv/.flv) -- kept as its own constant
# since this module's "which files are candidates at all" question is specific to the
# CLI's own -iregex list, not necessarily identical to the rest of this app's VIDEO_EXT.
NFO_VIDEO_EXT = VIDEO_EXT | {".wmv", ".flv", ".m4v"}


def is_nfo_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in NFO_VIDEO_EXT


def find_videos(directory: str, depth: int) -> list[str]:
    """Same as the CLI's `find "$DIRECTORY" -maxdepth "$TIEFE" -iregex '...'` -- a single
    file is returned as-is (matching the CLI's own "Detect if input is a single file"
    branch); for a directory, every matching video up to `depth` levels below it."""
    if os.path.isfile(directory):
        return [directory] if is_nfo_video(directory) else []
    if not os.path.isdir(directory):
        return []
    base_depth = directory.rstrip("/").count("/")
    out = []
    for root, dirs, files in os.walk(directory):
        rel_depth = root.rstrip("/").count("/") - base_depth
        if rel_depth >= depth:
            dirs[:] = []
        skipdirs.prune(dirs)
        for f in files:
            if is_nfo_video(f):
                out.append(os.path.join(root, f))
    return sorted(out)


# ---------------------------------------------------------------------------
# Camera / recording-software source detection -- byte-for-byte port of
# match_camera_source()'s case statement. Bash case patterns support the same glob
# syntax as fnmatch (*, ?, [...]), case-SENSITIVE (no nocasematch in the original).
# Each tuple is (patterns, vsource, helper) -- `helper` is only ever "Galaxy", used
# for the Galaxy S24 special case exactly like the CLI's $Helper side channel.
# ---------------------------------------------------------------------------
CAMERA_SOURCE_RULES: list[tuple[tuple[str, ...], str, Optional[str]]] = [
    (("iPhone*",), "Apple iPhone", None),
    (("DVCAM",), "DV-Cam alt (SD)", None),
    (("DV",), "DV-Cam alt (SD)", None),
    (("GoPro*",), "GoPro Hero 7", None),
    (("DJI OsmoAction3",), "DJI Osmo Action 3", None),
    (("9", "8?0?0", "10", "6*"), "Altes Android Smartphone", None),
    (("Xiaomi*", "14", "13"), "Xiaomi Smartphone", None),
    (("TG?6",), "Olympus TG-6", None),
    (("OLYMPUS*",), "Olympus Digicam alt", None),
    (("12", "Samsung?SM-A53*"), "Samsung Galaxy A53", None),
    # "Galaxy S24" literal was the only pattern here originally, matching whatever exact
    # string the CLI's own test files happened to carry. Real Samsung EXIF "Model" values
    # are the model NUMBER, not the marketing name (SM-S921B/S921U/S921N/S921W = S24,
    # SM-S926* = S24+, SM-S928* = S24 Ultra, SM-S721* = S24 FE) -- the literal-only pattern
    # never matched those, so a newer S24 recording fell through to a generic, dangerously
    # broad rule below (patterns like "10"/"6*" match all sorts of unrelated short field
    # values) and got mislabeled "Altes Android Smartphone". Matching the model-number
    # family directly is more reliable than the marketing name was ever going to be.
    (("Galaxy S24", "SM-S92*", "SM-S721*"), "Samsung Galaxy S24", "Galaxy"),
    (("Sony?DSC-WX300",), "Sony DSC-WX300", None),
    (("Sony?DSC-HX9V",), "Sony DSC-HX9V", None),
    (("Sony?PSP",), "Sony DSC-HX90V", None),
    (("SANYO*",), "SANYO DIGITAL CAMERA", None),
    (("FUJI*",), "FUJI Camera (old)", None),
    (("Canon*",), "Canon Camera (old)", None),
    (("pana",), "Panasonic DMC-FX37", None),
    (("DMC-FZ1000",), "Panasonic DMC-FZ1000", None),
    (("VIDEOEDIT",), "Videoediting Software", None),
    (("Adobe*",), "Adobe Premiere Pro", None),
    (("*Google*",), "Google Movie", None),
    (("3GPP*",), "Uraltes Handy", None),
    (("NVT-IM", "Ambarell*", "Unknown??icat?"), "Alte Full-HD ActionCam", None),
    (("WinX*",), "Video Converter", None),
    (("TG-6",), "Olympus TG-6", None),
    (("DJI OsmoAction5 Pro",), "DJI Osmo Action 5 PRO", None),
    (("DJI OsmoAction6",), "DJI Osmo Action 6", None),
    (("Insta360 Luna Ultra",), "Insta360 Luna Ultra", None),
]


def match_camera_source(value: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (vsource, helper) if `value` matches a rule, else (None, None). Callers
    must replicate the CLI's own "no early exit" loop (see resolve_camera_source()
    below) -- a non-match here must NOT overwrite a previous match."""
    if not value:
        return None, None
    for patterns, vsource, helper in CAMERA_SOURCE_RULES:
        if any(fnmatch.fnmatchcase(value, p) for p in patterns):
            return vsource, helper
    return None, None


def resolve_camera_source(curated_fields: list[str], all_strings: list[str]) -> tuple[str, str]:
    """Faithful port of the main loop's camera-detection logic:
    1. Scan every curated field IN ORDER, calling match_camera_source() for each --
       deliberately no early exit, so the LAST field (in VSourceArray order) that
       matches wins, not the first (this is the CLI's actual existing behavior).
    2. If nothing in the curated fields matched, fall back to scanning every string
       value found anywhere in the mediainfo/exiftool JSON (already deduplicated,
       sorted, and filtered to exclude pure-numeric strings and values already tried
       in step 1) -- THIS loop *does* stop at the first match, matching the CLI.
    3. If still nothing, VSource="Unknown" (reported to the caller as "" here --
       callers apply the "Unknown" fallback label themselves, consistent with the
       Helper/Galaxy special-case override being applied by the caller too).
    Returns (vsource, helper) -- vsource is "" if nothing at all matched.
    """
    vsource, helper = "", ""
    for value in curated_fields:
        v, h = match_camera_source(value)
        if v is not None:
            vsource = v
        if h is not None:
            helper = h

    if not vsource:
        tried = set(curated_fields)
        for candidate in all_strings:
            if not candidate or candidate == "null" or candidate in tried:
                continue
            if re.fullmatch(r"\d+(\.\d+)?", candidate):
                continue
            v, h = match_camera_source(candidate)
            if v is not None:
                vsource, helper = v, (h or helper)
                break

    return vsource, helper


# ---------------------------------------------------------------------------
# .xmp geo sidecar — READ ONLY (see module docstring). Same tag set as the CLI's
# read_geo_sidecar(): XMP-photoshop:Country/State/City, XMP-iptcExt's
# LocationCreatedProvinceState/LocationCreatedSublocation (County/Locality),
# XMP-iptcCore:Location (Street), and XMP-dc:Description (the AI description, if any
# was already written there by the AI Description module).
# ---------------------------------------------------------------------------

_GEO_TAGS = [
    "-XMP-photoshop:Country", "-XMP-photoshop:State", "-XMP-iptcExt:LocationCreatedProvinceState",
    "-XMP-photoshop:City", "-XMP-iptcCore:Location", "-XMP-iptcExt:LocationCreatedSublocation",
    "-XMP-dc:Description",
]


@dataclass
class GeoSidecar:
    exists: bool = False
    country: str = ""
    state: str = ""
    county: str = ""
    city: str = ""
    locality: str = ""
    street: str = ""
    ai_description: str = ""
    sight: str = ""


def read_geo_sidecar(sidecar_path: str, timeout: int = EXIFTOOL_TIMEOUT) -> GeoSidecar:
    if not os.path.isfile(sidecar_path):
        return GeoSidecar()
    try:
        res = exifpool.run(
            ["exiftool", "-m", "-T", "-api", "LargeFileSupport=1", *_GEO_TAGS, sidecar_path],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return GeoSidecar(exists=True)

    parts = (res.stdout or "").rstrip("\n").split("\t")
    parts += [""] * (7 - len(parts))
    vals = ["" if p in ("-", "") else p for p in parts[:7]]
    return GeoSidecar(
        exists=True, country=vals[0], state=vals[1], county=vals[2],
        city=vals[3], locality=vals[4], street=vals[5], ai_description=vals[6],
        sight=read_mon_fields(sidecar_path, ("CtxSight",)).get("CtxSight", ""),
    )


# ---------------------------------------------------------------------------
# XMP-mon fields (written by GPS Sidecar / AI-Beschreibung) -- read straight from the sidecar text,
# no exiftool call and no custom config needed.
# ---------------------------------------------------------------------------

_MON_NS = "http://ns.neuhofer.home/media/1.0/"


PLOT_FIELDS = ("CtxWeather", "CtxTempC", "CtxWindKmh", "CtxWindText", "CtxPrecipMm", "CtxDaylight", "CtxSunrise", "CtxSunset",
               "CtxMoon", "CtxHoliday", "CtxTrip", "CtxPois", "CtxCountry", "CtxCountryInfo")


def read_mon_fields(sidecar_path: str, names=("CameraName", "CameraNameSource") + PLOT_FIELDS) -> dict:
    """{field: value} for the XMP-mon:* fields present in the sidecar (only non-empty ones)."""
    try:
        with open(sidecar_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    prefixes = set(re.findall(r"xmlns:([A-Za-z0-9_]+)\s*=\s*[\"']" + re.escape(_MON_NS) + r"[\"']", text)) or {"mon"}
    out = {}
    for pre in prefixes:
        for n in names:
            m = (re.search(rf"<{pre}:{n}(?:\s[^>]*)?>(.*?)</{pre}:{n}>", text, re.S)
                 or re.search(rf"\b{pre}:{n}\s*=\s*\"([^\"]*)\"", text)
                 or re.search(rf"\b{pre}:{n}\s*=\s*'([^']*)'", text))
            if m:
                v = html_unescape(m.group(1)).strip()
                if v:
                    out.setdefault(n, v)
    return out


def _num(v: str) -> str:
    try:
        f = float(v)
        return f"{f:.0f}" if abs(f - round(f)) < 0.05 else f"{f:.1f}"
    except (TypeError, ValueError):
        return v


def plot_context_mode() -> str:
    """NFO_PLOT_CONTEXT: 'full' (default: weather, wind, rain, light, moon, holiday, trip, sights, country info),
    'basic' (temperature + weather only, as before) or 'off'."""
    v = os.environ.get("NFO_PLOT_CONTEXT", "full").strip().lower()
    return v if v in ("full", "basic", "off") else "full"


def build_context_plot_line(mon: dict, mode: str = "") -> str:
    """' [CR][COLOR lightblue]Temp: 11 °C[/COLOR][CR][COLOR lightblue]Wetter: Regen[/COLOR]...' -- only the parts that
    are in the sidecar, '' when none is. Order: weather, wind, rain, light, moon, holiday, trip, sights, country."""
    mode = mode or plot_context_mode()
    mon = mon or {}
    if mode == "off":
        return ""
    parts = []
    if mon.get("CtxTempC"):
        parts.append(f"Temp: {_num(mon['CtxTempC'])} \u00b0C")
    if mon.get("CtxWeather"):
        parts.append(f"Wetter: {mon['CtxWeather']}")
    if mode == "full":
        if mon.get("CtxWindText") or mon.get("CtxWindKmh"):
            bits = [mon["CtxWindText"]] if mon.get("CtxWindText") else []
            if mon.get("CtxWindKmh"):
                bits.append(f"({_num(mon['CtxWindKmh'])} km/h)")
            parts.append("Wind: " + " ".join(bits))
        try:
            if mon.get("CtxPrecipMm") and float(mon["CtxPrecipMm"]) >= 0.1:
                parts.append(f"Niederschlag: {_num(mon['CtxPrecipMm'])} mm")
        except ValueError:
            pass
        if mon.get("CtxDaylight"):
            sun = " \u00b7 ".join(x for x in (f"Aufgang {mon['CtxSunrise']}" if mon.get("CtxSunrise") else "",
                                              f"Untergang {mon['CtxSunset']}" if mon.get("CtxSunset") else "") if x)
            parts.append(f"Licht: {mon['CtxDaylight']}" + (f" ({sun})" if sun else ""))
        if mon.get("CtxMoon"):
            parts.append(f"Mond: {mon['CtxMoon']}")
        if mon.get("CtxHoliday"):
            parts.append(f"Feiertag: {mon['CtxHoliday']}")
        if mon.get("CtxTrip"):
            parts.append(f"Wo wars: {mon['CtxTrip']}")      # folder/trip name -- single days get it too, so not "Reise"
        if mon.get("CtxPois"):
            parts.append(f"In der N\u00e4he: {mon['CtxPois']}")
        if mon.get("CtxCountry") and mon.get("CtxCountryInfo"):
            parts.append(f"Land: {mon['CtxCountry']} \u2013 {mon['CtxCountryInfo']}")
    return "".join(f"[CR][COLOR lightblue]{xml_escape(p)}[/COLOR]" for p in parts)


_CTX_LABELS = ("Temp:", "Wetter:", "Wind:", "Niederschlag:", "Licht:", "Mond:", "Feiertag:", "Wo wars:", "Reise:", "In der N\u00e4he:", "Land:")
_CTX_SEG = re.compile(r"\[CR\]\[COLOR lightblue\](.*?)\[/COLOR\]", re.S)


def nfo_has_context(nfo_text: str, context_line: str) -> bool:
    """True when the .nfo's plot already carries exactly this context block."""
    return bool(context_line) and context_line in nfo_text


def patch_nfo_context(nfo_path: str, context_line: str) -> tuple:
    """(changed, message). Replaces the light-blue context block (Temp/Wetter/Wind/Licht/Mond/...) at the end of an
    EXISTING .nfo's <plot> with `context_line`, everything else stays. NFO_PLOT_CONTEXT=off or an empty line = untouched."""
    if not context_line or plot_context_mode() == "off":
        return False, "kein Kontext f\u00fcr die NFO"
    try:
        with open(nfo_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        return False, f".nfo nicht lesbar: {e}"
    m = re.search(r"<plot>(.*?)</plot>", text, re.S)
    if not m:
        return False, ".nfo hat kein <plot>"
    plot = m.group(1)
    kept = _CTX_SEG.sub(lambda s: "" if html_unescape(s.group(1)).strip().startswith(_CTX_LABELS) else s.group(0), plot)
    new_plot = kept + context_line
    if new_plot == plot:
        return False, "unver\u00e4ndert"
    try:
        with open(nfo_path, "w", encoding="utf-8") as fh:
            fh.write(text[:m.start(1)] + new_plot + text[m.end(1):])
    except OSError as e:
        return False, f".nfo nicht schreibbar: {e}"
    return True, ""


_ORT_SEG = re.compile(r"Ort: \[COLOR (\w+)\](.*?)\[/COLOR\]", re.S)


def patch_nfo_location(nfo_path: str, geo: "GeoSidecar") -> tuple:
    """(changed, message). Rewrites the "Ort: ..." line of an EXISTING .nfo from the sidecar's current place fields
    (same rule as build_geo_plot_line, colour kept); everything else stays."""
    try:
        with open(nfo_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        return False, f".nfo nicht lesbar: {e}"
    m = _ORT_SEG.search(text)
    if not m:
        return False, "keine Ort-Zeile in der .nfo"
    new = build_geo_plot_line(geo, m.group(1))
    if not new or new == m.group(0) or "No Location" in new:
        return False, "unver\u00e4ndert"
    try:
        with open(nfo_path, "w", encoding="utf-8") as fh:
            fh.write(text[:m.start()] + new + text[m.end():])
    except OSError as e:
        return False, f".nfo nicht schreibbar: {e}"
    return True, ""


def camera_name_from_sidecar(sidecar_path: str) -> tuple:
    """(name, source) of the camera as GPS Sidecar stored it. A name that was only the raw Make/Model
    is re-translated with the CURRENT camera_names.json (a rule added later still applies)."""
    mon = read_mon_fields(sidecar_path, ("CameraName", "CameraNameSource"))
    name, src = mon.get("CameraName", ""), mon.get("CameraNameSource", "")
    if name and src in ("", "raw"):
        try:
            from . import devicenames
            res = read_sidecar_make_model(sidecar_path)
            if res:
                t = devicenames.translate(*res)
                if t["label"] and t["source"] != "raw":
                    return t["label"], t["source"]
        except Exception:    # noqa: BLE001
            pass
    return name, src


def read_sidecar_make_model(sidecar_path: str):
    """(make, model) from the sidecar's XMP-tiff fields, parsed from the text."""
    try:
        with open(sidecar_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    def g(n):
        m = (re.search(rf"<tiff:{n}>(.*?)</tiff:{n}>", text, re.S) or re.search(rf"\btiff:{n}\s*=\s*\"([^\"]*)\"", text))
        return html_unescape(m.group(1)).strip() if m else ""
    mk, md = g("Make"), g("Model")
    return (mk, md) if (mk or md) else None


_GARBLED_REPEAT_RE = re.compile(r"(.)\1{14,}")
_GARBLED_LOG_RE = re.compile(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")


def ai_description_is_garbled(text: str) -> bool:
    """Port of ai_description_is_garbled(): catches a model repetition-loop (15+ identical
    characters in a row) or leaked internal script/log output sitting in the description
    field instead of a real description."""
    if not text:
        return False
    if _GARBLED_REPEAT_RE.search(text):
        return True
    if _GARBLED_LOG_RE.search(text):
        return True
    for marker in ("AI-Description:", "Requesting AI description", "ERROR:"):
        if marker in text:
            return True
    return False


_AT_COUNTRY = ("Österreich", "Osterreich", "Austria")


def build_geo_plot_line(geo: GeoSidecar, geo_color: str) -> str:
    """Port of the plot's geo line: Austria shows City/Street, everywhere else shows
    Country/State/City (never Street) -- same country-branching rule as the CLI."""
    if geo.country or geo.state or geo.city or geo.street:
        parts = []
        if geo.country in _AT_COUNTRY:
            if geo.city:
                parts.append(geo.city)
            if geo.street:
                parts.append(geo.street)
        else:
            if geo.country:
                parts.append(geo.country)
            if geo.state and geo.state != geo.country:
                parts.append(geo.state)
            if geo.city:
                parts.append(geo.city)
            elif geo.locality and geo.locality not in (geo.country, geo.state):
                parts.append(geo.locality)       # at sea: the specific bay / strait next to the sea in the country field
        if geo.sight and not any(geo.sight.casefold() in p.casefold() for p in parts):
            parts.append(geo.sight)              # the sight the photo is most likely of ("Wien, Stephansdom")
        joined = ", ".join(parts)
        return f"Ort: [COLOR {geo_color}]{xml_escape(joined)}[/COLOR]"
    if geo.exists:
        return f"Ort: [COLOR {geo_color}]No Location[/COLOR]"
    return ""


def build_ai_plot_line(geo: GeoSidecar) -> tuple[str, str]:
    """Returns (plot_line, note) -- the AI description line to embed (already
    XML-escaped), reusing the sidecar's own AI description verbatim (never generating
    one -- see module docstring), and a short note explaining why there's no line, for
    display in the UI (not written into the NFO)."""
    if not geo.ai_description:
        return "", "Keine AI-Beschreibung im .xmp-Sidecar gefunden."
    if ai_description_is_garbled(geo.ai_description):
        return "", "AI-Beschreibung im .xmp-Sidecar sieht garbled aus (Log-Leck oder Wiederholungsschleife) -- nicht übernommen."
    escaped = xml_escape(geo.ai_description)
    return f" [CR][COLOR white]{escaped}[/COLOR]", ""


# ---------------------------------------------------------------------------
# Mediainfo/exiftool metadata extraction
# ---------------------------------------------------------------------------

@dataclass
class VideoMetadata:
    width: str = ""
    height: str = ""
    aspect: str = "1,78"
    codec: str = ""        # mediainfo track[1].Format
    format_id: str = ""    # mediainfo track[1].CodecID
    duration_secs: int = 0
    container: str = ""
    framerate: int = 0
    bitrate_mbps: int = 0
    bitdepth: str = ""
    aformat: str = ""
    achannels: str = ""
    file_modified_date_local: str = ""
    vsource: str = "Unknown"
    helper: str = ""
    curated_fields: list = field(default_factory=list)
    all_strings: list = field(default_factory=list)
    error: str = ""


def quick_duration_secs(video_path: str) -> Optional[float]:
    """Container duration in seconds via the (pooled, ~5 ms) exiftool -- NOT authoritative, only a cheap
    first look so callers can skip the heavier mediainfo read when the answer is obvious. None = unknown."""
    try:
        res = exifpool.run(["exiftool", "-s3", "-n", "-m", "-api", "LargeFileSupport=1", "-Duration", video_path],
                           timeout=EXIFTOOL_TIMEOUT)
        val = float((res.stdout or "").strip().splitlines()[0])
        return val if val > 0 else None
    except Exception:
        return None


def _run_json(cmd: list[str], timeout: int) -> Optional[dict]:
    try:
        runner = exifpool.run if cmd and cmd[0] == "exiftool" else subprocess.run
        res = runner(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(res.stdout or "{}")
    except Exception:
        return None


def _jget(d: dict, *path, default=None):
    cur = d
    for key in path:
        if not isinstance(cur, (dict, list)):
            return default
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return default
    return default if cur is None else cur


def _strify(v) -> str:
    if v is None:
        return "null"
    return str(v)


def _collect_strings(obj) -> list[str]:
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_collect_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_collect_strings(v))
    return out


def extract_video_metadata(video_path: str) -> VideoMetadata:
    """Port of Section 2 ("Metadata Extraction") -- one mediainfo JSON call, one exiftool
    JSON call, parsed directly in Python (no jq needed, unlike the bash original)."""
    # The two reads are independent -> run side by side (mediainfo = own process, exiftool = pool).
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_media = ex.submit(_run_json, ["mediainfo", "--Output=JSON", video_path], MEDIAINFO_TIMEOUT)
        f_exif = ex.submit(_run_json, ["exiftool", "-j", video_path], EXIFTOOL_TIMEOUT)
        mediainfo, exifinfo = f_media.result(), f_exif.result()
    if mediainfo is None:
        return VideoMetadata(error="mediainfo konnte die Datei nicht lesen.")

    tracks = _jget(mediainfo, "media", "track", default=[]) or []
    t0 = tracks[0] if len(tracks) > 0 else {}
    t1 = tracks[1] if len(tracks) > 1 else {}
    t2 = tracks[2] if len(tracks) > 2 else {}

    exif0 = (exifinfo or [{}])[0] if exifinfo else {}
    make = _strify(exif0.get("Make"))
    model = _strify(exif0.get("Model"))
    vendor = _strify(exif0.get("VendorID"))
    adobe = _strify(exif0.get("PantryHistorySoftwareAgent"))

    width = _strify(t1.get("Width"))
    height = _strify(t1.get("Height"))
    aspect = _strify(t1.get("DisplayAspectRatio"))
    codec = _strify(t1.get("Format"))
    format_id = _strify(t1.get("CodecID"))
    duration_raw = _strify(t0.get("Duration"))
    container = _strify(t0.get("Format"))
    framerate_raw = _strify(t0.get("FrameRate"))
    bitrate_raw = _strify(t0.get("OverallBitRate"))
    bitdepth = _strify(t1.get("BitDepth"))
    aformat = _strify(t2.get("Format"))
    achannels = _strify(t2.get("Channels"))
    file_modified_date_local = _strify(t0.get("File_Modified_Date_Local"))

    vsource0 = _strify(_jget(t0, "extra", "com_android_manufacturer"))
    vsource1 = _strify(t1.get("Title"))
    vsource2 = _strify(t0.get("Encoded_Library_Name"))
    vsource3 = _strify(t2.get("Language"))
    vsource4 = _strify(_jget(t0, "extra", "com_android_version"))
    vsource5 = _strify(t0.get("Format_Commercial_IfAny"))
    vsource6 = _strify(t0.get("Encoded_Application"))
    vsource7 = _strify(t0.get("Format_Profile"))
    vsource9 = _strify(t0.get("Title_More"))
    vsource10 = _strify(t0.get("Performer"))
    vsource11 = _strify(_jget(t0, "extra", "PANA"))
    vsource13 = _strify(t2.get("Title"))
    vsource14 = _strify(_jget(t0, "extra", "Model"))
    vsource15 = _strify(_jget(t0, "extra", "Origin"))
    vsource16 = _strify(_jget(t1, "extra", "CodecConfigurationBox"))
    vsource8 = vsource5
    vsource12_raw = vsource6

    # VSource12 goes through its own pre-switch before joining the array (see CLI):
    # empty/null stays empty; anything containing WX300 -> "Sony DSC-WX300"; any OTHER
    # non-empty value is treated as evidence of video-editing software -> "VIDEOEDIT"
    # (which match_camera_source() then turns into "Videoediting Software").
    if vsource12_raw in ("", "null"):
        vsource12 = ""
    elif fnmatch.fnmatchcase(vsource12_raw, "*WX300"):
        vsource12 = "Sony DSC-WX300"
    else:
        vsource12 = "VIDEOEDIT"

    vsource17, vsource18, vsource19, vsource20 = make, model, adobe, vendor

    curated_fields = [
        vsource12, vsource0, vsource1, vsource2, vsource3, vsource4, vsource5, vsource6,
        vsource7, vsource8, vsource9, vsource10, vsource11, vsource13, vsource14,
        vsource15, vsource16, vsource17, vsource18, vsource19, vsource20,
    ]

    # Insta360 Luna Ultra special case: grep the file's own raw bytes (first 20MB + last
    # 5MB, same window the CLI's gyro2bb-equivalent `strings` pipeline used) for a
    # recognizable marker string -- cheaper and dependency-free vs. porting gyro2bb
    # itself, and only this one model needs it.
    vsource_insta = _detect_insta360_luna(video_path)
    if vsource_insta:
        curated_fields = ["Insta360 Luna Ultra"] + curated_fields

    all_strings = sorted(set(_collect_strings(mediainfo) + _collect_strings(exifinfo or {})))

    vsource, helper = resolve_camera_source(curated_fields, all_strings)
    if helper == "Galaxy":
        vsource = "Samsung Galaxy S24"
    # DJI drones that only name themselves in the encoder ("DJI NEO2") are not "video editing software"
    if (not vsource or vsource == "Videoediting Software") and vsource6.upper().startswith("DJI "):
        vsource = vsource6
    # the user's own camera_names.json wins over every built-in guess (same rules GPS Sidecar uses)
    try:
        from . import devicenames
        extra = [f"{make} {model}".strip(), model]
        for sv in all_strings:                       # DJI: 'pb_file:dvtm_NEO2.proto;model_name:FC9470;...'
            mm = re.search(r"model_name:([A-Za-z0-9_-]+)", sv)
            if mm:
                extra += [mm.group(1), f"DJI {mm.group(1)}"] if "dvtm_" in sv else [mm.group(1)]
        ov = devicenames.override_for(extra + curated_fields + all_strings)
        if ov:
            vsource = ov
    except Exception:    # noqa: BLE001
        pass
    if not vsource:
        vsource = "Unknown"

    if aspect in ("", "null"):
        aspect = "1,78"

    duration_digits = re.sub(r"\D", "", duration_raw.split(".")[0]) if duration_raw not in ("", "null") else ""
    try:
        duration_secs = int(duration_digits) if duration_digits else 0
    except ValueError:
        duration_secs = 0

    framerate_digits = re.sub(r"\D", "", framerate_raw) if framerate_raw not in ("", "null") else ""
    try:
        framerate = int(framerate_digits) // 1000 if framerate_digits else 0
    except ValueError:
        framerate = 0

    try:
        bitrate_mbps = int(float(bitrate_raw)) // (1024 * 1024) + 1 if bitrate_raw not in ("", "null") else 0
    except ValueError:
        bitrate_mbps = 0

    return VideoMetadata(
        width=width if width != "null" else "", height=height if height != "null" else "",
        aspect=aspect, codec=codec if codec != "null" else "", format_id=format_id if format_id != "null" else "",
        duration_secs=duration_secs, container=container if container != "null" else "",
        framerate=framerate, bitrate_mbps=bitrate_mbps, bitdepth=bitdepth if bitdepth != "null" else "",
        aformat=aformat if aformat != "null" else "", achannels=achannels if achannels != "null" else "",
        file_modified_date_local=file_modified_date_local if file_modified_date_local != "null" else "",
        vsource=vsource, helper=helper, curated_fields=curated_fields, all_strings=all_strings,
    )


def _detect_insta360_luna(video_path: str) -> bool:
    try:
        with open(video_path, "rb") as fh:
            head = fh.read(20 * 1024 * 1024)
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            tail_start = max(0, size - 5 * 1024 * 1024)
            fh.seek(tail_start)
            tail = fh.read()
        blob = (head + tail).lower()
        return b"insta360" in blob or b"luna ultra" in blob
    except OSError:
        return False


def format_duration(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def delete_short_video_with_companions(video_path: str, short_video_secs: int) -> dict:
    """Deletes a video together with its companion files -- but ONLY if a FRESH
    mediainfo read, done right here, confirms its duration is still under
    short_video_secs. Never trusts a duration/"is_short" flag the caller computed
    earlier (a scan result could be stale if the file changed since, or a replayed
    request could simply be lying) -- this is a real, unrecoverable os.remove(), so
    every candidate is independently re-verified and anything that doesn't pass is
    skipped and reported rather than guessed at.

    folder.jpg is deliberately NOT in the companion list -- it's one shared file per
    folder (the directory's own cover image), not a per-video companion, so removing
    one short clip must never take the whole folder's artwork with it.

    Shared by NFO Maker's own /delete and the standalone "Kurzvideos löschen" module's
    /delete, so the safety re-check and the companion-file list can't drift apart
    between the two call sites. Callers are expected to have already run
    pathsafety.check_allowed(video_path) themselves (this module has no FastAPI/
    pathsafety dependency of its own)."""
    entry = {"path": video_path, "filename": os.path.basename(video_path), "status": "", "message": ""}

    if not os.path.isfile(video_path):
        entry["status"] = "error"
        entry["message"] = "Datei nicht gefunden (evtl. bereits gelöscht oder umbenannt)."
        return entry

    meta = extract_video_metadata(video_path)
    if meta.error or not (0 < meta.duration_secs < short_video_secs):
        entry["status"] = "skipped"
        actual = format_duration(meta.duration_secs) if not meta.error else "unbekannt (Lesefehler)"
        entry["message"] = (
            f"Übersprungen -- tatsächliche Dauer ist {actual}, nicht < {short_video_secs}s "
            "(Sicherheitsprüfung direkt vor dem Löschen)."
        )
        return entry

    base_dir = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    companions = [
        video_path,
        os.path.join(base_dir, stem + ".nfo"),
        os.path.join(base_dir, stem + "-poster.jpg"),
        os.path.join(base_dir, stem + "-fanart.jpg"),
        os.path.join(base_dir, stem + "-thumb.jpg"),
        video_path + ".xmp",
    ]
    removed, error = [], ""
    for p in companions:
        if not os.path.exists(p):
            continue
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except OSError as e:
            error = f"Fehler beim Löschen von {os.path.basename(p)}: {e}"
            break
    entry["status"] = "error" if error else "deleted"
    entry["message"] = error or ("Gelöscht: " + ", ".join(removed) if removed else "Nichts zu löschen gefunden.")
    return entry


def format_size(num_bytes: int) -> str:
    """Port of `numfmt --to=si --format="%.2f"` -- SI (1000-based) short size string."""
    size = float(num_bytes)
    for unit in ("", "K", "M", "G", "T"):
        if size < 1000 or unit == "T":
            return f"{size:.2f}{unit}".rstrip("0").rstrip(".") if unit else f"{int(size)}"
        size /= 1000
    return f"{size:.2f}T"


# ---------------------------------------------------------------------------
# Thumbnail extraction (ffmpeg) -- folder.jpg / <stem>-poster.jpg / <stem>-fanart.jpg /
# <stem>-thumb.jpg. Faithful port of Section 1, including the "seek near the end for a
# range/year-named video" heuristic and the single-ffmpeg-call-for-three-outputs
# optimization already applied in the CLI script.
# ---------------------------------------------------------------------------

_RANGE_NAME_RE = re.compile(r"^\d{4}[._-]\d{2}[._-]\d{2}-\d{4}[._-]\d{2}[._-]\d{2}")
_YEAR_NAME_RE = re.compile(r"^\d{4}[-_ .][^0-9]")


def compute_seek_times(file_stem: str, video_path: str) -> tuple[int, int]:
    """Returns (thumb_seek, folder_seek) in seconds -- for a range- or year-named video,
    seeks 5 minutes before the end (or the midpoint, for anything under 10 minutes long)
    instead of the usual few seconds in, to skip past a title/intro clip."""
    thumb_seek, folder_seek = 6, 4
    if _RANGE_NAME_RE.match(file_stem) or _YEAR_NAME_RE.match(file_stem):
        q = quick_duration_secs(video_path)
        if q:
            duration_ms = int(q * 1000)
        else:
            try:
                res = subprocess.run(
                    ["mediainfo", "--Output=General;%Duration%", video_path],
                    capture_output=True, text=True, timeout=MEDIAINFO_TIMEOUT,
                )
                duration_ms = int((res.stdout or "").strip())
            except (ValueError, subprocess.SubprocessError):
                duration_ms = 0
        if duration_ms > 0:
            duration_secs = duration_ms // 1000
            if duration_secs > 600:
                thumb_seek = duration_secs - 300
            elif duration_secs > 0:
                thumb_seek = duration_secs // 2
            folder_seek = thumb_seek
    return thumb_seek, folder_seek


def _ffmpeg(args: list[str], timeout: int = FFMPEG_TIMEOUT) -> bool:
    try:
        # -nostdin: never read the (inherited) stdin; -loglevel error: far less to capture/parse
        res = subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                             capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return res.returncode == 0
    except Exception:
        return False


def _nonempty(*paths: str) -> bool:
    return all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in paths)


def generate_folder_jpg(video_path: str, folder_jpg: str, folder_seek: int, force: bool) -> bool:
    """Returns True if the file was (re)written this call. Unlike the bash original
    (which only retries the fallback seek on a non-zero ffmpeg exit), this also retries
    when ffmpeg exits 0 but produced no frame at all -- happens when the seek point
    lands past a short video's actual duration, which a plain exit-code check misses."""
    if os.path.exists(folder_jpg) and not force:
        return False
    _ffmpeg(["-ss", str(folder_seek), "-i", video_path, "-frames:v", "1", "-qscale:v", "9", folder_jpg])
    if not _nonempty(folder_jpg):
        _ffmpeg(["-ss", "1", "-i", video_path, "-frames:v", "1", "-qscale:v", "9", folder_jpg])
    return _nonempty(folder_jpg)


def generate_poster_fanart_thumb(video_path: str, poster_jpg: str, fanart_jpg: str, thumb_jpg: str,
                                  thumb_seek: int, force: bool) -> bool:
    """One ffmpeg call producing all three outputs, same as the CLI's own optimization.
    Returns True if the files were (re)written this call. Same fallback-on-empty-output
    robustness as generate_folder_jpg() above."""
    if os.path.exists(poster_jpg) and not force:
        return False

    def _attempt(seek: str) -> bool:
        args = [
            "-ss", seek, "-i", video_path,
            "-frames:v", "1", "-qscale:v", "9", poster_jpg,
            "-frames:v", "1", "-qscale:v", "9", fanart_jpg,
            "-vf", "scale=iw/4:-1", "-frames:v", "1", "-qscale:v", "11", thumb_jpg,
        ]
        _ffmpeg(args)
        return _nonempty(poster_jpg, fanart_jpg, thumb_jpg)

    if not _attempt(str(thumb_seek)):
        _attempt("6")  # same fallback seek the CLI uses for poster/fanart/thumb specifically
    return _nonempty(poster_jpg, fanart_jpg, thumb_jpg)


# ---------------------------------------------------------------------------
# NFO XML
# ---------------------------------------------------------------------------

def build_nfo_xml(
    *, title: str, sort_title: str, year: str, premiered: str, duration_secs: int,
    width: str, height: str, aspect: str, codec: str, format_id: str, bitrate_mbps: int,
    container: str, framerate: int, bitdepth: str, aformat: str, achannels: str,
    vsource: str, video_size_display: str, sub_path: str, geo_line: str, ai_line: str,
    stem: str, created_date: str, defaults: "settings.NfoDefaults", context_line: str = "",
) -> str:
    """Builds the exact NFO XML the CLI writes -- see build_nfo_xml's caller for how each
    piece is produced. Every dynamic text value that lands inside XML content (not an
    attribute) is XML-escaped; geo_line/ai_line are pre-built (already escaped) by
    build_geo_plot_line()/build_ai_plot_line() above."""
    plot = (
        f"Cam: [COLOR green]{xml_escape(vsource)}[/COLOR], "
        f"[COLOR blue]{xml_escape(width)} x {xml_escape(height)}[/COLOR], "
        f"Dauer: [COLOR red]{format_duration(duration_secs)}[/COLOR], "
        f"Größe: [COLOR orange]{xml_escape(video_size_display)}[/COLOR] [CR]"
        f"In: [COLOR yellow]{xml_escape(sub_path)}[/COLOR], {geo_line}{ai_line}{context_line}"
    )
    actors_xml = "".join(
        f'    <actor><name>{xml_escape(a.name)}</name><role>{xml_escape(a.role)}</role>'
        f'<thumb>{xml_escape(a.thumb)}</thumb></actor>\n'
        for a in defaults.actors
    )
    camera_actor = (
        f'    <actor><name>CAMERA</name><role>{xml_escape(vsource)}</role>'
        f'<thumb>{xml_escape(defaults.camera_thumb)}</thumb></actor>\n'
    )
    esc_title = xml_escape(title)
    esc_sort = xml_escape(sort_title)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<movie>
    <fileinfo>
        <streamdetails>
            <video>
                <width>{xml_escape(width)}</width>
                <height>{xml_escape(height)}</height>
                <aspect>{xml_escape(aspect)}</aspect>
                <codec>{xml_escape(codec)}</codec>
                <format>{xml_escape(format_id)}</format>
                <durationinseconds>{duration_secs}</durationinseconds>
                <bitrate>{bitrate_mbps} Mb/s</bitrate>
                <container>{xml_escape(container)}</container>
                <framerate>{framerate}</framerate>
                <NumVideoBits>{xml_escape(bitdepth)}</NumVideoBits>
            </video>
            <audio>
                <language>deu</language>
                <codec>{xml_escape(aformat)}</codec>
                <channels>{xml_escape(achannels)}</channels>
            </audio>
        </streamdetails>
    </fileinfo>
    <title>{esc_title}</title>
    <set>{xml_escape(defaults.set_name)}</set>
    <userrating>{defaults.userrating}</userrating>
    <criticrating>{defaults.criticrating}</criticrating>
    <sorttitle>{esc_sort}</sorttitle>
    <year>{xml_escape(year)}</year>
    <mpaa>{xml_escape(defaults.mpaa)}</mpaa>
    <certification>{xml_escape(defaults.certification)}</certification>
    <premiered>{xml_escape(premiered)}</premiered>
    <aired>{xml_escape(premiered)}</aired>
    <plot>{plot}</plot>
    <fanart>{xml_escape(stem)}-fanart.jpg</fanart>
    <thumb>{xml_escape(stem)}-thumb.jpg</thumb>
    <poster>{xml_escape(stem)}-poster.jpg</poster>
    <createdate>{xml_escape(created_date)}</createdate>
    <dateadded>{xml_escape(created_date)}</dateadded>
{camera_actor}{actors_xml}    <stars></stars>
    <credits>Located at: {xml_escape(sub_path)}</credits>
    <genre>{xml_escape(defaults.genre)}</genre>
    <tag>{xml_escape(defaults.tag)}</tag>
    <director>{xml_escape(defaults.director)}</director>
    <studio>{xml_escape(defaults.studio)}</studio>
</movie>
"""


# ---------------------------------------------------------------------------
# Repair-mode candidate scanning (--repair-ai / --reintegrate-ai / --repair-italic)
# ---------------------------------------------------------------------------

def _nfo_plot_has_ai_problem(text: str) -> bool:
    from . import aicore
    m = re.search(r"<plot>(.*?)</plot>", text, re.DOTALL)
    plot = re.sub(r"\[/?[A-Za-z]+[^\]]*\]|<[^>]+>", " ", m.group(1)) if m else ""
    return _nfo_plot_is_garbled(text) or bool(aicore.ai_description_problem(plot))


def scan_bad_ai_nfos(directory: str, depth: int) -> list[str]:
    """Like scan_garbled_nfos, plus model refusals, <think>/markdown leaks, phrase loops and foreign script."""
    return _scan_nfos_matching(directory, depth, _nfo_plot_has_ai_problem)


def scan_garbled_nfos(directory: str, depth: int) -> list[str]:
    """--repair-ai: every video whose .nfo plot contains a garbled AI description
    (repetition-loop or leaked log output)."""
    return _scan_nfos_matching(directory, depth, _nfo_plot_is_garbled)


def scan_italic_nfos(directory: str, depth: int) -> list[str]:
    """--repair-italic: every video whose .nfo still has the old [I]/[/I] plot markup."""
    return _scan_nfos_matching(directory, depth, lambda text: "[I]" in text or "[/I]" in text)


def scan_missing_ai_nfos(directory: str, depth: int) -> list[str]:
    """--repair-missing-ai: every .nfo whose plot has NO AI description at all (the plot template only ever wraps
    the description in [COLOR white]...[/COLOR], so its absence means none was embedded)."""
    return _scan_nfos_matching(directory, depth, lambda text: "[COLOR white]" not in text)


def _nfo_lacks_location(text: str) -> bool:
    return "No Location" in text or "Ort: [COLOR" not in text


def scan_missing_nfos(directory: str, depth: int) -> tuple[list[str], int, int]:
    """--repair-missing: .nfo files with no AI description, OR with no real location while the video's .xmp sidecar
    NOW has GPS (without GPS regenerating would only write "No Location" again). Returns (nfos, n_missing_ai, n_missing_loc)."""
    stats = {"ai": 0, "loc": 0}

    def pred_for(path):
        def pred(text):
            hit = False
            if "[COLOR white]" not in text:
                stats["ai"] += 1
                hit = True
            if _nfo_lacks_location(text):
                video = nfo_video_sibling(path)
                if video and _xmp_has_gps(video + ".xmp"):
                    stats["loc"] += 1
                    hit = True
            return hit
        return pred
    nfos = _scan_nfos_matching(directory, depth, None, pred_factory=pred_for)
    return nfos, stats["ai"], stats["loc"]


def _xmp_has_gps(xmp_path: str) -> bool:
    if not os.path.isfile(xmp_path):
        return False
    try:
        res = exifpool.run(["exiftool", "-m", "-n", "-T", "-api", "LargeFileSupport=1", "-GPSLatitude", xmp_path],
                           capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    except Exception:
        return False
    out = (res.stdout or "").strip()
    return bool(out) and out != "-"


def _scan_nfos_matching(directory: str, depth: int, predicate, pred_factory=None) -> list[str]:
    scan_root = os.path.dirname(directory) if os.path.isfile(directory) else directory
    if not os.path.isdir(scan_root):
        return []
    base_depth = scan_root.rstrip("/").count("/")
    matches = []
    for root, dirs, files in os.walk(scan_root):
        rel_depth = root.rstrip("/").count("/") - base_depth
        if rel_depth >= depth:
            dirs[:] = []
        skipdirs.prune(dirs)
        for f in files:
            if not f.lower().endswith(".nfo"):
                continue
            nfo_path = os.path.join(root, f)
            try:
                with open(nfo_path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if (pred_factory(nfo_path) if pred_factory else predicate)(text):
                matches.append(nfo_path)
    return sorted(matches)


# NFO files are indented XML: the repeat check must ignore whitespace runs (the NFO's own
# 16-space indentation would otherwise flag EVERY file -- original script uses (\S)\1{14,}).
_NFO_GARBLED_REPEAT_RE = re.compile(r"(\S)\1{14,}")


def _nfo_plot_is_garbled(text: str) -> bool:
    return bool(_NFO_GARBLED_REPEAT_RE.search(text) or _GARBLED_LOG_RE.search(text)) or any(
        marker in text for marker in ("AI-Description:", "Requesting AI description", "ERROR:")
    )


def nfo_video_sibling(nfo_path: str) -> Optional[str]:
    """Finds the video file matching an .nfo's stem, next to it -- same extension list
    the CLI tries, case-insensitively."""
    base_dir = os.path.dirname(nfo_path)
    stem = Path(nfo_path).stem
    for ext in sorted(NFO_VIDEO_EXT):
        for candidate_ext in (ext, ext.upper()):
            candidate = os.path.join(base_dir, stem + candidate_ext)
            if os.path.isfile(candidate):
                return candidate
    return None


def scan_reintegrate_candidates(videos: list[str]) -> list[str]:
    """--reintegrate-ai: every video (from `videos`) whose .xmp sidecar already has a
    clean (non-garbled) AI description -- queued so the main loop pulls it into the
    .nfo without any AI provider call."""
    out = []
    for video in videos:
        xmp_path = video + ".xmp"
        if not os.path.isfile(xmp_path):
            continue
        geo = read_geo_sidecar(xmp_path)
        if geo.ai_description and not ai_description_is_garbled(geo.ai_description):
            out.append(video)
    return out
