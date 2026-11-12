"""
router.py — the AI Description web module: browse a folder, generate a short German
AI scene description per photo/video (Ollama and/or Google Gemini, same priority-list
fallback as the CLI), preview it, select which to apply, write to .xmp (+ optionally
embed directly into images).

Mounted under /ai by app/main.py. Ported from Set-GPS-Data-via-traccar-and-dawarich.sh's
-AIM/-AIF passes and the generate_ai_description.py helper it shells out to -- see
app/core/aicore.py's module docstring for exactly what was and wasn't carried over.

Same two-phase, separately-cancelable job pattern as the GPS Sidecar module, for the
same reason: calling an AI provider per file is the expensive step here (a model
inference call, not a network geocoding lookup, but the same cost shape), so it's kept
out of the cheap scan phase entirely. /browse/start only reads what's already on disk
(existing description, existing location fields for the prompt's geo-context) --
nothing here calls an AI provider. The resulting list is then narrowed down by hand
before /generate/start ever calls a provider, so a deselected file never costs a call,
not just a wasted one.
"""
from __future__ import annotations

import html
import os
import json

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ...core import fileindex, mtime_routes, timecore, settings, aicore, geocore, pathsafety, jobs, mtimecore, xmpsync, handoff, devicenames, stepper, geoservice, fillcore

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()

# Only JPEG stills get a direct AI call in the CLI's own -AIM/-AIF passes (PNG/GIF/PSD
# are skipped even though timecore.IMAGE_EXT accepts them for Timestamp & Rename) --
# kept the same restriction here, since it's exactly what resize/ffmpeg + Immich/IPTC
# embedding were built and tested around.
_AI_IMAGE_EXT = {".jpg", ".jpeg"}


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = "", handoff_id: str = ""):
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    ai_cfg = settings.load_ai_settings()
    # A handoff_id (from "Weiter zu AI-Beschreibung") takes priority over plain
    # path/root_path -- it carries the EXACT files GPS Sidecar (or Timestamp & Rename
    # directly) just touched (see core/handoff.py). Resolving it here only sets
    # path/root_path for prefill; browse_start() re-resolves the same id for the actual
    # file list once the form submits.
    entry = handoff.fetch(handoff_id)
    if entry is not None:
        path, root_path = entry["path"], entry["root_path"]
    else:
        handoff_id = ""  # expired/invalid -- don't carry a dead id into the hidden field
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": root_folders,
        "ai_cfg": ai_cfg,
        # Set only when arriving via a "Weiter zu AI-Beschreibung" handoff from another
        # module -- prefills Pfad for this one load (see index.html's initLastPath()).
        "prefill_path": path,
        "prefill_root_path": root_path,
        "prefill_handoff_id": handoff_id,
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def _candidate_files(path: str, depth: int, type_filter: str) -> list:
    out = []
    for f in timecore.find_files(path, depth):
        ext = os.path.splitext(f)[1].lower()
        is_video = ext in timecore.VIDEO_EXT
        is_image = ext in _AI_IMAGE_EXT
        if not (is_video or is_image):
            continue
        if type_filter == "VIDEO" and not is_video:
            continue
        if type_filter == "IMAGE" and not is_image:
            continue
        out.append(f)
    return out


def browse_start_work(job, camera_model, candidates, depth, embed_in_image, overwrite_existing, path, root_path, step, type_filter, fix_bad=False, use_index=True):
    """Background work of browse_start (extracted so the Jahreslauf can call it without a request)."""
    rows = []
    skipped_done = 0
    camera_filtered = 0
    for i, f in enumerate(candidates):
        job.done, job.current_name = i, os.path.basename(f)
        if job.cancel_event.is_set():
            break

        if camera_model:
            make, model = timecore.read_camera(f)
            if not devicenames.camera_matches(make, model, camera_model):
                camera_filtered += 1
                continue

        is_video = os.path.splitext(f)[1].lower() in timecore.VIDEO_EXT
        if use_index and not overwrite_existing and not fix_bad and fileindex.ai_known(f):
            skipped_done += 1                # the index knows this file has a description and its signature is unchanged (the file stays master)
            continue
        existing = aicore.get_existing_ai_description(f)
        bad_reason = ""
        if fix_bad and existing:
            bad_reason = aicore.ai_description_problem(existing, strict=True)
            if bad_reason:
                existing = ""            # unusable -> counts as missing, gets regenerated

        if not overwrite_existing and existing:
            skipped_done += 1
            if not bad_reason:
                fileindex.ai_remember(f)
            continue

        geo_ctx = aicore.build_ai_geo_context_from_existing(f)
        extra, ctx_fields = geoservice.context_info(f)
        rows.append({
            "path": f, "filename": os.path.basename(f), "is_video": is_video,
            "existing_description": existing, "geo_context": geo_ctx, "bad_reason": bad_reason,
            "extra_context": extra, "context_items": context_items(ctx_fields),
            "rel_dir": pathsafety.rel_subdir(path, f),
        })
    else:
        job.done = len(candidates)

    if job.cancel_event.is_set():
        return {}

    form_state = {
        "path": path, "root_path": root_path, "depth": depth, "type_filter": type_filter,
        "overwrite_existing": overwrite_existing, "embed_in_image": embed_in_image,
        "camera_model": camera_model,
    }
    return {
        "rows": rows, "form_state": form_state, "error": None, "skipped_done": skipped_done,
        "camera_model": camera_model, "camera_filtered_count": camera_filtered,
        "all_rows_json": json.dumps(rows),
        "step": step.to_dict(), "step_url": "/ai/browse/start", "step_json": json.dumps(step.to_dict()),
    }


