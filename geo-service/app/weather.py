"""weather.py -- historical weather for a place and moment (opt-in, WEATHER_PROVIDER=open-meteo).

This is the only part of the service that talks to a third party. Coordinates are rounded to
0.1 degrees (~11 km) before they leave the house, and one request returns a whole UTC day of
hourly values, which is stored -- finished days permanently (see backfill.py: the 'shadow database'),
so a 14-day trip costs a handful of requests in total and each of them only once.
With WEATHER_PROVIDER=none (the default) nothing is ever sent anywhere.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .cache import Cache
from .config import Settings

# WMO weather interpretation codes -> (German text, kind)
WMO = {
    0: ("klarer Himmel", "clear"), 1: ("überwiegend klar", "clear"), 2: ("teils bewölkt", "cloudy"),
    3: ("bedeckt", "cloudy"), 45: ("Nebel", "fog"), 48: ("Raureifnebel", "fog"),
    51: ("leichter Nieselregen", "rain"), 53: ("Nieselregen", "rain"), 55: ("starker Nieselregen", "rain"),
    56: ("gefrierender Nieselregen", "rain"), 57: ("gefrierender Nieselregen", "rain"),
    61: ("leichter Regen", "rain"), 63: ("Regen", "rain"), 65: ("starker Regen", "rain"),
    66: ("gefrierender Regen", "rain"), 67: ("gefrierender Regen", "rain"),
    71: ("leichter Schneefall", "snow"), 73: ("Schneefall", "snow"), 75: ("starker Schneefall", "snow"),
    77: ("Schneegriesel", "snow"), 80: ("leichte Regenschauer", "rain"), 81: ("Regenschauer", "rain"),
    82: ("heftige Regenschauer", "rain"), 85: ("Schneeschauer", "snow"), 86: ("starke Schneeschauer", "snow"),
    95: ("Gewitter", "thunder"), 96: ("Gewitter mit Hagel", "thunder"), 99: ("schweres Gewitter mit Hagel", "thunder"),
}
HOURLY = "temperature_2m,precipitation,weather_code,cloud_cover,wind_speed_10m,relative_humidity_2m"
ROW_VERSION = 2          # rows stored before wind/humidity were requested are fetched again once, when they are used


def wind_de(kmh: float) -> str:
    return ("windstill" if kmh < 6 else "leichter Wind" if kmh < 20 else "mäßiger Wind" if kmh < 40
            else "starker Wind" if kmh < 62 else "Sturm" if kmh < 89 else "schwerer Sturm")


class WeatherService:
    def __init__(self, s: Settings, cache: Cache):
        self.s, self.cache = s, cache

    @property
    def enabled(self) -> bool:
        return self.s.weather_provider == "open-meteo"

    # Everything requested is archived, up to and including today (age 0); only days in the future (age < 0) are
    # not. Values of the last days are provisional (forecast model / ERA5T) and may differ slightly from the final
    # numbers later -- accepted on purpose: the archive keeps what it got first.
    ARCHIVE_MIN_AGE_DAYS = -1
    FORECAST_API_MAX_AGE_DAYS = 7      # days this young come from the forecast API, older ones from the archive API

    @staticmethod
    def cell(lat: float, lon: float) -> tuple:
        return round(lat, 1), round(lon, 1)

    @staticmethod
    def key(la: float, lo: float, day: str) -> str:
        return f"{la:.1f},{lo:.1f}|{day}"

    @staticmethod
    def age_days(day: str) -> int:
        return (datetime.now(timezone.utc).date() - datetime.strptime(day, "%Y-%m-%d").date()).days

    def archived(self, la: float, lo: float, day: str) -> bool:
        return self.cache.has("weather", self.key(la, lo, day))

    @staticmethod
    def row_ok(hit) -> bool:
        """A stored day that is complete: hours present, at least one weather code, current row version."""
        return bool(isinstance(hit, dict) and hit.get("time") and any(c is not None for c in (hit.get("weather_code") or []))
                    and hit.get("_v") == ROW_VERSION)

    def needs_fetch(self, la: float, lo: float, day: str, refresh: str = "none") -> bool:
        """refresh: none = only days not stored yet | bad = also stored days that are empty/incomplete/old | all = every day again."""
        if refresh == "all":
            return True
        if refresh == "bad":
            return not self.row_ok(self.cache.get("weather", self.key(la, lo, day)))
        return not self.archived(la, lo, day)

    def _day_status(self, lat: float, lon: float, day: str, upgrade: bool = False, force: bool = False) -> tuple:
        """(hourly or None, status) -- status: cached | fetched | failed | limited (HTTP 429) | empty.
        Finished days (older than ARCHIVE_MIN_AGE_DAYS) are stored permanently: this table is the
        'shadow database'; the past does not change, so those rows never expire."""
        la, lo = self.cell(lat, lon)
        key = self.key(la, lo, day)
        hit = self.cache.get("weather", key)
        if hit is not None and not force and not (upgrade and hit and hit.get("_v") != ROW_VERSION):
            return (hit or None), ("cached" if hit else "empty")
        old = hit or None                   # an old row stays usable if the refresh fails
        age_days = self.age_days(day)
        base = self.s.weather_archive_url if age_days > self.FORECAST_API_MAX_AGE_DAYS else self.s.weather_forecast_url
        url = (f"{base}?latitude={la}&longitude={lo}&start_date={day}&end_date={day}"
               f"&hourly={HOURLY}&timezone=GMT")
        try:
            r = requests.get(url, timeout=self.s.http_timeout * 2)
            if r.status_code == 429:
                return old, ("cached" if old else "limited")
            if r.status_code != 200:
                return old, ("cached" if old else "failed")
            hourly = r.json().get("hourly") or {}
        except (requests.RequestException, ValueError):
            return old, ("cached" if old else "failed")
        if not hourly.get("time") or not any(c is not None for c in (hourly.get("weather_code") or [])):
            # no hours at all, or only nulls (e.g. a day the provider has not computed yet): never keep that for good --
            # retried after an hour instead of blocking the day for ever
            self.cache.put("weather", key, {}, 3600)
            return None, "empty"
        hourly["_v"] = ROW_VERSION
        self.cache.put("weather", key, hourly, 0 if age_days > self.ARCHIVE_MIN_AGE_DAYS else 3600)
        return hourly, "fetched"

    def _day(self, lat: float, lon: float, day: str) -> Optional[dict]:
        """Hourly arrays for one UTC date ('YYYY-MM-DD'), from the archive or upstream. None on any problem."""
        return self._day_status(lat, lon, day, upgrade=True)[0]

    def fetch_for_archive(self, la: float, lo: float, day: str, refresh: str = "none") -> str:
        """Backfill helper: make sure (cell, day) is archived. Returns cached | fetched | failed | limited | empty | recent.
        refresh bad/all fetches stored days again too (a failed refresh keeps the old row)."""
        if self.age_days(day) <= self.ARCHIVE_MIN_AGE_DAYS:
            return "recent"
        if refresh in ("bad", "all"):
            return self._day_status(la, lo, day, force=True)[1] if self.needs_fetch(la, lo, day, refresh) else "cached"
        return self._day_status(la, lo, day)[1]

    def at(self, lat: float, lon: float, epoch: int) -> Optional[dict]:
        return self.at_status(lat, lon, epoch)[0]

    def at_status(self, lat: float, lon: float, epoch: int) -> tuple:
        """(weather dict | None, status). status: ok | off (WEATHER_PROVIDER=none) | limited (HTTP 429) | failed (HTTP error /
        unreachable) | empty (Open-Meteo returned no hours for that day/cell) | no-hour (the hour or its weather code is missing)."""
        if not self.enabled:
            return None, "off"
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        hourly, status = self._day_status(lat, lon, dt.strftime("%Y-%m-%d"), upgrade=True)
        if not hourly:
            return None, (status if status in ("limited", "failed", "empty") else "empty")
        try:
            i = hourly["time"].index(dt.strftime("%Y-%m-%dT%H:00"))
            code = hourly["weather_code"][i]
            temp = hourly["temperature_2m"][i]
            rain = hourly["precipitation"][i]
            cloud = hourly["cloud_cover"][i]
        except (ValueError, KeyError, IndexError):
            return None, "no-hour"

        def opt(key):
            try:
                v = hourly[key][i]
                return None if v is None else float(v)
            except (KeyError, IndexError, TypeError, ValueError):
                return None
        wind, hum = opt("wind_speed_10m"), opt("relative_humidity_2m")
        text, kind = WMO.get(int(code), ("", "")) if code is not None else ("", "")
        if not text:
            return None, "no-hour"
        return {"summary_de": text, "kind": kind, "code": int(code),
                "temp_c": None if temp is None else round(float(temp)),
                "precip_mm": None if rain is None else round(float(rain), 1),
                "cloud_pct": None if cloud is None else int(cloud),
                "wind_kmh": None if wind is None else round(wind), "wind_de": None if wind is None else wind_de(wind),
                "humidity_pct": None if hum is None else round(hum), "provider": "open-meteo"}, "ok"
