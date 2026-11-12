"""
settings.py — reusable renaming-pattern presets, stored as a plain JSON file on disk
(mirrors the maintainer's existing convention of plain config files, e.g. ~/.GEOCODING/env_set_GPS,
rather than a database). One file, human-editable, easy to back up alongside the NAS config.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from typing import Optional

_lock = threading.Lock()

DEFAULT_PRESETS_PATH = os.environ.get(
    "TIMESTAMP_PRESETS_FILE",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "presets.json"),
)


def config_dir() -> str:
    """The user's config folder: the one holding presets.json (so a single /config mount in Docker
    covers camera_names.json too, with no extra env var needed)."""
    return os.path.dirname(os.path.abspath(DEFAULT_PRESETS_PATH))


@dataclass
class Preset:
    name: str
    mode: str = "EXIF"            # FILE | EXIF
    default_tz: str = "Europe/Vienna"
    gps_filter: str = "ALL"       # ALL | WITH-GPS | NO-GPS
    type_filter: str = "ALL"      # ALL | IMAGE | VIDEO
    depth: int = 0
    do_rename: bool = False
    do_exifwrite: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _presets_path() -> str:
    return os.path.normpath(DEFAULT_PRESETS_PATH)


def _ensure_file() -> str:
    path = _presets_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"presets": []}, fh, indent=2)
    return path


def load_presets() -> list[Preset]:
    path = _ensure_file()
    with _lock:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    out = []
    for p in data.get("presets", []):
        try:
            out.append(Preset(**{k: v for k, v in p.items() if k in Preset.__dataclass_fields__}))
        except TypeError:
            continue
    return out


def save_preset(preset: Preset) -> None:
    path = _ensure_file()
    with _lock:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        presets = [p for p in data.get("presets", []) if p.get("name") != preset.name]
        presets.append(preset.to_dict())
        data["presets"] = presets
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)


def delete_preset(name: str) -> bool:
    path = _ensure_file()
    with _lock:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        before = len(data.get("presets", []))
        data["presets"] = [p for p in data.get("presets", []) if p.get("name") != name]
        changed = len(data["presets"]) != before
        if changed:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
    return changed


def get_preset(name: str) -> Optional[Preset]:
    for p in load_presets():
        if p.name == name:
            return p
    return None


# ---------------------------------------------------------------------------
# Root folders: named shortcuts for the browse path (e.g. "VIDEOS" ->
# /mnt/NAS/video/Home-Video/), so the maintainer picks a name/type instead of typing or
# remembering the NAS mount layout. Each root also carries the file-type filter
# that applies to everything under it (VIDEOS -> VIDEO, PHOTOS -> IMAGE), so
# there's no separate manual Typ-Filter control in the UI anymore.
#
# Configurable via ROOT_FOLDERS env var; falls back to the two defaults below
# if unset. Format: "NAME=path,NAME2=path2" (comma-separated NAME=path pairs).
# An optional ":TYPE" suffix on the path (ALL|IMAGE|VIDEO) sets the type
# explicitly, e.g. "RAW=/mnt/NAS/raw/:ALL"; without it, the type is guessed
# from the name (contains VIDEO -> VIDEO, PHOTO/IMAGE -> IMAGE, else ALL).
# ---------------------------------------------------------------------------
_VALID_TYPES = ("ALL", "IMAGE", "VIDEO")


@dataclass
class RootFolder:
    name: str
    path: str
    type_filter: str = "ALL"  # ALL | IMAGE | VIDEO

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_ROOT_FOLDERS = [
    RootFolder("VIDEOS", "/mnt/NAS/video/Home-Video/", "VIDEO"),
    RootFolder("PHOTOS", "/mnt/NAS/photo/", "IMAGE"),
]


def _strip_quotes(s: str) -> str:
    """Strips one layer of matching surrounding quotes. Guards against
    docker-compose's `environment:` list form, which (unlike shell) does NOT
    strip quotes from `- VAR="value"` — they end up literally inside the
    value, e.g. '"VIDEOS=...' showing up in the dropdown."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1].strip()
    return s