@router.post("/browse/start", response_class=HTMLResponse)
def browse_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
    depth: int = Form(0),
    type_filter: str = Form("ALL"),
    overwrite_existing: bool = Form(False),
    embed_in_image: bool = Form(False),
    camera_model: str = Form(""),
    handoff_id: str = Form(""),
    scan_id: str = Form(""),
    offset: int = Form(0),
):
    """Phase 1: local-only scan (no AI provider call). For each candidate file, reads
    whatever's already on disk -- the existing AI description (if any; see
    aicore.get_existing_ai_description, which treats a garbled one the same as missing)
    and the existing location fields, formatted into the same geo-context string the
    prompt uses. In fill-missing mode (default, overwrite_existing=False) a file that
    already has a usable description is excluded from the list entirely, same as GPS
    Sidecar's "Bereits abgeschlossene überspringen". In force-regenerate mode every
    matching file is listed instead, existing description shown for reference."""
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    root_folder = pathsafety.root_for_path(root_path)
    if root_folder is not None:
        type_filter = root_folder.type_filter

    if not os.path.exists(path):
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Pfad nicht gefunden: {path}", "rows": [], "skipped_done": 0,
        })

    depth = max(0, min(7, depth))

    # A handoff_id (from "Weiter zu AI-Beschreibung") carries the EXACT files the
    # previous module's execute step just touched -- use those directly instead of
    # walking the whole folder, same rationale as GPS Sidecar's own browse_start()
    # (see core/handoff.py). Falls back to the normal folder walk when there's no
    # handoff_id, or it's expired/invalid.
    step = stepper.resume("ai", scan_id, offset)
    if step is not None:
        candidates = step.paths
    else:
        handoff_entry = handoff.fetch(handoff_id)
        from_handoff = handoff_entry is not None
        if handoff_entry is not None:
            candidates = [
                p for p in handoff_entry["file_paths"]
                if os.path.exists(p) and (
                    (ext := os.path.splitext(p)[1].lower()) in timecore.VIDEO_EXT
                    or ext in _AI_IMAGE_EXT
                ) and (
                    type_filter == "ALL"
                    or (type_filter == "VIDEO" and ext in timecore.VIDEO_EXT)
                    or (type_filter == "IMAGE" and ext in _AI_IMAGE_EXT)
                )
            ]
        else:
            candidates = _candidate_files(path, depth, type_filter)
        step = stepper.start("ai", candidates, unlimited=from_handoff)
        candidates = step.paths

    camera_model = camera_model.strip()
    job = jobs.create_job()
    job.total = len(candidates)
    job.phase = "Vorhandene Beschreibungen prüfen"


    jobs.run_job(job, lambda: browse_start_work(job, camera_model=camera_model, candidates=candidates, depth=depth, embed_in_image=embed_in_image, overwrite_existing=overwrite_existing, path=path, root_path=root_path, step=step, type_filter=type_filter))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/ai/browse/progress/{job.id}", "cancel_url": f"/ai/browse/cancel/{job.id}",
    })


