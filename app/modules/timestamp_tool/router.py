"""
router.py — the Timestamp-Bilder-Videos web module: browse a folder, preview resolved
timestamps/renames for each file, select which to apply, execute.

Mounted under /timestamp by app/main.py. This is the first of "several modules" meant to
live in one container/app; future tools (GPS sidecar writer, NFO maker, collage generator)
would get their own sibling router under modules/ and their own prefix in main.py.
"""
from __future__ import annotations

import io
import os
import html
import json
from dataclasses import fields as dc_fields
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from ...core import mtime_routes, timecore, settings, pathsafety, jobs, mtimecore, handoff, geocore, devicenames, stepper, rotatecore
from ...core.executor import apply_resolution, apply_companion

try:
    from PIL import Image
except ImportError:
    Image = None

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()

# Path-containment checks (MEDIA_ROOT fence, Basis-Ordner containment, type-filter lookup,
# sub-folder suggestions) live in core.pathsafety now, shared with the GPS Sidecar module.
MEDIA_ROOT = pathsafety.MEDIA_ROOT
_check_allowed = pathsafety.check_allowed
_check_within_root = pathsafety.check_within_root
_root_for_path = pathsafety.root_for_path

_RES_INIT_FIELDS = {f.name for f in dc_fields(timecore.Resolution) if f.init}


