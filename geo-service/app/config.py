"""config.py -- all settings come from environment variables.

The variable names are the ones the metadata-suite's .env already uses (PHOTON_SERVER,
NOMINATIM_SERVER, PELIAS_SERVER, MARITIME_SERVER, TRACCAR_*, DAWARICH_*, GEO_LANG,
GEO_MAX_GAP_SECS*), so the same .env block can be pasted in unchanged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _s(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _i(name: str, default: int) -> int:
    try:
        return int(_s(name) or default)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except ValueError:
        return default


def parse_devices(raw: str) -> list[tuple[str, str]]:
    """'7:Phone A,19:gpslogger' -> [('7','Phone A'),('19','gpslogger')]."""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        dev, _, label = item.partition(":")
        out.append((dev.strip(), label.strip()))
    return out


@dataclass
class Settings:
    # reverse geocoding
    photon: str = ""
    nominatim: str = ""
    pelias: str = ""
    maritime: str = ""
    lang: str = "de"
    chain: list = field(default_factory=lambda: ["photon", "nominatim", "pelias", "gazetteer"])
    http_timeout: float = 5.0
    breaker_failures: int = 3
    breaker_cooldown: int = 60
    # cache
    cache_db: str = "/data/geo-cache.sqlite"
    cache_precision: int = 4          # decimals of lat/lon in the cache key (4 = ~11 m)
    cache_ttl_days: int = 365
    cache_neg_ttl: int = 3600         # "no place found here" is only remembered for an hour
    # tracks
    traccar_url: str = ""
    traccar_token: str = ""
    traccar_devices: list = field(default_factory=list)
    traccar_devices_additional: list = field(default_factory=list)
    dawarich_server: str = ""
    dawarich_api_key: str = ""
    dawarich_import_api_key: str = ""
    track_timeout: float = 60.0
    max_gap1: int = 600
    max_gap2: int = 21600
    # context / weather
    weather_provider: str = "none"    # none | open-meteo
    weather_archive_url: str = "https://archive-api.open-meteo.com/v1/archive"
    weather_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_backfill_delay: float = 0.4   # seconds between upstream requests while backfilling (Open-Meteo: 600/min, 10 000/day)
    weather_backfill_max: int = 5000      # upstream requests per backfill run (the rest is picked up by the next run)
    default_tz: str = "Europe/Vienna"
    # GeoNames-compatible endpoints (findNearbyPlaceName / findNearbyPostalCodes), see geonames.py
    gn_http_timeout: float = 10.0
    gn_max_rows: int = 50             # hard ceiling for every maxRows
    gn_probe_rings: int = 2           # postal fallback: rings of Nominatim probes ...
    gn_probe_points: int = 8          # ... with this many points each
    gn_probe_concurrency: int = 6     # ... run this many at a time
    slow_log_ms: int = 500              # log every request slower than this (ms) with a millisecond timestamp; 0 = all, -1 = off
    gn_children_max_probes: int = 400   # childrenJSON: most Nominatim lookups per call before it reports an incomplete list
    gn_search_order: list = field(default_factory=lambda: ["gazetteer", "pelias", "nominatim", "photon"])
    gn_country_file: str = "/geonames/countryInfo.txt"   # GeoNames countryInfo.txt, read from the GeoNames volume
    sights_db: str = ""               # own sights database built from the planet file (default: <data dir>/sights.sqlite)
    gn_gazetteer_db: str = ""        # own GeoNames database built from the dumps (default: <data dir>/gazetteer.sqlite)
    gn_gazetteer_langs: tuple = ("de", "en")   # languages of alternate names to keep
    gn_gazetteer_fts_classes: str = ""         # restrict the search index to these feature classes (empty = all)
    gn_gazetteer_reverse_km: float = 25.0     # /reverse via the own GeoNames database: nearest populated place within this distance
    gn_id_db: str = ""                # synthetic geonameId registry; default: next to the cache db
    # elevation / overrides
    elevation_providers: list = field(default_factory=lambda: ["local", "open-meteo"])   # priority list; [] = off
    elevation_url: str = "https://api.open-meteo.com/v1/elevation"
    srtm_dir: str = "/data/srtm"      # N47E016.hgt (SRTM3 1201x1201 or SRTM1 3601x3601), also .hgt.gz
    srtm_preload: list = field(default_factory=list)   # regions fetched in the background at start: austria
    srtm_source_url: str = "https://elevation-tiles-prod.s3.amazonaws.com/skadi"
    overrides_file: str = ""          # default: <dir of the cache db>/overrides.json
    # context extras
    home_country: str = "AT"          # ISO alpha-2 of home: country info is only added for OTHER countries
    home_lat: float = 0.0             # GEO_HOME_LAT/LON; both 0 = work out home from the tracks (where the nights are)
    home_lon: float = 0.0
    trip_away_km: float = 60.0        # a day further than this from home is an 'away' day
    trip_min_days: int = 2            # shorter absences are no trip (1 = day trips count too)
    trip_max_gap_days: int = 1        # home days allowed inside one trip
    trip_lookback_days: int = 45      # how far around a moment the trip search looks
    place_trip_km: float = 100.0      # no folder/track trip: a spot further than this from home gets 'Ort und Umgebung' (0 = off)
    poi_enabled: bool = True          # nearby sights via Photon (needs PHOTON_SERVER)
    poi_radius_m: int = 400
    poi_max: int = 3
    top_sight_m: int = 60
    top_sight_min_importance: float = 0.30
    poi_skip: tuple = ()               # extra historic=* values that are no sights (GEO_POI_SKIP)
    normalize_names: bool = True      # 'Marktgemeinde Musterdorf' -> 'Musterdorf'
    name_aliases_file: str = ""       # default: <data dir>/place-names.json
    # the editor's map
    tile_url: str = ""                # raster tiles of your tileserver-GL, e.g. http://host:8777/styles/x/{z}/{x}/{y}.png
    tile_attribution: str = ""
    tile_proxy: bool = False          # serve the tiles through /tiles/{z}/{x}/{y} (one HTTPS host, tileserver stays internal)
    tile_maxzoom: int = 19
    # rain radar page /radar
    radar_style_url: str = ""         # MapLibre style of your tileserver-GL (default: derived from GEO_TILE_URL)
    radar_api: str = "https://api.rainviewer.com/public/weather-maps.json"   # RainViewer-compatible (e.g. your own LibreWXR)
    radar_color: int = 2
    radar_options: str = "1_0"
    radar_maxzoom: int = 7
    # Nominatim-/Photon-compatible facades on their own ports (for Traccar, Dawarich, ...); 0 = off
    compat_nominatim_port: int = 8001
    compat_photon_port: int = 8002
    compat_auth: bool = False         # True: these ports ask for GEO_SERVICE_TOKEN too (?token= / ?key= / header)
    # access
    token: str = ""                   # optional bearer token for the whole API except /health

    @property
    def sights_index_path(self) -> str:
        return self.sights_db or os.path.join(os.path.dirname(self.cache_db) or ".", "sights.sqlite")

    @property
    def gazetteer(self) -> str:
        """Path of the own GeoNames database when it exists (then 'gazetteer' counts as a configured geocoder)."""
        path = self.gn_gazetteer_db or os.path.join(os.path.dirname(self.cache_db) or ".", "gazetteer.sqlite")
        return path if os.path.isfile(path) else ""

    @property
    def any_geocoder(self) -> bool:
        return bool(self.photon or self.nominatim or self.pelias or self.maritime or self.gazetteer)


def load() -> Settings:
    chain = [c.strip().lower() for c in _s("GEO_CHAIN", "photon,nominatim,pelias,gazetteer").split(",") if c.strip()]
    return Settings(
        photon=_s("PHOTON_SERVER"), nominatim=_s("NOMINATIM_SERVER"), pelias=_s("PELIAS_SERVER"),
        maritime=_s("MARITIME_SERVER"), lang=_s("GEO_LANG", "de"), chain=chain,
        http_timeout=_f("GEO_HTTP_TIMEOUT", 5.0),
        breaker_failures=_i("GEO_BREAKER_FAILURES", 3), breaker_cooldown=_i("GEO_BREAKER_COOLDOWN", 60),
        cache_db=_s("GEO_CACHE_DB", "/data/geo-cache.sqlite"),
        cache_precision=max(2, min(6, _i("GEO_CACHE_PRECISION", 4))),
        cache_ttl_days=_i("GEO_CACHE_TTL_DAYS", 365), cache_neg_ttl=_i("GEO_CACHE_NEG_TTL", 3600),
        traccar_url=_s("TRACCAR_URL"), traccar_token=_s("TRACCAR_TOKEN"),
        traccar_devices=parse_devices(_s("TRACCAR_DEVICES")),
        traccar_devices_additional=parse_devices(_s("TRACCAR_DEVICES_ADDITIONAL")),
        dawarich_server=_s("DAWARICH_SERVER"), dawarich_api_key=_s("DAWARICH_API_KEY"),
        dawarich_import_api_key=_s("DAWARICH_IMPORT_API_KEY"),
        track_timeout=_f("GEO_TRACK_TIMEOUT", 60.0),
        max_gap1=_i("GEO_MAX_GAP_SECS", 600), max_gap2=_i("GEO_MAX_GAP_SECS_TIER2", 21600),
        weather_provider=_s("WEATHER_PROVIDER", "none").lower(),
        weather_archive_url=_s("WEATHER_ARCHIVE_URL", "https://archive-api.open-meteo.com/v1/archive"),
        weather_forecast_url=_s("WEATHER_FORECAST_URL", "https://api.open-meteo.com/v1/forecast"),
        weather_backfill_delay=_f("WEATHER_BACKFILL_DELAY", 0.4), weather_backfill_max=_i("WEATHER_BACKFILL_MAX", 5000),
        default_tz=_s("GEO_DEFAULT_TZ", "Europe/Vienna"),
        gn_http_timeout=_f("GN_HTTP_TIMEOUT", 10.0), gn_max_rows=max(1, _i("GN_MAX_ROWS_LIMIT", 50)),
        gn_probe_rings=max(1, _i("GN_POSTAL_PROBE_RINGS", 2)), gn_probe_points=max(1, _i("GN_POSTAL_PROBE_POINTS_PER_RING", 8)),
        gn_probe_concurrency=max(1, _i("GN_POSTAL_PROBE_CONCURRENCY", 6)),
        slow_log_ms=_i("GEO_SLOW_LOG_MS", 500), gn_children_max_probes=max(10, _i("GN_CHILDREN_MAX_PROBES", 400)), gn_id_db=_s("GN_ID_DB"), gn_gazetteer_db=_s("GN_GAZETTEER_DB"), sights_db=_s("GEO_SIGHTS_DB"),
        gn_gazetteer_langs=tuple(x.strip().lower() for x in _s("GN_GAZETTEER_LANGS", "de,en").split(",") if x.strip()),
        gn_gazetteer_fts_classes=_s("GN_GAZETTEER_FTS_CLASSES"), gn_gazetteer_reverse_km=_f("GN_GAZETTEER_REVERSE_KM", 25.0),
        gn_search_order=[x.strip().lower() for x in _s("GN_SEARCH_ORDER", _s("SEARCH_BACKEND_ORDER", "gazetteer,pelias,nominatim,photon")).split(",") if x.strip()],
        gn_country_file=_s("GN_COUNTRY_INFO_FILE", _s("COUNTRY_INFO_FILE", "/geonames/countryInfo.txt")),
        elevation_providers=[x.strip().lower() for x in _s("ELEVATION_PROVIDER", "local,open-meteo").split(",")
                             if x.strip() and x.strip().lower() not in ("none", "off", "false", "0")],
        elevation_url=_s("ELEVATION_URL", "https://api.open-meteo.com/v1/elevation"),
        srtm_dir=_s("GEO_SRTM_DIR", "/data/srtm"),
        srtm_preload=[x.strip().lower() for x in _s("GEO_SRTM_PRELOAD", "austria").split(",")
                      if x.strip() and x.strip().lower() not in ("none", "off", "false", "0")],
        srtm_source_url=_s("GEO_SRTM_SOURCE_URL", "https://elevation-tiles-prod.s3.amazonaws.com/skadi"),
        overrides_file=_s("GEO_OVERRIDES_FILE"),
        home_country=_s("GEO_HOME_COUNTRY", "AT").upper(), home_lat=_f("GEO_HOME_LAT", 0.0), home_lon=_f("GEO_HOME_LON", 0.0),
        trip_away_km=_f("GEO_TRIP_AWAY_KM", 60.0), trip_min_days=max(1, _i("GEO_TRIP_MIN_DAYS", 2)),
        trip_max_gap_days=max(0, _i("GEO_TRIP_MAX_GAP_DAYS", 1)), trip_lookback_days=max(7, min(120, _i("GEO_TRIP_LOOKBACK_DAYS", 45))),
        place_trip_km=max(0.0, _f("GEO_PLACE_TRIP_KM", 100.0)),
        poi_enabled=_s("GEO_POI", "true").lower() not in ("0", "false", "no", "off"), poi_radius_m=max(50, _i("GEO_POI_RADIUS_M", 400)),
        poi_max=max(1, _i("GEO_POI_MAX", 3)),
        top_sight_m=max(0, _i("GEO_TOP_SIGHT_M", 60)), top_sight_min_importance=_f("GEO_TOP_SIGHT_MIN_IMPORTANCE", 0.30),
        poi_skip=tuple(x.strip().lower() for x in _s("GEO_POI_SKIP", "").split(",") if x.strip()),
        normalize_names=_s("GEO_NORMALIZE_NAMES", "true").lower() not in ("0", "false", "no", "off"),
        name_aliases_file=_s("GEO_NAME_ALIASES_FILE"),
        tile_url=_s("GEO_TILE_URL"), tile_attribution=_s("GEO_TILE_ATTRIBUTION"),
        tile_proxy=_s("GEO_TILE_PROXY").lower() in ("1", "true", "yes", "on"),
        tile_maxzoom=max(1, min(22, _i("GEO_TILE_MAXZOOM", 19))),
        radar_style_url=_s("GEO_RADAR_STYLE_URL"),
        radar_api=_s("GEO_RADAR_API", "https://api.rainviewer.com/public/weather-maps.json"),
        radar_color=_i("GEO_RADAR_COLOR", 2), radar_options=_s("GEO_RADAR_OPTIONS", "1_0") or "1_0",
        radar_maxzoom=max(1, min(12, _i("GEO_RADAR_MAXZOOM", 7))),
        compat_nominatim_port=max(0, _i("GEO_NOMINATIM_PORT", 8001)),
        compat_photon_port=max(0, _i("GEO_PHOTON_PORT", 8002)),
        compat_auth=_s("GEO_COMPAT_AUTH").lower() in ("1", "true", "yes", "on"),
        token=_s("GEO_SERVICE_TOKEN"),
    )