@router.get("/browse/progress/{job_id}", response_class=HTMLResponse)
def browse_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [], "skipped_done": 0,
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/ai/browse/progress/{job.id}", "cancel_url": f"/ai/browse/cancel/{job.id}",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": None, "rows": [], "skipped_done": 0,
            "cancelled_info": f"Abgebrochen nach {job.done} von {job.total} Datei(en).",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Fehler beim Scannen: {job.error}", "rows": [], "skipped_done": 0,
        })
    # done -- shows the scan results as a selectable list; AI calls only start once
    # "Beschreibung generieren" is clicked (see /generate/start below).
    return templates.TemplateResponse(request, "_scan_list.html", job.result or {})


@router.post("/browse/cancel/{job_id}")
def browse_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return Response(status_code=204)


def generate_start_work(job, embed_in_image, path, root_path, scanned_rows, selected_set, step_ctx, to_process):
    """Background work of generate_start (extracted so the Jahreslauf can call it without a request)."""
    ai_cfg = settings.load_ai_settings()
    rows = []
    for i, r in enumerate(to_process):
        if job.cancel_event.is_set():
            break
        job.done, job.current_name = i, r.get("filename", "")

        # '' unless AI_CONTEXT=true and GEO_SERVICE_URL is set: time of day / weather / trip
        # name on top of the place already in geo_context (see core/geoservice.py).
        extra, ctx_fields = geoservice.context_info(r["path"])
        # only passed when there is something to pass, so the call is byte-for-byte the old one otherwise
        result = aicore.generate_description(r["path"], r.get("geo_context", ""), ai_cfg, r.get("is_video", False),
                                             **({"extra_context": extra} if extra else {}))
        warn = ""
        if not result.description:
            warn = "; ".join(result.failures) or "Unbekannter Fehler bei der KI-Beschreibung."

        rows.append({
            **r, "new_description": result.description, "provider": result.provider,
            "model": result.model, "warn": warn, "extra_context": extra, "context_fields": ctx_fields,
            "context_items": context_items(ctx_fields),
        })
    else:
        job.done = len(to_process)

    if job.cancel_event.is_set():
        # Hands the whole scanned set back as an editable selection list, not an
        # error page -- whatever was already generated before Abbrechen is simply
        # not kept, but nothing needs re-scanning: adjust the selection and click
        # "Beschreibung generieren" again.
        return {
            "rows": scanned_rows, "error": None, "skipped_done": 0,
            "all_rows_json": json.dumps(scanned_rows), "selected_paths": selected_set,
            "form_state": {"embed_in_image": embed_in_image, "path": path, "root_path": root_path},
            "notice": f"⏹ Generierung abgebrochen nach {job.done} von {job.total} Datei(en). "
                      f"Auswahl ggf. anpassen und „Beschreibung generieren“ erneut klicken.",
            **step_ctx,
        }

    return {
        "rows": rows, "error": None, "all_rows_json": json.dumps(rows),
        "form_state": {"embed_in_image": embed_in_image, "path": path, "root_path": root_path},
        **step_ctx,
    }


@router.post("/generate/start", response_class=HTMLResponse)
def generate_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_scanned_rows: str = Form(...),
    embed_in_image: bool = Form(False),
    path: str = Form(""),
    root_path: str = Form(""),
    step_json: str = Form(""),
):
    """Phase 2: the expensive step -- one AI-provider call per still-selected file
    (Ollama and/or Gemini, in AI_PROVIDER's configured order, falling through on
    failure). Everything else (existing description, geo-context) was already resolved
    locally in /browse/start, so a deselected file costs nothing here at all."""
    try:
        scanned_rows = json.loads(all_scanned_rows)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")

    try:
        step_d = json.loads(step_json) if step_json else None
    except json.JSONDecodeError:
        step_d = None
    step_ctx = {"step": step_d, "step_url": "/ai/browse/start", "step_json": step_json}
    selected_set = set(selected)
    to_process = [r for r in scanned_rows if r["path"] in selected_set]

    job = jobs.create_job()
    job.total = len(to_process)
    job.phase = "Beschreibung generieren"


    jobs.run_job(job, lambda: generate_start_work(job, embed_in_image=embed_in_image, path=path, root_path=root_path, scanned_rows=scanned_rows, selected_set=selected_set, step_ctx=step_ctx, to_process=to_process))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/ai/generate/progress/{job.id}", "cancel_url": f"/ai/generate/cancel/{job.id}",
    })