def _resolution_from_dict(d: dict) -> timecore.Resolution:
    return timecore.Resolution(**{k: v for k, v in d.items() if k in _RES_INIT_FIELDS})


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    presets = [p.to_dict() for p in settings.load_presets()]
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    return templates.TemplateResponse(request, "index.html", {
        "presets": presets,
        "media_root": MEDIA_ROOT,
        "root_folders": root_folders,
        "timezonefinder_available": timecore.timezonefinder_available(),
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    """Datalist suggestions for the Pfad field (see core.pathsafety.list_subfolders)."""
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def _rows_from_resolutions(resolutions: list, base_path: str) -> list:
    """One row per resolved primary file, plus one companion row for every OTHER file in
    the same folder that belongs to the same capture (its .xmp sidecar, a companion audio
    track, a generated -thumb/-poster/-fanart image, or any other same-stem sibling --
    see timecore.find_companions()). A companion never gets its own independent timestamp
    resolution (a sidecar or a thumbnail has no EXIF of its own worth trusting more than
    its primary file's) -- it simply follows the primary's already-resolved target
    timestamp/epoch, keeping its own suffix/extension, and is shown as its own selectable
    row so renaming the primary never silently leaves one of these behind under the OLD
    filename.

    base_path is the folder actually typed into "Pfad" (not the Basis-Ordner) -- every
    row's rel_dir is that file's subfolder relative to it, so when Tiefe > 0 actually
    found something below base_path, the preview can show which subfolder each file came
    from (pathsafety.rel_subdir() -- empty for a file directly in base_path itself)."""
    primary_paths = {res.path for res in resolutions}
    rows = []
    for res in resolutions:
        d = res.to_dict()
        d["row_type"] = "primary"
        cam = devicenames.translate(res.camera_make, res.camera_model)
        d["camera_label"] = cam["label"]
        d["camera_raw"] = cam["raw"]
        d["camera_label_source"] = devicenames.SOURCE_LABELS.get(cam["source"], "")
        d["camera_label_is_raw"] = cam["source"] in (devicenames.SOURCE_RAW, "")
        d["rel_dir"] = pathsafety.rel_subdir(base_path, res.path)
        d["companions"] = []
        rows.append(d)

        stem = os.path.splitext(os.path.basename(res.path))[0]
        for comp_path in timecore.find_companions(res.path):
            if comp_path in primary_paths:
                # Also independently resolved as its own primary row (e.g. a "clip.jpg"
                # still sharing a stem with "clip.avi") -- its own EXIF-based resolution
                # is more trustworthy than blindly copying this file's, so it's left as
                # a standalone row rather than attached here too.
                continue
            comp_name = os.path.basename(comp_path)
            # Every pattern find_companions() matches is built as stem+something, so
            # slicing off the stem's length recovers exactly the right trailing piece to
            # re-attach to the NEW stem -- ".mp4.xmp" for a sidecar, "-thumb.jpg" for a
            # generated image, ".aac" for companion audio, or a plain ".jpg".
            suffix = comp_name[len(stem):]
            target_name = f"{res.local_str}{res.name_suffix}{suffix}"
            if target_name == comp_name:
                # Nothing would actually happen to this companion (it's only ever
                # renamed/touched to follow its primary's NEW stem -- see
                # executor.apply_companion -- never exiftool-written on its own), which is
                # exactly the case whenever the primary itself isn't being renamed either
                # (any "Umbenannte einblenden" / camera-filter re-check of an
                # already-correctly-named file, where the primary's own target_name also
                # already matches). Showing it anyway used to flood the preview with
                # every .xmp/.nfo/poster/fanart/thumb companion of EVERY revealed
                # already-named file -- the maintainer's "reads/shows jpg, xmp, nfo" report, with
                # a VIDEOS-only Basis-Ordner where none of those extensions could ever
                # have been independent primary candidates themselves, so this is the
                # only place they could have been coming from. A companion that WOULD
                # change (primary is actually being renamed) still shows, same as before.
                continue
            d["companions"].append({"name": comp_name, "target": target_name})
            rows.append({
                "row_type": "companion",
                "path": comp_path,
                "filename": comp_name,
                "primary_path": res.path,
                "dir": res.dir,
                "target_name": target_name,
                "target_path": os.path.join(res.dir, target_name),
                "will_rename": target_name != comp_name,
                "epoch": res.epoch,
                "source_display": f"Begleitdatei zu {os.path.basename(res.path)}",
                "rel_dir": d["rel_dir"],
            })
    return rows


@router.post("/browse/start", response_class=HTMLResponse)
def browse_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
    depth: int = Form(0),
    mode: str = Form("EXIF"),
    default_tz: str = Form("Europe/Vienna"),
    force_zone: bool = Form(False),
    time_shift: str = Form(""),
    gps_filter: str = Form("ALL"),
    type_filter: str = Form("ALL"),
    do_rename: bool = Form(False),
    do_exifwrite: bool = Form(False),
    include_already_named: bool = Form(False),
    camera_model: str = Form(""),
    only_needs_backwrite: bool = Form(False),
    only_needs_fix: bool = Form(False),
    only_needs_rename: bool = Form(False),
    do_xmpdate: bool = Form(False),
    do_rotate: bool = Form(False),
    rotate_backup: bool = Form(False),
    opts_set: bool = Form(False),
    scan_id: str = Form(""),
    offset: int = Form(0),
):
    """Kicks off a browse/scan as a background job instead of blocking the request until
    every file's exiftool call is done -- returns immediately with a progress fragment
    that polls itself (see /browse/progress) and offers a real Cancel button."""
    _check_allowed(path)
    _check_within_root(path, root_path)

    if not opts_set:
        # First load: the execute options live in the list (bottom toolbar); defaults.
        do_rename, do_xmpdate, do_exifwrite, do_rotate = True, True, False, True
    camera_model = camera_model.strip()
    if camera_model:
        # Re-checking files from one specific camera (e.g. "Insta360 Luna Ultra" or
        # "Osmo Action 6" after the CreateDate-for-photos timezone fix) is exactly the
        # case where a file can already be named in the correct YYYY.MM.DD-HH.MM.SS
        # PATTERN (from an earlier, wrongly-resolved run) while its actual timestamp is
        # still wrong -- is_already_named() only checks the pattern, not correctness, so
        # such a file would otherwise stay hidden unless "Umbenannte einblenden" is also
        # ticked by hand. A camera filter implies that intent, so it's forced on here.
        include_already_named = True
    if only_needs_backwrite or only_needs_fix or only_needs_rename:
        # Needs the already-named files too -- they are exactly the ones that matter here.
        include_already_named = True
    # The Basis-Ordner determines the file type (VIDEOS -> VIDEO, PHOTOS -> IMAGE); the
    # server re-derives it from root_path itself rather than trusting the submitted
    # type_filter, which is only a UI convenience value set by the root-folder dropdown.
    root_folder = _root_for_path(root_path)
    if root_folder is not None:
        type_filter = root_folder.type_filter

    if not os.path.exists(path):
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Pfad nicht gefunden: {path}",
            "rows": [], "form_state": locals(), "already_named_count": 0,
        })

    try:
        time_shift_s = timecore.parse_shift(time_shift)
    except ValueError as e:
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": str(e), "rows": [], "form_state": locals(), "already_named_count": 0,
        })

    depth = max(0, min(7, depth))
    # Cheap pass (filename regex + os.walk only, no exiftool) just to know how many files
    # will actually need the slow per-file work, so the progress bar has a real total from
    # its very first render instead of starting at "0 / 0" until the first file finishes.
    step = stepper.resume("timestamp", scan_id, offset)
    if step is not None:
        # "Nächste N laden": continue from the snapshot of the first scan (no re-walk)
        candidate_paths = step.paths
        already_named_count = step.meta.get("already_named_count", 0)
    else:
        candidate_paths, already_named_count = timecore.scan_candidates(
            path, depth, type_filter, include_already_named,
        )
        # A "<stem>-thumb/-poster/-fanart.jpg" generated image (AI Description's persisted
        # video frame, or one the NAS toolchain's NFO writer left behind) technically has a
        # recognized image extension, so scan_candidates would otherwise treat it as its own
        # independent candidate -- resolving ITS OWN (meaningless) EXIF/filename as if it were
        # a separate capture. When the video/photo it actually belongs to is also sitting in
        # this folder, pull it out of independent resolution here; it's attached instead as
        # that file's own companion row (see _rows_from_resolutions/timecore.find_companions),
        # so it only ever moves in lockstep with its real media file. An ORPHANED thumbnail
        # (its video/photo missing) is left as an independent candidate, same as before, so it
        # never just silently disappears from the preview.
        # Same exclusion for a ".nfo" file (NFO Maker's own output, always named with the
        # EXACT same stem as its video, never a suffix) -- it has no capture-time EXIF of its
        # own worth resolving either, and "Umbenannte einblenden" bypassing is_already_named()
        # used to let an already-correctly-named .nfo slip through scan_candidates(), get
        # independently "resolved" from mtime/filename, and -- because find_companions() only
        # attaches a file as a companion when it ISN'T also already its own primary row --
        # show up as its own meaningless row instead of staying attached under its video (the
        # bug the maintainer reported as "reads/shows jpg, xmp, nfo" when clicking that button). An
        # orphaned .nfo (its video missing) is still left as an independent candidate, same
        # convention as an orphaned thumbnail below.
        candidate_paths = [
            p for p in candidate_paths
            if not (
                (base_stem := timecore.generated_companion_base_stem(p)) is not None
                and timecore.has_sibling_media(p, base_stem)
            )
            and not (
                os.path.splitext(p)[1].lower() == ".nfo"
                and timecore.has_sibling_media(p, os.path.splitext(os.path.basename(p))[0])
            )
        ]
        step = stepper.start("timestamp", candidate_paths, {"already_named_count": already_named_count})
        candidate_paths = step.paths

    job = jobs.create_job()
    job.total = len(candidate_paths)

    def work():
        def progress_cb(done, total, name):
            job.done, job.total, job.current_name = done, total, name

        resolutions = timecore.resolve_batch(
            candidate_paths, mode, default_tz, gps_filter, type_filter,
            progress_cb=progress_cb, cancel_event=job.cancel_event, force_zone=force_zone,
            time_shift_s=time_shift_s,
        )
        # Same-second collisions (or an existing file with the target name) get _01, _02, ...
        timecore.assign_unique_targets(resolutions)
        camera_filtered_count = 0
        if camera_model:
            needle = camera_model.lower()
            kept = []
            for r in resolutions:
                # Matches the raw Make/Model AND the translated name (own rule / NFO Maker
                # rule / Google list), so "Luna Ultra", "SM-S921B", "S24" and "Galaxy" all work.
                haystack = (f"{r.camera_make} {r.camera_model} "
                            f"{devicenames.translate(r.camera_make, r.camera_model)['label']}").strip().lower()
                if needle in haystack:
                    kept.append(r)
                else:
                    camera_filtered_count += 1
            resolutions = kept
        # "Nur Dateien, die umbenannt werden müssen": after the full resolution (filename,
        # GPS, EXIF offset, ...) keep only files whose computed target name differs from the
        # current name -- this also catches already-pattern-named files with a wrong time.
        # Load filters are OR-combined (any ticked condition qualifies a file); none ticked =
        # everything. "rename" alone is decided before the (slower) diagnostics; combined with
        # the diagnostic-based ones it is applied together with them below.
        def _renames(r):
            return r.target_name != os.path.basename(r.path)
        rename_only = only_needs_rename and not (only_needs_fix or only_needs_backwrite)
        filter_dropped = 0
        if rename_only:
            kept = [r for r in resolutions if _renames(r)]
            filter_dropped = len(resolutions) - len(kept)
            resolutions = kept
        diag_by_path = {}
        # Per-file "is the local time really stored in the file / the .xmp?" check (one small
        # exiftool call per file, run in parallel). Always done now: the list's selection
        # buttons ("Nur umbenennen", "Nur .xmp anpassen", "Nur ohne Lokalzeit") need the flags
        # of every row, and the rows show where each date stands.
        if resolutions and not job.cancel_event.is_set():
            from concurrent.futures import ThreadPoolExecutor
            job.phase = "Prüfe .xmp / Datei-Datum"
            job.done, job.total = 0, len(resolutions)
            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(timecore.date_diagnostics, r): r for r in resolutions}
                for fut, r in futs.items():
                    if job.cancel_event.is_set():
                        break
                    diag_by_path[r.path] = fut.result()
                    job.done += 1
                    job.current_name = os.path.basename(r.path)
        if only_needs_backwrite or only_needs_fix:
            def _hit(r):
                d = diag_by_path.get(r.path, {})
                return bool((only_needs_rename and _renames(r))
                            or (only_needs_fix and d.get("needs_fix"))
                            or (only_needs_backwrite and d.get("needs_backwrite")))
            kept = [r for r in resolutions if _hit(r)]
            filter_dropped = len(resolutions) - len(kept)
            resolutions = kept
        rows = _rows_from_resolutions(resolutions, path)
        for row in rows:
            if row.get("row_type") == "primary" and row["path"] in diag_by_path:
                row["diag"] = diag_by_path[row["path"]]
        form_state = {
            "path": path, "root_path": root_path, "depth": depth, "mode": mode,
            "default_tz": default_tz, "force_zone": force_zone, "time_shift": time_shift, "gps_filter": gps_filter, "type_filter": type_filter,
            "do_rename": do_rename, "do_exifwrite": do_exifwrite, "do_xmpdate": do_xmpdate, "do_rotate": do_rotate, "rotate_backup": rotate_backup, "camera_model": camera_model,
            "only_needs_backwrite": only_needs_backwrite, "only_needs_fix": only_needs_fix,
            "only_needs_rename": only_needs_rename,
        }
        # Suggest a better Fallback-Zone from what the folder's own confidently-resolved
        # files show (real GPS, UTC-diff trick, or a borrowed sibling) -- only surfaced
        # when it's both different from what's set now and backed by more than a single
        # file, so a lone outlier doesn't prompt a pointless suggestion.
        suggested_tz = None
        tz_suggestion = timecore.suggest_fallback_zone(resolutions)
        if tz_suggestion:
            zone, count, confident_total = tz_suggestion
            if zone != default_tz and count > 1:
                suggested_tz = {"zone": zone, "count": count, "confident_total": confident_total}
        return {
            "rows": rows, "form_state": form_state, "error": None,
            "all_resolutions_json": json.dumps(rows),
            "already_named_count": already_named_count,
            "include_already_named": include_already_named,
            "suggested_tz": suggested_tz,
            "camera_model": camera_model, "camera_filtered_count": camera_filtered_count,
            "only_needs_backwrite": only_needs_backwrite, "only_needs_fix": only_needs_fix,
            "filter_dropped": filter_dropped, "only_needs_rename": only_needs_rename,
            "step": step.to_dict(), "step_url": "/timestamp/browse/start",
        }

    jobs.run_job(job, work)
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(), "module_prefix": "/timestamp",
    })


