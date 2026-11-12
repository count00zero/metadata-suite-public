"""
ctxfill.py -- engine of the module "Kontext nachfüllen": finds photos/videos whose .xmp sidecar lacks the context data that
later versions of the suite write (height, time zone, weather, daylight/moon, date/trip, IPTC credits) and fills ONLY what
is missing -- from the geo-service, using the position already stored in the sidecar.

The scan reads nothing but the .xmp files (plus the .nfo's text for videos), never the media files themselves, so it is
cheap even for big folders. Writing reuses the existing writers: fillcore.write_context_fields (XMP-mon:Ctx*),
geocore.write_place_extras (GPSAltitude + time zone), iptcfill.fill_sidecar (Credit/Source/DateCreated twins),
xmpsync.sync_xmp_to_image (images: embed the sidecar incl. altitude + IPTC) and nfocore.patch_nfo_context (videos: .nfo).
"""
from __future__ import annotations

import os
import re
from html import unescape
from typing import Optional

from . import exifpool, fillcore, geocore, geoservice, iptcfill, nfocore, settings

# group key -> (label, Ctx field keys of geoservice.context_info that belong to it)
GROUPS = {
    "hoehe": ("Höhe & Zeitzone", ("elevation", "timezone")),
    "wetter": ("Wetter", ("weather", "temp_c", "wind_kmh", "wind_text", "precip_mm", "cloud_pct", "humidity_pct")),
    "licht": ("Licht & Mond", ("daylight", "sunrise", "sunset", "moon")),
    "tag": ("Datum & Wo wars", ("daypart", "season", "month", "year", "weekday", "local_time", "holiday", "trip", "trip_source",
                              "pois", "sight", "time_basis")),
    "land": ("Land & Währung", ("country", "country_info")),
    "ort": ("Ort & Gewässer", ()),
    "iptc": ("IPTC (Credit/Source/Datum)", ()),
    "nfo": ("NFO-Zeilen (Videos)", ()),
}
# the tag whose absence flags a group as incomplete (trip/pois/country are legitimately absent most of the time)
_MARK = {"hoehe": ("CtxElevation", "CtxTimezone"), "wetter": ("CtxWeather", "CtxTempC"), "licht": ("CtxDaylight",),
         "tag": ("CtxDaypart",)}
# Version of the context data set. Raise it when a NEW kind of data is added (and give its group the new number in GROUP_SINCE):
# sidecars stamped with an older CtxSchema then get exactly the newer groups added, nothing else is touched or recomputed.
CTX_SCHEMA = 3
TRUST_STAMP_FROM = 3          # stamps below this were written before 'conclusive' also required a known capture time: those sidecars are
                              # checked by their marker tags again (schema 2 stamped files WITHOUT time-of-day / weather / light data)
GROUP_SINCE = {"hoehe": 1, "wetter": 1, "licht": 1, "tag": 1, "land": 2}      # group -> CtxSchema that introduced it
CTX_GROUPS = tuple(GROUP_SINCE)
STAMP_TAGS = ("CtxSchema", "AiStale", "NfoStale")                          # bookkeeping, not data (excluded from "what was added")
_ALL_CTX = tuple(sorted((set(fillcore.CTX_TAGS.values()) - set(STAMP_TAGS)) | {"AltitudeSource"}))


def _plain(text: str, name: str) -> str:
    """Value of a standard-namespace XMP property (element or attribute form), '' if absent."""
    m = (re.search(rf"<[A-Za-z0-9]+:{name}(?:\s[^>]*)?>(.*?)</[A-Za-z0-9]+:{name}>", text, re.S)
         or re.search(rf"\b[A-Za-z0-9]+:{name}\s*=\s*\"([^\"]*)\"", text)
         or re.search(rf"\b[A-Za-z0-9]+:{name}\s*=\s*'([^']*)'", text))
    return unescape(m.group(1)).strip() if m else ""


def _photoshop(text: str, name: str) -> str:
    m = (re.search(rf"<photoshop:{name}(?:\s[^>]*)?>(.*?)</photoshop:{name}>", text, re.S)
         or re.search(rf"\bphotoshop:{name}\s*=\s*\"([^\"]*)\"", text))
    return unescape(m.group(1)).strip() if m else ""


def _xmp_alt(xmp: str) -> str:
    try:
        with open(xmp, encoding="utf-8", errors="replace") as fh:
            return _plain(fh.read(), "GPSAltitude")
    except OSError:
        return ""


