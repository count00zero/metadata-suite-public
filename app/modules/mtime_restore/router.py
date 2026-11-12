"""
router.py — the Datei-Zeitstempel web module (module #4): browse a folder, compare each
file's (and .xmp sidecar's) current filesystem mtime against the capture time encoded in
its filename (or, failing that, its own EXIF/QuickTime metadata), preview which ones
differ, pick which to fix, execute.

Mounted under /mtime by app/main.py. A standalone front end for the same
core/mtimecore.py logic GPS Sidecar and AI Description already offer as a one-click "fix
what I just wrote" button on their own results screens -- this module is for the broader
case: a whole folder (or NAS share) whose mtimes may have drifted for any reason (a
backup/restore, a copy between filesystems, an exiftool write from outside this app, ...),
not just files this app itself just touched.

Same single-phase browse/preview-then-execute workflow as Timestamp & Rename, since (like
that module, and unlike GPS Sidecar/AI Description) there's no separate expensive
network/inference phase to protect here -- the only per-file cost is a cheap os.stat()
plus, only when the filename doesn't already encode the date, one exiftool read.
"""
from __future__ import annotations

from app.core import skipdirs
import os
import json
from datetime import datetime

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ...core import stepper, timecore, settings, pathsafety, jobs, mtimecore, rotatecore

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()

# Media files AND their .xmp sidecars -- unlike timecore.find_files() (whose ALL_EXT is
# images/videos/.nfo only), this module cares about a sidecar's own mtime too, since GPS
# Sidecar and AI Description both write those directly and can leave them "now"-stamped.
_CANDIDATE_EXT = timecore.VIDEO_EXT | timecore.IMAGE_EXT


def _inner_ext(name: str) -> str:
    """The extension that actually determines a file's type -- for a ".xmp" sidecar,
    that's the extension of the name it's a sidecar TO, not ".xmp" itself."""
    if name.lower().endswith(".xmp"):
        name = name[: -len(".xmp")]
    return os.path.splitext(name)[1].lower()


def _candidate_files(path: str, depth: int, type_filter: str) -> list:
    if os.path.isfile(path):
        candidates = [path]
    elif os.path.isdir(path):
        candidates = []
        base_depth = path.rstrip("/").count("/")
        for root, dirs, files in os.walk(path):
            d = root.rstrip("/").count("/") - base_depth
            if d >= depth:
                dirs[:] = []
            sibling_dirs = list(dirs)
            skipdirs.prune(dirs)
            for f in files:
                if skipdirs.is_generated_collage(f, sibling_dirs):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext == ".xmp":
                    if _inner_ext(f) in _CANDIDATE_EXT:
                        candidates.append(os.path.join(root, f))
                elif ext in _CANDIDATE_EXT:
                    candidates.append(os.path.join(root, f))
    else:
        candidates = []

    out = []
    for f in candidates:
        inner = _inner_ext(os.path.basename(f))
        is_video = inner in timecore.VIDEO_EXT
        is_image = inner in timecore.IMAGE_EXT
        if type_filter == "VIDEO" and not is_video:
            continue
        if type_filter == "IMAGE" and not is_image:
            continue
        out.append(f)
    return out


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = ""):
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": root_folders,
        # Set only when arriving via a "Weiter zu ..." handoff from another module --
        # prefills Pfad for this one load (see index.html's initLastPath()).
        "prefill_path": path,
        "prefill_root_path": root_path,
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def browse_start_work(job, candidates, find_rotation, path, step):
    """Background work of browse_start (extracted so the Jahreslauf can call it without a request)."""
    rows = []
    already_correct_count = 0
    unresolved_count = 0
    rotate_count = 0
    rotate_other = 0
    for i, f in enumerate(candidates):
        job.done, job.current_name = i, os.path.basename(f)
        if job.cancel_event.is_set():
            break
        scanned = mtimecore.scan_one(f)
        scanned["rel_dir"] = pathsafety.rel_subdir(path, f)
        # Optional: JPEGs shown upright only through the EXIF Orientation tag (see core/rotatecore.py)
        scanned["rotate"] = None
        if find_rotation and not f.lower().endswith(".xmp"):
            if rotatecore.is_jpeg(f):
                scanned["rotate"] = rotatecore.needs_rotation(f)
                rotate_count += 1 if scanned["rotate"] else 0
            elif os.path.splitext(f)[1].lower() in timecore.IMAGE_EXT and rotatecore.orientation(f) != 1:
                rotate_other += 1
        if scanned["unresolved"]:
            if scanned["rotate"]:
                scanned["date_unresolved"] = True      # rotation can still be done, the mtime part cannot
                scanned["unresolved"] = False
                scanned["differs"] = False
                rows.append(scanned)
            else:
                unresolved_count += 1
                rows.append(scanned)
        elif not scanned["differs"] and not scanned["rotate"]:
            already_correct_count += 1
        else:
            rows.append(scanned)
    else:
        job.done = len(candidates)

    if job.cancel_event.is_set():
        return {}

    return {
        "rows": rows, "error": None,
        "already_correct_count": already_correct_count,
        "unresolved_count": unresolved_count,
        "rotate_count": rotate_count, "rotate_other": rotate_other, "find_rotation": bool(find_rotation),
        "exiftran_ok": rotatecore.available(),
        "all_rows_json": json.dumps(rows),
        "step": step.to_dict(), "step_url": "/mtime/browse/start",
    }