@router.get("/browse/progress/{job_id}", response_class=HTMLResponse)
def browse_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [], "form_state": {}, "already_named_count": 0,
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(), "module_prefix": "/timestamp",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": None, "rows": [], "form_state": {}, "already_named_count": 0,
            "cancelled_info": f"Abgebrochen nach {job.done} von {job.total} Datei(en).",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Fehler beim Scannen: {job.error}",
            "rows": [], "form_state": {}, "already_named_count": 0,
        })
    # done
    return templates.TemplateResponse(request, "_file_list.html", job.result or {})


@router.post("/browse/cancel/{job_id}")
def browse_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return Response(status_code=204)


def _run_execute(all_rows, selected_set, do_rename, do_exifwrite, exiftool_timeout,
                 handoff_path, handoff_root_path, job=None, do_xmpdate=False, force_tz: str = "", do_rotate=False, rotate_backup=False) -> dict:
    """The actual per-file work of "Ausführen" (shared by the background job and by the
    synchronous /execute endpoint). When `job` is given, updates job.done/current_name per
    file and stops cleanly between files once job.cancel_event is set."""
    # Companion rows are never shown/selected on their own: they run whenever their media
    # file is selected.
    todo = [r for r in all_rows if r["path"] in selected_set
            or (r.get("row_type") == "companion" and r.get("primary_path") in selected_set)]
    if force_tz:      # write-time "Fallback-Zone erzwingen": same wall clock, offset of the Fallback-Zone
        todo = [r if r.get("row_type") == "companion" else timecore.row_with_zone(r, force_tz) for r in todo]
    if job is not None:
        job.total = len(todo)
        job.done = 0
    results = []
    written_paths = []
    untouched_paths = []
    xmp_paths = []
    cancelled = False
    for row in todo:
        if job is not None:
            if job.cancel_event.is_set():
                cancelled = True
                break
            job.current_name = os.path.basename(row["path"])

        if row.get("row_type") == "companion":
            # A companion row: rename-to-match-the-primary + mtime touch only, never an
            # exiftool write (see executor.apply_companion's docstring).
            outcome = apply_companion(row, do_rename)
            results.append(outcome.to_dict())
            written_paths.append(outcome.renamed_to or row["path"])
            if job is not None:
                job.done += 1
            continue

        res = _resolution_from_dict(row)
        rotation_note = ""
        rotation_error = ""
        if do_rotate and rotatecore.is_jpeg(res.path):
            # Lossless upright rotation BEFORE the rename/time write: exiftran -p keeps EXIF dates and mtime, resets the
            # Orientation tag (and the .xmp sidecar's), and the time write below sets the final mtime anyway.
            rr = rotatecore.rotate_lossless(res.path, backup=bool(rotate_backup))
            if rr["rotated"]:
                rotation_note = "\u21bb " + rr["message"]
            elif not rr["ok"]:
                rotation_error = rr["message"]
        outcome = apply_resolution(res, do_rename, do_exifwrite, exiftool_timeout)

        # Re-sync an ALREADY-EXISTING .xmp sidecar right after the EXIF write (Immich
        # reads xmp:CreateDate from it for videos; a stale sidecar is how a correct
        # filename + 2h-off timestamp in Immich happens). A missing sidecar is created for VIDEOS only
        # (see below); photos keep theirs for GPS Sidecar/AI-Beschreibung.
        out_dict_extra = {}
        if (do_exifwrite or do_xmpdate) and res.ext.lower() != ".nfo":
            current_path = outcome.renamed_to or res.path
            xmp_path = current_path + ".xmp"
            is_vid = res.ext.lower() in timecore.VIDEO_EXT
            have_xmp = os.path.isfile(xmp_path)
            if not have_xmp and is_vid and do_xmpdate:
                # VIDEOS get their sidecar created right here: the video's own CreateDate is raw UTC and
                # Immich reads the sidecar, so without it the file sorts 2 h off (the preview already
                # flags "keine .xmp" as needing a fix). Photos carry local time + offset themselves.
                ok_sc, msg_sc = geocore.ensure_xmp_sidecar_ex(current_path, xmp_path, True)
                have_xmp = ok_sc
                if ok_sc:
                    out_dict_extra["xmp_created"] = True
                else:
                    out_dict_extra["exif_sidecar_note"] = f"\u26a0 .xmp-Sidecar konnte nicht angelegt werden: {msg_sc}"
            if have_xmp:
                if do_exifwrite and outcome.exif_written and not out_dict_extra.get("xmp_created"):
                    geocore.ensure_xmp_sidecar(current_path, xmp_path, is_vid)
                # Always pin the sidecar's own dates to the resolved local time.
                if geocore.write_dates_to_xmp(xmp_path, res.zieldate):
                    out_dict_extra["exif_sidecar_note"] = "\u2713 .xmp-Sidecar angelegt, Datum gesetzt" if out_dict_extra.get("xmp_created") else "\u2713 .xmp-Sidecar-Datum gesetzt"
                    # exiftool just bumped the sidecar's mtime to "now" -- pin it to the
                    # capture time too, like the media file itself, and say so.
                    try:
                        os.utime(xmp_path, (res.epoch, res.epoch))
                        out_dict_extra["xmp_touched"] = True
                    except OSError as e:
                        out_dict_extra["exif_sidecar_note"] += f" (\u26a0 .xmp-Zeitstempel nicht gesetzt: {e})"
                    xmp_paths.append(xmp_path)

        if rotation_note:
            out_dict_extra["rotation"] = rotation_note
        if rotation_error:
            out_dict_extra["rotation_error"] = rotation_error
        results.append({**outcome.to_dict(), **out_dict_extra})
        # apply_resolution() already sets the mtime itself; restore_mtime is an optional
        # safety net (manual button or RESTORE_TIMESTAMP=true).
        written_paths.append(outcome.renamed_to or res.path)
        if not outcome.touched:
            untouched_paths.append(outcome.renamed_to or res.path)
        if job is not None:
            job.done += 1

    auto_mtime_results = None
    # apply_resolution() already set the exact per-file epoch (incl. GPS/sibling zone). The
    # mtime-restore step resolves with ONE fixed MTIME_TIMEZONE, so rerunning it over files
    # that were just touched correctly would overwrite e.g. an Abu Dhabi file's epoch with a
    # Vienna-based one -- only the files whose direct touch failed need that safety net.
    if untouched_paths and settings.restore_timestamp_enabled():
        auto_mtime_results = [r.__dict__ for r in mtimecore.restore_many(untouched_paths)]

    # Hands "Weiter zu GPS Sidecar" the EXACT files just touched (see core/handoff.py).
    handoff_id = handoff.stash(handoff_path, handoff_root_path, written_paths) if written_paths else ""

    ctx = {
        "results": results, "written_paths": written_paths + xmp_paths, "auto_mtime_results": auto_mtime_results,
        "handoff_path": handoff_path, "handoff_root_path": handoff_root_path, "handoff_id": handoff_id,
    }
    if cancelled:
        ctx["cancelled_info"] = (f"Abgebrochen nach {len(results)} von {len(todo)} Datei(en) "
                                 f"– bereits verarbeitete Dateien bleiben geändert.")
    return ctx