def _infer_type(name: str) -> str:
    upper = name.upper()
    if "VIDEO" in upper:
        return "VIDEO"
    if "PHOTO" in upper or "IMAGE" in upper:
        return "IMAGE"
    return "ALL"


def load_root_folders() -> list[RootFolder]:
    raw = _strip_quotes(os.environ.get("ROOT_FOLDERS", ""))
    if not raw:
        return list(DEFAULT_ROOT_FOLDERS)
    out: list[RootFolder] = []
    for pair in raw.split(","):
        pair = _strip_quotes(pair)
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = _strip_quotes(name)
        value = _strip_quotes(value)
        if not name or not value:
            continue
        if ":" in value:
            # Paths themselves don't contain ':' on Linux, so the last ':'
            # safely separates an optional explicit TYPE suffix.
            path_part, _, type_part = value.rpartition(":")
            type_part = type_part.strip().upper()
            if path_part and type_part in _VALID_TYPES:
                out.append(RootFolder(name, path_part, type_part))
                continue
        out.append(RootFolder(name, value, _infer_type(name)))
    return out or list(DEFAULT_ROOT_FOLDERS)


# ---------------------------------------------------------------------------
# GPS Sidecar module config: Traccar/Dawarich (GPS track sources) and the
# Photon/Nominatim/Pelias/maritime reverse-geocoding chain. All of these are
# the maintainer's own already-running self-hosted services, reached purely by URL --
# this container doesn't run or depend on any of them, so there's nothing new
# to add to docker-compose for them beyond the URLs/keys themselves.
#
# TRACCAR_DEVICES / TRACCAR_DEVICES_ADDITIONAL follow the same "NAME=value[:extra]"
# convention as ROOT_FOLDERS for consistency: "ID[:Label],ID2[:Label2]" (comma-
# separated). The label is cosmetic only (shown in the UI); if omitted, the raw ID is
# shown instead. Every device from EITHER list is individually selectable per browse
# (a per-device checkbox, not an all-or-nothing "use Traccar" toggle) -- the only
# difference between the two lists is which ones start out checked:
# TRACCAR_DEVICES are your every-day devices (checked by default), while
# TRACCAR_DEVICES_ADDITIONAL are ones you only need occasionally (e.g. tagging from
# someone else's phone) -- listed and selectable, but unchecked unless you tick them.
# A device ID can simply be listed in TRACCAR_DEVICES_ADDITIONAL too (e.g. if you keep
# one combined list and just repeat the everyday ones) -- it's deduplicated by ID and
# shown once, checked, never twice.
# ---------------------------------------------------------------------------

@dataclass
class GeoSettings:
    traccar_url: str = ""
    traccar_token: str = ""
    traccar_devices: list = None             # list[(id, label)] -- checked by default
    traccar_devices_additional: list = None  # list[(id, label)] -- listed, unchecked by default
    dawarich_server: str = ""
    dawarich_api_key: str = ""
    dawarich_import_api_key: str = ""  # optional second "archive" account, same server
    photon_server: str = ""
    nominatim_server: str = ""
    pelias_server: str = ""
    maritime_server: str = ""
    lang: str = "de"
    max_gap_secs: int = 600            # tier 1, same default as the CLI's EXT_SECS
    max_gap_secs_tier2: int = 6 * 3600  # tier 2, same default as the CLI's EXT_HOURS=6

    def __post_init__(self):
        if self.traccar_devices is None:
            self.traccar_devices = []
        if self.traccar_devices_additional is None:
            self.traccar_devices_additional = []

    @property
    def traccar_all_devices(self) -> list:
        """Combined, DEDUPLICATED-by-device-ID list for the UI, each tagged with whether
        it should start checked: TRACCAR_DEVICES first (checked), then whichever
        TRACCAR_DEVICES_ADDITIONAL device IDs aren't already in TRACCAR_DEVICES
        (unchecked) -- every one of them individually selectable per browse. A device ID
        listed in both env vars is shown only once (checked, since TRACCAR_DEVICES always
        wins) -- there's no need to leave a device out of TRACCAR_DEVICES_ADDITIONAL just
        to avoid it appearing twice."""
        by_id: dict[str, tuple] = {}
        order: list[str] = []
        for dev_id, label in self.traccar_devices:
            by_id[dev_id] = (dev_id, label, True)
            order.append(dev_id)
        for dev_id, label in self.traccar_devices_additional:
            if dev_id in by_id:
                continue
            by_id[dev_id] = (dev_id, label, False)
            order.append(dev_id)
        return [by_id[dev_id] for dev_id in order]

    @property
    def dawarich_configured(self) -> bool:
        return bool(self.dawarich_server and self.dawarich_api_key)

    @property
    def traccar_configured(self) -> bool:
        return bool(self.traccar_url and self.traccar_token and self.traccar_all_devices)

    @property
    def any_geocoder_configured(self) -> bool:
        return bool(self.photon_server or self.nominatim_server or self.pelias_server)


