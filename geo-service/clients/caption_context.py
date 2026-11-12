#!/usr/bin/env python3
"""caption_context.py -- the facts an AI caption should know that aren't in the picture.

    caption_context.py "/mnt/NAS/photo/Urlaube/2026/2026.08.05-2026.08.22 Sri Lanka & Malediven/2026.08.09-17.31.02.jpg"
    -> Aufgenommen in Kandy (Zentralprovinz, Sri Lanka), nachmittags im August 2026; Regen bei 24 °C. Reise: Sri Lanka & Malediven.

Place + local time of day + season (+ opt-in weather) come from geo-service /context, the trip
name from the folder name ("<start>-<end> <Trip name>"). Without a reachable service you still
get time of day, month and trip name -- a caption is never blocked by an offline service.

As a library:
    from caption_context import build
    ctx = build(path, GeoClient())           # dict: text_de, extra_de (no place), place, weather, trip, ...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mediameta as mm                                    # noqa: E402
import textde                                             # noqa: E402
from geoclient import GeoClient, GeoServiceError          # noqa: E402

_TRIP = re.compile(r"^\d{4}\.\d{2}\.\d{2}(?:-\d{4}\.\d{2}\.\d{2})?\s+(?P<name>\S.*)$")


def trip_name(path: str) -> str:
    """'2026.08.05-2026.08.22 Sri Lanka & Malediven' somewhere above the file -> 'Sri Lanka & Malediven'.
    Folders that are only a date (a day, a month) don't name a trip."""
    d = os.path.dirname(os.path.abspath(path))
    while d and d != os.path.dirname(d):
        m = _TRIP.match(os.path.basename(d))
        if m:
            return m.group("name").strip()
        d = os.path.dirname(d)
    return ""


def extra_de(ctx: dict, with_place: bool = False) -> str:
    """What goes into the AI prompt besides the place the suite already passes:
    'nachmittags im Oktober 2026; Regen bei 11 °C; Wo wars: „Sri Lanka & Malediven“'.
    Uses the service's own wording (ctx['base_extra_de']) when there is one, else builds it locally."""
    parts = []
    if with_place and (ctx.get("place") or {}).get("label"):
        parts.append(ctx["place"]["label"])
    base = ctx.get("base_extra_de")
    if base is None and ctx.get("daypart_hour") is not None:
        base = f"{textde.daypart_adverb_de(ctx['daypart_hour'])} im {ctx['month_de']} {ctx['year']}"
        w = ctx.get("weather")
        if w:
            t = f" bei {w['temp_c']} °C" if w.get("temp_c") is not None else ""
            base += f"; {w['summary_de']}{t}"
    if base:
        parts.append(base)
    if ctx.get("trip"):
        parts.append(f"Wo wars: „{ctx['trip']}“")
    return "; ".join(parts)


def _local_fallback(epoch: int, tz: str) -> dict:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ZoneInfo(tz))
    month = textde.month_de(dt.month)
    return {"local_time": dt.isoformat(timespec="seconds"), "tz": tz, "year": dt.year, "month_de": month,
            "season_de": textde.season_de(dt.month), "daypart_de": textde.daypart_de(dt.hour), "place": None, "weather": None,
            "text_de": f"Aufgenommen, {textde.daypart_adverb_de(dt.hour)} im {month} {dt.year}."}      # same shape the service gives


def build(path: str, gc: Optional[GeoClient] = None, weather: bool = True, default_tz: str = "Europe/Vienna") -> dict:
    items = mm.collect([path], depth=0)
    mm.resolve_times(items, default_tz)
    m = items[0] if items else None
    lat = lon = None
    if m:
        if m.sidecar and mm.real_gps(m.sidecar.lat, m.sidecar.lon):
            lat, lon = m.sidecar.lat, m.sidecar.lon
        elif m.has_exif_gps():
            lat, lon = m.lat, m.lon
    epoch = m.epoch if m else None
    ctx: dict = {"source": "none"}
    if epoch is not None:
        ctx = None
        if gc is not None and lat is not None:
            try:
                ctx = gc.context(lat, lon, epoch, weather)
                ctx["source"] = "service"
                ctx["base_extra_de"] = ctx.get("extra_de")
            except GeoServiceError:
                ctx = None
        if ctx is None:
            ctx = _local_fallback(epoch, default_tz)
            ctx["source"] = "local"
        ctx["daypart_hour"] = int(ctx["local_time"][11:13])
    ctx["trip"] = trip_name(path)
    ctx["lat"], ctx["lon"], ctx["epoch"] = lat, lon, epoch
    ctx["extra_de"] = extra_de(ctx)
    head = ctx.get("text_de") or ""
    ctx["text_de"] = (head + (f" Reise: {ctx['trip']}." if ctx["trip"] else "")).strip() or (
        f"Reise: {ctx['trip']}." if ctx["trip"] else "")
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("file")
    ap.add_argument("-service", "--service")
    ap.add_argument("-token", "--token")
    ap.add_argument("-no-weather", "--no-weather", action="store_true", dest="no_weather")
    ap.add_argument("-offline", "--offline", action="store_true", help="don't call the service at all")
    ap.add_argument("-tz", "--tz", default=os.environ.get("MEDIA_TIMEZONE", "Europe/Vienna"))
    ap.add_argument("-json", "--json", action="store_true")
    ap.add_argument("-extra", "--extra", action="store_true", help="print only the part for the AI prompt (no place)")
    a = ap.parse_args(argv)
    gc = None if a.offline else GeoClient(a.service, a.token)
    ctx = build(a.file, gc, weather=not a.no_weather, default_tz=a.tz)
    if a.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(ctx["extra_de"] if a.extra else ctx["text_de"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
