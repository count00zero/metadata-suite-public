"""timezone.py -- GeoNames-style timezone answer for a coordinate: zone id, offsets, local time, sunrise/sunset.

Zone from timezonefinder (offline polygon data), offsets from the tz database, country from the tz database's zone.tab,
sunrise/sunset from the standard sunrise equation (no network, no cache needed: it is a pure calculation).
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import context as ctxmod

_ZONE_COUNTRY: Optional[dict] = None


def _zone_country() -> dict:
    """{'Asia/Colombo': 'LK', ...} from zone.tab (system copy, else the tzdata package's)."""
    global _ZONE_COUNTRY
    if _ZONE_COUNTRY is not None:
        return _ZONE_COUNTRY
    text = ""
    p = "/usr/share/zoneinfo/zone.tab"
    try:
        if os.path.isfile(p):
            text = open(p, encoding="utf-8").read()
        else:
            import importlib.resources as r
            text = r.files("tzdata.zoneinfo").joinpath("zone.tab").read_text(encoding="utf-8")
    except Exception:           # noqa: BLE001
        text = ""
    out = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and not line.startswith("#"):
            out[parts[2]] = parts[0]
    _ZONE_COUNTRY = out
    return out


def zone_name(lat: float, lng: float) -> Optional[str]:
    """IANA zone at the point; None if the polygon data is not installed or finds nothing."""
    tf = getattr(ctxmod, "_TF", None)
    if tf is None:
        return None
    try:
        return tf.timezone_at(lat=lat, lng=lng)
    except Exception:           # noqa: BLE001
        return None


def _hours(off: Optional[timedelta]) -> float:
    return round(off.total_seconds() / 3600, 2) if off is not None else 0.0


def sun_times(lat: float, lng: float, d: date, tz: ZoneInfo) -> tuple:
    """(sunrise, sunset) as local 'YYYY-MM-DD HH:MM' strings; None where the sun does not rise / set that day."""
    n = d.timetuple().tm_yday
    lng_hour = lng / 15.0
    rad, deg = math.radians, math.degrees

    def one(rising: bool) -> Optional[str]:
        t = n + ((6 if rising else 18) - lng_hour) / 24.0
        m = 0.9856 * t - 3.289
        l_ = (m + 1.916 * math.sin(rad(m)) + 0.020 * math.sin(rad(2 * m)) + 282.634) % 360
        ra = deg(math.atan(0.91764 * math.tan(rad(l_)))) % 360
        ra += math.floor(l_ / 90) * 90 - math.floor(ra / 90) * 90
        ra /= 15.0
        sin_dec = 0.39782 * math.sin(rad(l_))
        cos_dec = math.cos(math.asin(sin_dec))
        cos_h = (math.cos(rad(90.833)) - sin_dec * math.sin(rad(lat))) / (cos_dec * math.cos(rad(lat)))
        if cos_h > 1 or cos_h < -1:
            return None
        h = (360 - deg(math.acos(cos_h)) if rising else deg(math.acos(cos_h))) / 15.0
        ut = (h + ra - 0.06571 * t - 6.622 - lng_hour) % 24
        base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(hours=ut)
        loc = base.astimezone(tz)
        if loc.date() < d:
            loc = (base + timedelta(days=1)).astimezone(tz)
        elif loc.date() > d:
            loc = (base - timedelta(days=1)).astimezone(tz)
        return loc.strftime("%Y-%m-%d %H:%M")
    return one(True), one(False)


def info(lat: float, lng: float, on: Optional[date] = None, now: Optional[datetime] = None,
         countries=None, default_tz: str = "") -> Optional[dict]:
    name = zone_name(lat, lng) or default_tz
    if not name:
        return None
    try:
        tz = ZoneInfo(name)
    except Exception:           # noqa: BLE001
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    d = on or now.date()
    jan = datetime(d.year, 1, 1, 12, tzinfo=tz).utcoffset()
    jul = datetime(d.year, 7, 1, 12, tzinfo=tz).utcoffset()
    sunrise, sunset = sun_times(lat, lng, d, tz)
    cc = _zone_country().get(name, "")
    cn = ((countries.by_code(cc) or {}).get("countryName", "") if countries and cc else "")
    return {"sunrise": sunrise, "lat": lat, "lng": lng, "countryCode": cc, "gmtOffset": _hours(jan),
            "rawOffset": min(_hours(jan), _hours(jul)), "sunset": sunset, "timezoneId": name, "dstOffset": _hours(jul),
            "countryName": cn, "time": now.strftime("%Y-%m-%d %H:%M")}