def _parse_devices(raw: str) -> list:
    out = []
    for pair in _strip_quotes(raw).split(","):
        pair = _strip_quotes(pair)
        if not pair:
            continue
        if ":" in pair:
            dev_id, _, label = pair.partition(":")
            out.append((_strip_quotes(dev_id), _strip_quotes(label)))
        else:
            out.append((pair, ""))
    return out


def load_geo_settings() -> GeoSettings:
    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "").strip() or default)
        except ValueError:
            return default

    return GeoSettings(
        traccar_url=_strip_quotes(os.environ.get("TRACCAR_URL", "")).rstrip("/"),
        traccar_token=_strip_quotes(os.environ.get("TRACCAR_TOKEN", "")),
        traccar_devices=_parse_devices(os.environ.get("TRACCAR_DEVICES", "")),
        traccar_devices_additional=_parse_devices(os.environ.get("TRACCAR_DEVICES_ADDITIONAL", "")),
        dawarich_server=_strip_quotes(os.environ.get("DAWARICH_SERVER", "")).rstrip("/"),
        dawarich_api_key=_strip_quotes(os.environ.get("DAWARICH_API_KEY", "")),
        dawarich_import_api_key=_strip_quotes(os.environ.get("DAWARICH_IMPORT_API_KEY", "")),
        photon_server=_strip_quotes(os.environ.get("PHOTON_SERVER", "")).rstrip("/"),
        nominatim_server=_strip_quotes(os.environ.get("NOMINATIM_SERVER", "")).rstrip("/"),
        pelias_server=_strip_quotes(os.environ.get("PELIAS_SERVER", "")).rstrip("/"),
        maritime_server=_strip_quotes(os.environ.get("MARITIME_SERVER", "")).rstrip("/"),
        lang=_strip_quotes(os.environ.get("GEO_LANG", "de")) or "de",
        max_gap_secs=_int_env("GEO_MAX_GAP_SECS", 600),
        max_gap_secs_tier2=_int_env("GEO_MAX_GAP_SECS_TIER2", 6 * 3600),
    )


# ---------------------------------------------------------------------------
# AI Description module config -- same env var names as the CLI's own
# ~/.GEOCODING/env_nfo_ai, so an existing setup's values can be copied
# straight into this container's environment unchanged. AI_PROVIDER is a
# comma-separated priority list ("ollama,google" or just "ollama"); each
# configured provider is tried in order, falling through to the next on
# failure, same as the CLI's generate_ai_description.py helper.
# ---------------------------------------------------------------------------