def _parse_execute_input(all_resolutions: str, selected: list[str]):
    try:
        all_rows = json.loads(all_resolutions)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")
    selected_set = set(selected)
    for row in all_rows:
        if row["path"] in selected_set or (
                row.get("row_type") == "companion" and row.get("primary_path") in selected_set):
            _check_allowed(row["path"])
    return all_rows, selected_set


def _exec_progress_ctx(job) -> dict:
    return {"job_id": job.id, "job": job.to_progress_dict(), "module_prefix": "/timestamp",
            "progress_url": f"/timestamp/execute/progress/{job.id}",
            "cancel_url": f"/timestamp/execute/cancel/{job.id}",
            "unit_label": "Datei(en) geschrieben"}


@router.post("/execute/start", response_class=HTMLResponse)
def execute_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_resolutions: str = Form(...),
    do_rename: bool = Form(False),
    do_exifwrite: bool = Form(False),
    do_xmpdate: bool = Form(False),
    do_rotate: bool = Form(False),
    rotate_backup: bool = Form(False),
    exiftool_timeout: int = Form(120),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
    force_zone: bool = Form(False),
    default_tz: str = Form(""),
):
    all_rows, selected_set = _parse_execute_input(all_resolutions, selected)
    job = jobs.create_job()
    job.total = sum(1 for r in all_rows if r["path"] in selected_set)
    jobs.run_job(job, lambda: _run_execute(all_rows, selected_set, do_rename, do_exifwrite,
                                           exiftool_timeout, handoff_path, handoff_root_path, job,
                                           do_xmpdate, default_tz if force_zone else "", do_rotate, rotate_backup))
    return templates.TemplateResponse(request, "_progress.html", _exec_progress_ctx(job))