# --- stale flags: "new context data arrived after the AI text / the .nfo was made" ------------------------------------------
FLAG_TAG = {"ai": "AiStale", "nfo": "NfoStale"}
FLAG_KEY = {"ai": "ai_stale", "nfo": "nfo_stale"}


def read_flags(media_path: str) -> dict:
    """{'schema': int, 'ai': bool, 'nfo': bool} straight from the sidecar text (one cheap read, no exiftool)."""
    try:
        with open(media_path + ".xmp", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {"schema": 0, "ai": False, "nfo": False}
    m = re.search(r"[A-Za-z0-9]+:CtxSchema(?:\s[^>]*)?>\s*(\d+)|[A-Za-z0-9]+:CtxSchema\s*=\s*[\"'](\d+)", text)
    return {"schema": int((m.group(1) or m.group(2)) if m else 0), "ai": ":AiStale" in text, "nfo": ":NfoStale" in text}


def find_flagged(paths: list, kind: str) -> list:
    """The media files among `paths` whose sidecar carries the stale flag 'ai' or 'nfo'."""
    return [p for p in paths if read_flags(p)[kind]]


def set_flag(media_path: str, kind: str) -> bool:
    return fillcore.write_context_fields(media_path + ".xmp", {FLAG_KEY[kind]: CTX_SCHEMA})[0]


def clear_flag(media_path: str, kind: str) -> bool:
    return fillcore.exiftool_write_mon([f"-XMP-mon:{FLAG_TAG[kind]}="], media_path + ".xmp")[0]


def place_missing(text: str) -> bool:
    """True when the sidecar has no usable place: no country, or neither a city nor a locality (earlier versions found nothing
    at sea or in coastal waters; the geo-service now names the sea and the nearest bay / strait / island)."""
    country, city, loc = _photoshop(text, "Country"), _photoshop(text, "City"), _plain(text, "Location")
    return not country or not (city or loc)


def _existing_subject(xmp: str) -> list:
    try:
        res = exifpool.run(["exiftool", "-j", "-XMP-dc:Subject", xmp], capture_output=True, text=True, timeout=60)
        import json
        v = (json.loads(res.stdout or "[]") or [{}])[0].get("Subject", [])
        return [v] if isinstance(v, str) else [str(x) for x in v]
    except Exception:                                    # noqa: BLE001
        return []


def fill_place(xmp: str, lat: float, lon: float, overwrite: bool, res: dict) -> bool:
    """Resolves the position (geo-service chain incl. sea / nearest water feature) and writes the place fields into the
    sidecar; the geo keywords are added without touching the other keywords. Only when the place is missing, or `overwrite`."""
    with open(xmp, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if not overwrite and not place_missing(text):
        return False
    cfg = settings.load_geo_settings()
    geo = geocore.reverse_geocode(lat, lon, photon_server=cfg.photon_server, nominatim_server=cfg.nominatim_server,
                                  pelias_server=cfg.pelias_server, maritime_server=cfg.maritime_server, lang=cfg.lang)
    if geo is None or geo.is_empty:
        res["notes"].append("Ort: kein Ergebnis für diese Position")
        return False
    args = geocore._sidecar_location_args(geo)
    have = set(_existing_subject(xmp))
    for kw in geocore._geo_keyword_args(lat, lon, geo)[1:]:               # [0] is the clear-all; keep the other keywords
        if kw.split("=", 1)[1] not in have:
            args.append(kw)
    ok, msg = geocore._exiftool_write(args, xmp)
    if not ok:
        res["ok"] = False
        res["notes"].append("Ort: " + (msg or "nicht geschrieben"))
        return False
    res["added"].append("Ort")
    return True


def nfo_path_for(video: str) -> str:
    return os.path.join(os.path.dirname(video), os.path.splitext(os.path.basename(video))[0] + ".nfo")


def env_sig(place_version: str = "") -> str:
    """Everything outside the sidecar that inspect() depends on: a cached result is only valid for the same value."""
    return f"{CTX_SCHEMA}|{place_version}|{int(iptcfill.enabled())}|{iptcfill.credit() if iptcfill.enabled() else ''}|{nfocore.plot_context_mode()}"


def inspect(path: str, lat: Optional[float], lon: Optional[float], place_version: str = "") -> dict:
    """What the sidecar of `path` has / lacks. {'has_xmp','has_pos','missing': [group keys], 'present': set(Ctx tags)}.
    Reads the .xmp (and a video's .nfo) as plain text."""
    xmp = path + ".xmp"
    out = {"has_xmp": os.path.isfile(xmp), "has_pos": geocore.has_real_gps(lat, lon), "missing": [], "present": set(), "stale": False}
    if not out["has_xmp"]:
        return out
    try:
        with open(xmp, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        out["has_xmp"] = False
        return out
    mon = nfocore.read_mon_fields(xmp, _ALL_CTX + ("CameraName",) + STAMP_TAGS)
    out["present"] = set(mon) - {"CameraName"} - set(STAMP_TAGS)
    try:
        out["schema"] = int(mon.get("CtxSchema") or 0)
    except ValueError:
        out["schema"] = 0
    out["ai_stale"], out["nfo_stale"] = "AiStale" in mon, "NfoStale" in mon
    if not out["has_pos"]:
        return out
    missing = []
    if out["schema"] >= TRUST_STAMP_FROM:
        # stamped: the stamp says which groups were tried conclusively -> only the groups introduced since are open (no endless
        # re-checking of data that legitimately does not exist, e.g. weather for a day Open-Meteo has no hours for)
        missing = [g for g in CTX_GROUPS if GROUP_SINCE[g] > out["schema"]]
    else:
        # legacy / older stamp: presence of the marker tags, plus every group introduced after the stamp that the markers cannot tell
        # (country / currency)
        if not _plain(text, "GPSAltitude") or any(t not in mon for t in _MARK["hoehe"]):
            missing.append("hoehe")
        for g in ("wetter", "licht", "tag"):
            if any(t not in mon for t in _MARK[g]):
                missing.append(g)
        missing += [g for g in CTX_GROUPS if GROUP_SINCE[g] > max(out["schema"], 1) and g not in missing]      # (1 = what the marker tags cover)
    if place_missing(text):
        missing.append("ort")
    stamp = mon.get("CtxPlaceVersion", "")
    if place_version and stamp and stamp != place_version:           # resolved with older data (new index, new resolver revision)
        out["stale"] = True
        for g in ("ort", "tag"):
            if g not in missing:
                missing.append(g)
    if iptcfill.enabled():
        cr = iptcfill.credit()
        has_cam = bool(mon.get("CameraName") or _plain(text, "Model"))        # without any camera info there is no Source to write
        if (_plain(text, "DateTimeOriginal") and not _photoshop(text, "DateCreated")) or (has_cam and not _photoshop(text, "Source")) or (cr and not _photoshop(text, "Credit")):
            missing.append("iptc")
    if os.path.splitext(path)[1].lower().lstrip(".") in fillcore.VIDEO_EXT:
        nfo = nfo_path_for(path)
        if os.path.isfile(nfo) and nfocore.plot_context_mode() != "off":
            line = nfocore.build_context_plot_line(nfocore.read_mon_fields(xmp))
            try:
                with open(nfo, encoding="utf-8", errors="replace") as fh:
                    ntext = fh.read()
            except OSError:
                ntext = ""
            if line and not nfocore.nfo_has_context(ntext, line):
                missing.append("nfo")
    out["missing"] = missing
    return out


def fill_one(path: str, groups: set, overwrite: bool = False, embed: bool = False, mark_stale: bool = True) -> dict:
    """Fills the chosen groups for one file. Returns {'ok','added': [Ctx tags], 'notes': [..], 'image','nfo','touched': [paths]}."""
    res = {"path": path, "ok": True, "added": [], "notes": [], "image": False, "nfo": False, "touched": []}
    xmp = path + ".xmp"
    if not os.path.isfile(path) or not os.path.isfile(xmp):
        res.update(ok=False, notes=["Datei oder .xmp nicht (mehr) vorhanden"])
        return res
    lat, lon, _marker = geocore.read_xmp_gps(xmp)
    if not geocore.has_real_gps(lat, lon):
        res.update(ok=False, notes=["keine Position im .xmp"])
        return res
    before = set(nfocore.read_mon_fields(xmp, _ALL_CTX))
    had_alt = bool(_xmp_alt(xmp))
    conclusive, new_data = False, False         # conclusive: the geo-service really answered (-> the sidecar may get the CtxSchema stamp)
    is_video = os.path.splitext(path)[1].lower().lstrip(".") in fillcore.VIDEO_EXT

    keys = {k for g in groups if g in GROUPS for k in GROUPS[g][1]}
    if keys:
        _extra, fields = geoservice.context_info(path, lat, lon, force=True, clock=False)
        conclusive = "daypart" in fields and not ("wetter" in groups and "weather" not in fields and "CtxWeather" not in before
                                           and fields.get("weather_status", "") in ("limited", "failed"))
        if not fields:
            res["notes"].append("geo-service lieferte keinen Kontext (nicht erreichbar / keine Aufnahmezeit)")
        elif "daypart" not in fields:
            res["notes"].append("keine sichere Aufnahmezeit (weder Datum mit Zeitzone im Sidecar noch Zeit im Dateinamen) – Tageszeit, Wetter, Licht und Mond nicht möglich; "
                                "Abhilfe: Schritt „Umbenennen“ mitlaufen lassen (legt das Datum mit Zeitzone fest)")
        picked = {k: v for k, v in fields.items()
                  if k in keys and (overwrite or fillcore.CTX_TAGS.get(k) not in before)}
        new_data = bool(picked)
        if picked:
            if mark_stale:
                picked = dict(picked, ai_stale=CTX_SCHEMA, **({"nfo_stale": CTX_SCHEMA} if is_video else {}))     # AI text / .nfo predate this data
            ok, msg = fillcore.write_context_fields(xmp, picked)
            if not ok:
                res.update(ok=False)
                res["notes"].append(msg or "Kontext-Felder nicht geschrieben")
        if "wetter" in groups and "weather" not in fields and "CtxWeather" not in before:
            why = {"off": "WEATHER_PROVIDER am geo-service ist nicht open-meteo",
                   "limited": "Open-Meteo-Limit erreicht (HTTP 429) – später erneut versuchen",
                   "failed": "Abruf bei Open-Meteo fehlgeschlagen (Fehler/nicht erreichbar vom geo-service-Container)",
                   "empty": "Open-Meteo lieferte für diesen Tag/Ort keine Stunden",
                   "no-hour": "die Stunde der Aufnahme fehlt in den Open-Meteo-Daten"}.get(fields.get("weather_status", ""), "")
            res["notes"].append("kein Wetter verfügbar" + (f": {why}" if why else " (Status unbekannt – geo-service ohne weather_status?)"))
    placed = False
    if "ort" in groups:
        placed = fill_place(xmp, lat, lon, overwrite, res)
        if placed and mark_stale and not new_data:
            fillcore.write_context_fields(xmp, dict({"ai_stale": CTX_SCHEMA}, **({"nfo_stale": CTX_SCHEMA} if is_video else {})))
    if "hoehe" in groups:
        geocore.write_place_extras(xmp, lat, lon)          # GPSAltitude + AltitudeSource + time zone, only where missing
    if "iptc" in groups:
        cam = iptcfill.camera_name(path, xmp)
        out = iptcfill.fill_sidecar(xmp, cam)
        if not out[0]:
            res["notes"].append("IPTC: " + (out[1] or "Fehler"))
    pv = geoservice.place_version() if ({"ort", "tag"} & groups) else ""
    if pv and (placed or (overwrite and {"ort", "tag"} & groups)):
        fillcore.write_context_fields(xmp, {"place_version": pv})      # "resolved with this data version"
    if conclusive and set(CTX_GROUPS) <= set(groups):         # every context group was tried -> the sidecar is complete for this schema
        fillcore.write_context_fields(xmp, {"ctx_schema": CTX_SCHEMA})
    after = set(nfocore.read_mon_fields(xmp, _ALL_CTX))
    res["added"] = sorted(after - before) + (["GPSAltitude"] if not had_alt and _xmp_alt(xmp) else [])
    res["touched"].append(xmp)
    if "nfo" in groups and is_video:
        nfo = nfo_path_for(path)
        if os.path.isfile(nfo):
            changed, msg = nfocore.patch_nfo_context(nfo, nfocore.build_context_plot_line(nfocore.read_mon_fields(xmp)))
            res["nfo"] = changed
            if changed:
                res["touched"].append(nfo)
            elif msg not in ("unverändert", "kein Kontext für die NFO"):
                res["notes"].append("NFO: " + msg)
    if "ort" in groups and is_video:
        nfo = nfo_path_for(path)
        if os.path.isfile(nfo):
            changed, msg = nfocore.patch_nfo_location(nfo, nfocore.read_geo_sidecar(xmp))
            res["nfo"] = res["nfo"] or changed
            if changed and nfo not in res["touched"]:
                res["touched"].append(nfo)
            elif msg not in ("unverändert", "keine Ort-Zeile in der .nfo"):
                res["notes"].append("NFO-Ort: " + msg)
    if embed and not is_video:
        from . import xmpsync
        ok, msg = xmpsync.sync_xmp_to_image(path, xmp)
        res["image"] = ok
        if ok:
            res["touched"].append(path)
        else:
            res["ok"] = False
            res["notes"].append("Bild einbetten: " + (msg or "Fehler"))
    return res
