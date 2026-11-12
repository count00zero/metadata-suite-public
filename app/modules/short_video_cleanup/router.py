"""
router.py — the "Kurzvideos löschen" web module (#7): finds videos shorter than the
configured NFO_SHORT_VIDEO_SECS threshold (together with their .nfo/poster/fanart/
thumb/.xmp companion files) and lets the maintainer delete them directly -- without first
running an NFO Maker scan over the whole folder (which also generates thumbnails for
every OTHER video it finds, something this module explicitly does not need: "Will
speed things up!" was the whole point of a dedicated module here).

Mounted under /shortvideos by app/main.py, positioned as the last ("ganz rechts")
module pill in every template's nav.

Two-phase, same shape as every other module here: /scan/start is the cheap-ish phase
(find candidate videos, read each one's real duration via mediainfo, keep only the
ones actually under the threshold) and shows them as a selectable list; /delete is the
destructive phase, and only ever acts on files still selected on that list.

Deletion itself (the actual os.remove() calls, the companion-file list, and the
mandatory fresh re-check of each video's duration right before deleting) is NOT
duplicated here -- it's the exact same core.nfocore.delete_short_video_with_companions()
NFO Maker's own /delete already uses, so the two call sites can never drift apart.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse

from ...core import nfocore, settings, pathsafety, jobs, stepper

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
from fastapi.templating import Jinja2Templates
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = ""):
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    short_video_secs = settings.load_nfo_defaults().short_video_secs
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": root_folders,
        "short_video_secs": short_video_secs,
        "prefill_path": path,
        "prefill_root_path": root_path,
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


@router.post("/scan/start", response_class=HTMLResponse)
def scan_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
    depth: int = Form(1),
    short_video_secs: int = Form(10),
    scan_id: str = Form(""),
    offset: int = Form(0),
):
    """Cheap-ish phase: walk the folder for videos, run one mediainfo read per video
    (unavoidable -- duration isn't knowable from the filename), keep only the ones
    actually shorter than `short_video_secs`. No thumbnail extraction, no .nfo writing,
    no AI/geo reads at all -- this module only ever looks at duration + which companion
    files already exist, which is exactly what makes it faster than routing through
    NFO Maker's own scan just to get here."""
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    depth = max(0, min(7, depth))
    short_video_secs = max(1, short_video_secs)

    if not os.path.exists(path):
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Pfad nicht gefunden: {path}", "rows": [], "short_video_secs": short_video_secs,
        })

    step = stepper.resume("shortvideos", scan_id, offset)
    if step is None:
        step = stepper.start("shortvideos", nfocore.find_videos(path, depth))
    all_videos = step.paths

    job = jobs.create_job()
    job.total = len(all_videos)
    job.phase = "Videodauer prüfen"

    def work():
        rows = []
        for i, video in enumerate(all_videos):
            if job.cancel_event.is_set():
                break
            job.done, job.current_name = i, os.path.basename(video)
            # Cheap pre-check (pooled exiftool, ~5 ms): a video that is clearly long never needs the
            # authoritative mediainfo read. Anything near/under the threshold or unknown still gets it,
            # so the result is identical -- only obviously-long files skip the second tool.
            q = nfocore.quick_duration_secs(video)
            if q is not None and q >= short_video_secs * 1.1 + 1:
                continue
            meta = nfocore.extract_video_metadata(video)
            if meta.error or not (0 < meta.duration_secs < short_video_secs):
                continue
            base_dir = os.path.dirname(video)
            stem = os.path.splitext(os.path.basename(video))[0]
            companions = {
                "nfo": os.path.exists(os.path.join(base_dir, stem + ".nfo")),
                "poster": os.path.exists(os.path.join(base_dir, stem + "-poster.jpg")),
                "fanart": os.path.exists(os.path.join(base_dir, stem + "-fanart.jpg")),
                "thumb": os.path.exists(os.path.join(base_dir, stem + "-thumb.jpg")),
                "xmp": os.path.exists(video + ".xmp"),
            }
            rows.append({
                "path": video, "filename": os.path.basename(video),
                "duration_secs": meta.duration_secs,
                "duration_display": nfocore.format_duration(meta.duration_secs),
                "companions": companions,
                "companion_count": sum(companions.values()),
                "rel_dir": pathsafety.rel_subdir(path, video),
            })
        else:
            job.done = len(all_videos)
        return {
            "rows": rows, "error": None, "short_video_secs": short_video_secs,
            "scanned_count": len(all_videos),
            "step": step.to_dict(), "step_url": "/shortvideos/scan/start",
        }

    jobs.run_job(job, work)
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/shortvideos/scan/progress/{job.id}", "cancel_url": f"/shortvideos/scan/cancel/{job.id}",
    })


@router.get("/scan/progress/{job_id}", response_class=HTMLResponse)
def scan_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [], "short_video_secs": 0,
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/shortvideos/scan/progress/{job.id}", "cancel_url": f"/shortvideos/scan/cancel/{job.id}",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_scan_list.html", job.result or {"rows": [], "short_video_secs": 0})
    if job.status == "error":
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Fehler beim Scannen: {job.error}", "rows": [], "short_video_secs": 0,
        })
    return templates.TemplateResponse(request, "_scan_list.html", job.result or {})


@router.post("/scan/cancel/{job_id}")
def scan_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


@router.post("/delete", response_class=HTMLResponse)
def delete_short_videos(request: Request, selected: list[str] = Form(default=[])):
    """Same safety contract as NFO Maker's own /delete: re-verifies EVERY candidate's
    duration with a fresh mediainfo read right here, server-side, before removing
    anything -- never trusts what the scan preview last showed. See
    core.nfocore.delete_short_video_with_companions()'s own docstring."""
    short_video_secs = settings.load_nfo_defaults().short_video_secs
    results = []
    for video in selected:
        try:
            pathsafety.check_allowed(video)
        except HTTPException as e:
            results.append({
                "path": video, "filename": os.path.basename(video),
                "status": "error", "message": str(e.detail),
            })
            continue
        results.append(nfocore.delete_short_video_with_companions(video, short_video_secs))

    return templates.TemplateResponse(request, "_delete_results.html", {"results": results})