@router.get("/execute/progress/{job_id}", response_class=HTMLResponse)
def execute_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return HTMLResponse('<div class="error">Vorgang nicht gefunden (abgelaufen oder Server neu gestartet). '
                            'Bitte Vorschau neu laden und den Ordner prüfen – bereits geschriebene Dateien bleiben geändert.</div>')
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", _exec_progress_ctx(job))
    if job.status == "error":
        return HTMLResponse(f'<div class="error">Fehler beim Ausführen: {html.escape(job.error)}</div>')
    return templates.TemplateResponse(request, "_results.html", job.result or {})


@router.post("/execute/cancel/{job_id}")
def execute_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return Response(status_code=204)


@router.post("/execute", response_class=HTMLResponse)
def execute(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_resolutions: str = Form(...),
    do_rename: bool = Form(False),
    do_exifwrite: bool = Form(False),
    do_xmpdate: bool = Form(False),
    do_rotate: bool = Form(False),
    rotate_backup: bool = Form(False),
    exiftool_timeout: int = Form(120),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
    force_zone: bool = Form(False),
    default_tz: str = Form(""),
):
    """Synchronous variant (kept for scripts/tests); the UI uses /execute/start."""
    all_rows, selected_set = _parse_execute_input(all_resolutions, selected)
    ctx = _run_execute(all_rows, selected_set, do_rename, do_exifwrite, exiftool_timeout,
                       handoff_path, handoff_root_path, do_xmpdate=do_xmpdate,
                       force_tz=default_tz if force_zone else "", do_rotate=do_rotate, rotate_backup=rotate_backup)
    return templates.TemplateResponse(request, "_results.html", ctx)


