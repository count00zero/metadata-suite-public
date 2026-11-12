"""
router.py — the Collage web module: given a folder of trip folders (or one trip folder
directly), generates one scattered-photo-pile/mosaic collage per trip, the same way
photo_pile_collage.py's --recursive mode does. Mounted under /collage by app/main.py.

Two-phase, like GPS Sidecar/AI Description: /discover/start is the cheap phase (no image
decoding, just filesystem walks) that figures out which folder(s) a run would produce one
collage each for (core.collagecore.discover_trips) and shows them as a selectable list;
/generate/start is the expensive phase (actual image decoding/rendering) and only runs for
whichever trip folders are still selected. /regenerate is a synchronous single-trip
do-over (new random seed, always overwrites) for when a result wasn't to the maintainer's liking --
small enough to not need its own background job.

Settings (layout, canvas size, border, rotation, ...) default from
core.settings.load_collage_defaults() (configurable via COLLAGE_* env vars, the maintainer's own
standard run) but are plain form fields on this module's own page, carried through both
phases via a hidden settings_json field -- so any single run can override any of them
without touching the environment.
"""
from __future__ import annotations

import os
import json
from dataclasses import fields as dc_fields

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...core import collagecore, settings, pathsafety, jobs

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

router = APIRouter()

_SETTINGS_FIELDS = {f.name for f in dc_fields(collagecore.CollageSettings)}


def _settings_from_dict(d: dict) -> collagecore.CollageSettings:
    return collagecore.CollageSettings(**{k: v for k, v in d.items() if k in _SETTINGS_FIELDS})


def _opt_int(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _opt_float(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _settings_from_form(
    layout, width, height, cell, border, rotation, jitter, seed, count,
    spacing, tile_size, size_variation, force, sync_folder_jpg,
) -> collagecore.CollageSettings:
    return collagecore.CollageSettings(
        layout=layout if layout in collagecore.ALL_LAYOUT_CHOICES else "random",
        width=max(200, int(width)), height=max(200, int(height)),
        cell=_opt_int(cell), border=max(0, int(border)),
        rotation=_opt_float(rotation), jitter=float(jitter),
        seed=_opt_int(seed), count=_opt_int(count),
        spacing=max(0, int(spacing)), tile_size=_opt_int(tile_size),
        size_variation=float(size_variation), force=force, sync_folder_jpg=sync_folder_jpg,
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    defaults = settings.load_collage_defaults()
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": root_folders,
        "defaults": defaults,
        "layout_choices": collagecore.ALL_LAYOUT_CHOICES,
        "photo_root": settings.photo_root(),
        "video_root": settings.video_root(),
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def discover_start_work(job, path):
    """Background work of discover_start (extracted so the Jahreslauf can call it without a request)."""
    trips = collagecore.discover_trips(path)
    job.total = len(trips)
    job.done = len(trips)
    trips_dicts = [t.__dict__ for t in trips]
    return {
        "error": None, "trips": trips_dicts,
        "all_trips_json": json.dumps(trips_dicts),
        "scanned_path": path,
    }


@router.post("/discover/start", response_class=HTMLResponse)
def discover_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
):
    """Cheap phase: figure out which trip folder(s) a run against `path` would produce
    one collage each for (core.collagecore.discover_trips -- just os.walk/os.scandir, no
    image decoding), and show them as a selectable list. This endpoint only needs `path`/
    `root_path` -- the other #browse-form fields (layout, force, width, ...) are collected
    straight off that live form when "Collagen erstellen" is clicked (see _trip_list.html's
    hx-include="#browse-form" and generate_start below), not snapshotted here, so toggling
    a setting after the trip list is already on screen (e.g. "Vorhandene Collagen
    überschreiben" once you've seen which trips already exist) actually takes effect."""
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)

    if not os.path.isdir(path):
        return templates.TemplateResponse(request, "_trip_list.html", {
            "error": f"Pfad nicht gefunden oder kein Ordner: {path}", "trips": [],
        })

    job = jobs.create_job()
    job.phase = "Collagen suchen"


    jobs.run_job(job, lambda: discover_start_work(job, path=path))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/collage/discover/progress/{job.id}", "cancel_url": f"/collage/discover/cancel/{job.id}",
    })


@router.get("/discover/progress/{job_id}", response_class=HTMLResponse)
def discover_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_trip_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "trips": [],
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/collage/discover/progress/{job.id}", "cancel_url": f"/collage/discover/cancel/{job.id}",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_trip_list.html", {
            "error": None, "trips": [], "cancelled_info": "Suche abgebrochen.",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_trip_list.html", {
            "error": f"Fehler bei der Collagen-Suche: {job.error}", "trips": [],
        })
    return templates.TemplateResponse(request, "_trip_list.html", job.result or {})


@router.post("/discover/cancel/{job_id}")
def discover_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


def _check_output_allowed(trip: dict) -> None:
    """The collage for a trip folder is written next to it (its PARENT directory), which
    is correct and intended (see collagecore.discover_trips) but may be one level above
    the selected Basis-Ordner in the single-trip-folder case. Only the MEDIA_ROOT fence
    is enforced here, not Basis-Ordner containment -- same as GPS Sidecar's own .xmp
    sidecar writes, which likewise land next to the source file rather than strictly
    "inside" whatever was typed into Pfad."""
    pathsafety.check_allowed(trip["path"])
    pathsafety.check_allowed(os.path.dirname(trip["output_path"]) or ".")