@dataclass
class AiSettings:
    provider: str = ""  # raw AI_PROVIDER value, e.g. "ollama,google"
    ollama_url: str = ""
    ollama_model: str = ""
    google_api_key: str = ""
    google_model: str = ""

    @property
    def providers(self) -> list:
        """AI_PROVIDER parsed into an ordered list, keeping only the two known
        values -- same exact-match guard as the CLI (never a substring match)."""
        return [p.strip() for p in self.provider.split(",") if p.strip() in ("ollama", "google")]

    @property
    def configured_providers(self) -> list:
        """Subset of `providers` that actually has its required env vars set --
        what's shown in the UI as "ready to use" vs. listed-but-unconfigured."""
        out = []
        for p in self.providers:
            if p == "ollama" and self.ollama_url and self.ollama_model:
                out.append(p)
            elif p == "google" and self.google_api_key and self.google_model:
                out.append(p)
        return out

    @property
    def any_configured(self) -> bool:
        return bool(self.configured_providers)


def load_ai_settings() -> AiSettings:
    return AiSettings(
        provider=_strip_quotes(os.environ.get("AI_PROVIDER", "")),
        ollama_url=_strip_quotes(os.environ.get("OLLAMA_URL", "")).rstrip("/"),
        ollama_model=_strip_quotes(os.environ.get("OLLAMA_MODEL", "")),
        google_api_key=_strip_quotes(os.environ.get("GOOGLE_AI_API_KEY", "")),
        google_model=_strip_quotes(os.environ.get("GOOGLE_AI_MODEL", "")),
    )


# ---------------------------------------------------------------------------
# Collage module config -- every tunable knob photo_pile_collage.py's CLI exposes as a
# flag, reduced to one standard set of defaults the maintainer runs with (his own real-world
# invocation: --width 4096 --height 2304 --count 139 --border 6 --layout random
# --rotation 25), configurable via COLLAGE_* env vars so that standard lives in one place
# (~/.GEOCODING/env_set_GPS-equivalent for this container) -- but every one of these is
# still a plain form field on the Collage module page itself, so a single run can
# override any of them without touching the environment. --recursive isn't a setting
# here: the module always tries trip-folder auto-discovery first and falls back to
# treating Pfad as a single trip folder itself, same as the CLI does when
# discover_trip_folders() finds nothing (see collagecore.discover_trips()).
# ---------------------------------------------------------------------------

@dataclass
class CollageDefaults:
    layout: str = "random"
    width: int = 4096
    height: int = 2304
    cell: Optional[int] = None
    border: int = 6
    rotation: Optional[float] = 25
    jitter: float = 0.55
    seed: Optional[int] = None
    count: Optional[int] = 139
    spacing: int = 0
    tile_size: Optional[int] = None
    size_variation: float = 0.35
    sync_folder_jpg: bool = False