mtime_routes.add_restore_routes(router, templates, "/timestamp", lambda p: _check_allowed(p))


@router.post("/restore-mtime", response_class=HTMLResponse)
def restore_mtime(request: Request, paths: list[str] = Form(default=[])):
    """Manual equivalent of RESTORE_TIMESTAMP=true for one batch -- see
    core/mtimecore.py's module docstring for why this module offers it at all despite
    already setting its own mtimes correctly (a safety net, not a routine fix)."""
    for p in paths:
        _check_allowed(p)
    results = mtimecore.restore_many(paths)
    return templates.TemplateResponse(request, "_mtime_results.html", {
        "results": [r.__dict__ for r in results],
    })


def _scaled_jpeg(src_path: str) -> Optional[bytes]:
    """Loads an image file and returns it re-encoded as a small JPEG thumbnail, or None if
    that fails (missing Pillow, unreadable/corrupt file, ...) so the caller can fall back."""
    if Image is None or not os.path.isfile(src_path):
        return None
    try:
        with Image.open(src_path) as im:
            im.thumbnail((160, 160))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=72)
            return buf.getvalue()
    except Exception:
        return None


@router.get("/thumb")
def thumb(path: str):
    _check_allowed(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in timecore.IMAGE_EXT:
        data = _scaled_jpeg(path)
        if data is not None:
            return Response(content=data, media_type="image/jpeg")
    is_video = ext in timecore.VIDEO_EXT
    if is_video:
        # Many of the maintainer's videos already have a "<stem>-thumb.jpg" sidecar sitting next
        # to them (e.g. "2026.08.09-17.17.49-thumb.jpg" next to "...17.49.mp4") from the
        # existing NAS toolchain -- use that real frame instead of the generic "▶"
        # placeholder whenever it exists, no frame extraction needed here.
        sibling_thumb = f"{os.path.splitext(path)[0]}-thumb.jpg"
        data = _scaled_jpeg(sibling_thumb)
        if data is not None:
            return Response(content=data, media_type="image/jpeg")
    return Response(content=_placeholder_svg(is_video), media_type="image/svg+xml")


def _placeholder_svg(is_video: bool) -> str:
    glyph = "▶" if is_video else "🖼"
    bg = "#2b2f3a" if is_video else "#3a3530"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160">
<rect width="160" height="160" fill="{bg}" rx="8"/>
<text x="50%" y="52%" font-size="40" text-anchor="middle" dominant-baseline="middle"
 fill="#ffffffaa">{glyph}</text>
</svg>'''


@router.get("/presets", response_class=HTMLResponse)
def list_presets(request: Request):
    presets = settings.load_presets()
    return templates.TemplateResponse(request, "_presets_options.html", {"presets": presets})


@router.post("/presets/save", response_class=HTMLResponse)
def save_preset(
    request: Request,
    name: str = Form(...),
    mode: str = Form("EXIF"),
    default_tz: str = Form("Europe/Vienna"),
    gps_filter: str = Form("ALL"),
    type_filter: str = Form("ALL"),
    depth: int = Form(0),
    do_rename: bool = Form(False),
    do_exifwrite: bool = Form(False),
):
    preset = settings.Preset(
        name=name.strip(), mode=mode, default_tz=default_tz, gps_filter=gps_filter,
        type_filter=type_filter, depth=max(0, min(7, depth)),
        do_rename=do_rename, do_exifwrite=do_exifwrite,
    )
    settings.save_preset(preset)
    presets = settings.load_presets()
    return templates.TemplateResponse(request, "_presets_options.html", {"presets": presets})


@router.post("/presets/delete", response_class=HTMLResponse)
def delete_preset(request: Request, name: str = Form(...)):
    settings.delete_preset(name)
    presets = settings.load_presets()
    return templates.TemplateResponse(request, "_presets_options.html", {"presets": presets})