@router.get("/generate/progress/{job_id}", response_class=HTMLResponse)
def generate_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [],
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/ai/generate/progress/{job.id}", "cancel_url": f"/ai/generate/cancel/{job.id}",
        })
    if job.status == "cancelled":
        # job.result was still populated on cancel (see work() above) -- an editable
        # scan list, not a dead end.
        return templates.TemplateResponse(request, "_scan_list.html", job.result or {})
    if job.status == "error":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Fehler bei der Generierung: {job.error}", "rows": [],
        })
    # done
    return templates.TemplateResponse(request, "_file_list.html", job.result or {})


@router.post("/generate/cancel/{job_id}")
def generate_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return Response(status_code=204)


_CTX_LABELS = (("daypart", "Tageszeit"), ("local_time", "Uhrzeit"), ("weekday", "Wochentag"),
               ("month", "Monat"), ("year", "Jahr"), ("season", "Jahreszeit"),
               ("weather", "Wetter"), ("temp_c", "Temperatur"), ("elevation", "H\u00f6he"), ("timezone", "Zeitzone"), ("trip", "Wo wars"),
               ("daylight", "Licht"), ("moon", "Mond"), ("holiday", "Feiertag"), ("wind_text", "Wind"), ("precip_mm", "Niederschlag"),
               ("pois", "In der N\u00e4he"), ("country", "Land"), ("clock", "Kamera-Uhr"))


def context_items(fields: dict) -> list:
    """[(label, value)] of every context field that is passed to the prompt / written to the sidecar."""
    out = []
    for k, label in _CTX_LABELS:
        v = (fields or {}).get(k)
        if v in (None, ""):
            continue
        if k == "local_time" and "T" in str(v):      # ISO -> HH:MM
            v = str(v).split("T", 1)[1][:5]
        out.append((label, f"{v} \u00b0C" if k == "temp_c" else f"{int(float(v))} m" if k == "elevation" else f"{v} mm" if k == "precip_mm" else str(v)))
    return out


def _parse_rows(all_rows: str):
    try:
        return json.loads(all_rows)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")


def _execute_one(row, embed_in_image) -> dict:
    file = row["path"]
    pathsafety.check_allowed(file)
    description = row.get("new_description")
    is_video = bool(row.get("is_video"))
    xmp_path = file + ".xmp"
    result = {
        "path": file, "xmp_written": False, "xmp_error": "", "image_written": False, "image_error": "",
        "is_video": is_video,
    }
    if not os.path.exists(file):
        result["xmp_error"] = "Datei nicht mehr gefunden -- Vorschau neu laden."
        return result
    ctx_fields = row.get("context_fields") or {}
    write_ctx = bool(ctx_fields) and geoservice.xmp_context_enabled()
    if not description and not write_ctx:
        result["xmp_error"] = "Keine generierte Beschreibung vorhanden -- Vorschau neu laden."
        return result

    sc_ok, sc_msg = geocore.ensure_xmp_sidecar_ex(file, xmp_path, is_video)
    if sc_ok:
        if description:
            ok, msg = aicore.write_description_to_xmp(xmp_path, description)
            result["xmp_written"] = ok
            if not ok:
                result["xmp_error"] = msg or "Unbekannter Fehler beim Schreiben des .xmp-Sidecars."
        if write_ctx:       # independent of the AI text: also stored when the description failed/was skipped
            ok, msg = fillcore.write_context_fields(xmp_path, ctx_fields)
            result["context_written"] = ok
            if not ok:
                result["xmp_error"] = (result["xmp_error"] + " " if result["xmp_error"] else "") + (msg or "Kontext-Felder nicht geschrieben.")
            elif not description:
                result["xmp_written"] = True
    else:
        result["xmp_error"] = "Konnte XMP-Sidecar nicht erstellen/aktualisieren" + (f": {sc_msg}" if sc_msg else ".")

    if not is_video and embed_in_image:
        # Embeds the full CURRENT .xmp snapshot (description, AND GPS+Location if GPS Sidecar
        # already wrote them) -- see xmpsync.py. Default ON here: for a photo this is
        # normally "der allerletzte Schritt", so the one direct-image write happens here.
        ok, msg = xmpsync.sync_xmp_to_image(file, xmp_path)
        result["image_written"] = ok
        if not ok:
            result["image_error"] = msg or "Unbekannter Fehler beim direkten Einbetten."
    return result


