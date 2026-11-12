#!/usr/bin/env python3
"""geoclient.py -- tiny stdlib client for geo-service (runs on the NAS with a bare python3.13).

    from geoclient import GeoClient
    gc = GeoClient()                         # GEO_SERVICE_URL / GEO_SERVICE_TOKEN from the environment
    gc.reverse(47.07, 15.43)["label"]        # 'Graz, Steiermark, Österreich'
    gc.context(47.07, 15.43, epoch)["text_de"]

From bash (e.g. inside Set-GPS-Data-via-traccar-and-dawarich.sh):
    python3 geoclient.py reverse 47.07 15.43 --field city
    python3 geoclient.py reverse 47.07 15.43              # full JSON
    python3 geoclient.py health
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CHUNK_S = 5 * 86400          # the service caps /track-range at 7 days; stay below
SOURCE_NAMES = {"T": "Traccar", "D": "Dawarich", "I": "Dawarich-Import"}     # /track-range sends 1-letter codes


class GeoServiceError(RuntimeError):
    pass


class GeoClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.base = (base_url or os.environ.get("GEO_SERVICE_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.token = token if token is not None else os.environ.get("GEO_SERVICE_TOKEN", "")
        self.timeout = timeout

    # -- transport ------------------------------------------------------------------------
    def _req(self, path: str, params: dict | None = None, body: dict | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-API-Key"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:                                   # noqa: BLE001
                pass
            raise GeoServiceError(f"{path}: HTTP {e.code} {detail}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GeoServiceError(f"{path}: {e}") from None

    # -- API ------------------------------------------------------------------------------
    def health(self) -> dict:
        return self._req("/health")

    def reverse(self, lat: float, lon: float, lang: str | None = None) -> dict:
        return self._req("/reverse", {"lat": lat, "lon": lon, "lang": lang})

    def context(self, lat: float, lon: float, epoch, weather: bool = True) -> dict:
        """`epoch`: epoch seconds, or an ISO 8601 string (zone-less = UTC)."""
        t = epoch if isinstance(epoch, str) else int(epoch)
        return self._req("/context", {"lat": lat, "lon": lon, "time": t, "weather": str(weather).lower()})

    def track_at(self, epoch: int, **kw) -> dict:
        return self._req("/track-at", {"time": int(epoch), **kw})

    def track_range(self, t0: int, t1: int, sources: str = "traccar,dawarich", devices: str = "default",
                    include_import: bool = False) -> list[tuple[int, float, float, str]]:
        """All points in [t0, t1] as (epoch, lat, lon, source) -- fetched in <= 5-day chunks."""
        out: list[tuple[int, float, float, str]] = []
        t = int(t0)
        while t <= t1:
            e = min(int(t1), t + CHUNK_S)
            r = self._req("/track-range", {"from": t, "to": e, "sources": sources, "devices": devices,
                                           "include_import": str(include_import).lower()})
            out.extend((p[0], p[1], p[2], SOURCE_NAMES.get(p[3], p[3])) for p in r["points"])
            t = e + 1
        out.sort()
        return out


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="geo-service command line client")
    ap.add_argument("-url", "--url")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    r = sub.add_parser("reverse"); r.add_argument("lat", type=float); r.add_argument("lon", type=float)
    r.add_argument("-field", "--field", help="print just this field (city, country, label, ...)")
    c = sub.add_parser("context"); c.add_argument("lat", type=float); c.add_argument("lon", type=float)
    c.add_argument("time", help="epoch seconds or ISO 8601")
    a = ap.parse_args(argv)
    gc = GeoClient(a.url)
    try:
        if a.cmd == "health":
            out = gc.health()
        elif a.cmd == "reverse":
            out = gc.reverse(a.lat, a.lon)
            if a.field:
                print(out.get(a.field, "")); return 0 if out.get("found") else 1
        else:
            out = gc.context(a.lat, a.lon, a.time)
    except GeoServiceError as e:
        print(f"geoclient: {e}", file=sys.stderr)          # errors to stderr, never stdout (command substitution!)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
