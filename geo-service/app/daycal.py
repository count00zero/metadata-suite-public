"""daycal.py -- public holidays (offline, python-holidays) for the country/region of a place, in German where available.

The country comes from the place (ISO alpha-3 from the geocoder), for Austria and Germany also the federal state
(some holidays are regional). Without the `holidays` package or for an unknown country nothing is reported.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Optional

from .iso import ALPHA2_TO_ALPHA3

try:
    import holidays as _hol
except Exception:                            # pragma: no cover
    _hol = None

ALPHA3_TO_2 = {v: k for k, v in ALPHA2_TO_ALPHA3.items()}
AT_STATES = {"burgenland": "1", "kärnten": "2", "kaernten": "2", "carinthia": "2", "niederösterreich": "3",
             "niederoesterreich": "3", "lower austria": "3", "oberösterreich": "4", "oberoesterreich": "4",
             "upper austria": "4", "salzburg": "5", "steiermark": "6", "styria": "6", "tirol": "7", "tyrol": "7",
             "vorarlberg": "8", "wien": "9", "vienna": "9"}
DE_STATES = {"baden-württemberg": "BW", "bayern": "BY", "bavaria": "BY", "berlin": "BE", "brandenburg": "BB",
             "bremen": "HB", "hamburg": "HH", "hessen": "HE", "hesse": "HE", "mecklenburg-vorpommern": "MV",
             "niedersachsen": "NI", "lower saxony": "NI", "nordrhein-westfalen": "NW", "north rhine-westphalia": "NW",
             "rheinland-pfalz": "RP", "saarland": "SL", "sachsen": "SN", "saxony": "SN", "sachsen-anhalt": "ST",
             "schleswig-holstein": "SH", "thüringen": "TH", "thuringia": "TH"}


def country_code(place: Optional[dict]) -> str:
    a3 = ((place or {}).get("country_a") or "").upper()
    return ALPHA3_TO_2.get(a3, a3 if len(a3) == 2 else "")


def subdivision(cc: str, state: str) -> Optional[str]:
    s = (state or "").strip().lower()
    return (AT_STATES if cc == "AT" else DE_STATES if cc == "DE" else {}).get(s)


@lru_cache(maxsize=64)
def _calendar(cc: str, subdiv: Optional[str], year: int):
    if _hol is None or not cc:
        return None
    try:
        if cc not in _hol.list_supported_countries():
            return None
        langs = _hol.country_holidays(cc, years=year).supported_languages     # German, else English, else local names
        lang = "de" if "de" in langs else "en_US" if "en_US" in langs else None
        try:
            return _hol.country_holidays(cc, subdiv=subdiv or None, years=year, language=lang)
        except Exception:                    # noqa: BLE001  subdivision not known to this version
            return _hol.country_holidays(cc, years=year, language=lang)
    except Exception:                        # noqa: BLE001
        return None


def holiday(place: Optional[dict], d: date) -> dict:
    """{'name': 'Ostermontag', 'country': 'AT'} or {} (also {} for an ordinary day)."""
    cc = country_code(place)
    cal = _calendar(cc, subdivision(cc, (place or {}).get("state", "")), d.year)
    if not cal:
        return {}
    name = cal.get(d)
    return {"name": name.split("; ")[0], "country": cc} if name else {}
