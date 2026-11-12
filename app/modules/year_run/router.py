"""
router.py -- the "Jahreslauf" web module: the browser version of Media_actualyear_nfo_Creator.sh.

Pick a year (+ type, scope, depth, AIM), look at the preview of the (up to) four folders, press start: every folder is then
processed unattended in the background -- GPS Sidecar -> AI-Beschreibung -> NFO Maker (videos) -> Kontext nachfüllen -> Datei-Zeitstempel,
plus the Collage for photo/Urlaube. The page polls the run (folder, step, progress, log) and can be reopened later; one run at a time.
Engine: runner.py.
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from . import runner

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()


def _opts(year: int, kind: str, scope: str, depth: int, aim: bool, steps: list, default_tz: str, recheck: bool = False, fixbad: bool = False, month: int = 0, backfill: bool = False) -> runner.Opts:
    kind = kind if kind in ("all", "video", "image") else "all"
    scope = scope if scope in ("all", "regular", "urlaub") else "all"
    chosen = tuple(s for s in runner.STEP_ORDER if s in steps or s == "mtime")      # the timestamp step is always part of a run
    if not 1900 <= year <= 2200:
        year = datetime.now().year
    return runner.Opts(year=year, kind=kind, scope=scope, depth=max(0, min(7, depth)), aim=aim, steps=chosen,
                       default_tz=(default_tz or "Europe/Vienna").strip(), recheck=recheck, fixbad=fixbad, backfill=backfill,
                       month=month if 1 <= month <= 12 else 0)


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    cur = runner.active_run()
    return templates.TemplateResponse(request, "index.html", {
        "year": datetime.now().year, "step_labels": runner.STEP_LABELS, "step_order": runner.STEP_ORDER,
        "default_steps": runner.DEFAULT_STEPS, "default_tz": "Europe/Vienna",
        "running_id": cur.id if cur else "",
    })


@router.post("/preview", response_class=HTMLResponse)
def preview(request: Request, year: int = Form(...), kind: str = Form("all"), scope: str = Form("all"),
            depth: int = Form(7), aim: str = Form(""), steps: list[str] = Form(default=[]),
            default_tz: str = Form("Europe/Vienna"), recheck: str = Form(""), fixbad: str = Form(""), month: int = Form(0), backfill: str = Form("")):
    o = _opts(year, kind, scope, depth, bool(aim), steps, default_tz, bool(recheck), bool(fixbad), month, bool(backfill))
    folders = runner.preview(o)
    return templates.TemplateResponse(request, "_preview.html", {
        "opts": o, "folders": folders, "step_labels": runner.STEP_LABELS,
        "folder_steps": {f.label: [runner.STEP_LABELS[s] for s in runner.steps_for(f, o)] for f in folders},
        "busy": runner.active_run() is not None,
    })


@router.post("/start", response_class=HTMLResponse)
def start(request: Request, year: int = Form(...), kind: str = Form("all"), scope: str = Form("all"),
          depth: int = Form(7), aim: str = Form(""), steps: list[str] = Form(default=[]),
          default_tz: str = Form("Europe/Vienna"), recheck: str = Form(""), fixbad: str = Form(""), month: int = Form(0), backfill: str = Form("")):
    o = _opts(year, kind, scope, depth, bool(aim), steps, default_tz, bool(recheck), bool(fixbad), month, bool(backfill))
    run, created = runner.start(o)
    return templates.TemplateResponse(request, "_run.html", _ctx(run, note="" if created else "Es läuft bereits ein Jahreslauf – du siehst dessen Fortschritt."))


def _log_text(entries) -> str:
    return "".join(f"{t} {'⚠ ' if lvl == 'warn' else ('✗ ' if lvl == 'error' else '')}{msg}\n" for t, lvl, msg in entries if lvl != "debug")


def _ctx(run, note: str = "", oob: bool = False) -> dict:
    entries = run.log[-400:]
    return {"run": run, "p": run.progress(), "log": [e for e in entries if e[1] != "debug"], "log_text": _log_text(entries),
            "oob": oob, "summary": run.summary, "note": note,
            "elapsed": int((run.finished or __import__("time").time()) - run.started)}


@router.get("/run/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: str):
    run = runner.get(run_id)
    if run is None:
        return HTMLResponse('<p class="muted">Dieser Jahreslauf ist nicht mehr vorhanden (abgelaufen oder Server neu gestartet).</p>')
    return templates.TemplateResponse(request, "_run.html", _ctx(run, oob=True))


@router.post("/finish/{run_id}", response_class=HTMLResponse)
def finish(request: Request, run_id: str):
    """"OK" after a cancelled run: sets the timestamps of everything the run changed."""
    run = runner.get(run_id)
    if run is None:
        return HTMLResponse('<p class="muted">Dieser Jahreslauf ist nicht mehr vorhanden.</p>')
    ok = runner.finish_fix(run)
    return templates.TemplateResponse(request, "_run.html", _ctx(run, note="" if ok else "Es gibt nichts mehr richtigzustellen (oder es läuft gerade etwas anderes)."))


@router.post("/cancel/{run_id}")
def cancel(run_id: str):
    run = runner.get(run_id)
    if run is not None:
        run.cancel_event.set()
    return Response(status_code=204)
