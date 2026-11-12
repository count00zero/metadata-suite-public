"""
router.py -- the "Kontext nachfüllen" web module: finds photos/videos whose .xmp sidecar misses data that newer versions of
the suite write (height & time zone, weather, daylight/moon, date/trip, IPTC credits, the NFO's context lines) and fills
only what is missing from the geo-service.

Phase 1 /scan/start reads ONLY the .xmp sidecars (and a video's .nfo text), not the media files. Phase 2 /apply fills the
selected files with the chosen groups (engine: core/ctxfill.py) and can embed the result into photos (altitude + IPTC)
and patch existing .nfo files. Stepped loading like the other list modules (BATCH_SIZE_CTXFILL).
"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...core import fileindex, ctxfill, fillcore, geoservice, handoff, jobs, mtimecore, pathsafety, settings, stepper

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = "", handoff_id: str = ""):
    # "Weiter zu Kontext nachfüllen" from GPS Sidecar carries the exact files it just wrote (core/handoff.py)
    entry = handoff.fetch(handoff_id)
    if entry is not None:
        path, root_path = entry["path"], entry["root_path"]
    else:
        handoff_id = ""
    return templates.TemplateResponse(request, "index.html", {
        "prefill_handoff_id": handoff_id,
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": [rf.to_dict() for rf in settings.load_root_folders()],
        "prefill_path": path, "prefill_root_path": root_path,
        "service_url": geoservice.url(), "service_enabled": geoservice.enabled(),
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def _err(request: Request, msg: str):
    return templates.TemplateResponse(request, "_scan_list.html", {"error": msg, "rows": []})


def _progress(request: Request, job, kind: str, label: str):
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(), "label": label,
        "poll_url": f"/ctxfill/{kind}/progress/{job.id}", "cancel_url": f"/ctxfill/scan/cancel/{job.id}"})


def scan_start_work(job, path, paths, step, refresh=False, use_index=True, root_path="", carry=False):
    """Background work of scan_start (extracted so the Jahreslauf can call it without a request).
    carry: the files came from the previous module's handoff -> ALL of them (also those that need nothing here) go on to
    "AI-Beschreibung" afterwards, not only the ones filled in this step.
    use_index: results of earlier scans come from the SQLite index while the sidecar's stat signature is unchanged (the file stays the
    master); refresh / recheck read every sidecar again. Results are stored either way."""
    def prog(done, total):
        job.done, job.total = done, max(1, total)

    pv = geoservice.place_version()
    env = ctxfill.env_sig(pv)
    video_ext = fillcore.VIDEO_EXT
    cached: dict = {}
    if use_index and not refresh:
        for f in paths:
            hit = fileindex.ctx_get(f, os.path.splitext(f)[1].lower().lstrip(".") in video_ext, env)
            if hit is not None:
                cached[f] = hit
    todo = [f for f in paths if f not in cached]
    sidecars = fillcore.read_sidecars(todo, job.cancel_event) if todo else {}      # positions of the .xmp files (one pooled exiftool call per 100)
    counts = {"files": len(paths), "no_xmp": 0, "no_pos": 0, "complete": 0, "from_index": len(cached)}
    per_group: dict = {}
    rows = []
    to_store = []
    for i, f in enumerate(paths):
        if job.cancel_event.is_set():
            return {}
        job.done, job.current_name = i, os.path.basename(f)
        st = cached.get(f)
        if st is None:
            sc = sidecars.get(f)
            st = ctxfill.inspect(f, sc.lat if sc else None, sc.lon if sc else None, pv)
            st["lat"], st["lon"] = (sc.lat, sc.lon) if sc else (None, None)
            to_store.append((f, os.path.splitext(f)[1].lower().lstrip(".") in video_ext, env, st))
        if not st["has_xmp"]:
            counts["no_xmp"] += 1
            continue
        if not st["has_pos"]:
            counts["no_pos"] += 1
            continue
        if not st["missing"]:
            counts["complete"] += 1
            if not refresh:
                continue
        for g in st["missing"]:
            per_group[g] = per_group.get(g, 0) + 1
        rows.append({"path": f, "filename": os.path.basename(f), "missing": st["missing"],
                     "stale": st.get("stale", False), "missing_labels": [ctxfill.GROUPS[g][0] for g in st["missing"]] or ["nichts – zum Auffrischen angeboten"],
                     "is_video": os.path.splitext(f)[1].lower().lstrip(".") in fillcore.VIDEO_EXT,
                     "lat": st.get("lat"), "lon": st.get("lon"), "rel_dir": pathsafety.rel_subdir(path, f)})
    fileindex.ctx_put_many([(f, v, e, {k: (sorted(x) if isinstance(x, set) else x) for k, x in info.items()}) for f, v, e, info in to_store])
    job.done = job.total
    carry_id = handoff.stash(path, root_path, [f for f in paths if os.path.exists(f)]) if carry and paths else ""
    return {"rows": rows, "error": None, "counts": counts, "carry_id": carry_id, "handoff_path": path, "handoff_root_path": root_path,
            "per_group": [(g, ctxfill.GROUPS[g][0], per_group[g]) for g in ctxfill.GROUPS if g in per_group],
            "groups": [(g, v[0]) for g, v in ctxfill.GROUPS.items()],
            "step": step.to_dict(), "step_url": "/ctxfill/scan/start", "service_enabled": geoservice.enabled()}


@router.post("/scan/start", response_class=HTMLResponse)
def scan_start(request: Request, path: str = Form(...), root_path: str = Form(""), depth: int = Form(3),
               scan_id: str = Form(""), offset: int = Form(0), refresh: str = Form(""), handoff_id: str = Form("")):
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    depth = max(0, min(7, depth))
    root_folder = pathsafety.root_for_path(root_path)
    types = {"VIDEO": "video", "IMAGE": "image"}.get(root_folder.type_filter if root_folder else "ALL", "all")
    if not os.path.exists(path):
        return _err(request, f"Pfad nicht gefunden: {path}")

    step = stepper.resume("ctxfill", scan_id, offset)
    carry = False
    if step is None:
        entry = handoff.fetch(handoff_id)
        if entry is not None:
            # exactly the files the previous module wrote, all in ONE step (already chosen there -> no "Nächste laden")
            def kind_ok(p):
                ext = os.path.splitext(p)[1].lower().lstrip(".")
                return ext in fillcore.VIDEO_EXT and types != "image" or ext in fillcore.IMAGE_EXT and types != "video"
            cand = [p for p in entry["file_paths"] if os.path.exists(p) and kind_ok(p)]
            step, carry = stepper.start("ctxfill", cand, unlimited=True), True
        else:
            step = stepper.start("ctxfill", fillcore.find_media(path, depth, types))
    paths = step.paths

    job = jobs.create_job()
    job.total, job.phase = max(1, len(paths)), "Sidecars lesen"


    jobs.run_job(job, lambda: scan_start_work(job, path=path, paths=paths, step=step, refresh=bool(refresh), root_path=root_path, carry=carry))
    return _progress(request, job, "scan", "Datei(en)")


@router.get("/scan/progress/{job_id}", response_class=HTMLResponse)
def scan_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _err(request, "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.")
    if job.status == "running":
        return _progress(request, job, "scan", "Datei(en)")
    if job.status == "error":
        return _err(request, f"Fehler beim Scannen: {job.error}")
    if job.status == "cancelled" or not job.result:
        return templates.TemplateResponse(request, "_scan_list.html", {"rows": [], "cancelled_info": "Scan abgebrochen."})
    return templates.TemplateResponse(request, "_scan_list.html", job.result)


@router.post("/scan/cancel/{job_id}")
def scan_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


def apply_work(job, chosen, embed, gset, overwrite, carry_id="", path="", root_path=""):
    """Background work of apply (extracted so the Jahreslauf can call it without a request)."""
    results, touched = [], []
    for i, p in enumerate(chosen):
        if job.cancel_event.is_set():
            break
        job.done, job.current_name = i, os.path.basename(p)
        r = ctxfill.fill_one(p, gset, bool(overwrite), bool(embed))
        r["filename"] = os.path.basename(p)
        touched += [t for t in r.pop("touched") if t != p + ".xmp"]
        results.append(r)
    job.done = job.total
    if touched and settings.restore_timestamp_enabled():
        mtimecore.restore_many(touched)
    # "Weiter zu AI-Beschreibung": all files that came in via the handoff, otherwise the ones filled here
    entry = handoff.fetch(carry_id)
    onward = [p for p in entry["file_paths"] if os.path.exists(p)] if entry else [r["path"] for r in results if r["ok"]]
    next_id = handoff.stash(path, root_path, onward) if onward else ""
    return {"results": results, "error": None, "handoff_id": next_id, "handoff_path": path, "handoff_root_path": root_path,
            "ok_n": sum(1 for r in results if r["ok"]), "err_n": sum(1 for r in results if not r["ok"]),
            "added_n": sum(1 for r in results if r["added"]), "nfo_n": sum(1 for r in results if r["nfo"]),
            "image_n": sum(1 for r in results if r["image"]),
            "cancelled": job.cancel_event.is_set(), "done_at": datetime.now().strftime("%H:%M:%S")}


@router.post("/apply", response_class=HTMLResponse)
def apply(request: Request, selected: list[str] = Form(default=[]), groups: list[str] = Form(default=[]),
          overwrite: str = Form(""), embed: str = Form(""), carry_id: str = Form(""),
          handoff_path: str = Form(""), handoff_root_path: str = Form("")):
    if not geoservice.enabled():
        return templates.TemplateResponse(request, "_results.html", {
            "error": "GEO_SERVICE_URL ist nicht gesetzt – der Kontext kommt vom geo-service."})
    chosen = []
    for p in selected:
        try:
            pathsafety.check_allowed(p)
        except HTTPException:
            continue
        chosen.append(p)
    gset = {g for g in groups if g in ctxfill.GROUPS}
    if not chosen or not gset:
        return templates.TemplateResponse(request, "_results.html", {"error": "Nichts ausgewählt (Dateien und mindestens eine Gruppe)."})

    job = jobs.create_job()
    job.total, job.phase = max(1, len(chosen)), "Kontext nachfüllen"


    jobs.run_job(job, lambda: apply_work(job, chosen=chosen, embed=embed, gset=gset, overwrite=overwrite, carry_id=carry_id,
                                         path=handoff_path, root_path=handoff_root_path))
    return _progress(request, job, "apply", "Datei(en)")


@router.get("/apply/progress/{job_id}", response_class=HTMLResponse)
def apply_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_results.html", {"error": "Vorgang nicht gefunden."})
    if job.status == "running":
        return _progress(request, job, "apply", "Datei(en)")
    if job.status == "error":
        return templates.TemplateResponse(request, "_results.html", {"error": f"Fehler: {job.error}"})
    return templates.TemplateResponse(request, "_results.html", job.result or {"results": [], "ok_n": 0, "err_n": 0})
