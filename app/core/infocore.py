"""
infocore.py — everything the Info page shows, collected in one place:

  * build info      (BUILD_DATE / GIT_COMMIT / APP_VERSION build args baked into the image as ENV)
  * tool versions   (exiftool, ffmpeg, ffprobe, mediainfo, perl, python + the pip packages)
  * environment     (every env var the app reads, grouped, with default/state; secrets masked)
  * paths           (config files and media roots: exists / readable / writable / size / mtime)
  * service checks  (optional, on demand: reachability of Traccar, Dawarich, geocoders, Ollama)

Nothing here changes any state. Secrets (tokens / API keys) are never returned in clear text,
only "set, N chars". Slow-ish bits (subprocess version calls) are cached for the life of the
process -- tool versions can't change while the container runs.
"""
from __future__ import annotations

import functools
import importlib.metadata as im
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from . import settings

_STARTED = time.time()

APP_NAME = "Metadata Suite"


# ---------------------------------------------------------------------------
# build info
# ---------------------------------------------------------------------------

def build_info() -> dict:
    """Build args are baked in as ENV by the Dockerfile (APP_VERSION / BUILD_DATE / GIT_COMMIT /
    GIT_BRANCH); outside Docker they're simply "unbekannt" -- except the start time and uptime."""
    def e(name: str) -> str:
        v = (os.environ.get(name) or "").strip()
        return v if v and v.lower() != "unknown" else ""

    build_date = e("BUILD_DATE")
    if not build_date:   # Dockerfile stamps /srv/.build_date (project root) when no build arg was given
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "..", ".build_date"), encoding="utf-8") as fh:
                build_date = fh.read().strip()
        except OSError:
            pass
    age = ""
    if build_date:
        try:
            dt = datetime.fromisoformat(build_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - dt).days
            age = "heute" if days <= 0 else f"vor {days} Tag{'en' if days != 1 else ''}"
        except ValueError:
            pass
    version = e("APP_VERSION")
    if not version and build_date:   # automatic label: build day (+ short commit), e.g. 2026.10.13+ab12cd3
        version = build_date[:10].replace("-", ".")
        if e("GIT_COMMIT"):
            version += "+" + e("GIT_COMMIT")[:7]
    started = datetime.fromtimestamp(_STARTED)
    up = int(time.time() - _STARTED)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    uptime = (f"{d} d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"
    return {
        "version": version,
        "build_date": build_date,
        "build_age": age,
        "git_commit": e("GIT_COMMIT"),
        "git_branch": e("GIT_BRANCH"),
        "git_message": e("GIT_MESSAGE"),
        "started": started.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": uptime,
        "now": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "hostname": socket.gethostname(),
        "in_docker": os.path.exists("/.dockerenv"),
        "container_tz": os.environ.get("TZ", "") or "(nicht gesetzt – UTC; in der .env TZ=Europe/Vienna setzen)",
        "mtime_tz": settings.mtime_timezone(),
        "python": f"{platform.python_version()} ({platform.python_implementation()})",
        "platform": f"{platform.system()} {platform.release()} · {platform.machine()}",
    }


# ---------------------------------------------------------------------------
# tool + package versions
# ---------------------------------------------------------------------------

def _run(cmd: list, timeout: float = 5.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=1)
def tool_versions() -> list[dict]:
    """[{name, version, path, note, status}] -- status: ok | warn | missing."""
    out: list[dict] = []

    def add(name: str, binary: str, version_cmd: list, parse, note: str = "", status: Optional[str] = None):
        path = shutil.which(binary) or ""
        if not path:
            out.append({"name": name, "version": "nicht gefunden", "path": "", "note": note, "status": "missing"})
            return
        raw = _run(version_cmd)
        ver = parse(raw) or (raw.splitlines()[0][:80] if raw else "?")
        out.append({"name": name, "version": ver, "path": path, "note": note, "status": status or "ok"})

    # exiftool: compare with the version pinned at build time (Dockerfile ARG EXIFTOOL_VERSION)
    pinned = (os.environ.get("EXIFTOOL_VERSION") or "").strip()
    et_path = shutil.which("exiftool")
    et_ver = _run(["exiftool", "-ver"]) if et_path else ""
    et_status, et_note = "ok", ""
    if not et_path:
        et_status = "missing"
    elif pinned and et_ver != pinned:
        et_status, et_note = "warn", f"Image wurde mit {pinned} gebaut, installiert ist {et_ver}"
    elif pinned:
        et_note = f"= gepinnte Version (EXIFTOOL_VERSION={pinned})"
    else:
        et_note = "keine Version gepinnt (lokal / nicht per Docker gebaut)"
    out.append({"name": "exiftool", "version": et_ver or "nicht gefunden", "path": et_path or "",
                "note": et_note, "status": et_status})

    # exiftran has no --version switch: the version is the apt package version (the image installs it with apt-get)
    et_pkg = _run(["dpkg-query", "-W", "-f=${Version}", "exiftran"]) if shutil.which("dpkg-query") else ""
    et_pkg = et_pkg if et_pkg and " " not in et_pkg and "dpkg-query" not in et_pkg and "no packages" not in et_pkg.lower() else ""
    add("exiftran", "exiftran", ["exiftran", "-h"], lambda r: et_pkg or "vorhanden (Version nicht ermittelbar)",
        note="verlustfreies JPEG-Drehen (Datei-Zeitstempel: Bilder aufrecht drehen)" + (" \u00b7 Paketversion (apt)" if et_pkg else ""))

    def pin_check(env_name: str, ver: str) -> tuple[str, str]:
        """(status, note) for a tool whose version is pinned at build time via the Dockerfile ARG/ENV."""
        pin = (os.environ.get(env_name) or "").strip()
        if not pin:
            return "ok", "keine Version gepinnt (lokal / nicht per Docker gebaut)"
        if ver != pin:
            return "warn", f"Image wurde mit {pin} gebaut, installiert ist {ver}"
        return "ok", f"= gepinnte Version ({env_name}={pin})"

    for label, binary, cmd, rx, env_name in (
        ("ffmpeg", "ffmpeg", ["ffmpeg", "-version"], r"ffmpeg version (\S+)", "FFMPEG_VERSION"),
        ("ffprobe", "ffprobe", ["ffprobe", "-version"], r"ffprobe version (\S+)", "FFMPEG_VERSION"),
        ("mediainfo", "mediainfo", ["mediainfo", "--Version"], r"v(\d[\w.\-]*)", "MEDIAINFO_VERSION"),
    ):
        if not shutil.which(binary):
            add(label, binary, cmd, lambda r: "")
            continue
        raw = _run(cmd)
        ver = (re.search(rx, raw) or [None, ""])[1] or (raw.splitlines()[0][:80] if raw else "?")
        st, note = pin_check(env_name, ver)
        out.append({"name": label, "version": ver, "path": shutil.which(binary), "note": note, "status": st})
    add("perl (für exiftool)", "perl", ["perl", "-e", "print $^V"], lambda r: r.lstrip("v"))
    out.append({"name": "Python", "version": platform.python_version(), "path": sys.executable,
                "note": platform.python_implementation(), "status": "ok"})
    return out


_PKG_ORDER = ["fastapi", "uvicorn", "starlette", "jinja2", "python-multipart", "pillow",
              "timezonefinder", "tzdata", "requests", "pydantic", "h3", "numpy"]


@functools.lru_cache(maxsize=1)
def package_versions() -> list[dict]:
    """pip packages: first requirements.txt's own (pinned) ones, then a few notable transitive ones."""
    wanted: list[tuple[str, str]] = []   # (dist name, pinned-in-requirements or "")
    req = os.path.join(os.path.dirname(__file__), "..", "..", "requirements.txt")
    pinned: dict[str, str] = {}
    try:
        with open(req, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                m = re.match(r"([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;]+)", line)
                if m:
                    pinned[m.group(1).lower().replace("_", "-")] = m.group(2)
    except OSError:
        pass
    names = list(dict.fromkeys(list(pinned) + _PKG_ORDER))
    rows = []
    for n in names:
        try:
            ver = im.version(n)
        except im.PackageNotFoundError:
            if n in pinned:
                rows.append({"name": n, "version": "nicht installiert", "pinned": pinned[n], "status": "missing"})
            continue
        p = pinned.get(n, "")
        rows.append({"name": n, "version": ver, "pinned": p,
                     "status": "warn" if p and p != ver else "ok"})
    return rows


def frontend_versions() -> list[dict]:
    """htmx / Alpine.js are served from app/static (no CDN needed) -- read the version from the file header."""
    out = []
    sdir = os.path.join(os.path.dirname(__file__), "..", "static")
    for name, fn, rx in (("htmx", "htmx.min.js", r'version:\s*"([\d.]+)"'), ("Alpine.js", "alpine.min.js", r'version\W{1,6}([\d.]{5,})')):
        ver = "?"
        try:
            with open(os.path.join(sdir, fn), encoding="utf-8", errors="ignore") as fh:
                m = re.search(rx, fh.read())
            ver = m.group(1) if m else "?"
        except OSError:
            pass
        out.append({"name": name, "version": ver, "note": "lokal aus /static ausgeliefert (kein CDN nötig)"})
    return out


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

_SECRET_RE = re.compile(r"(TOKEN|API_KEY|PASSWORD|SECRET)", re.I)

# (group, [(NAME, default shown when unset, help)])
ENV_GROUPS: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("Pfade & Ordner", "📁", [
        ("MEDIA_ROOT", "(keine Einschränkung)", "Sicherheitszaun: Browsen/Ausführen nur unterhalb dieses Pfads"),
        ("ROOT_FOLDERS", "VIDEOS=/mnt/NAS/video/Home-Video/, PHOTOS=/mnt/NAS/photo/", "Basis-Ordner-Dropdown (NAME=pfad[:TYP],…)"),
        ("PHOTO_ROOT", "/mnt/NAS/photo", "Foto-Bibliothek (Collage-Quelle)"),
        ("VIDEO_ROOT", "/mnt/NAS/video/Home-Video", "Video-Bibliothek (Collage-Ziel; leer = Spiegeln aus)"),
        ("TIMESTAMP_PRESETS_FILE", "config/presets.json", "Gespeicherte Presets"),
        ("CAMERA_NAMES_FILE", "(Ordner von presets.json)/camera_names.json", "Eigene Kamera-Namen"),
        ("GEO_SERVICE_PUBLIC_URL", "", "Endpunkt-Seite: Host/Adresse des geo-service, wie dein Browser ihn erreicht (leer = Host der Suite-Seite)"),
        ("GEO_PUBLIC_PORT", "8430", "Endpunkt-Seite: Host-Port der API des geo-service"),
        ("GEO_PUBLIC_PORT_NOMINATIM", "8431", "Endpunkt-Seite: Host-Port der Nominatim-Schnittstelle"),
        ("GEO_PUBLIC_PORT_PHOTON", "8432", "Endpunkt-Seite: Host-Port der Photon-Schnittstelle"),
        ("ANDROID_DEVICES_FILE", "(Ordner von presets.json)/android_devices.json", "Google-Geräteliste (Modellcode -> Name), liegt NICHT im Image; holen/aktualisieren: python -m app.core.devicenames update"),
    ]),
    ("Performance (Schritte & exiftool-Pool)", "🪜", [
        ("BATCH_SIZE", "200", "Dateien pro Schritt in allen Listen-Modulen (0 = alles auf einmal)"),
        ("BATCH_SIZE_TIMESTAMP", "(wie BATCH_SIZE)", "Überschreibt BATCH_SIZE für Timestamp & Rename"),
        ("BATCH_SIZE_GPS", "(wie BATCH_SIZE)", "… für GPS Sidecar"),
        ("BATCH_SIZE_AI", "(wie BATCH_SIZE)", "… für AI-Beschreibung"),
        ("BATCH_SIZE_NFO", "(wie BATCH_SIZE)", "… für NFO Maker"),
        ("BATCH_SIZE_MTIME", "(wie BATCH_SIZE)", "… für Datei-Zeitstempel"),
        ("BATCH_SIZE_SHORTVIDEOS", "(wie BATCH_SIZE)", "… für Kurzvideos"),
        ("BATCH_SIZE_GPSFILL", "(wie BATCH_SIZE)", "… für GPS füllen"),
        ("BATCH_SIZE_CTXFILL", "(wie BATCH_SIZE)", "… für Kontext nachfüllen"),
        ("YEAR_RUN_STATE_DAYS", "30", "Jahreslauf: Schritte ohne offene Arbeit werden bei unverändertem Ordner so lange übersprungen (Tage; 0 = immer prüfen)"),
        ("EXIFTOOL_POOL_SIZE", "6", "Dauerhaft laufende exiftool-Prozesse (0 = pro Aufruf neu starten)"),
        ("EXIFTOOL_POOL_IDLE_SECS", "300", "Leerlauf-Sekunden, nach denen ein Pool-Prozess beendet wird"),
    ]),
    ("Zeit & Zeitzone", "🕒", [
        ("TZ", "(UTC)", "Zeitzone des Containers – in der .env setzen, z. B. TZ=Europe/Vienna (wirkt auf Logs, NFO-Datum, Info-Uhrzeit)"),
        ("MTIME_TIMEZONE", "Europe/Vienna", "Zone für Datei-Zeitstempel (mtime)"),
        ("RESTORE_TIMESTAMP", "false", "mtime nach „Ausführen“ automatisch wiederherstellen"),
        ("GPS_UTC_TRUST", "auto", "always = GPS-UTC-Zeit immer vertrauen"),
    ]),
    ("GPS-Tracks", "📍", [
        ("TRACCAR_URL", "", "Traccar-Server"),
        ("TRACCAR_TOKEN", "", "Traccar-API-Token"),
        ("TRACCAR_DEVICES", "", "Standard-Geräte (ID[:Label],…)"),
        ("TRACCAR_DEVICES_ADDITIONAL", "", "Zusätzliche Geräte (nicht vorausgewählt)"),
        ("DAWARICH_SERVER", "", "Dawarich-Server"),
        ("DAWARICH_API_KEY", "", "Dawarich-API-Key"),
        ("DAWARICH_IMPORT_API_KEY", "", "Zweiter Dawarich-Account (Archiv)"),
        ("GPS_TRACK_BRIDGE_DAYS", "3", "Aufnahmetage mit höchstens so vielen Tagen Abstand teilen sich EINEN Track-Abruf"),
        ("GPS_TRACK_MAX_SPAN_DAYS", "31", "Längster Zeitraum pro Track-Abruf (länger = mehrere Abrufe)"),
        ("GEO_MAX_GAP_SECS", "600", "Track-Lücke Stufe 1 (s)"),
        ("GEO_MAX_GAP_SECS_TIER2", "21600", "Track-Lücke Stufe 2 (s)"),
    ]),
    ("Reverse-Geocoding", "🗺", [
        ("PHOTON_SERVER", "", "Photon"),
        ("NOMINATIM_SERVER", "", "Nominatim"),
        ("PELIAS_SERVER", "", "Pelias"),
        ("MARITIME_SERVER", "", "Maritime-Server"),
        ("GEO_LANG", "de", "Sprache der Ortsnamen"),
    ]),
    ("Geo-Service (optional)", "🧭", [
        ("GEO_SERVICE_URL", "", "Zweiter Service in derselben docker-compose.yml (Default http://geo-service:8000); leer = lokale Ketten"),
        ("GEO_SERVICE_TOKEN", "", "Optionaler API-Key des Services"),
        ("GEO_SERVICE_TIMEOUT", "8", "Timeout pro Anfrage (s); bei 2 Fehlern in Folge 60 s Pause"),
        ("GEO_CONTEXT_TIMEOUT", "40", "Timeout (s) nur für /context – der erste Abruf je Ort und Tag ist langsam (Open-Meteo, Sehenswürdigkeiten, Reise)"),
        ("GN_MAX_ROWS_LIMIT", "50", "geo-service: Obergrenze für maxRows der GeoNames-Endpunkte (findNearbyPlaceName/-PostalCodes)"),
        ("GN_POSTAL_PROBE_RINGS", "2", "geo-service: Nominatim-Ringe für findNearbyPostalCodes, wenn Pelias keine Postleitzahlen hat"),
        ("GN_POSTAL_PROBE_POINTS_PER_RING", "8", "geo-service: Messpunkte je Ring"),
        ("GN_POSTAL_PROBE_CONCURRENCY", "6", "geo-service: gleichzeitige Nominatim-Abfragen dabei"),
        ("GN_CHILDREN_MAX_PROBES", "400", "geo-service childrenJSON: höchstens so viele Nominatim-Abfragen pro Aufruf"),
        ("GEO_SLOW_LOG_MS", "500", "geo-service: Anfragen, die länger dauern, mit Zeitstempel (ms) ins Docker-Log schreiben (0 = alle, -1 = aus)"),
        ("GEO_TILE_PROXY", "false", "geo-service: Kartenkacheln über /tiles/{z}/{x}/{y} des Dienstes laden (HTTPS-Seite + http-Tileserver, Handy ohne Zugriff auf den Tileserver)"),
        ("GEO_SIGHTS_DB", "<cache>/sights.sqlite", "geo-service: Sehenswürdigkeiten-Index (SQLite + R-Tree, vom Import-Container gebaut); fehlt er, fragt /sights Photon"),
        ("GEO_OSM_PBF_HOST_DIR", "./osm", "Nur compose (Import-Container): Host-Ordner mit planet-latest.osm.pbf, wird schreibgeschützt nach /osm gemountet"),
        ("GEO_OSM_PBF_FILE", "/osm/planet-latest.osm.pbf", "Import-Container: Pfad der Planet-PBF im Container"),
        ("GEO_SIGHTS_WORK_DIR", "<data>/sights-work", "Import-Container: Arbeitsordner für die osmium-Zwischendateien (einige GB)"),
        ("GEO_SIGHTS_OSMIUM_INDEX", "flex_mem", "Import-Container: osmium-Index für den Export (flex_mem schnell/viel RAM, sparse_file_array langsamer/wenig RAM)"),
        ("GEO_SIGHTS_WD_DELAY", "1.0", "Import-Container: Pause in Sekunden nach jeder Wikidata-Abfrage (50 IDs je Abfrage, je Worker)"),
        ("GEO_SIGHTS_WD_WORKERS", "2", "Import-Container: gleichzeitige Wikidata-Abfragen (Standard 2; 1 = am schonendsten)"),
        ("GEO_SIGHTS_WD_MAXLAG", "0", "Import-Container: 0 = aus. >0 = bei hoher Wikidata-Replikationsverzögerung (Sekunden) warten; 5 ist oft dauerhaft überschritten (maxlag-Meldungen im Log)"),
        ("GEO_SIGHTS_WD_CONTACT", "", "Import-Container: deine E-Mail oder URL im User-Agent der Wikidata-Abfragen (Wikimedia-Empfehlung, optional)"),
        ("OLLAMA_MODEL_TEXT", "", "Import-Container, translate: Textmodell für die Übersetzung der Sehenswürdigkeiten-Beschreibungen (leer = OLLAMA_MODEL, meist das Bildmodell)"),
        ("GOOGLE_AI_MODEL_TEXT", "", "Import-Container, translate: Google-Textmodell für die Übersetzung (leer = GOOGLE_AI_MODEL)"),
        ("GEO_SIGHTS_TR_SCOPE", "notable", "Import-Container, Befehl translate: notable = nur Sehenswürdigkeiten mit Wikimedia-Wichtigkeit/Sitelinks, all = alle ohne deutsche Beschreibung"),
        ("GEO_SIGHTS_TR_MAX", "0", "Import-Container, translate: höchstens so viele Einträge pro Lauf (0 = alle; zum Testen z.B. 20)"),
        ("GEO_SIGHTS_TR_SLEEP", "0", "Import-Container, translate: Pause in Sekunden zwischen zwei Anfragen (Google-Free-Tier z.B. 4)"),
        ("GEO_SIGHTS_TR_TIMEOUT", "60", "Import-Container, translate: Sekunden je Anfrage an Ollama/Google"),
        ("GEO_SIGHTS_TR_RETRY", "0", "Import-Container, translate: 1 = auch Einträge erneut fragen, die das Modell mit UNKNOWN beantwortet hat"),
        ("GEO_SIGHTS_TR_BATCH", "10", "Import-Container, translate: Beschreibungen je Modellanfrage (1 = einzeln; Abgelehnte werden einzeln erneut gefragt)"),
        ("GEO_SIGHTS_TR_WORKERS", "2", "Import-Container, translate: gleichzeitige Anfragen (bei Ollama OLLAMA_NUM_PARALLEL beachten)"),
        ("GEO_SIGHTS_TR_LOG", "1", "Import-Container, translate: 1 = je Eintrag eine Logzeile mit Original, Übersetzung bzw. Ablehnungsgrund; 0 = nur Fortschritt"),
        ("GEO_WIKIMEDIA_HOST_DIR", "./wikimedia", "Nur compose (Import-Container): Host-Ordner mit wikimedia-importance.sql.gz, schreibgeschützt nach /wikimedia"),
        ("GEO_WIKIMEDIA_IMPORTANCE_FILE", "/wikimedia/wikimedia-importance.sql.gz", "Import-Container: Wikimedia-Importance-Datei (.sql.gz/.csv.gz) – fließt in die Wertung der Sehenswürdigkeiten ein"),
        ("AI_CONTEXT", "true (compose)", "AI-Beschreibung: Tageszeit/Wetter/Reisename mitgeben (braucht den Service)"),
        ("AI_CONTEXT_ELEVATION", "false", "Auch die Seehöhe ('auf ca. 1200 m Seehöhe') an das Modell geben; XMP CtxElevation wird immer geschrieben"),
        ("AI_CONTEXT_MAX", "2", "Wie viele Kontext-Angaben der Prompt höchstens nutzen darf (Zahl oder all)"),
        ("AI_CONTEXT_EXTRAS", "daylight,moon,holiday,trip,poi", "Welche Service-Zusatzangaben in den KI-Prompt dürfen: daylight, moon, holiday, trip, poi, wind, precip, country (all/none)"),
        ("NFO_PLOT_CONTEXT", "full", "NFO-Plot: full (Wetter, Wind, Regen, Licht, Mond, Feiertag, Reise, Sehenswürdigkeiten, Land) | basic (Temp/Wetter) | off"),
        ("GEO_HOME_COUNTRY", "AT", "geo-service: Heimat – Länderinfo (Hauptstadt, Währung, Sprachen) nur außerhalb"),
        ("GEO_HOME_LAT", "0", "geo-service: Zuhause (Breite); 0/0 = Ort 'Zuhause' in Meine Orte, sonst Nachtposition"),
        ("GEO_HOME_LON", "0", "geo-service: Zuhause (Länge)"),
        ("GEO_TRIP_AWAY_KM", "60", "geo-service: ab dieser Entfernung vom Zuhause zählt ein Tag als 'unterwegs'"),
        ("GEO_TRIP_MIN_DAYS", "2", "geo-service: kürzer ist keine Reise"),
        ("GEO_TRIP_MAX_GAP_DAYS", "1", "geo-service: Tage zu Hause, die eine Reise nicht beenden"),
        ("GEO_TRIP_LOOKBACK_DAYS", "45", "geo-service: so weit schaut die Reise-Erkennung um einen Zeitpunkt herum"),
        ("GEO_POI", "true", "geo-service: Sehenswürdigkeiten in der Nähe (Photon) + Meine Orte mit Häkchen"),
        ("GEO_POI_RADIUS_M", "400", "geo-service: Radius für Sehenswürdigkeiten (m)"),
        ("GEO_TOP_SIGHT_M", "60", "geo-service: liegt eine wichtige Sehenswürdigkeit innerhalb so vieler Meter, gilt sie als das Motiv (Feld CtxSight, Ort-Zeile der .nfo); 0 = aus"),
        ("GEO_TOP_SIGHT_MIN_IMPORTANCE", "0.30", "geo-service: ab welcher Wikimedia-Wichtigkeit (0..1) eine nahe Sehenswürdigkeit als Motiv gilt (eigene Orte immer)"),
        ("GEO_SIGHTS_WP_SCOPE", "notable", "Import-Container, Befehl wikitext: notable = nur Sehenswürdigkeiten mit Wikimedia-Wichtigkeit, all = alle mit Wikipedia-Artikel"),
        ("GEO_SIGHTS_WP_LANGS", "de,en", "Import-Container, wikitext: Sprachen der Wikipedia-Kurztexte"),
        ("GEO_SIGHTS_WP_MAX", "0", "Import-Container, wikitext: höchstens so viele Artikel pro Lauf (0 = alle)"),
        ("GEO_SIGHTS_WP_DELAY", "0.5", "Import-Container, wikitext: Sekunden Pause zwischen zwei Abfragen (20 Artikel je Abfrage)"),
        ("GEO_SIGHTS_WP_WORKERS", "2", "Import-Container, wikitext: gleichzeitige Abfragen"),
        ("GEO_SIGHTS_WP_SENTENCES", "3", "Import-Container, wikitext: höchstens so viele Sätze aus dem Artikelanfang"),
        ("GEO_SIGHTS_WP_MAXCHARS", "400", "Import-Container, wikitext: Kurztext höchstens so lang (Satzende bevorzugt)"),
        ("GEO_SIGHTS_REFRESH_DAYS", "0", "Automatische Aktualisierung des Sehenswürdigkeiten-Index: alle so viele Tage (0 = aus). Dienst sights-refresh (Profil refresh)"),
        ("GEO_SIGHTS_REFRESH_AT", "03:30", "Automatische Aktualisierung: Uhrzeit (HH:MM, Zeitzone des Containers/TZ)"),
        ("GEO_SIGHTS_IMPORTANCE_URL", "https://nominatim.org/data/wikimedia-importance.csv.gz", "Automatische Aktualisierung: Download der Wikimedia-Importance-Datei (off = nicht laden)"),
        ("GEO_SIGHTS_PBF_URL", "", "Automatische Aktualisierung: Download der Planet-PBF (z.B. https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf, ca. 85 GB); leer = die vorhandene GEO_OSM_PBF_FILE (Nominatim) nutzen"),
        ("GEO_DOWNLOAD_HOST_DIR", "./downloads", "Nur compose: Host-Ordner für heruntergeladene Quelldateien (nach /downloads; behalten werden die neueste und die letzte erfolgreich genutzte Version)"),
        ("GEO_SIGHTS_REFRESH_WIKITEXT", "1", "Automatische Aktualisierung: auch Wikipedia-Kurztexte holen (0 = nicht)"),
        ("GEO_SIGHTS_REFRESH_TRANSLATE", "1", "Automatische Aktualisierung: auch Beschreibungen per LLM übersetzen (braucht AI_PROVIDER; 0 = nicht)"),
        ("GEO_SIGHTS_REFRESH_FORCE_BUILD", "0", "Automatische Aktualisierung: 1 = den Index in jedem Lauf neu bauen, auch wenn die PBF gleich ist"),
        ("GEO_SIGHTS_KEEP_PREV", "1", "Beim Neuaufbau die bisherige sights.sqlite als sights.sqlite.prev behalten (Hardlink, kostet keinen Platz zusätzlich; 0 = nicht)"),
        ("GEO_POI_MAX", "3", "geo-service: höchstens so viele Sehenswürdigkeiten"),
        ("GEO_POI_SKIP", "", "geo-service: zusätzliche historic=*-Werte, die keine Sehenswürdigkeit sind (Komma-Liste)"),
        ("XMP_EE_MAX_MB", "100", "exiftool -ee (alle Videoframes) nur bis zu dieser Dateigröße; größere Videos mit -fast2"),
        ("XMP_TIMEOUT_MAX", "1800", "Höchst-Timeout (s) beim Lesen großer Videos (60 s + 0,25 s/MB)"),
        ("GEO_TRACK_TIMEOUT", "60", "geo-service: Timeout (s) je Abruf bei Traccar/Dawarich"),
        ("WEATHER_ARCHIVE_URL", "archive-api.open-meteo.com/v1/archive", "geo-service: Open-Meteo-Archiv"),
        ("WEATHER_FORECAST_URL", "api.open-meteo.com/v1/forecast", "geo-service: Open-Meteo-Vorhersage"),
        ("ELEVATION_URL", "api.open-meteo.com/v1/elevation", "geo-service: Höhen-API ohne lokale SRTM-Kacheln"),
        ("GEO_SRTM_SOURCE_URL", "elevation-tiles-prod.s3.amazonaws.com/skadi", "geo-service: Quelle der SRTM-Kacheln"),
        ("GN_ID_DB", "", "geo-service: Datei der synthetischen geonameIds"),
        ("GN_GAZETTEER_DB", "", "geo-service: eigene GeoNames-Datenbank (Standard: gazetteer.sqlite neben dem Cache)"),
        ("GN_GAZETTEER_LANGS", "de,en", "geo-service: Sprachen der alternativen Namen beim Import"),
        ("GN_GAZETTEER_FTS_CLASSES", "", "geo-service: Suchindex nur für diese Feature-Klassen (leer = alle)"),
        ("GN_GAZETTEER_REVERSE_KM", "25", "geo-service: /reverse über die eigene Datenbank, höchster Abstand zum Ort in km"),
        ("GN_DUMP_DIR", "", "geo-service: Ordner der GeoNames-Dumps (Standard: /geonames)"),
        ("GEO_NORMALIZE_NAMES", "true", "geo-service: einheitliche Ortsnamen ('Marktgemeinde X' -> 'X')"),
        ("GEO_NAME_ALIASES_FILE", "place-names.json", "geo-service: eigene Schreibweisen für Ortsnamen"),
        ("IPTC_FILL", "true", "IPTC Credits/Origin nur wenn leer füllen: Date created (Aufnahmezeit), Source (Kameraname), Credits"),
        ("IPTC_CREDIT", "", "Text für IPTC Credits / photoshop:Credit (leer = nichts schreiben)"),
        ("XMP_PLACE_EXTRAS", "true", "GPS Sidecar / GPS-Fill ergänzen fehlende Höhe (XMP-exif:GPSAltitude) und Zeitzone (XMP-mon:CtxTimezone); die Höhe kommt beim Einbetten auch ins Bild"),
        ("IMAGE_OFFSET_FROM_ZONE", "true", "Beim Einbetten EXIF OffsetTimeOriginal aus der Zeitzone ergänzen (nur wenn das Bild keinen hat; setzt voraus, dass die Kamerauhr Ortszeit zeigt)"),
        ("XMP_CONTEXT", "true", "Kontext (Tageszeit, Jahreszeit, Wetter, Reise) als XMP-mon:Ctx*-Felder ins Sidecar schreiben, auch ohne KI-Text"),
        ("AI_SENTENCES", "2-3", "Sätze pro Beschreibung ohne Zusatzkontext"),
        ("AI_WORDS_MAX", "50", "Wortlimit ohne Zusatzkontext"),
        ("AI_SENTENCES_CONTEXT", "3-4", "Sätze pro Beschreibung mit Zusatzkontext"),
        ("AI_WORDS_MAX_CONTEXT", "75", "Wortlimit mit Zusatzkontext"),
    ]),
    ("AI-Beschreibung", "🤖", [
        ("AI_PROVIDER", "", "Prioritätsliste, z.B. ollama,google"),
        ("OLLAMA_URL", "", "Ollama-Server"),
        ("OLLAMA_MODEL", "", "Ollama-Modell"),
        ("GOOGLE_AI_API_KEY", "", "Google-AI-Key"),
        ("GOOGLE_AI_MODEL", "", "Google-AI-Modell"),
    ]),
    ("NFO Maker", "🎞", [
        ("NFO_DIRECTOR", "", ""), ("NFO_STUDIO", "", ""),
        ("NFO_SET", "Home Movie", ""), ("NFO_GENRE", "Home-Video", ""), ("NFO_TAG", "Home-Video", ""),
        ("NFO_MPAA", "Rated R", ""), ("NFO_CERTIFICATION", "Germany:6", ""),
        ("NFO_USERRATING", "10", ""), ("NFO_CRITICRATING", "100", ""),
        ("NFO_GEO_COLOR", "cyan", ""), ("NFO_CAMERA_THUMB", "…/.actors/camera.png", ""),
        ("NFO_ACTORS", "(8 Standard-Darsteller)", "Name|Rolle|Thumb;…"),
        ("NFO_AI_DESCRIPTION", "true", "AI-Beschreibung in den Plot übernehmen"),
        ("NFO_SKIP_LOCATION", "false", "Ort nicht in den Plot schreiben"),
        ("NFO_OLDER", "", "-O/--older Schutzfenster (Tage ≤ 0)"),
        ("NFO_SHORT_VIDEO_SECS", "10", "„Kurzes Video“-Schwelle (s)"),
    ]),
    ("Collage", "🖼", [
        ("COLLAGE_LAYOUT", "random", ""), ("COLLAGE_WIDTH", "4096", ""), ("COLLAGE_HEIGHT", "2304", ""),
        ("COLLAGE_COUNT", "139", ""), ("COLLAGE_BORDER", "6", ""), ("COLLAGE_ROTATION", "25", ""),
        ("COLLAGE_JITTER", "0.55", ""), ("COLLAGE_SPACING", "0", ""), ("COLLAGE_SIZE_VARIATION", "0.35", ""),
        ("COLLAGE_CELL", "", ""), ("COLLAGE_TILE_SIZE", "", ""), ("COLLAGE_SEED", "", ""),
        ("COLLAGE_SYNC_FOLDER_JPG", "false", ""),
    ]),
    ("Build", "🏗", [
        ("APP_VERSION", "", "Versions-Label des Images; leer = automatisch Build-Datum (+ Commit), z. B. 2026.10.13+ab12cd3"),
        ("BUILD_DATE", "", "Build-Zeitpunkt (UTC); sonst /srv/.build_date aus dem Image"),
        ("GIT_COMMIT", "", "Git-Commit des Builds"),
        ("GIT_BRANCH", "", "Git-Branch des Builds"),
        ("GIT_MESSAGE", "", "Commit-Kommentar (erste Zeile) des Builds"),
        ("EXIFTOOL_VERSION", "", "Beim Build gepinnte exiftool-Version"),
        ("FFMPEG_VERSION", "", "Beim Build gepinnte ffmpeg-/ffprobe-Version"),
        ("MEDIAINFO_VERSION", "", "Beim Build gepinnte mediainfo-Version"),
    ]),
]


def _mask(name: str, value: str) -> str:
    return f"●●●●●●●● ({len(value)} Zeichen)" if value else ""



def _service_env() -> dict:
    """Variablen, die im geo-service-Container gelten (der Service meldet sie in /health; Secrets nie)."""
    try:
        from . import geoservice
        if geoservice.enabled():
            return geoservice.health().get("env") or {}
    except Exception:       # noqa: BLE001
        pass
    return {}


def env_groups() -> list[dict]:
    senv = _service_env()
    b = build_info()
    # value actually in effect when the variable itself is empty/unset (shown as "automatisch")
    effective = {"APP_VERSION": b["version"], "BUILD_DATE": b["build_date"]}
    groups = []
    for title, icon, items in ENV_GROUPS:
        rows = []
        for name, default, help_ in items:
            raw = os.environ.get(name)
            if raw is None and name in senv:            # lives in the geo-service container
                raw = senv[name]
            is_secret = bool(_SECRET_RE.search(name))
            if raw is None:
                state, shown = "default", ""
            elif not raw.strip():
                state, shown = "empty", ""
            else:
                state = "set"
                shown = _mask(name, raw) if is_secret else raw
            rows.append({"name": name, "value": shown, "default": default, "help": help_,
                         "state": state, "secret": is_secret,
                         "effective": effective.get(name, "") if state != "set" else ""})
        groups.append({"title": title, "icon": icon, "rows": rows,
                       "n_set": sum(1 for r in rows if r["state"] == "set")})
    return groups


def other_env() -> list[dict]:
    """Env vars that look app-related but aren't in the known table (typos, forgotten leftovers)."""
    known = {n for _, _, items in ENV_GROUPS for n, _, _ in items}
    prefixes = ("NFO_", "COLLAGE_", "GEO_", "TRACCAR_", "DAWARICH_", "OLLAMA_", "GOOGLE_AI_", "AI_",
                "BATCH_SIZE_", "GPS_TRACK_", "PHOTON_", "NOMINATIM_", "PELIAS_", "MARITIME_", "MTIME_", "GPS_", "TIMESTAMP_", "CAMERA_")
    rows = []
    for k in sorted(os.environ):
        if k in known or not k.startswith(prefixes):
            continue
        v = os.environ[k]
        rows.append({"name": k, "value": _mask(k, v) if _SECRET_RE.search(k) else v})
    return rows


def root_folders() -> list[dict]:
    rows = []
    for rf in settings.load_root_folders():
        p = rf.path
        rows.append({"name": rf.name, "path": p, "type": rf.type_filter,
                     "exists": os.path.isdir(p)})
    return rows


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _path_row(label: str, path: str, kind: str = "file", note: str = "") -> dict:
    p = os.path.normpath(path) if path else ""
    row = {"label": label, "path": p, "kind": kind, "note": note,
           "exists": False, "readable": False, "writable": False, "size": "", "mtime": ""}
    if not p:
        return row
    row["exists"] = os.path.exists(p)
    if row["exists"]:
        row["readable"] = os.access(p, os.R_OK)
        row["writable"] = os.access(p, os.W_OK)
        try:
            st = os.stat(p)
            row["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            if kind == "file":
                row["size"] = _human_size(st.st_size)
            else:
                row["size"] = f"{len(os.listdir(p))} Einträge" if row["readable"] else ""
        except OSError:
            pass
    return row


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def path_rows() -> list[dict]:
    from . import devicenames
    rows = [
        _path_row("Presets", settings.DEFAULT_PRESETS_PATH, note="TIMESTAMP_PRESETS_FILE"),
        _path_row("Kamera-Namen (eigene)", devicenames.OVERRIDES_PATH, note="CAMERA_NAMES_FILE · optional"),
        _path_row("Android-Geräteliste", devicenames.ANDROID_DB_PATH, note="ANDROID_DEVICES_FILE · optional · holen/aktualisieren: docker compose exec metadata-suite python -m app.core.devicenames update"),
    ]
    mr = os.environ.get("MEDIA_ROOT", "").strip()
    rows.append(_path_row("MEDIA_ROOT", mr, "dir", "Sicherheitszaun") if mr else
                {"label": "MEDIA_ROOT", "path": "", "kind": "dir", "note": "nicht gesetzt – keine Einschränkung",
                 "exists": False, "readable": False, "writable": False, "size": "", "mtime": ""})
    rows.append(_path_row("PHOTO_ROOT", settings.photo_root(), "dir", "Collage-Quelle"))
    vr = settings.video_root()
    rows.append(_path_row("VIDEO_ROOT", vr, "dir", "Collage-Ziel") if vr else
                {"label": "VIDEO_ROOT", "path": "", "kind": "dir", "note": "leer – Spiegeln deaktiviert",
                 "exists": False, "readable": False, "writable": False, "size": "", "mtime": ""})
    return rows


# ---------------------------------------------------------------------------
# service reachability (on demand)
# ---------------------------------------------------------------------------

def service_targets() -> list[dict]:
    g = settings.load_geo_settings()
    a = settings.load_ai_settings()
    cand = [("Traccar", g.traccar_url), ("Dawarich", g.dawarich_server), ("Photon", g.photon_server),
            ("Nominatim", g.nominatim_server), ("Pelias", g.pelias_server), ("Maritime", g.maritime_server),
            ("Ollama", a.ollama_url)]
    from . import geoservice
    if geoservice.enabled():
        cand.insert(0, ("Geo-Service", geoservice.url() + "/health"))
    return [{"name": n, "url": u} for n, u in cand if u]


def _ping(t: dict, timeout: float = 3.0) -> dict:
    url = t["url"]
    started = time.time()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "metadata-suite-info"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code           # server answered -> reachable (401/404 are fine here)
    except Exception as e:      # noqa: BLE001 - any network failure is just "not reachable"
        return {**t, "ok": False, "detail": type(e).__name__ + (f": {e.reason}" if hasattr(e, "reason") else ""),
                "ms": int((time.time() - started) * 1000)}
    return {**t, "ok": True, "detail": f"HTTP {code}", "ms": int((time.time() - started) * 1000)}


def sights_status() -> dict:
    """State of the geo-service's sights index (GET /sights/status) for the Info page: {'ok', 'data'|'error'}."""
    from . import geoservice
    if not geoservice.enabled():
        return {"ok": False, "error": "GEO_SERVICE_URL ist nicht gesetzt"}
    data, state = geoservice._get("/sights/status", {})
    if state != "ok" or not isinstance(data, dict):
        return {"ok": False, "error": "der geo-service ist nicht erreichbar oder zu alt (kein /sights/status)"}
    dls = []
    for kind, lst in ((data.get("refresh") or {}).get("downloads") or {}).items():
        for e in lst:
            dls.append({"kind": kind, **e})
    return {"ok": True, "data": data, "downloads": dls}


def check_services() -> list[dict]:
    targets = service_targets()
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        return list(ex.map(_ping, targets))


# ---------------------------------------------------------------------------
# everything, plus a plain-text report (secrets stay masked) for copy/paste into a bug report
# ---------------------------------------------------------------------------

def collect() -> dict:
    return {
        "build": build_info(),
        "tools": tool_versions(),
        "packages": package_versions(),
        "frontend": frontend_versions(),
        "env_groups": env_groups(),
        "other_env": other_env(),
        "root_folders": root_folders(),
        "paths": path_rows(),
        "services": service_targets(),
    }


def text_report(d: dict) -> str:
    b = d["build"]
    L = [f"{APP_NAME} – Info", "=" * 40,
         f"Version     : {b['version'] or '?'}", f"Build       : {b['build_date'] or '?'}",
         f"Git         : {b['git_commit'] or '?'} {b['git_branch']}".rstrip(),
         f"Gestartet   : {b['started']}  (Laufzeit {b['uptime']})",
         f"Host        : {b['hostname']} · {b['platform']}", "", "Tools", "-" * 40]
    L += [f"{t['name']:<22} {t['version']}  {t['note']}".rstrip() for t in d["tools"]]
    L += ["", "Python-Pakete", "-" * 40]
    L += [f"{p['name']:<22} {p['version']}" for p in d["packages"]]
    L += ["", "Pfade", "-" * 40]
    L += [f"{p['label']:<24} {p['path'] or '-'}  [{'ok' if p['exists'] else 'fehlt'}]" for p in d["paths"]]
    L += ["", "Umgebung (nur gesetzte)", "-" * 40]
    for g in d["env_groups"]:
        for r in g["rows"]:
            if r["state"] == "set":
                L.append(f"{r['name']}={r['value']}")
    return "\n".join(L) + "\n"
