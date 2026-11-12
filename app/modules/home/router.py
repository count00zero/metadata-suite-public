"""
router.py -- the start page ("Dashboard") at "/": one tile per module with a one-line purpose, the typical workflow order,
and a live status strip (geo-service, weather archive, tracks, 'Meine Orte') that is loaded AFTER the page appears, so a slow
or absent geo-service never delays the dashboard.

  GET /               the dashboard
  GET /home/status    HTMX fragment: status strip
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...core import geoservice, infocore, settings

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# (href, icon, title, text, step) -- step = position in the usual workflow ("" = tool on the side)
TILES = [
    ("/timestamp/", "🕒", "Timestamp", "Dateien nach Aufnahmezeit benennen, Zeitzone und Uhr-Abweichung korrigieren, Presets speichern.", "1"),
    ("/gps/", "📍", "GPS Sidecar", "Position aus Track (Traccar/Dawarich), Ort und Kamera-Name in die .xmp schreiben; optional ins Foto.", "2"),
    ("/gpsfill/", "🧭", "GPS füllen", "Dateien ohne GPS aus dem Track füllen (interpoliert, mit Konfidenz), sonst aus einer Nachbardatei desselben Tages; Wetter-Archiv aufbauen.", "3"),
    ("/geooverrides/", "🗺️", "Geo-Overrides", "Eigene Orte, Adressen und Höhen auf der Karte pflegen – gelten für alle Abfragen.", ""),
    ("/ctxfill/", "🧩", "Kontext nachfüllen", "Höhe, Zeitzone, Wetter, Licht, Reise und IPTC in die .xmp (und NFO-Zeilen) schreiben – Schritt nach GPS Sidecar, auch für alte Dateien.", "4"),
    ("/ai/", "✍️", "AI-Beschreibung", "Bildbeschreibungen mit KI erzeugen (mit Ort, Wetter und Reise als Kontext) und in .xmp/Foto schreiben.", "5"),
    ("/nfo/", "🎬", "NFO Maker", "Kodi-NFO-Dateien für Videos mit Kamera, Ort, Beschreibung und Wetter erzeugen.", "6"),
    ("/collage/", "🖼️", "Collage", "Kontaktbögen/Collagen aus Videos als Vorschaubilder erzeugen.", "7"),
    ("/mtime/", "📅", "Datei-Zeitstempel", "Änderungsdatum aus dem Dateinamen bzw. der Aufnahmezeit wiederherstellen.", "8"),
    ("/shortvideos/", "🧹", "Kurzvideos löschen", "Sehr kurze Videos finden und aufräumen.", ""),
    ("/year/", "📅", "Jahreslauf", "Ein ganzes Jahr automatisch verarbeiten: GPS, AI, NFO, Kontext, Zeitstempel, Collage – wie das Jahres-Skript.", ""),
    ("/endpoints/", "🔗", "Endpunkte", "Alle geo-service-Endpunkte mit klickbaren Beispielen aus deinen Orten und curl-Schnelltest.", ""),
    ("/info/", "ℹ️", "Info", "Versionen, Umgebung, Pfade, Erreichbarkeit der Dienste, Bericht für Fehlermeldungen.", ""),
]


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    b = infocore.build_info()
    return templates.TemplateResponse(request, "index.html", {
        "tiles": TILES, "build": b, "service_enabled": geoservice.enabled(),
        "roots": [rf.to_dict() for rf in settings.load_root_folders()],
    })


def _fmt_s(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    d, rem = divmod(n, 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d} d " if d else "") + f"{h} h {rem // 60} min"


@router.get("/home/status", response_class=HTMLResponse)
def status(request: Request):
    ctx: dict = {"enabled": geoservice.enabled(), "url": geoservice.url(), "ok": False, "error": ""}
    if ctx["enabled"]:
        try:
            h = geoservice.health()
            ctx.update(ok=True, version=h.get("version", ""), uptime=_fmt_s(h.get("uptime_s")),
                       weather=h.get("weather", "none"), tracks=h.get("tracks") or {},
                       geocoders=[(n, v) for n, v in (h.get("geocoders") or {}).items() if v.get("configured")])
        except geoservice.ServiceError as e:
            ctx["error"] = str(e)
        if ctx["ok"]:
            try:
                a = (geoservice.weather_stats() or {}).get("archive") or {}
                ctx["archive"] = a
            except geoservice.ServiceError:
                ctx["archive"] = None
            try:
                ctx["places"] = len(geoservice.places())
            except Exception:           # noqa: BLE001
                ctx["places"] = None
            try:
                ctx["overrides"] = len(geoservice.overrides_list())
            except Exception:           # noqa: BLE001
                ctx["overrides"] = None
    return templates.TemplateResponse(request, "_status.html", ctx)
