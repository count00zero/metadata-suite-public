"""srtmfetch.py -- download 1x1 degree elevation tiles and store them as SRTM3 .hgt (1201x1201, ~2.9 MB each).

Source: the public AWS "Terrain Tiles" (skadi) set -- SRTM-based 1 arc-second tiles, N47/N47E016.hgt.gz -- which are
thinned to the 3 arc-second SRTM3 grid (every 3rd sample) so one tile costs 2.9 MB instead of 26 MB.
Standard library only; also usable as a script:  python -m app.srtmfetch --austria --out /data/srtm
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
import urllib.error
import urllib.request
from array import array
from typing import Iterable

def _box(min_la: float, min_lo: float, max_la: float, max_lo: float) -> list:
    """All 1x1 degree tiles touching the box (decimal degrees)."""
    import math
    return [(la, lo) for la in range(math.floor(min_la), math.floor(max_la) + 1)
            for lo in range(math.floor(min_lo), math.floor(max_lo) + 1)]


# GEO_SRTM_PRELOAD=austria,germany,... -- bounding boxes (minLat, minLon, maxLat, maxLon) incl. a small margin.
# Tiles over open sea simply don't exist at the source and are skipped. ~3 MB per land tile.
_BOXES = {
    "austria": (46, 9, 48.99, 17.99),          # 27 tiles ~ 78 MB
    "germany": (47, 5, 55.5, 15.5),
    "italy": (35.4, 6.5, 47.2, 18.9),
    "croatia": (42.2, 13.3, 46.7, 19.6),
    "slovenia": (45.3, 13.3, 47, 16.7),
    "switzerland": (45.7, 5.9, 47.9, 10.6),
    "czechia": (48.5, 12, 51.1, 18.9),
    "slovakia": (47.7, 16.8, 49.7, 22.6),
    "hungary": (45.7, 16, 48.6, 22.9),
    "poland": (49, 14, 54.9, 24.2),
    "france": (41.3, -5.2, 51.2, 9.6),
    "spain": (35.9, -9.4, 43.9, 3.4),
    "portugal": (36.9, -9.6, 42.2, -6.1),
    "greece": (34.7, 19.3, 41.8, 28.3),
    "bosnia": (42.5, 15.7, 45.3, 19.7),
    "serbia": (42.2, 18.8, 46.2, 23),
    "romania": (43.5, 20.2, 48.3, 29.8),
    "bulgaria": (41.2, 22.3, 44.3, 28.7),
    "netherlands": (50.7, 3.3, 53.6, 7.3),
    "belgium": (49.4, 2.5, 51.6, 6.5),
    "uk": (49.9, -8.7, 60.9, 1.8),
    "ireland": (51.3, -10.7, 55.5, -5.4),
    "denmark": (54.5, 8, 57.8, 15.3),
    "sweden": (55.3, 10.9, 69.1, 24.2),
    "norway": (57.9, 4.4, 71.2, 31.2),
    "finland": (59.7, 20.5, 70.1, 31.6),
    "iceland": (63.2, -24.6, 66.6, -13.4),
    "turkey": (35.8, 25.6, 42.2, 44.9),
    # Arab world: "arabia" = whole peninsula + Levant + Iraq (~700 tiles, ~2 GB) and already contains the single countries below
    "arabia": (12, 34, 37.5, 60.5),
    "uae": (22.4, 51, 26.2, 56.5),
    "oman": (16.5, 51.8, 26.5, 59.9),
    "saudi_arabia": (16.2, 34.4, 32.2, 55.7),
    "qatar": (24.4, 50.7, 26.2, 51.7),
    "bahrain": (25.7, 50.3, 26.4, 50.9),
    "kuwait": (28.5, 46.5, 30.1, 48.5),
    "jordan": (29.1, 34.9, 33.4, 39.4),
    "egypt": (21.9, 24.6, 31.7, 36.9),
    "north_africa": (19, -17.2, 37.5, 37),     # Morocco .. Egypt, big: ~1000 tiles
    # USA: "usa" = usa_mainland + alaska + hawaii together (~2400 tiles, ~7 GB); the Aleutians west of 170 W are left out
    "usa_mainland": (24.4, -125, 49.5, -66.9),  # ~1500 tiles, ~4.4 GB
    "alaska": (51, -170, 71.5, -130),           # ~800 tiles, much of it sea / ice
    "hawaii": (18.9, -160.3, 22.3, -154.8),
    "sri_lanka": (5.8, 79.5, 9.9, 81.9),         # ~ 12 tiles
    "maldives": (-0.8, 72.5, 7.2, 73.9),       # ~ 18 tiles, mostly sea
    "europe": (35, -10, 71.5, 40),             # big: well over 1000 tiles, several GB, hours of download
}
_ALIASES = {
    "oesterreich": "austria", "österreich": "austria", "deutschland": "germany", "italien": "italy", "kroatien": "croatia",
    "slowenien": "slovenia", "schweiz": "switzerland", "tschechien": "czechia", "czech": "czechia", "slowakei": "slovakia",
    "ungarn": "hungary", "polen": "poland", "frankreich": "france", "spanien": "spain", "griechenland": "greece",
    "bosnien": "bosnia", "serbien": "serbia", "rumaenien": "romania", "rumänien": "romania", "bulgarien": "bulgaria",
    "niederlande": "netherlands", "belgien": "belgium", "gb": "uk", "irland": "ireland", "daenemark": "denmark",
    "dänemark": "denmark", "schweden": "sweden", "norwegen": "norway", "finnland": "finland", "island": "iceland",
    "us": "usa", "vereinigte staaten": "usa", "amerika": "usa", "festland-usa": "usa_mainland", "usa-festland": "usa_mainland",
    "arabien": "arabia", "arabische halbinsel": "arabia", "vae": "uae", "emirate": "uae", "dubai": "uae",
    "saudi-arabien": "saudi_arabia", "saudiarabien": "saudi_arabia", "saudi": "saudi_arabia", "katar": "qatar",
    "jordanien": "jordan", "aegypten": "egypt", "ägypten": "egypt", "nordafrika": "north_africa", "bahrein": "bahrain", "tuerkei": "turkey",
    "türkei": "turkey", "srilanka": "sri_lanka", "sri-lanka": "sri_lanka", "sri lanka": "sri_lanka",
    "malediven": "maldives", "maledives": "maldives",
}
REGIONS = {k: _box(*v) for k, v in _BOXES.items()}
REGIONS["usa"] = sorted(set(REGIONS["usa_mainland"]) | set(REGIONS["alaska"]) | set(REGIONS["hawaii"]))


def resolve_regions(names: Iterable[str]) -> tuple:
    """(sorted tile list, unknown names). Names are case-insensitive; German names and 'sri lanka' / 'sri-lanka' work too."""
    tiles, unknown = set(), []
    for n in names:
        k = _ALIASES.get(n.strip().lower(), n.strip().lower())
        if k in REGIONS:
            tiles.update(REGIONS[k])
        else:
            unknown.append(n)
    return sorted(tiles), unknown


def name(la: int, lo: int) -> str:
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def thin_to_srtm3(raw: bytes) -> bytes:
    """3601x3601 big-endian int16 -> 1201x1201 (every 3rd row and column). 1201x1201 input passes through."""
    if len(raw) == 1201 * 1201 * 2:
        return raw
    if len(raw) != 3601 * 3601 * 2:
        raise ValueError(f"unexpected tile size {len(raw)}")
    a = array("h")
    a.frombytes(raw)
    if sys.byteorder == "little":
        a.byteswap()
    out = array("h")
    for r in range(0, 3601, 3):
        out.extend(a[r * 3601:(r + 1) * 3601:3])
    if sys.byteorder == "little":
        out.byteswap()
    return out.tobytes()


def fetch_tile(base_url: str, la: int, lo: int, out_dir: str, timeout: float = 60.0) -> str:
    """Download one tile unless it is already there. Returns 'have' | 'new' | 'missing' (ocean tiles do not exist)."""
    n = name(la, lo)
    dest = os.path.join(out_dir, n + ".hgt")
    if os.path.isfile(dest) or os.path.isfile(dest + ".gz"):
        return "have"
    url = f"{base_url.rstrip('/')}/{n[:3]}/{n}.hgt.gz"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "geo-service/1.0"}), timeout=timeout) as r:
            raw = gzip.decompress(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            return "missing"
        raise
    os.makedirs(out_dir, exist_ok=True)
    tmp = f"{dest}.{os.getpid()}.part"        # per process: two writers never share a half-written file
    with open(tmp, "wb") as f:
        f.write(thin_to_srtm3(raw))
    os.replace(tmp, dest)
    return "new"


def fetch_many(base_url: str, tiles: Iterable, out_dir: str, log=print, every: int = 25, retries: int = 3,
               retry_wait: float = 30.0) -> dict:
    """Fetches every tile that is not there yet; logs a progress line every `every` tiles actually looked at.
    Tiles that failed (network hiccup, 5xx) are tried again up to `retries` more times after `retry_wait` seconds."""
    import time
    tiles = list(tiles)
    res = {"have": 0, "new": 0, "missing": 0, "failed": 0}
    t0 = time.time()
    failed: list = []
    for i, (la, lo) in enumerate(tiles, 1):
        try:
            res[fetch_tile(base_url, la, lo, out_dir)] += 1
        except Exception as e:      # noqa: BLE001  one bad tile must not stop the rest
            failed.append((la, lo))
            log(f"srtm {name(la, lo)}: {type(e).__name__}: {e}")
        if every and (i % every == 0) and i < len(tiles):
            log(f"srtm preload: {i}/{len(tiles)} tiles checked ({res['new']} new, {res['have']} already there, "
                f"{res['missing']} sea/none, {len(failed)} failed so far), {int(time.time() - t0)} s")
    for attempt in range(1, retries + 1):
        if not failed:
            break
        log(f"srtm preload: retrying {len(failed)} failed tile(s), attempt {attempt}/{retries} in {int(retry_wait)} s")
        time.sleep(retry_wait)
        still = []
        for la, lo in failed:
            try:
                res[fetch_tile(base_url, la, lo, out_dir)] += 1
            except Exception as e:  # noqa: BLE001
                still.append((la, lo))
                log(f"srtm {name(la, lo)}: {type(e).__name__}: {e}")
        failed = still
    res["failed"] = len(failed)
    return res


def parse_tiles(spec: str) -> list:
    """'N47E016' or 'N47E016,N46E009' -> [(47,16),(46,9)]"""
    out = []
    for t in spec.replace(" ", "").split(","):
        if len(t) == 7 and t[0] in "NS" and t[3] in "EW":
            out.append((int(t[1:3]) * (1 if t[0] == "N" else -1), int(t[4:]) * (1 if t[3] == "E" else -1)))
        elif t:
            raise ValueError(f"bad tile name {t!r} (expected like N47E016)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Download elevation tiles as SRTM3 .hgt files")
    ap.add_argument("--out", default="./geo-data/srtm")
    ap.add_argument("--austria", action="store_true", help="all 27 tiles N46-N48 x E009-E017 (~78 MB)")
    ap.add_argument("--region", default="", help="comma list of region names, e.g. germany,italy,sri_lanka (see REGIONS)")
    ap.add_argument("--tiles", default="", help="comma list, e.g. N47E016,N46E014")
    ap.add_argument("--bbox", default="", help="minLat,minLon,maxLat,maxLon (decimal degrees)")
    ap.add_argument("--source", default="https://elevation-tiles-prod.s3.amazonaws.com/skadi")
    a = ap.parse_args(argv)
    tiles = list(REGIONS["austria"]) if a.austria else []
    tiles += parse_tiles(a.tiles)
    if a.region:
        rt, unknown = resolve_regions(a.region.split(","))
        if unknown:
            ap.error(f"unknown region(s): {', '.join(unknown)}; known: {', '.join(sorted(REGIONS))}")
        tiles += rt
    if a.bbox:
        la0, lo0, la1, lo1 = (float(x) for x in a.bbox.split(","))
        tiles += [(la, lo) for la in range(int(la0 // 1), int(la1 // 1) + 1) for lo in range(int(lo0 // 1), int(lo1 // 1) + 1)]
    if not tiles:
        ap.error("give --austria, --region, --tiles or --bbox")
    res = fetch_many(a.source, sorted(set(tiles)), a.out)
    print(f"{res['new']} new, {res['have']} already there, {res['missing']} not available (ocean), {res['failed']} failed -> {a.out}")
    return 1 if res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