def _run_execute(rows, selected_set, embed_in_image, handoff_path, handoff_root_path, job=None) -> dict:
    todo = [r for r in rows if r["path"] in selected_set]
    if job is not None:
        job.total, job.done = len(todo), 0
    results = []
    cancelled = False
    for row in todo:
        if job is not None:
            if job.cancel_event.is_set():
                cancelled = True
                break
            job.current_name = os.path.basename(row["path"])
        results.append(_execute_one(row, embed_in_image))
        if job is not None:
            job.done += 1
    # "Weiter zu NFO Maker" only makes sense when at least one video was processed.
    has_video = any(r["is_video"] for r in results)

    # Every write went through exiftool -overwrite_original (mtime = "now"): opt-in restore
    # button on the results screen, or automatic with RESTORE_TIMESTAMP=true.
    written_paths = []
    for r in results:
        if r["xmp_written"]:
            written_paths.append(r["path"] + ".xmp")
        if r["image_written"]:
            written_paths.append(r["path"])
    auto_mtime_results = None
    if written_paths and settings.restore_timestamp_enabled():
        auto_mtime_results = [r.__dict__ for r in mtimecore.restore_many(written_paths)]

    # NFO Maker only ever wants the VIDEO files actually written (see core/handoff.py).
    video_touched_paths = [r["path"] for r in results if r["xmp_written"] and r["is_video"]]
    handoff_id = handoff.stash(handoff_path, handoff_root_path, video_touched_paths) if video_touched_paths else ""
    ctx = {
        "results": results, "written_paths": written_paths, "auto_mtime_results": auto_mtime_results,
        "has_video": has_video, "handoff_path": handoff_path, "handoff_root_path": handoff_root_path,
        "handoff_id": handoff_id,
    }
    if cancelled:
        ctx["cancelled_info"] = (f"Abgebrochen nach {len(results)} von {len(todo)} Datei(en) "
                                 f"– bereits geschriebene Dateien bleiben geändert.")
    return ctx


def _exec_progress_ctx(job) -> dict:
    return {"job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/ai/execute/progress/{job.id}", "cancel_url": f"/ai/execute/cancel/{job.id}"}


@router.post("/execute/start", response_class=HTMLResponse)
def execute_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_rows: str = Form(...),
    embed_in_image: bool = Form(False),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
):
    rows = _parse_rows(all_rows)
    selected_set = set(selected)
    for r in rows:
        if r["path"] in selected_set:
            pathsafety.check_allowed(r["path"])
    job = jobs.create_job()
    job.total = sum(1 for r in rows if r["path"] in selected_set)
    job.phase = "Schreiben"
    jobs.run_job(job, lambda: _run_execute(rows, selected_set, embed_in_image,
                                           handoff_path, handoff_root_path, job))
    return templates.TemplateResponse(request, "_progress.html", _exec_progress_ctx(job))


@router.get("/execute/progress/{job_id}", response_class=HTMLResponse)
def execute_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return HTMLResponse('<div class="error">Vorgang nicht gefunden (abgelaufen oder Server neu gestartet). '
                            'Bitte Vorschau neu laden – bereits geschriebene Dateien bleiben geändert.</div>')
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", _exec_progress_ctx(job))
    if job.status == "error":
        return HTMLResponse(f'<div class="error">Fehler beim Schreiben: {html.escape(job.error)}</div>')
    return templates.TemplateResponse(request, "_results.html", job.result or {})


@router.post("/execute/cancel/{job_id}")
def execute_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


@router.post("/execute", response_class=HTMLResponse)
def execute(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_rows: str = Form(...),
    embed_in_image: bool = Form(False),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
):
    """Synchronous variant (scripts/tests); the UI uses /execute/start."""
    ctx = _run_execute(_parse_rows(all_rows), set(selected), embed_in_image, handoff_path, handoff_root_path)
    return templates.TemplateResponse(request, "_results.html", ctx)


mtime_routes.add_restore_routes(router, templates, "/ai", lambda p: pathsafety.check_allowed(p))


@router.post("/restore-mtime", response_class=HTMLResponse)
def restore_mtime(request: Request, paths: list[str] = Form(default=[])):
    """Explicit opt-in step after "Ausführen": resets each written file's (and/or its
    .xmp sidecar's) mtime back to its real capture time, undoing the "now" timestamp
    exiftool -overwrite_original left behind. See core/mtimecore.py's module docstring
    for why this module needs it at all (unlike Timestamp & Rename)."""
    for p in paths:
        pathsafety.check_allowed(p)
    results = mtimecore.restore_many(paths)
    return templates.TemplateResponse(request, "_mtime_results.html", {
        "results": [r.__dict__ for r in results],
    })
