"""
router.py -- the endpoint page: every endpoint of the geo-service as a ready-to-click link (new tab), filled with the
coordinates / postcode / place of YOUR overrides (home first). Links go straight from the browser to the service, so
the public address is needed (the suite itself talks to the container-internal one):

  GEO_SERVICE_PUBLIC_URL   e.g. http://192.168.1.33   (host only; default: the host you opened the suite with)
  GEO_PUBLIC_PORT / GEO_PUBLIC_PORT_NOMINATIM / GEO_PUBLIC_PORT_PHOTON   8430 / 8431 / 8432 (the compose defaults)

  GET /endpoints/        the page
  GET /endpoints/samples sample points as JSON
"""
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from ...core import endpointscatalog, geoservice

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def _mdcode(text: str):
    """`code` -> <code>code</code>, everything else escaped."""
    from markupsafe import Markup, escape
    return Markup(re.sub(r"`([^`]+)`", r"<code>\1</code>", str(escape(text or ""))))


templates.env.filters["mdcode"] = _mdcode

DEFAULT_SAMPLE = {"id": "_default", "label": "Beispiel: Volksschule Musterdorf", "lat": 48.1454285, "lng": 16.3257166,
                  "postal": "2552", "city": "Musterdorf", "cc": "AT", "home": False}


def _port(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def bases(request: Request) -> dict:
    pub = geoservice._env("GEO_SERVICE_PUBLIC_URL")
    if pub:
        u = urlsplit(pub if "//" in pub else "http://" + pub)
        scheme, host = u.scheme or "http", u.hostname or request.url.hostname
    else:
        scheme, host = request.url.scheme, request.url.hostname
    if host and ":" in host and not host.startswith("["):
        host = f"[{host}]"                        # IPv6 literal
    return {"api": f"{scheme}://{host}:{_port('GEO_PUBLIC_PORT', 8430)}",
            "nom": f"{scheme}://{host}:{_port('GEO_PUBLIC_PORT_NOMINATIM', 8431)}",
            "photon": f"{scheme}://{host}:{_port('GEO_PUBLIC_PORT_PHOTON', 8432)}"}


def _label(it: dict) -> str:
    pl, rv, ad = it.get("place") or {}, it.get("reverse") or {}, it.get("address") or {}
    return pl.get("name") or rv.get("label") or ad.get("placename") or rv.get("locality") or it.get("id") or "Override"


def samples() -> list:
    out = []
    for it in geoservice.overrides_list():
        try:
            lat, lng = float(it["lat"]), float(it["lng"])
        except (KeyError, TypeError, ValueError):
            continue
        rv, ad, pl = it.get("reverse") or {}, it.get("address") or {}, it.get("place") or {}
        cc = (ad.get("countryCode") or "").upper()
        if not cc and rv.get("country_a"):
            cc = {"AUT": "AT", "DEU": "DE", "CHE": "CH", "ITA": "IT", "SVN": "SI", "HRV": "HR", "HUN": "HU", "CZE": "CZ",
                  "SVK": "SK", "FRA": "FR", "ESP": "ES"}.get(str(rv["country_a"]).upper(), "")
        out.append({"id": str(it.get("id", "")), "label": _label(it), "lat": lat, "lng": lng,
                    "postal": str(ad.get("postalcode") or ""), "city": str(rv.get("city") or ad.get("placename") or ""),
                    "cc": cc or "AT", "home": (pl.get("kind") or "").strip().lower() == "zuhause"})
    out.sort(key=lambda s: (not s["home"], s["label"].lower()))
    for s in out:                                  # fall back to the default sample for what an override does not state
        s["postal"] = s["postal"] or DEFAULT_SAMPLE["postal"] if s["cc"] == "AT" else s["postal"]
        s["city"] = s["city"] or DEFAULT_SAMPLE["city"]
    return out + [DEFAULT_SAMPLE]


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "sections": endpointscatalog.SECTIONS, "bases": bases(request), "samples": samples(),
        "enabled": geoservice.enabled(), "token": bool(geoservice._env("GEO_SERVICE_TOKEN")),
    })


@router.get("/ids", response_class=JSONResponse)
def ids(lat: float, lng: float):
    """geonameIds for the chosen example point (place, state, county) -- fills the get/hierarchy/children links."""
    return geoservice.sample_ids(lat, lng)


@router.get("/samples", response_class=JSONResponse)
def samples_json():
    return {"samples": samples()}
