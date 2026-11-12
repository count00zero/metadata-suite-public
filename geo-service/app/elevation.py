"""elevation.py -- height above sea level for a coordinate.

Order for every lookup:  override  ->  cache  ->  local SRTM3 tiles (GEO_SRTM_DIR/N47E016.hgt)  ->  Open-Meteo
elevation API (only if ELEVATION_PROVIDER lists it)  ->  none.  Whatever was found is cached for good, so a point
is computed / asked upstream once.  Rows are keyed on a ~11 m grid (4 decimals) -- far finer than the 90 m source.

Tiles: 1 degree x 1 degree, big-endian int16, row 0 = north edge, -32768 = void.  1201x1201 (SRTM3, 90 m) and
3601x3601 (SRTM1, 30 m) are recognised by file size; plain .hgt and .hgt.gz both work.
"""
from __future__ import annotations

import gzip
import math
import mmap
import os
import struct
import threading
from collections import OrderedDict
from typing import Optional

from .backends import _get_json
from .cache import Cache
from .config import Settings
from .overrides import Overrides

VOID = -32768


def tile_name(lat: float, lon: float) -> str:
    la, lo = math.floor(lat), math.floor(lon)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


class Tile:
    def __init__(self, buf, n: int, closer=None):
        self.buf, self.n, self._closer = buf, n, closer

    @classmethod
    def open(cls, path: str) -> Optional["Tile"]:
        try:
            if path.endswith(".gz"):
                with gzip.open(path, "rb") as f:
                    data = f.read()
                size, buf, closer = len(data), data, None
            else:
                f = open(path, "rb")
                size = os.fstat(f.fileno()).st_size
                buf = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                closer = (buf, f)
        except (OSError, ValueError):
            return None
        n = int(math.isqrt(size // 2))
        if n * n * 2 != size or n not in (1201, 3601):
            if closer:
                closer[0].close(), closer[1].close()
            return None
        return cls(buf, n, closer)

    def _v(self, row: int, col: int) -> int:
        return struct.unpack_from(">h", self.buf, (row * self.n + col) * 2)[0]

    def at(self, lat: float, lon: float) -> Optional[float]:
        """Bilinear value in metres; voids among the four corners are left out of the average."""
        fy = (math.floor(lat) + 1 - lat) * (self.n - 1)
        fx = (lon - math.floor(lon)) * (self.n - 1)
        r0, c0 = min(int(fy), self.n - 2), min(int(fx), self.n - 2)
        dy, dx = fy - r0, fx - c0
        corners = [(self._v(r0, c0), (1 - dy) * (1 - dx)), (self._v(r0, c0 + 1), (1 - dy) * dx),
                   (self._v(r0 + 1, c0), dy * (1 - dx)), (self._v(r0 + 1, c0 + 1), dy * dx)]
        good = [(v, w) for v, w in corners if v != VOID]
        if not good:
            return None
        tw = sum(w for _, w in good)
        return sum(v * w for v, w in good) / tw if tw > 0 else float(good[0][0])

    def close(self) -> None:
        if self._closer:
            try:
                self._closer[0].close()
                self._closer[1].close()
            except Exception:       # noqa: BLE001
                pass


class Elevation:
    MAX_OPEN_TILES = 12

    def __init__(self, s: Settings, cache: Cache, overrides: Overrides):
        self.s, self.cache, self.overrides = s, cache, overrides
        self._tiles: "OrderedDict[str, Optional[Tile]]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def providers(self) -> list:
        return [p for p in self.s.elevation_providers if p in ("local", "open-meteo")]

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    # ------------------------------------------------------------ tiles
    def _tile(self, lat: float, lon: float) -> Optional[Tile]:
        name = tile_name(lat, lon)
        with self._lock:
            if name in self._tiles:
                self._tiles.move_to_end(name)
                t = self._tiles[name]
                if t is not None or not self._missing_may_exist(name):
                    return t
            t = None
            for ext in (".hgt", ".hgt.gz"):
                p = os.path.join(self.s.srtm_dir, name + ext)
                if os.path.isfile(p):
                    t = Tile.open(p)
                    if t:
                        break
            self._tiles[name] = t
            while len(self._tiles) > self.MAX_OPEN_TILES:
                _, old = self._tiles.popitem(last=False)
                if old:
                    old.close()
            return t

    def _missing_may_exist(self, name: str) -> bool:
        """A tile that was missing may have been downloaded since (preload / fetch script)."""
        return any(os.path.isfile(os.path.join(self.s.srtm_dir, name + e)) for e in (".hgt", ".hgt.gz"))

    def local_tiles(self) -> list:
        try:
            return sorted(f.split(".")[0] for f in os.listdir(self.s.srtm_dir) if f.endswith((".hgt", ".hgt.gz")))
        except OSError:
            return []

    def _from_tile(self, lat: float, lon: float) -> Optional[float]:
        t = self._tile(lat, lon)
        return t.at(lat, lon) if t else None

    def _from_open_meteo(self, lat: float, lon: float) -> Optional[float]:
        data, _up = _get_json(f"{self.s.elevation_url}?latitude={lat:.3f}&longitude={lon:.3f}", self.s.http_timeout)
        try:
            v = (data or {}).get("elevation")
            v = v[0] if isinstance(v, list) else v
            return None if v is None else float(v)
        except (TypeError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------ lookup
    def lookup(self, lat: float, lon: float, use_override: bool = True) -> dict:
        """{"elevation": metres | None, "source": override|cache|srtm|open-meteo|none, "cached": bool}"""
        ov = self.overrides.find("elevation", lat, lon) if use_override else None
        if ov:
            return {"elevation": float(ov[1]), "source": "override", "cached": False, "override": ov[0]}
        key = f"{lat:.4f}|{lon:.4f}"
        hit = self.cache.get("elevation", key)
        if hit is not None:
            return {"elevation": hit.get("v"), "source": hit.get("src", "cache"), "cached": True}
        value, src = None, "none"
        for p in self.providers:
            if p == "local":
                value = self._from_tile(lat, lon)
                src = "srtm"
            else:
                value = self._from_open_meteo(lat, lon)
                src = "open-meteo"
            if value is not None:
                break
        if value is None:
            src = "none"
        value = None if value is None else round(value, 1)
        self.cache.put("elevation", key, {"v": value, "src": src}, 0 if value is not None else self.s.cache_neg_ttl)
        return {"elevation": value, "source": src, "cached": False}