@router.post("/browse/start", response_class=HTMLResponse)
def browse_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
    depth: int = Form(0),
    type_filter: str = Form("ALL"),
    find_rotation: str = Form(""),
    scan_id: str = Form(""),
    offset: int = Form(0),
):
    """Scans the folder as a background job: for each candidate file (and .xmp sidecar),
    resolve_target()'s filename-first/EXIF-fallback logic computes what its mtime SHOULD
    be, compared against what it actually IS right now -- nothing is written yet. A file
    already at the correct mtime is left out of the list entirely (just counted); one
    whose date can't be resolved at all is shown disabled with a warning rather than
    silently dropped, same "never guess, never hide" rule as the rest of this app."""
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    root_folder = pathsafety.root_for_path(root_path)
    if root_folder is not None:
        type_filter = root_folder.type_filter

    if not os.path.exists(path):
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Pfad nicht gefunden: {path}", "rows": [],
            "already_correct_count": 0, "unresolved_count": 0,
        })

    depth = max(0, min(7, depth))
    step = stepper.resume("mtime", scan_id, offset)
    if step is None:
        step = stepper.start("mtime", _candidate_files(path, depth, type_filter))
    candidates = step.paths

    job = jobs.create_job()
    job.total = len(candidates)


    jobs.run_job(job, lambda: browse_start_work(job, candidates=candidates, find_rotation=find_rotation, path=path, step=step))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/mtime/browse/progress/{job.id}", "cancel_url": f"/mtime/browse/cancel/{job.id}",
    })


@router.get("/browse/progress/{job_id}", response_class=HTMLResponse)
def browse_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [], "already_correct_count": 0, "unresolved_count": 0,
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/mtime/browse/progress/{job.id}", "cancel_url": f"/mtime/browse/cancel/{job.id}",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": None, "rows": [], "already_correct_count": 0, "unresolved_count": 0,
            "cancelled_info": f"Abgebrochen nach {job.done} von {job.total} Datei(en).",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Fehler beim Scannen: {job.error}", "rows": [],
            "already_correct_count": 0, "unresolved_count": 0,
        })
    # done
    return templates.TemplateResponse(request, "_file_list.html", job.result or {})


@router.post("/browse/cancel/{job_id}")
def browse_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return Response(status_code=204)


def apply_rows(rows, selected_set, rotate_set, rotate_backup) -> list:
    """Applies the previewed rows (rotation + mtime). Used by /execute and by the Jahreslauf."""
    results = []
    for row in rows:
        path = row["path"]
        if path not in selected_set:
            continue
        pathsafety.check_allowed(path)
        result = {"path": path, "changed": False, "error": "", "restored_to": "", "source": "", "rotation": ""}

        if not os.path.exists(path):
            result["error"] = "Datei nicht mehr gefunden -- Vorschau neu laden."
            results.append(result)
            continue

        # 1) lossless rotation first (exiftran -p keeps the old mtime; the mtime fix below then overrides it)
        if path in rotate_set and row.get("rotate"):
            rr = rotatecore.rotate_lossless(path, backup=bool(rotate_backup))
            result["rotation"] = rr["message"] + (" · Backup: " + os.path.basename(rr["backup"]) if rr["backup"] else "")
            if not rr["ok"]:
                result["error"] = "Drehen fehlgeschlagen: " + rr["message"]
            result["rotated"] = rr["rotated"]
        # 2) mtime
        if not row.get("target"):
            if not row.get("date_unresolved") and not result["rotation"]:
                result["error"] = "Kein Zieldatum in der Vorschau (ungültige/abgelaufene Daten) -- Vorschau neu laden."
            results.append(result)
            continue
        if not row.get("differs", True):
            results.append(result)
            continue

        try:
            # Aware, via the same configured zone as the scan step (mtimecore.zone(),
            # default Europe/Vienna) -- NOT a naive .timestamp() call, which would
            # silently use the container's own system timezone (UTC by default) instead
            # of the real-world locale these wall-clock strings actually mean. See
            # core/mtimecore.py's module docstring for the full explanation.
            dt = datetime.strptime(row["target"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=mtimecore.zone())
            ts = dt.timestamp()
            os.utime(path, (ts, ts))
            result["changed"] = True
            result["restored_to"] = row["target"]
            result["source"] = row.get("source", "")
        except (ValueError, OSError) as e:
            result["error"] = f"Zeitstempel konnte nicht gesetzt werden: {e}"
        results.append(result)

    return results


@router.post("/execute", response_class=HTMLResponse)
def execute(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_rows: str = Form(...),
    rotate: list[str] = Form(default=[]),
    rotate_backup: str = Form(""),
):
    """Applies exactly what was previewed -- trusts each selected row's already-resolved
    "target"/"source" from the scan step rather than re-resolving (same convention as
    Timestamp & Rename's own execute trusting its preview), so what you saw is what you
    get even if a file changes on disk between preview and execute."""
    try:
        rows = json.loads(all_rows)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")

    results = apply_rows(rows, set(selected), set(rotate), rotate_backup)
    return templates.TemplateResponse(request, "_results.html", {"results": results})
