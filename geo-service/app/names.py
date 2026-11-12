"""names.py -- one spelling per place: 'Marktgemeinde Musterdorf' -> 'Musterdorf', 'Politischer Bezirk Baden' ->
'Bezirk Baden', plus your own aliases (<data dir>/place-names.json, reloaded when the file changes):

    {"city":     {"Wien, Innere Stadt": "Wien"},
     "locality": {"Bahnhof Graz Hbf": "Graz Hauptbahnhof"},
     "*":        {"Oesterreich": "Österreich"}}              # "*" = any field

Fields: country, state, county, city, locality, street. Applied to what the geocoders (and the cache) said, before the
hand-made overrides -- an override always has the last word. GEO_NORMALIZE_NAMES=false switches the prefix rules off;
aliases are explicit and stay on.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

FIELDS = ("country", "state", "county", "city", "locality", "street")
_CITY = re.compile(r"^(?:Marktgemeinde|Stadtgemeinde|Gemeinde|Statutarstadt|Landeshauptstadt|Stadt)\s+(?=\S)")
_COUNTY = re.compile(r"^(?:Politischer Bezirk|Polit\.? Bezirk)\s+(?=\S)")
_WS = re.compile(r"\s{2,}")


def clean(field: str, value: str) -> str:
    v = _WS.sub(" ", (value or "").strip())
    if field in ("city", "locality"):
        v = _CITY.sub("", v)
    elif field == "county":
        v = _COUNTY.sub("Bezirk ", v)
    return v


class Aliases:
    def __init__(self, path: str):
        self.path, self._sig, self._map, self.error, self._lock = path, None, {}, "", threading.Lock()

    def _load(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            self._map, self._sig, self.error = {}, None, ""
            return
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self._map = {k: {str(a): str(b) for a, b in v.items()} for k, v in data.items() if isinstance(v, dict)}
            self.error = ""
        except (OSError, ValueError, AttributeError) as e:
            self.error = f"{type(e).__name__}: {e}"
        self._sig = sig

    def get(self, field: str, value: str) -> str:
        with self._lock:
            self._load()
            return self._map.get(field, {}).get(value) or self._map.get("*", {}).get(value) or value


def normalize(rec: dict, aliases: Optional[Aliases], strip: bool = True) -> dict:
    """A copy of a /reverse-like dict with its place fields normalised; label/sublocation follow (re-derived)."""
    out = dict(rec)
    changed = False
    for f in FIELDS:
        v = rec.get(f)
        if not isinstance(v, str) or not v:
            continue
        n = clean(f, v) if strip else v
        if aliases is not None:
            n = aliases.get(f, n)
        if n != v:
            out[f], changed = n, True
    if changed and rec.get("found"):
        from .backends import GeoResult
        d = GeoResult.from_dict({k: out.get(k, "") for k in ("country", "country_a", "state", "county", "city", "locality",
                                                              "street", "source")}).to_dict()
        out.update({"label": d["label"], "sublocation": d["sublocation"]})
    return out


def normalize_place(p: dict, aliases: Optional[Aliases], strip: bool = True) -> dict:
    """The same for the Nominatim/Photon facade's place record (keys: country, state, county, city, name, street)."""
    out = dict(p)
    for src, f in (("country", "country"), ("state", "state"), ("county", "county"), ("city", "city"), ("street", "street"),
                   ("suburb", "locality")):
        v = p.get(src)
        if isinstance(v, str) and v:
            n = clean(f, v) if strip else v
            out[src] = aliases.get(f, n) if aliases is not None else n
    return out