def generate_start_work(job, coll_settings, to_process):
    """Background work of generate_start (extracted so the Jahreslauf can call it without a request)."""
    photo_root, video_root = settings.photo_root(), settings.video_root()
    results = []
    for i, t in enumerate(to_process):
        if job.cancel_event.is_set():
            break
        job.done, job.current_name = i, t.get("name", "")
        # One bad trip must not discard the whole batch (the original script counted
        # failures per folder and carried on) -- record it as an error row instead.
        try:
            _check_output_allowed(t)
            trip = collagecore.TripFolder(**{k: t[k] for k in ("path", "name", "image_count", "output_path", "already_exists")})
            result = collagecore.generate_trip_collage(trip, coll_settings, photo_root, video_root)
            results.append(result.to_dict())
        except Exception as e:  # noqa: BLE001
            err = getattr(e, "detail", None) or str(e)
            results.append(collagecore.CollageResult(
                trip_path=t.get("path", ""), trip_name=t.get("name", ""), status="error",
                error=str(err)).to_dict())
    else:
        job.done = len(to_process)

    return {
        "results": results, "settings_json": json.dumps(coll_settings.to_dict()),
        "cancelled_info": (
            f"Abgebrochen nach {job.done} von {job.total} Collage(n)." if job.cancel_event.is_set() else None
        ),
    }


@router.post("/generate/start", response_class=HTMLResponse)
def generate_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_trips: str = Form(...),
    layout: str = Form("random"),
    width: str = Form("4096"),
    height: str = Form("2304"),
    cell: str = Form(""),
    border: str = Form("6"),
    rotation: str = Form(""),
    jitter: str = Form("0.55"),
    seed: str = Form(""),
    count: str = Form(""),
    spacing: str = Form("0"),
    tile_size: str = Form(""),
    size_variation: str = Form("0.35"),
    force: bool = Form(False),
    sync_folder_jpg: bool = Form(False),
):
    """The settings fields here are no longer a snapshot carried over from
    /discover/start's hidden settings_json (see _trip_list.html's hx-include="#browse-form"
    on the generate-form) -- they are read live off #browse-form at the moment "Collagen
    erstellen" is actually clicked. That matters for exactly the case the maintainer reported:
    checking "Vorhandene Collagen überschreiben" (force) AFTER the trip list is already on
    screen used to have no effect, because the old settings_json hidden field had already
    baked in whatever `force` was at the time "Collagen suchen" was clicked, before the
    checkbox was touched."""
    try:
        trips_data = json.loads(all_trips)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")

    coll_settings = _settings_from_form(
        layout, width, height, cell, border, rotation, jitter, seed, count,
        spacing, tile_size, size_variation, force, sync_folder_jpg,
    )

    selected_set = set(selected)
    to_process = [t for t in trips_data if t["path"] in selected_set]

    job = jobs.create_job()
    job.total = len(to_process)
    job.phase = "Collagen erstellen"


    jobs.run_job(job, lambda: generate_start_work(job, coll_settings=coll_settings, to_process=to_process))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/collage/generate/progress/{job.id}", "cancel_url": f"/collage/generate/cancel/{job.id}",
    })


@router.get("/generate/progress/{job_id}", response_class=HTMLResponse)
def generate_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_results.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "results": [],
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/collage/generate/progress/{job.id}", "cancel_url": f"/collage/generate/cancel/{job.id}",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_results.html", {
            "error": f"Fehler beim Erstellen: {job.error}", "results": [],
        })
    # done or cancelled -- job.result is populated either way (partial results kept on cancel)
    return templates.TemplateResponse(request, "_results.html", job.result or {})


@router.post("/generate/cancel/{job_id}")
def generate_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


@router.post("/regenerate", response_class=HTMLResponse)
def regenerate(
    request: Request,
    trip_path: str = Form(...),
    trip_name: str = Form(...),
    trip_output_path: str = Form(...),
    trip_image_count: int = Form(0),
    settings_json: str = Form(...),
):
    """Synchronous single-trip do-over: always overwrites (ignoring whatever `force` was
    submitted originally) and always uses a fresh random seed (ignoring any configured
    seed) so clicking it again actually gives a different result -- for when a generated
    collage wasn't to the maintainer's liking. Small enough (one trip) to not need its own
    background job; returns just the one updated row, swapped in place."""
    try:
        coll_settings = _settings_from_dict(json.loads(settings_json))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Einstellungen, bitte neu laden.")

    coll_settings.force = True
    coll_settings.seed = None

    trip = collagecore.TripFolder(
        path=trip_path, name=trip_name, image_count=trip_image_count,
        output_path=trip_output_path, already_exists=os.path.exists(trip_output_path),
    )
    _check_output_allowed({"path": trip.path, "output_path": trip.output_path})

    photo_root, video_root = settings.photo_root(), settings.video_root()
    result = collagecore.generate_trip_collage(trip, coll_settings, photo_root, video_root)
    return templates.TemplateResponse(request, "_result_row.html", {
        "r": result.to_dict(), "settings_json": settings_json,
    })