def _collage_int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw = _strip_quotes(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _collage_float_env(name: str, default: Optional[float]) -> Optional[float]:
    raw = _strip_quotes(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_collage_defaults() -> CollageDefaults:
    layout = _strip_quotes(os.environ.get("COLLAGE_LAYOUT", "")).strip() or "random"
    if layout not in ("polaroid", "postcard", "cascade", "magazine", "mosaic", "random"):
        layout = "random"
    return CollageDefaults(
        layout=layout,
        width=_collage_int_env("COLLAGE_WIDTH", 4096),
        height=_collage_int_env("COLLAGE_HEIGHT", 2304),
        cell=_collage_int_env("COLLAGE_CELL", None),
        border=_collage_int_env("COLLAGE_BORDER", 6),
        rotation=_collage_float_env("COLLAGE_ROTATION", 25),
        jitter=_collage_float_env("COLLAGE_JITTER", 0.55),
        seed=_collage_int_env("COLLAGE_SEED", None),
        count=_collage_int_env("COLLAGE_COUNT", 139),
        spacing=_collage_int_env("COLLAGE_SPACING", 0),
        tile_size=_collage_int_env("COLLAGE_TILE_SIZE", None),
        size_variation=_collage_float_env("COLLAGE_SIZE_VARIATION", 0.35),
        sync_folder_jpg=_strip_quotes(os.environ.get("COLLAGE_SYNC_FOLDER_JPG", "")).strip().lower() in ("1", "true", "yes", "on"),
    )


def photo_root() -> str:
    """Base photo-library path the Collage module mirrors generated collages FROM --
    same PHOTO_ROOT env var the CLI script already reads, default /mnt/NAS/photo. Read
    fresh each call (same "never cached" convention as the rest of this module)."""
    return _strip_quotes(os.environ.get("PHOTO_ROOT", "")).rstrip("/") or "/mnt/NAS/photo"


def video_root() -> str:
    """Home-Video library path collages get mirrored TO -- same VIDEO_ROOT env var the
    CLI script already reads, default /mnt/NAS/video/Home-Video. Set to an explicitly
    empty string (VIDEO_ROOT=) to disable mirroring entirely, same as the CLI's
    --video-root ''."""
    if "VIDEO_ROOT" in os.environ and not os.environ["VIDEO_ROOT"].strip():
        return ""
    return _strip_quotes(os.environ.get("VIDEO_ROOT", "")).rstrip("/") or "/mnt/NAS/video/Home-Video"


# ---------------------------------------------------------------------------
# NFO Maker module (#6) config -- director/studio/rating/actors for the generated Kodi
# .nfo, ported from HomeVideo_NFO_Maker_new.sh's own hardcoded heredoc values. Kept
# configurable (same ROOT_FOLDERS-style "plain env var, sensible default" pattern as
# every other module here) since these are the maintainer's own family/library conventions,
# not something intrinsic to the script -- but every default below matches the CLI
# exactly, so an unconfigured container behaves identically to it.
#
# NFO_ACTORS format: actors separated by ";", each actor's Name/Role/Thumb-path
# separated by "|" (not "," -- a Role value regularly contains commas itself, e.g.
# "Anna, Papa, Cameraman, Director"). The special CAMERA actor (role = the detected
# camera/software source, set per-video) is NOT part of this list -- its thumb path is
# NFO_CAMERA_THUMB instead.
# ---------------------------------------------------------------------------

@dataclass
class Actor:
    name: str
    role: str
    thumb: str


DEFAULT_NFO_ACTORS: list = []          # no built-in people: set NFO_ACTORS (see .env.example)


def _parse_actors(raw: str) -> list:
    out = []
    for chunk in raw.split(";"):
        chunk = _strip_quotes(chunk)
        if not chunk:
            continue
        parts = chunk.split("|")
        if len(parts) != 3:
            continue
        name, role, thumb = (p.strip() for p in parts)
        if not name:
            continue
        out.append(Actor(name, role, thumb))
    return out


@dataclass
class NfoDefaults:
    director: str = ""
    studio: str = ""
    set_name: str = "Home Movie"
    genre: str = "Home-Video"
    tag: str = "Home-Video"
    mpaa: str = "Rated R"
    certification: str = "Germany:6"
    userrating: int = 10
    criticrating: int = 100
    geo_color: str = "cyan"
    camera_thumb: str = "/storage/videos/HomeVideo/.actors/camera.png"
    actors: list = None
    ai_description_enabled: bool = True
    skip_location: bool = False
    older: Optional[int] = None  # -O/--older: protect .nfo files modified within N<=0 days of today
    short_video_secs: int = 10  # "short video" filter/delete threshold: duration_secs must be < this

    def __post_init__(self):
        if self.actors is None:
            self.actors = list(DEFAULT_NFO_ACTORS)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def load_nfo_defaults() -> NfoDefaults:
    def _int_env(name: str, default: int) -> int:
        raw = _strip_quotes(os.environ.get(name, "")).strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    older_raw = _strip_quotes(os.environ.get("NFO_OLDER", "")).strip()
    older = None
    if older_raw:
        try:
            older = min(0, int(older_raw))
        except ValueError:
            older = None

    actors_raw = os.environ.get("NFO_ACTORS", "")
    actors = _parse_actors(actors_raw) if actors_raw.strip() else list(DEFAULT_NFO_ACTORS)

    return NfoDefaults(
        director=_strip_quotes(os.environ.get("NFO_DIRECTOR", "")) or "",
        studio=_strip_quotes(os.environ.get("NFO_STUDIO", "")) or "",
        set_name=_strip_quotes(os.environ.get("NFO_SET", "")) or "Home Movie",
        genre=_strip_quotes(os.environ.get("NFO_GENRE", "")) or "Home-Video",
        tag=_strip_quotes(os.environ.get("NFO_TAG", "")) or "Home-Video",
        mpaa=_strip_quotes(os.environ.get("NFO_MPAA", "")) or "Rated R",
        certification=_strip_quotes(os.environ.get("NFO_CERTIFICATION", "")) or "Germany:6",
        userrating=_int_env("NFO_USERRATING", 10),
        criticrating=_int_env("NFO_CRITICRATING", 100),
        geo_color=_strip_quotes(os.environ.get("NFO_GEO_COLOR", "")) or "cyan",
        camera_thumb=_strip_quotes(os.environ.get("NFO_CAMERA_THUMB", "")) or "/storage/videos/HomeVideo/.actors/camera.png",
        actors=actors,
        ai_description_enabled=_strip_quotes(os.environ.get("NFO_AI_DESCRIPTION", "")).strip().lower() not in ("0", "false", "no", "off"),
        skip_location=_strip_quotes(os.environ.get("NFO_SKIP_LOCATION", "")).strip().lower() in ("1", "true", "yes", "on"),
        older=older,
        short_video_secs=_int_env("NFO_SHORT_VIDEO_SECS", 10),
    )


def mtime_timezone() -> str:
    """IANA zone name core/mtimecore.py uses to interpret filename-encoded capture times
    and to read/compare/write filesystem mtimes as real-world wall-clock time -- used by
    the standalone Datei-Zeitstempel module (#4) and by every other module's "fix the
    mtime" button / RESTORE_TIMESTAMP auto-path.

    This is NOT optional cosmetics: without an explicit zone, converting between epoch and
    wall-clock (datetime.fromtimestamp()/.timestamp() on a naive datetime) implicitly uses
    the container's own system timezone -- which defaults to UTC in the stock
    python:3.13-slim image used by this project's Dockerfile, since nothing there sets TZ
    or installs OS tzdata. That silently shifts every "Aktuell"/comparison/applied
    timestamp by the difference between UTC and the real locale (e.g. exactly 2 hours for
    Europe/Vienna in CEST) -- which is what caused a file whose name/mtime already agreed
    to be reported and treated as 2 hours off. Timestamp & Rename's own resolution
    (core/timecore.py) doesn't have this problem because it already builds its timestamps
    from explicit ZoneInfo-aware datetimes rather than naive ones; mtimecore.py now does
    the same.

    Defaults to "Europe/Vienna" (same default as the Timestamp & Rename module's own
    Fallback-Zone preset). Configurable via MTIME_TIMEZONE for a different locale. Works
    regardless of the container's own system timezone because the `tzdata` PyPI package
    (requirements.txt) ships the full IANA database for Python's zoneinfo module to use
    even when the OS image itself has none installed.
    """
    return _strip_quotes(os.environ.get("MTIME_TIMEZONE", "")).strip() or "Europe/Vienna"


def restore_timestamp_enabled() -> bool:
    """RESTORE_TIMESTAMP=true: Timestamp & Rename, GPS Sidecar, and AI Description each
    run their own "fix the mtime" step (core/mtimecore.py) automatically right after
    "Ausführen" -- same effect as clicking their "🕒 Datei-Zeitstempel wiederherstellen"
    button yourself, just without having to. Read fresh on every execute() call (same
    "never cached" convention as load_ai_settings()/load_geo_settings()), so toggling it
    takes effect on the container's next request, no restart needed. Off by default,
    since the manual button already covers GPS Sidecar/AI Description and this also adds
    a (normally redundant, but still real) extra pass over Timestamp & Rename's own
    already-correct writes."""
    return _strip_quotes(os.environ.get("RESTORE_TIMESTAMP", "")).strip().lower() in ("1", "true", "yes", "on")
