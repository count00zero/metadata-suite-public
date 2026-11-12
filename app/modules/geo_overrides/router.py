"""
router.py -- "Geo-Overrides": the geo-service's override editor, embedded in the suite next to GPS füllen.

The editor itself is the geo-service's own page (GET /ui there, one source of truth); the suite shows it in an iframe and
forwards its API calls through a small allow-listed proxy, so the browser only ever talks to the suite (the service URL is
usually a container-internal name like http://geo-service:8000) and the service token stays on the server.

  GET  /geooverrides/                 page with the suite's nav + the editor
  GET  /geooverrides/editor           the service's editor page with the API base pointed at the proxy below
  ANY  /geooverrides/api/<path>       proxy: searchJSON, overrides, overrides/lookup, context (details panel), overrides/<id>, ui-assets/* (map library)
"""
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from ...core import geoservice

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

API_BASE = "/geooverrides/api"
# (method, path regex) the editor needs -- nothing else of the service is reachable through here
ALLOWED = (("GET", r"ui-assets/.+"), ("GET", r"searchJSON"), ("GET", r"overrides"), ("GET", r"overrides/lookup"), ("GET", r"context"),
           ("POST", r"overrides"), ("DELETE", r"overrides/[^/]+"))


def _headers() -> dict:
    h = {"Accept": "application/json"}
    token = geoservice._env("GEO_SERVICE_TOKEN")
    if token:
        h["X-API-Key"] = token
    return h


def _message(title: str, text: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><body style='font:15px system-ui;padding:24px;color:#555'>"
                        f"<h3>{title}</h3><p>{text}</p>", status_code=200)


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"enabled": geoservice.enabled()})


@router.get("/editor", response_class=HTMLResponse)
def editor():
    if not geoservice.enabled():
        return _message("Geo-Service nicht aktiviert", "GEO_SERVICE_URL ist leer – ohne den geo-service gibt es keine Overrides.")
    try:
        r = geoservice.requests.get(geoservice.url() + "/ui", timeout=geoservice.timeout())
        r.raise_for_status()
        html = r.text
    except Exception as e:      # noqa: BLE001
        return _message("Geo-Service nicht erreichbar", f"{geoservice.url()}/ui: {type(e).__name__}. Läuft der Container? "
                                                       "(Neu laden, sobald er wieder da ist.)")
    inject = f'<script>window.GEO_API_BASE="{API_BASE}";</script>'
    return HTMLResponse(html.replace("<head>", "<head>" + inject, 1) if "<head>" in html else inject + html)


@router.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
async def api(path: str, request: Request):
    if not geoservice.enabled():
        return JSONResponse({"error": "GEO_SERVICE_URL ist leer"}, status_code=503)
    if not any(request.method == m and re.fullmatch(p, path) for m, p in ALLOWED):
        return JSONResponse({"error": "not allowed"}, status_code=404)
    headers = _headers()
    body = await request.body()
    if body:
        headers["Content-Type"] = request.headers.get("content-type", "application/json")
    try:
        r = geoservice.requests.request(request.method, f"{geoservice.url()}/{path}", params=list(request.query_params.multi_items()),
                                        data=body or None, headers=headers, timeout=geoservice.timeout() * 2)
    except Exception as e:      # noqa: BLE001
        return JSONResponse({"error": f"Geo-Service nicht erreichbar ({type(e).__name__})"}, status_code=502)
    if r.status_code == 401:    # our own token is wrong -- say so instead of asking the browser user for one
        return JSONResponse({"error": "Geo-Service lehnt den Token ab (GEO_SERVICE_TOKEN der Suite prüfen)"}, status_code=502)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type", "application/json"))
