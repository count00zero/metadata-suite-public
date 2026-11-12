"""pois.py -- the short form of the sights ({name, kind, distance_m, lat, lon, osm, ...} nearest first) for /pois and
/context. Since the sights rework (v3) this is a thin wrapper: everything -- Photon requests, filter, cache, de-duplication,
details -- lives in sights.py and is shared with /sights and findNearbyWikipedia."""
from __future__ import annotations

from typing import Optional

from . import sights
from .sights import KIND_DE, SKIP_HISTORIC, kind_de, phrase_de  # noqa: F401  (kept for importers of the old module)


def nearby(s, cache, lat: float, lon: float, radius_m: int, limit: int, lang: str = "de",
           overrides_places: Optional[list] = None) -> Optional[list]:
    """Nearest sights first; None when Photon is not configured/reachable and nothing is cached.
    `overrides_places` (your own named places) are merged in."""
    return sights.search(s, cache, lat, lon, radius_m, limit, lang, sort="distance", own_places=overrides_places)
