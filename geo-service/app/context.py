"""context.py -- everything an AI caption (or a human) wants to know about a moment:
where, local time of day, season, and (opt-in) the weather."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import daycal, solar, textde, timezone as tzmod

try:                                     # optional, ~50 MB; without it the default zone is used
    from timezonefinder import TimezoneFinder
    _TF = TimezoneFinder()
except Exception:                        # pragma: no cover
    _TF = None


def local_zone(lat: Optional[float], lon: Optional[float], default: str) -> ZoneInfo:
    name = default
    if _TF is not None and lat is not None and lon is not None:
        try:
            name = _TF.timezone_at(lat=lat, lng=lon) or default
        except Exception:
            name = default
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def build(place: Optional[dict], weather: Optional[dict], lat: Optional[float], lon: Optional[float],
          epoch: int, default_tz: str) -> dict:
    tz = local_zone(lat, lon, default_tz)
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(tz)
    ctx = {
        "place": place,
        "local_time": dt.isoformat(timespec="seconds"),
        "tz": str(tz),
        "year": dt.year,
        "month_de": textde.month_de(dt.month),
        "season_de": textde.season_de(dt.month),
        "weekday_de": textde.WEEKDAYS_DE[dt.weekday()],
        "daypart_de": textde.daypart_de(dt.hour),
        "weather": weather,
    }
    ctx["text_de"] = text_de(ctx)
    ctx["extra_de"] = extra_de(ctx)
    sky(ctx, place, lat, lon, epoch, dt, tz)
    return ctx


def sky(ctx: dict, place: Optional[dict], lat: Optional[float], lon: Optional[float], epoch: int, dt: datetime, tz) -> None:
    """Daylight band, sunrise/sunset, moon phase and public holiday -- plus ready-made German phrases in
    ctx['parts_de'] that a caption may append ('in der goldenen Stunde vor Sonnenuntergang (19:42)', 'am Feiertag
    Ostermontag', 'bei Vollmond'). Nothing here needs a network."""
    parts: dict = {}
    if lat is not None and lon is not None:
        d = solar.daylight(lat, lon, epoch)
        rise, sets = tzmod.sun_times(lat, lon, dt.date(), tz)
        ctx["daylight"] = {**d, "sunrise": rise[11:] if rise else "", "sunset": sets[11:] if sets else ""}
        ph, side = d["phase"], d["side"]
        if ph == "goldene Stunde":
            t = ctx["daylight"]["sunset" if side == "abends" else "sunrise"]
            parts["daylight"] = ("in der goldenen Stunde vor Sonnenuntergang" if side == "abends" else
                                 "in der goldenen Stunde nach Sonnenaufgang") + (f" ({t})" if t else "")
        elif ph == "blaue Stunde":
            t = ctx["daylight"]["sunset" if side == "abends" else "sunrise"]
            parts["daylight"] = ("in der blauen Stunde nach Sonnenuntergang" if side == "abends" else
                                 "in der blauen Stunde vor Sonnenaufgang") + (f" ({t})" if t else "")
        elif ph == "Dämmerung":
            parts["daylight"] = f"in der Dämmerung ({side})"
    m = solar.moon(epoch)
    ctx["moon"] = m
    if solar.notable_moon_de(m):
        parts["moon"] = solar.notable_moon_de(m)
    h = daycal.holiday(place, dt.date())
    ctx["holiday"] = h or None
    ctx["weekend"] = dt.weekday() >= 5
    if h:
        parts["holiday"] = f"am Feiertag {h['name']}"
    ctx["parts_de"] = parts


def extra_de(ctx: dict) -> str:
    """The part an AI prompt wants when the place is passed separately:
    'nachmittags im Oktober 2026; Regen bei 11 °C'."""
    hour = int(ctx["local_time"][11:13])
    parts = [f"{textde.daypart_adverb_de(hour)} im {ctx['month_de']} {ctx['year']}"]
    w = ctx.get("weather")
    if w:
        t = f" bei {w['temp_c']} °C" if w.get("temp_c") is not None else ""
        parts.append(f"{w['summary_de']}{t}")
    return "; ".join(parts)


def text_de(ctx: dict) -> str:
    """One compact German sentence, e.g.
    'Aufgenommen in Graz (Steiermark, Österreich), nachmittags im Oktober 2026; Regen bei 11 °C.'"""
    parts = []
    place = ctx.get("place") or {}
    label = place.get("label") or ""
    head = "Aufgenommen"
    if label:
        city = place.get("city") or place.get("locality") or ""
        rest = ", ".join(p for p in (place.get("state") or place.get("county"), place.get("country")) if p and p != city)
        head += f" in {city or label}" + (f" ({rest})" if city and rest else "")
    hour = int(ctx["local_time"][11:13])
    head += f", {textde.daypart_adverb_de(hour)} im {ctx['month_de']} {ctx['year']}"
    parts.append(head)
    w = ctx.get("weather")
    if w:
        t = f" bei {w['temp_c']} °C" if w.get("temp_c") is not None else ""
        parts.append(f"{w['summary_de']}{t}")
    return "; ".join(parts) + "."
