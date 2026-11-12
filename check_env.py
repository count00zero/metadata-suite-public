#!/usr/bin/env python3
"""check_env.py -- prueft deine lokale .env, ohne dass irgendein Wert das Repo oder deinen Rechner verlaesst.

Aufruf (nur Python 3.8+, keine Pakete noetig; laeuft auch auf dem Laptop, ohne Docker):

    python check_env.py                       # .env im selben Ordner gegen .env.example
    python check_env.py --env /pfad/.env      # andere Datei
    python check_env.py --paths               # zusaetzlich: gibt es die Ordner wirklich (nur auf dem Server sinnvoll)

Was geprueft wird
  FEHLER   unbedingt noetige Werte fehlen/ungueltig (Exit-Code 1)
  WARNUNG  empfohlene private Werte sind leer, noch Beispielwerte aus .env.example, oder verdaechtige Tippfehler im Namen
  HINWEIS  Nebensaechliches (Abhaengigkeiten, Dateirechte, Git)

Werte von Tokens/Schluesseln werden NIE ausgegeben (nur "gesetzt, N Zeichen").
Exit-Code: 0 = in Ordnung (evtl. Warnungen), 1 = mindestens ein Fehler, 2 = Datei nicht lesbar.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------------------------------------------------
# .env lesen (docker compose style: KEY=VALUE, "quoted", 'quoted', # Kommentar, export KEY=...)
# ---------------------------------------------------------------------------------------------------------------------
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def parse_env(text: str) -> dict:
    out: dict = {}
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val and val[0] in "\"'":
            q = val[0]
            end = val.find(q, 1)
            val = val[1:end] if end != -1 else val[1:]
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key] = val
    return out


def parse_example(text: str) -> tuple[dict, set]:
    """(aktive Beispielwerte, alle bekannten Namen inkl. auskommentierter '#KEY=...'-Zeilen)."""
    active = parse_env(text)
    known = set(active)
    for raw in text.splitlines():
        m = re.match(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\s*=", raw)
        if m:
            known.add(m.group(1))
    return active, known


# ---------------------------------------------------------------------------------------------------------------------
# Regeln
# ---------------------------------------------------------------------------------------------------------------------
SECRET = re.compile(r"(TOKEN|API_KEY|SECRET|PASSWORD|PASSWD)", re.I)
PLACEHOLDERS = [r"192\.168\.1\.\d+", r"\bexample\.(org|com)\b", r"\bYour (Name|Studio)\b", r"\bBeispiel", r"\bPhone [A-C]\b",
                r"\bWork phone\b", r"\bMusterdorf\b", r"\bMustermann\b", r"/mnt/ssd/", r"DEIN_?TOKEN", r"changeme", r"xxx+"]
_PH = re.compile("|".join(PLACEHOLDERS), re.I)
TRUE = {"1", "true", "yes", "on", "0", "false", "no", "off", ""}


class Report:
    def __init__(self):
        self.items: list = []

    def add(self, level: str, key: str, msg: str):
        self.items.append((level, key, msg))

    @property
    def errors(self) -> int:
        return sum(1 for i in self.items if i[0] == "FEHLER")


def shown(key: str, val: str) -> str:
    if SECRET.search(key):
        return f"gesetzt ({len(val)} Zeichen)" if val else "leer"
    return repr(val) if len(val) <= 60 else repr(val[:57] + "...")


def check(env: dict, example: dict, known: set, paths: bool) -> Report:
    r = Report()

    def has(k: str) -> bool:
        return bool((env.get(k) or "").strip())

    def need(k: str, why: str, level: str = "FEHLER"):
        if not has(k):
            r.add(level, k, f"fehlt oder ist leer -- {why}")
            return False
        return True

    def url(k: str):
        v = env.get(k, "").strip()
        if v and not re.match(r"^https?://[^\s/]+", v):
            r.add("FEHLER", k, f"keine gueltige URL (http://host:port erwartet): {shown(k, v)}")

    def integer(k: str, lo: int = 0, hi: int = 10 ** 9):
        v = env.get(k, "").strip()
        if v:
            try:
                ok = lo <= int(v) <= hi
            except ValueError:
                ok = False
            if not ok:
                r.add("FEHLER", k, f"muss eine ganze Zahl zwischen {lo} und {hi} sein: {shown(k, v)}")

    def boolean(k: str):
        v = env.get(k, "").strip().lower()
        if v and v not in TRUE:
            r.add("FEHLER", k, f"true/false erwartet: {shown(k, v)}")

    # --- Medien-Ordner (ohne die laeuft nichts) --------------------------------------------------------------
    if has("MEDIA_ROOT") and not env["MEDIA_ROOT"].startswith("/"):
        r.add("FEHLER", "MEDIA_ROOT", "muss ein absoluter Pfad sein")
    for k in ("MEDIA_ROOT", "MEDIA_HOST_DIR"):
        if not has(k):
            r.add("WARNUNG", k, "leer -- Standard aus docker-compose.yml (/mnt/NAS) gilt; stimmt das fuer dein NAS?")
    rf = env.get("ROOT_FOLDERS", "").strip()
    if rf:
        bad = [p for p in rf.split(",") if p.strip() and not re.match(r"^[^=,]+=/", p.strip())]
        if bad:
            r.add("FEHLER", "ROOT_FOLDERS", "Format NAME=/pfad,NAME2=/pfad2 -- fehlerhaft: " + ", ".join(bad))
    else:
        r.add("WARNUNG", "ROOT_FOLDERS", "leer -- die Basis-Ordner-Auswahl nutzt dann die eingebauten Standardwerte VIDEOS/PHOTOS")
    for k in ("PHOTO_ROOT", "VIDEO_ROOT"):
        if k in env and not has(k):
            r.add("WARNUNG", k, "leer")

    # --- Quellen fuer GPS (nur wenn benutzt, dann aber mit allem Noetigen) --------------------------------------
    if has("TRACCAR_URL"):
        url("TRACCAR_URL")
        need("TRACCAR_TOKEN", "ohne Token kann Traccar nicht abgefragt werden (sensibel)")
        if not has("TRACCAR_DEVICES"):
            r.add("WARNUNG", "TRACCAR_DEVICES", "leer -- ohne Geraete-IDs ('7:Handy,19:Logger') werden keine Tracks abgefragt")
    elif has("TRACCAR_TOKEN") or has("TRACCAR_DEVICES"):
        r.add("WARNUNG", "TRACCAR_URL", "Token/Geraete gesetzt, aber keine URL")
    for k in ("TRACCAR_DEVICES", "TRACCAR_DEVICES_ADDITIONAL"):
        v = env.get(k, "").strip()
        if v:
            bad = [p for p in v.split(",") if p.strip() and not re.match(r"^\d+(:.*)?$", p.strip())]
            if bad:
                r.add("FEHLER", k, "Format ID[:Name],ID[:Name] -- fehlerhaft: " + ", ".join(bad))
    if has("DAWARICH_SERVER"):
        url("DAWARICH_SERVER")
        if not (has("DAWARICH_API_KEY") or has("DAWARICH_IMPORT_API_KEY")):
            r.add("FEHLER", "DAWARICH_API_KEY", "DAWARICH_SERVER ist gesetzt, aber kein API-Key (sensibel)")
    elif has("DAWARICH_API_KEY") or has("DAWARICH_IMPORT_API_KEY"):
        r.add("WARNUNG", "DAWARICH_SERVER", "API-Key gesetzt, aber keine Server-URL")
    for k in ("PHOTON_SERVER", "NOMINATIM_SERVER", "PELIAS_SERVER", "MARITIME_SERVER", "GEO_SERVICE_URL", "GEO_SERVICE_PUBLIC_URL",
              "GEO_TILE_URL", "GEO_RADAR_STYLE_URL", "OLLAMA_URL"):
        url(k)
    if not any(has(k) for k in ("PHOTON_SERVER", "NOMINATIM_SERVER", "PELIAS_SERVER")) and ("PHOTON_SERVER" in known):
        r.add("WARNUNG", "PHOTON_SERVER", "kein Geocoder (PHOTON/NOMINATIM/PELIAS_SERVER) gesetzt -- ohne einen davon gibt es keine Ortsnamen, "
                                           "ausser der geo-service liefert sie selbst")

    # --- KI ----------------------------------------------------------------------------------------------------
    prov = [p.strip().lower() for p in env.get("AI_PROVIDER", "").split(",") if p.strip()]
    if prov:
        bad = [p for p in prov if p not in ("ollama", "google")]
        if bad:
            r.add("FEHLER", "AI_PROVIDER", "unbekannter Anbieter: " + ", ".join(bad) + " (erlaubt: ollama, google)")
        if "ollama" in prov:
            need("OLLAMA_URL", "AI_PROVIDER enthaelt ollama")
            need("OLLAMA_MODEL", "AI_PROVIDER enthaelt ollama")
        if "google" in prov:
            need("GOOGLE_AI_API_KEY", "AI_PROVIDER enthaelt google (sensibel)")
    else:
        r.add("HINWEIS", "AI_PROVIDER", "leer -- KI-Beschreibungen sind aus")
    if has("GOOGLE_AI_API_KEY") and "google" not in prov:
        r.add("HINWEIS", "GOOGLE_AI_API_KEY", "gesetzt, aber AI_PROVIDER nutzt google nicht")

    # --- eigene Angaben, die nicht mehr im Code stehen (leer = nichts / wird weggelassen) ------------------------
    for k, why in (("NFO_DIRECTOR", "Regisseur in jeder .nfo"), ("NFO_STUDIO", "Studio in jeder .nfo"),
                   ("NFO_ACTORS", "Schauspielerliste der .nfo (Format Name|Rolle|Thumb;Name|Rolle|Thumb)"),
                   ("IPTC_CREDIT", "IPTC-Credit/Urheber in Fotos und Sidecars")):
        if not has(k):
            r.add("WARNUNG", k, f"leer -- {why} wird dann nicht geschrieben")
    actors = env.get("NFO_ACTORS", "").strip()
    if actors:
        bad = [a for a in actors.split(";") if a.strip() and a.count("|") != 2]
        if bad:
            r.add("FEHLER", "NFO_ACTORS", f"jeder Eintrag braucht genau Name|Rolle|Thumb-Pfad -- fehlerhaft: {len(bad)} Eintrag/Eintraege")

    # --- geo-service Zugang ------------------------------------------------------------------------------------
    if "GEO_SERVICE_TOKEN" in known or "GEO_SERVICE_TOKEN" in env:
        if not has("GEO_SERVICE_TOKEN"):
            r.add("WARNUNG", "GEO_SERVICE_TOKEN", "leer -- der geo-service ist ohne Token fuer jeden im Netz offen (sensibel; "
                                                  "besonders wenn er ueber Proxy/Internet erreichbar ist)")
        elif len(env["GEO_SERVICE_TOKEN"]) < 16:
            r.add("WARNUNG", "GEO_SERVICE_TOKEN", "sehr kurz (< 16 Zeichen) -- besser ein langer Zufallswert (openssl rand -hex 24)")

    # --- Formate/Zahlen ---------------------------------------------------------------------------------------
    for k in ("BATCH_SIZE", "EXIFTOOL_POOL_SIZE", "COLLAGE_WIDTH", "COLLAGE_HEIGHT", "COLLAGE_COUNT", "NFO_SHORT_VIDEO_SECS"):
        integer(k)
    for k in ("PUID", "PGID"):
        integer(k, 0, 65535)
    for k in ("RESTORE_TIMESTAMP", "NFO_AI_DESCRIPTION", "NFO_SKIP_LOCATION", "COLLAGE_SYNC_FOLDER_JPG"):
        boolean(k)
    tz = env.get("TZ", "").strip() or env.get("MTIME_TIMEZONE", "").strip()
    if not tz:
        r.add("WARNUNG", "TZ", "TZ und MTIME_TIMEZONE leer -- Container laufen in UTC, Zeitstempel in Logs/Dateien weichen ab (z. B. Europe/Vienna)")

    # --- Beispielwerte, die noch unveraendert aus .env.example stammen -----------------------------------------
    for k, v in env.items():
        if v and (_PH.search(v) or (k in example and example[k] == v and _PH.search(example[k]))):
            if not SECRET.search(k):
                r.add("WARNUNG", k, f"sieht nach einem Beispielwert aus .env.example aus: {shown(k, v)}")

    # --- Pfade wirklich vorhanden (nur auf dem Server sinnvoll) ----------------------------------------------
    if paths:
        for k in ("MEDIA_HOST_DIR", "CONFIG_HOST_DIR", "GEO_DATA_HOST_DIR", "GEONAMES_HOST_DIR", "MEDIA_IMAGE_DIR"):
            v = env.get(k, "").strip().split("#")[0].strip()
            if v and not os.path.isdir(v if os.path.isabs(v) else os.path.join(HERE, v)):
                r.add("WARNUNG", k, f"Ordner existiert nicht: {v}")

    # --- Tippfehler: Namen, die weder .env.example noch die Compose-Datei kennt -------------------------------
    for k in env:
        if k not in known and not k.startswith(("COMPOSE_", "DOCKER_")):
            guess = difflib.get_close_matches(k, sorted(known), n=1, cutoff=0.8)
            r.add("WARNUNG", k, "unbekannter Name" + (f" -- meintest du {guess[0]}?" if guess else " (wird von keinem Dienst gelesen)"))
    return r


def file_hygiene(env_path: str, r: Report):
    try:
        mode = os.stat(env_path).st_mode
        if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            r.add("HINWEIS", os.path.basename(env_path), f"Datei ist fuer andere lesbar ({oct(mode & 0o777)}) -- sie enthaelt Tokens: chmod 600 {env_path}")
    except OSError:
        pass
    try:
        d = os.path.dirname(os.path.abspath(env_path))
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", os.path.abspath(env_path)], cwd=d, capture_output=True, timeout=10)
        if tracked.returncode == 0:
            r.add("FEHLER", os.path.basename(env_path), "wird von Git verfolgt! Sofort aus dem Repo nehmen (git rm --cached) und alle Tokens darin erneuern")
        else:
            ign = subprocess.run(["git", "check-ignore", "-q", os.path.abspath(env_path)], cwd=d, capture_output=True, timeout=10)
            if ign.returncode == 1:
                r.add("WARNUNG", os.path.basename(env_path), "steht nicht in .gitignore -- ein 'git add .' wuerde sie mitnehmen")
    except (OSError, subprocess.SubprocessError):
        pass      # kein Git vorhanden / kein Repo: kein Problem


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prueft die lokale .env auf noetige private/sensible Werte (gibt keine Geheimnisse aus).")
    ap.add_argument("--env", default=os.path.join(HERE, ".env"), help="zu pruefende Datei (Standard: .env neben diesem Skript)")
    ap.add_argument("--example", default=os.path.join(HERE, ".env.example"), help="Vorlage (Standard: .env.example)")
    ap.add_argument("--paths", action="store_true", help="auch pruefen, ob die *_HOST_DIR-Ordner existieren (nur auf dem Server)")
    ap.add_argument("--no-color", action="store_true")
    a = ap.parse_args(argv)

    try:
        env_text = open(a.env, encoding="utf-8-sig").read()
    except OSError as e:
        print(f"Kann {a.env} nicht lesen: {e}\nHinweis: Suite und geo-service lesen dieselbe Haupt-.env -- eine geo-service/.env gibt es nicht.", file=sys.stderr)
        return 2
    try:
        example, known = parse_example(open(a.example, encoding="utf-8-sig").read())
    except OSError:
        example, known = {}, set()
    # geo-service/.env.example: beide Dienste lesen dieselbe Haupt-.env, deren Namen sind also ebenfalls bekannt
    try:
        known |= parse_example(open(os.path.join(os.path.dirname(os.path.abspath(a.example)), "geo-service", ".env.example"),
                                    encoding="utf-8-sig").read())[1]
    except OSError:
        pass
    # Namen, die die Compose-Dateien lesen, gelten ebenfalls als bekannt (${NAME...})
    for cf in ("docker-compose.yml", "geo-service/docker-compose.yml"):
        try:
            txt = open(os.path.join(os.path.dirname(os.path.abspath(a.example)), cf), encoding="utf-8").read()
            known |= set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", txt))
        except OSError:
            pass

    env = parse_env(env_text)
    r = check(env, example, known, a.paths)
    file_hygiene(a.env, r)

    color = sys.stdout.isatty() and not a.no_color and os.name == "posix"
    col = {"FEHLER": "31", "WARNUNG": "33", "HINWEIS": "36"}
    order = {"FEHLER": 0, "WARNUNG": 1, "HINWEIS": 2}
    for level, key, msg in sorted(r.items, key=lambda i: (order[i[0]], i[1])):
        tag = f"\033[{col[level]}m{level:<7}\033[0m" if color else f"{level:<7}"
        print(f"{tag} {key}: {msg}")
    n = {lv: sum(1 for i in r.items if i[0] == lv) for lv in order}
    print(f"\n{a.env}: {len(env)} Werte gelesen -- {n['FEHLER']} Fehler, {n['WARNUNG']} Warnungen, {n['HINWEIS']} Hinweise.")
    if not r.errors:
        print("Alle unbedingt noetigen Werte sind gesetzt." + (" Warnungen bitte ansehen." if n["WARNUNG"] else ""))
    return 1 if r.errors else 0


if __name__ == "__main__":
    sys.exit(main())
