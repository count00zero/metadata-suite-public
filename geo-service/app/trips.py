"""trips.py -- find trips in your GPS history: periods in which the days are spent far from home.

No I/O in here (the service feeds it track points and a reverse-geocoder callback).
  * home      GEO_HOME_LAT/LON, or a 'Zuhause' entry in Meine Orte, or: where the nights (00-06 local) are spent most often
  * away day  the day's median position is more than away_km from home
  * trip      consecutive away days; up to max_gap home days and up to UNKNOWN_BRIDGE days without any track point
              (phone off, flight mode) are bridged; shorter than min_days is no trip
  * name      the countries visited ('Sri Lanka & Malediven'), at home only the region/city that was visited most
"""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .trackmath import haversine_m

UNKNOWN_BRIDGE = 5          # days without any point that still belong to a running trip
NAME_SAMPLES = 12           # reverse lookups per trip at most


def _med(v: list) -> float:
    return statistics.median(v)


def daily(points, tz) -> dict:
    """{local date: {'lat', 'lon', 'n'}} -- the median position per day."""
    by: dict = {}
    for p in points:
        d = datetime.fromtimestamp(p.epoch, tz=timezone.utc).astimezone(tz).date()
        by.setdefault(d, []).append(p)
    return {d: {"lat": _med([p.lat for p in ps]), "lon": _med([p.lon for p in ps]), "n": len(ps)} for d, ps in by.items()}


def home_from_nights(points, tz) -> Optional[tuple]:
    """(lat, lon) of the most frequent night position (cells of ~1 km), None without enough night points."""
    cells: dict = {}
    for p in points:
        if datetime.fromtimestamp(p.epoch, tz=timezone.utc).astimezone(tz).hour < 6:
            cells.setdefault((round(p.lat, 2), round(p.lon, 2)), []).append(p)
    if not cells:
        return None
    pts = max(cells.values(), key=len)
    if len(pts) < 10:
        return None
    return _med([p.lat for p in pts]), _med([p.lon for p in pts])


def detect(day_pos: dict, home: tuple, first: date, last: date, away_km: float, min_days: int, max_gap: int) -> list:
    """[{'start': date, 'end': date, 'away': [dates], 'max_km': float}] in time order."""
    trips, cur = [], None
    home_run = unk_run = 0
    d = first
    while d <= last:
        pos = day_pos.get(d)
        if pos is None:
            unk_run += 1
            if cur and unk_run > UNKNOWN_BRIDGE:
                trips.append(cur)
                cur = None
        else:
            km = haversine_m(pos["lat"], pos["lon"], home[0], home[1]) / 1000
            if km > away_km:
                if cur is None:
                    cur = {"start": d, "end": d, "away": [], "max_km": 0.0}
                cur["away"].append(d)
                cur["end"] = d
                cur["max_km"] = max(cur["max_km"], km)
                home_run = unk_run = 0
            else:
                unk_run = 0
                home_run += 1
                if cur and home_run > max_gap:
                    trips.append(cur)
                    cur = None
        if pos is not None and cur is None:
            home_run = 0
        d += timedelta(days=1)
    if cur:
        trips.append(cur)
    return [t for t in trips if (t["end"] - t["start"]).days + 1 >= min_days]


def _join(names: list) -> str:
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return " & ".join(names)
    if len(names) == 3:
        return f"{names[0]}, {names[1]} & {names[2]}"
    return f"{names[0]}, {names[1]}, {names[2]} u. a."


def name_trip(trip: dict, day_pos: dict, reverse: Callable[[float, float], Optional[dict]], home_cc: str) -> dict:
    """Adds name / countries / places to a detected trip by reverse-geocoding a sample of its away days."""
    days = trip["away"]
    step = max(1, len(days) // NAME_SAMPLES)
    countries, cc_seen, places = [], set(), Counter()
    states: Counter = Counter()
    abroad = False
    for d in days[::step][:NAME_SAMPLES]:
        pos = day_pos[d]
        pl = reverse(pos["lat"], pos["lon"]) or {}
        c, a3 = pl.get("country") or "", (pl.get("country_a") or "").upper()
        if c and c not in countries:
            countries.append(c)
        if a3 and a3 not in cc_seen:
            cc_seen.add(a3)
        from .daycal import country_code
        cc = country_code(pl)
        if cc and home_cc and cc != home_cc:
            abroad = True
        city = pl.get("city") or pl.get("locality") or ""
        if city:
            places[city] += 1
        if pl.get("state"):
            states[pl["state"]] += 1
    if abroad:
        foreign = [c for c in countries]               # every country seen, in order: 'Italien & Slowenien'
        name = _join(foreign)
    elif states and len(states) == 1 or (states and states.most_common(1)[0][1] >= max(2, len(days) // 2)):
        name = states.most_common(1)[0][0]
    else:
        name = places.most_common(1)[0][0] if places else ""
    return {"name": name, "countries": countries, "places": [p for p, _ in places.most_common(5)], "abroad": abroad}


def place_title(place: Optional[dict], lat: float, lon: float, home: Optional[tuple], away_km: float,
                suffix: str = " und Umgebung") -> Optional[dict]:
    """Trip title from the position alone ('Triest und Umgebung') for pictures that sit in no dated trip folder and on no
    recorded track. Only when home is known and the spot is further away than `away_km` (0 = off)."""
    if not place or not home or away_km <= 0:
        return None
    if haversine_m(lat, lon, home[0], home[1]) / 1000 < away_km:
        return None
    name = place.get("city") or place.get("locality") or place.get("state") or place.get("country") or ""
    if not name:
        return None
    suf = suffix if (place.get("city") or place.get("locality")) else ""
    return {"name": f"{name}{suf}", "folder": "", "source": "place", "day": 1, "days": 1}


def summary(trip: dict, named: dict) -> dict:
    s, e = trip["start"], trip["end"]
    folder = f"{s:%Y.%m.%d}-{e:%Y.%m.%d}" if s != e else f"{s:%Y.%m.%d}"
    return {"from": s.isoformat(), "to": e.isoformat(), "days": (e - s).days + 1, "away_days": len(trip["away"]),
            "max_km": round(trip["max_km"]), "name": named["name"], "countries": named["countries"],
            "places": named["places"], "abroad": named["abroad"],
            "folder": f"{folder} {named['name']}".strip()}
