"""tracks.py -- GPS history from Traccar and Dawarich, cached per UTC day.

A request for [t0, t1] is split into UTC days; each (source, day) chunk is fetched once and
kept in the SQLite cache -- 7 days for finished days, 5 minutes for the last two days (they
may still receive points). A 14-day trip therefore costs 14 upstream fetches the first time
and none afterwards, however many photos ask for a position.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

import requests

from .cache import Cache
from .config import Settings
from .trackmath import Pt

DAY = 86400
SHORT = {"Traccar": "T", "Dawarich": "D", "Dawarich-Import": "I"}


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(s: str) -> Optional[int]:
    s = (s or "").strip()
    try:
        if s.endswith("Z"):
            return int(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def parse_gpx(content: bytes, source: str) -> list[Pt]:
    try:
        root = ET.fromstring(content.decode("utf-8", errors="replace").replace(
            'xmlns="http://www.topografix.com/GPX/1/1"', ""))
    except ET.ParseError:
        return []
    out = []
    for tp in root.iter("trkpt"):
        t_el = tp.find("time")
        try:
            lat, lon = float(tp.get("lat")), float(tp.get("lon"))
        except (TypeError, ValueError):
            continue
        ep = _parse_time(t_el.text) if t_el is not None and t_el.text else None
        if ep is not None:
            out.append(Pt(ep, lat, lon, source))
    return out


class TrackService:
    def __init__(self, s: Settings, cache: Cache):
        self.s, self.cache = s, cache

    # -- what is configured ------------------------------------------------------------
    def available(self) -> dict:
        s = self.s
        return {"traccar": bool(s.traccar_url and s.traccar_token and s.traccar_devices),
                "dawarich": bool(s.dawarich_server and s.dawarich_api_key),
                "dawarich_import": bool(s.dawarich_server and s.dawarich_import_api_key)}

    def device_ids(self, devices: str) -> list[str]:
        """'default' = TRACCAR_DEVICES, 'all' = + TRACCAR_DEVICES_ADDITIONAL, or '7,19' explicitly."""
        s = self.s
        if devices in ("", "default"):
            return [d for d, _ in s.traccar_devices]
        if devices == "all":
            return [d for d, _ in s.traccar_devices + s.traccar_devices_additional]
        return [d.strip() for d in devices.split(",") if d.strip()]

    # -- upstream fetches (one UTC day) --------------------------------------------------
    def _fetch_traccar(self, dev: str, t0: int, t1: int) -> list[Pt]:
        s = self.s
        url = f"{s.traccar_url.rstrip('/')}/api/positions/gpx?deviceId={dev}&from={_iso(t0)}&to={_iso(t1)}"
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {s.traccar_token}"}, timeout=s.track_timeout)
        except requests.RequestException:
            raise
        if r.status_code != 200:
            raise RuntimeError(f"traccar HTTP {r.status_code}")
        return parse_gpx(r.content, "Traccar") if r.content else []

    def _fetch_dawarich(self, key: str, label: str, t0: int, t1: int, max_pages: int = 0) -> list[Pt]:
        s = self.s
        pts: list[Pt] = []
        page, total = 1, 1
        while page <= total and not (max_pages and page > max_pages):
            url = (f"{s.dawarich_server.rstrip('/')}/api/v1/points?api_key={key}"
                   f"&start_at={_iso(t0)}&end_at={_iso(t1)}&order=asc&page={page}")
            r = requests.get(url, timeout=s.track_timeout)
            if r.status_code != 200:
                raise RuntimeError(f"dawarich HTTP {r.status_code}")
            if page == 1:
                try:
                    total = max(1, int(r.headers.get("X-Total-Pages", "1")))
                except ValueError:
                    total = 1
            for item in r.json() or []:
                la, lo, ts = item.get("latitude"), item.get("longitude"), item.get("timestamp")
                if la is None or lo is None or ts is None:
                    continue
                try:
                    pts.append(Pt(int(ts), float(la), float(lo), label))
                except (TypeError, ValueError):
                    continue
            page += 1
        return pts

    # -- cached day chunk ------------------------------------------------------------------
    def _day(self, srckey: str, day0: int, fetch) -> tuple[list[Pt], bool]:
        """(points, upstream_ok). A failed fetch is NOT cached, so the next request retries."""
        ck = f"{srckey}|{day0}"
        hit = self.cache.get("trackday", ck)
        if hit is not None:
            return [Pt(e, la, lo, {"T": "Traccar", "D": "Dawarich", "I": "Dawarich-Import"}.get(c, c)) for e, la, lo, c in hit], True
        try:
            pts = fetch(day0, day0 + DAY - 1)
        except Exception:
            return [], False
        finished = day0 + DAY < time.time() - 2 * DAY
        self.cache.put("trackday", ck, [[p.epoch, p.lat, p.lon, SHORT.get(p.source, p.source)] for p in pts],
                       (self.s.cache_ttl_days * DAY if pts else 7 * DAY) if finished else 300)   # empty finished day: re-check after 7 d (late sync)
        return pts, True

    def probe(self, t0: int, t1: int, sources=("traccar", "dawarich"), devices: str = "default",
              include_import: bool = False) -> tuple[bool, bool]:
        """Cheap 'is there anything at all in [t0, t1]?' -- ONE request per source/device for the whole
        block, nothing cached. Returns (has_points, all_upstreams_answered). Used to skip empty years
        quickly when a huge range (e.g. from 1900) is requested."""
        avail = self.available()
        has, ok = False, True
        jobs = []
        if "traccar" in sources and avail["traccar"]:
            for dev in self.device_ids(devices):
                jobs.append(lambda d=dev: self._fetch_traccar(d, t0, t1))
        if "dawarich" in sources and avail["dawarich"]:
            jobs.append(lambda: self._fetch_dawarich(self.s.dawarich_api_key, "Dawarich", t0, t1, 1))
        if include_import and avail["dawarich_import"]:
            jobs.append(lambda: self._fetch_dawarich(self.s.dawarich_import_api_key, "Dawarich-Import", t0, t1, 1))
        for j in jobs:
            try:
                if any(t0 <= p.epoch <= t1 for p in j()):
                    has = True
            except Exception:
                ok = False
        return has, ok

    def points(self, t0: int, t1: int, sources=("traccar", "dawarich"), devices: str = "default",
               include_import: bool = False) -> tuple[list[Pt], dict]:
        """All points in [t0, t1] from the requested sources, plus {source: ok} so the caller
        can tell 'no points that day' from 'Traccar was unreachable'."""
        avail = self.available()
        status: dict[str, bool] = {}
        out: list[Pt] = []
        day = t0 - t0 % DAY
        while day <= t1:
            if "traccar" in sources and avail["traccar"]:
                ids = self.device_ids(devices)
                for dev in ids:
                    pts, ok = self._day(f"traccar:{dev}", day, lambda a, b, d=dev: self._fetch_traccar(d, a, b))
                    status["traccar"] = status.get("traccar", True) and ok
                    out.extend(pts)
            if "dawarich" in sources and avail["dawarich"]:
                pts, ok = self._day("dawarich", day, lambda a, b: self._fetch_dawarich(self.s.dawarich_api_key, "Dawarich", a, b))
                status["dawarich"] = status.get("dawarich", True) and ok
                out.extend(pts)
            if include_import and avail["dawarich_import"]:
                pts, ok = self._day("dawarich-import", day, lambda a, b: self._fetch_dawarich(self.s.dawarich_import_api_key, "Dawarich-Import", a, b))
                status["dawarich_import"] = status.get("dawarich_import", True) and ok
                out.extend(pts)
            day += DAY
        out = [p for p in out if t0 <= p.epoch <= t1]
        out.sort(key=lambda p: p.epoch)
        return out, status
