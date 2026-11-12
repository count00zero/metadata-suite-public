"""
router.py — the "Info" page (module #8, the ⓘ icon at the far right of every nav):
build/version info, tool + package versions, all environment settings, config/media paths and an
on-demand reachability check of the configured external services. Read-only, nothing is changed.

  GET /info/             the page
  GET /info/services     HTMX fragment: reachability check (Traccar, Dawarich, geocoders, Ollama)
  GET /info/sights       HTMX fragment: state of the sights index (counts, dates, translations, last automatic refresh)
  GET /info/report.txt   plain-text report for bug reports (secrets stay masked)
  GET /info/data.json    the same data as JSON
"""
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from ...core import infocore

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"d": infocore.collect()})


@router.get("/services", response_class=HTMLResponse)
def services(request: Request):
    return templates.TemplateResponse(request, "_services.html", {"rows": infocore.check_services()})


@router.get("/sights", response_class=HTMLResponse)
def sights(request: Request):
    return templates.TemplateResponse(request, "_sights.html", {"s": infocore.sights_status()})


@router.get("/report.txt", response_class=PlainTextResponse)
def report():
    return infocore.text_report(infocore.collect())


@router.get("/data.json")
def data():
    return JSONResponse(infocore.collect())
