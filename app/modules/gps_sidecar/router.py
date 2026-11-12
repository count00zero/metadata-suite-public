"""
router.py — the GPS Sidecar web module: for every photo/video in a folder, resolve a
complete GPS position (existing EXIF GPS, or the nearest Traccar/Dawarich track point in
time) and a full reverse-geocoded location (country/state/county/city/locality/street +
ISO alpha-3 country code) via the Photon -> Nominatim -> Pelias -> maritime-backup chain,
preview it, then write the result to a .xmp sidecar (and, for images, optionally directly
into the file too).

Mounted under /gps by app/main.py. Ported from Set-GPS-Data-via-traccar-and-dawarich.sh --
see app/core/geocore.py's module docstring for exactly what was and wasn't carried over
from the CLI script in this first pass.

Heavily reuses module #1's own core: timecore.resolve_many()/resolve_one() (EXIF mode)
for per-file capture-time + existing-GPS resolution (Resolution.lat/lon/epoch/has_gps --
added to timecore.Resolution specifically so this module could reuse that single
exiftool call instead of re-reading each file itself), and core.pathsafety for every
path-containment check (MEDIA_ROOT fence, Basis-Ordner containment, sub-folder
suggestions) -- factored out of timestamp_tool/router.py for exactly this reuse.
"""
from __future__ import annotations

import html
import os
import json
from dataclasses import fields as dc_fields
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...core import mtime_routes, devicenames, timecore, settings, geocore, pathsafety, jobs, mtimecore, xmpsync, handoff, stepper, geoservice

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()

_GEO_FIELDS = {f.name for f in dc_fields(geocore.GeoResult)}


def _geo_from_dict(d: dict | None) -> geocore.GeoResult | None:
    if not d:
        return None
    return geocore.GeoResult(**{k: v for k, v in d.items() if k in _GEO_FIELDS})


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = "", handoff_id: str = ""):
    root_folders = [rf.to_dict() for rf in settings.load_root_folders()]
    geo_cfg = settings.load_geo_settings()
    # A handoff_id (from the "Weiter zu GPS Sidecar" link) takes priority over plain
    # path/root_path query params -- it carries the EXACT files Timestamp & Rename just
    # touched (see core/handoff.py), not just the folder. Resolving it here only sets
    # path/root_path for prefill; browse_start() itself re-resolves the same id to get
    # the actual file list once the form actually submits.
    entry = handoff.fetch(handoff_id)
    if entry is not None:
        path, root_path = entry["path"], entry["root_path"]
    else:
        handoff_id = ""  # expired/invalid -- don't carry a dead id into the hidden field
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": root_folders,
        "geo_cfg": geo_cfg,
        "predefined_locations": geocore.all_places(),
        # Set only when arriving via the "Weiter zu GPS Sidecar" handoff from module #1 --
        # prefills Pfad for this one load, taking priority over the usual per-Basis-Ordner
        # localStorage memory (see index.html's initLastPath()).
        "prefill_path": path,
        "prefill_root_path": root_path,
        "prefill_handoff_id": handoff_id,
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def browse_start_work(job, camera_model, candidate_paths, default_tz, delete_srt, depth, embed_in_image, fixed_location, override_gps_from_track, path, remember_no_gps, retry_no_gps, root_path, skip_already_done, step, traccar_device_ids, type_filter, use_dawarich, use_dawarich_import, use_dji_srt, time_shift_s=0, regeocode=False):
    """Background work of browse_start (extracted so the Jahreslauf can call it without a request)."""
    geo_cfg = settings.load_geo_settings()

    def progress_cb(done, total, name):
        job.done, job.total, job.current_name = done, total, name

    # Cheap pre-filter: a file whose sidecar already names a place is skipped further down anyway (skip_already_done) -- decide that from the
    # sidecar text NOW, before the expensive exiftool pass over the (possibly huge, NAS-hosted) media files.
    pre_skipped = 0
    if skip_already_done and not override_gps_from_track and not regeocode:
        keep = []
        for p_ in candidate_paths:
            ctry, cty = geocore.read_xmp_location(p_ + ".xmp")
            if ctry or cty:
                pre_skipped += 1
            else:
                keep.append(p_)
        candidate_paths = keep

    # Reuse module #1's full resolution chain (mode=EXIF) for capture time + any GPS
    # the file already carries -- one exiftool call per file, same as a normal
    # Timestamp & Rename browse, instead of this module reading EXIF a second way.
    candidates = timecore.resolve_batch(
        candidate_paths, "EXIF", default_tz, "ALL", type_filter,
        progress_cb=progress_cb, cancel_event=job.cancel_event,
    )
    if job.cancel_event.is_set():
        return {}

    # Optional camera filter (substring on Make+Model, or the NFO device alias --
    # same matching as AI-Beschreibung): re-run only e.g. the Insta360 footage.
    camera_filtered = 0
    cam = camera_model.strip()
    if cam:
        kept = []
        for r in candidates:
            if devicenames.camera_matches(r.camera_make, r.camera_model, cam):
                kept.append(r)
            else:
                camera_filtered += 1
        candidates = kept

    fixed = None
    if fixed_location:
        for pl in geocore.all_places():
            if pl.name == fixed_location:
                fixed = pl
                break

    # Local pre-pass per file: GPS the sidecar already carries, a remembered "no GPS
    # found" marker, and DJI .srt telemetry next to videos.
    info = {}
    for res in candidates:
        is_video = os.path.splitext(res.path)[1].lower() in timecore.VIDEO_EXT
        xlat, xlon, nogps = geocore.read_xmp_gps(res.path + ".xmp")
        srt_path, srt_gps = None, None
        if is_video and (use_dji_srt or delete_srt):
            srt_path = geocore.find_dji_srt(res.path)
            if srt_path and use_dji_srt:
                srt_gps = geocore.parse_dji_srt(srt_path)
        t_epoch, t_note = timecore.track_match_time(res, res.path)
        if time_shift_s and t_epoch is not None:
            # camera clock off by a known amount: the track is searched at the corrected time (the captured time itself is not touched)
            t_epoch += time_shift_s
            t_note = (t_note + " · " if t_note else "") + f"Zeitkorrektur {timecore.format_shift(time_shift_s)}"
        info[res.path] = {"t_epoch": t_epoch, "t_note": t_note, "is_video": is_video, "xlat": xlat, "xlon": xlon, "nogps": nogps,
                          "srt_path": srt_path, "srt_gps": srt_gps}
    if job.cancel_event.is_set():
        return {}

    def _existing(res):
        if res.has_gps:
            return res.lat, res.lon, "Datei (vorhandenes GPS)"
        i = info[res.path]
        if i["xlat"] is not None:
            return i["xlat"], i["xlon"], "Sidecar (vorhandenes GPS)"
        return None, None, ""

    def _needs_external(res):
        if fixed is not None:
            return False
        elat, _elon, _src = _existing(res)
        if elat is not None and not override_gps_from_track:
            return False
        if info[res.path]["srt_gps"]:
            return False
        return info[res.path]["t_epoch"] is not None

    # One track fetch per folder-scan covering every file that actually needs a GPS
    # lookup (padded by 12h either side), not one fetch per file -- same "fetch once,
    # match many" shape as the CLI's fetch_and_merge_gps_data()/GEO_LOOKUP_FILE.
    needing_lookup = [info[r.path]["t_epoch"] for r in candidates if _needs_external(r)]
    track: list[geocore.TrackPoint] = []
    track_windows = 0
    track_fetch_errors = []
    lookup_attempted = False
    if needing_lookup and (traccar_device_ids or use_dawarich or use_dawarich_import):
        lookup_attempted = True
        # One request per source for a whole trip/month instead of one per day: capture times
        # are grouped into few windows (see geocore.plan_track_windows), never cut at midnight.
        windows = geocore.plan_track_windows(needing_lookup)
        track_windows = len(windows)
        tracks = []
        # geo-service first (one place for Traccar/Dawarich access + cache); the direct fetch below only
        # runs when it is not configured or not reachable, so this module keeps working without it.
        via_service = False
        if geoservice.enabled():
            svc_sources = ",".join(n for n, on in (("traccar", bool(traccar_device_ids)), ("dawarich", use_dawarich)) if on)
            try:
                pts = []
                for from_dt, to_dt in windows:
                    if job.cancel_event.is_set():
                        break
                    pts += [geocore.TrackPoint(epoch=e, lat=la, lon=lo, source=src) for e, la, lo, src in
                            geoservice.track_range(int(from_dt.timestamp()), int(to_dt.timestamp()), svc_sources,
                                                   ",".join(traccar_device_ids) or "default", bool(use_dawarich_import))]
                tracks.append(pts)
                via_service = True
            except geoservice.ServiceError as e:
                track_fetch_errors.append(f"geo-service nicht nutzbar, hole Tracks direkt: {e}")
        if not via_service:
            use_traccar = bool(traccar_device_ids)
            if use_traccar and not (geo_cfg.traccar_url and geo_cfg.traccar_token):
                use_traccar = False
                track_fetch_errors.append("Traccar ist nicht vollständig konfiguriert (TRACCAR_URL/TRACCAR_TOKEN).")
            use_dw = use_dawarich
            if use_dw and not geo_cfg.dawarich_configured:
                use_dw = False
                track_fetch_errors.append("Dawarich ist nicht konfiguriert (DAWARICH_SERVER/DAWARICH_API_KEY).")
            use_import = use_dawarich_import
            if use_import and not (geo_cfg.dawarich_server and geo_cfg.dawarich_import_api_key):
                use_import = False
                track_fetch_errors.append("Das Dawarich-Archiv-Konto ist nicht konfiguriert (DAWARICH_IMPORT_API_KEY).")
            for from_dt, to_dt in windows:
                if job.cancel_event.is_set():
                    break
                if use_traccar:
                    tracks.append(geocore.fetch_traccar_track(
                        geo_cfg.traccar_url, geo_cfg.traccar_token, traccar_device_ids, from_dt, to_dt))
                if use_dw:
                    tracks.append(geocore.fetch_dawarich_track(
                        geo_cfg.dawarich_server, geo_cfg.dawarich_api_key, from_dt, to_dt, label="Dawarich"))
                if use_import:
                    tracks.append(geocore.fetch_dawarich_track(
                        geo_cfg.dawarich_server, geo_cfg.dawarich_import_api_key, from_dt, to_dt, label="Dawarich-Archiv"))
        track = geocore.merge_tracks(*tracks)

    if job.cancel_event.is_set():
        return {}

    # Local-only pass: pick a GPS position per file and check for an already-complete
    # .xmp sidecar. Priority: fixed place ('-GEO') > DJI .srt > track (when the file has
    # no GPS, or 'override' is on) > the GPS the file/sidecar already has. The one step
    # that needs the network (reverse_geocode) is deferred to /geocode/start.
    rows = []
    skipped_done = pre_skipped
    skipped_nogps = 0
    marked_nogps = 0
    for res in candidates:
        i = info[res.path]
        is_video = i["is_video"]
        xmp_path = res.path + ".xmp"
        elat, elon, esrc = _existing(res)

        old_location = ""
        if regeocode:
            # new reverse-geocoding of files that already have a place: nothing is skipped, the position they carry is
            # kept and only the place fields are asked again (past the geo-service cache) and rewritten
            oc, ocity = geocore.read_xmp_location(xmp_path)
            old_location = ", ".join(x for x in (oc, ocity) if x)
        elif skip_already_done and not override_gps_from_track:
            existing_country, existing_city = geocore.read_xmp_location(xmp_path)
            if existing_country or existing_city:
                skipped_done += 1
                continue
        if i["nogps"] and elat is None and not retry_no_gps and fixed is None and not i["srt_gps"]:
            skipped_nogps += 1
            continue

        lat, lon, gps_source, diff_secs = elat, elon, esrc, None
        override_applied = False
        replace_existing = elat is None or override_gps_from_track
        if replace_existing:
            new = None
            if fixed is not None:
                new = (fixed.lat, fixed.lon, f"Fester Ort: {fixed.name}", None)
            elif i["srt_gps"]:
                new = (i["srt_gps"][0], i["srt_gps"][1], "DJI-.srt", None)
            elif track and i["t_epoch"] is not None:
                hit = (geocore.nearest_point(track, i["t_epoch"], geo_cfg.max_gap_secs)
                       or geocore.nearest_point(track, i["t_epoch"], geo_cfg.max_gap_secs_tier2))
                if hit:
                    point, d = hit
                    new = (point.lat, point.lon, point.source, d)
            if new:
                lat, lon, gps_source, diff_secs = new
                override_applied = elat is not None

        if lat is None and remember_no_gps and lookup_attempted and not track_fetch_errors \
                and not i["nogps"]:
            if geocore.mark_no_gps(res.path, xmp_path, is_video):
                marked_nogps += 1

        rows.append({
            "path": res.path, "filename": os.path.basename(res.path), "is_video": is_video,
            "capture_display": res.zieldate, "lat": lat, "lon": lon, "gps_source": gps_source,
            "diff_secs": diff_secs, "diff_display": _format_seconds_diff(diff_secs) if diff_secs else "",
            "rel_dir": pathsafety.rel_subdir(path, res.path),
            "override_applied": override_applied,
            "track_note": i["t_note"] if gps_source and gps_source not in (
                "Datei (vorhandenes GPS)", "Sidecar (vorhandenes GPS)", "DJI-.srt") and not gps_source.startswith("Fester Ort") else "",
            "old_lat": elat if override_applied else None, "old_lon": elon if override_applied else None,
            "srt_path": i["srt_path"] if (gps_source == "DJI-.srt" or delete_srt) else None,
            "fixed_geo": fixed.geo.to_dict() if (fixed is not None and gps_source.startswith("Fester Ort")) else None,
            "camera": " ".join(x for x in (res.camera_make, res.camera_model) if x),
            "regeocode": bool(regeocode), "old_location": old_location,
        })

    form_state = {
        "path": path, "root_path": root_path, "depth": depth, "default_tz": default_tz,
        "type_filter": type_filter, "traccar_device_ids": traccar_device_ids, "use_dawarich": use_dawarich,
        "use_dawarich_import": use_dawarich_import, "skip_already_done": skip_already_done, "regeocode": regeocode,
        "embed_in_image": embed_in_image, "camera_model": cam,
        "override_gps_from_track": override_gps_from_track, "retry_no_gps": retry_no_gps,
        "remember_no_gps": remember_no_gps, "use_dji_srt": use_dji_srt, "delete_srt": delete_srt,
        "fixed_location": fixed_location,
    }
    # Same Fallback-Zone suggestion as module #1 -- only shown when it differs from
    # what's set now and is backed by more than a single file. Surfaced here (before
    # geocoding) rather than after, since this is the point where re-scanning with a
    # better zone is still cheap.
    suggested_tz = None
    tz_suggestion = timecore.suggest_fallback_zone(candidates)
    if tz_suggestion:
        zone, count, confident_total = tz_suggestion
        if zone != default_tz and count > 1:
            suggested_tz = {"zone": zone, "count": count, "confident_total": confident_total}
    return {
        "rows": rows, "form_state": form_state, "error": None, "skipped_done": skipped_done,
        "skipped_nogps": skipped_nogps, "marked_nogps": marked_nogps, "camera_filtered": camera_filtered,
        "track_fetch_errors": track_fetch_errors, "track_points": len(track), "track_windows": track_windows,
        "all_rows_json": json.dumps(rows), "suggested_tz": suggested_tz,
        # Carried through geocode/start -> execute purely so _results.html can offer a
        # "Weiter zu AI-Beschreibung" handoff button prefilled with this folder.
        "handoff_path": path, "handoff_root_path": root_path,
        "step": step.to_dict(), "step_url": "/gps/browse/start", "step_json": json.dumps(step.to_dict()),
    }


@router.post("/browse/start", response_class=HTMLResponse)
def browse_start(
    request: Request,
    path: str = Form(...),
    root_path: str = Form(""),
    depth: int = Form(0),
    default_tz: str = Form("Europe/Vienna"),
    type_filter: str = Form("ALL"),
    traccar_device_ids: list[str] = Form(default=[]),
    use_dawarich: bool = Form(False),
    use_dawarich_import: bool = Form(False),
    time_shift: str = Form(""),
    regeocode: bool = Form(False),
    skip_already_done: bool = Form(False),
    embed_in_image: bool = Form(False),
    handoff_id: str = Form(""),
    camera_model: str = Form(""),
    override_gps_from_track: bool = Form(False),
    retry_no_gps: bool = Form(False),
    remember_no_gps: bool = Form(False),
    use_dji_srt: bool = Form(False),
    delete_srt: bool = Form(False),
    fixed_location: str = Form(""),
    scan_id: str = Form(""),
    offset: int = Form(0),
):
    """Kicks off the scan as a background job: metadata/GPS extraction (per-file exiftool,
    the same as a Timestamp & Rename browse) plus one one-off Traccar/Dawarich track fetch
    and local nearest-point matching -- all of that is local/cheap-ish and happens here.
    The genuinely expensive step, per-file *network* reverse-geocoding, is deliberately
    NOT run in this job: its own /geocode/start is only kicked off afterwards, and only for
    whichever files are still selected on the resulting list -- so files you deselect here
    never cost a geocoding request at all, not just a wasted one. See timestamp_tool/
    router.py's browse_start for the same job/progress/cancel pattern explained."""
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    root_folder = pathsafety.root_for_path(root_path)
    if root_folder is not None:
        type_filter = root_folder.type_filter

    if not os.path.exists(path):
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Pfad nicht gefunden: {path}", "rows": [], "skipped_done": 0,
        })

    try:
        time_shift_s = timecore.parse_shift(time_shift)
    except ValueError as e:
        return templates.TemplateResponse(request, "_scan_list.html", {"error": str(e), "rows": [], "skipped_done": 0})

    depth = max(0, min(7, depth))

    # A handoff_id (from "Weiter zu GPS Sidecar") carries the EXACT files the previous
    # module's execute step just touched -- use those directly as the only candidates
    # instead of walking the whole folder, so an otherwise-unrelated file sitting in the
    # same folder never gets scanned, counted, or reported as "bereits abgeschlossen"
    # (the maintainer's own report: renaming 1 of 22 files, then handing off, used to re-scan
    # all 22). Falls back to the normal folder walk when there's no handoff_id, or it's
    # expired/invalid -- same as before this feature existed. Still narrowed to
    # type_filter either way (a VIDEOS Basis-Ordner never counts photos).
    step = stepper.resume("gps", scan_id, offset)
    if step is not None:
        candidate_paths = step.paths
    else:
        handoff_entry = handoff.fetch(handoff_id)
        from_handoff = handoff_entry is not None
        if handoff_entry is not None:
            candidate_paths = [
                p for p in handoff_entry["file_paths"]
                if os.path.exists(p)
                and timecore._matches_type_filter(os.path.splitext(p)[1].lower(), type_filter)
            ]
        else:
            candidate_paths = timecore.find_files(path, depth, type_filter)
        step = stepper.start("gps", candidate_paths, unlimited=from_handoff)
        candidate_paths = step.paths

    job = jobs.create_job()
    job.total = len(candidate_paths)
    job.phase = "Metadaten lesen"


    jobs.run_job(job, lambda: browse_start_work(job, camera_model=camera_model, candidate_paths=candidate_paths, default_tz=default_tz, delete_srt=delete_srt, depth=depth, embed_in_image=embed_in_image, fixed_location=fixed_location, override_gps_from_track=override_gps_from_track, path=path, remember_no_gps=remember_no_gps, retry_no_gps=retry_no_gps, root_path=root_path, skip_already_done=skip_already_done, step=step, traccar_device_ids=traccar_device_ids, type_filter=type_filter, use_dawarich=use_dawarich, use_dawarich_import=use_dawarich_import, use_dji_srt=use_dji_srt, time_shift_s=time_shift_s, regeocode=regeocode))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/gps/browse/progress/{job.id}", "cancel_url": f"/gps/browse/cancel/{job.id}",
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
            "poll_url": f"/gps/browse/progress/{job.id}", "cancel_url": f"/gps/browse/cancel/{job.id}",
        })
    if job.status == "cancelled":
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": None, "rows": [], "skipped_done": 0,
            "cancelled_info": f"Abgebrochen nach {job.done} von {job.total} Datei(en) ({job.phase}).",
        })
    if job.status == "error":
        return templates.TemplateResponse(request, "_scan_list.html", {
            "error": f"Fehler beim Scannen: {job.error}", "rows": [], "skipped_done": 0,
        })
    # done -- shows the scan results as a selectable list; geocoding only starts once
    # "Geocoding starten" is clicked (see /geocode/start below).
    return templates.TemplateResponse(request, "_scan_list.html", job.result or {})


@router.post("/browse/cancel/{job_id}")
def browse_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


def geocode_start_work(job, delete_srt, embed_in_image, handoff_path, handoff_root_path, scanned_rows, selected_set, step_ctx, to_process):
    """Background work of geocode_start (extracted so the Jahreslauf can call it without a request)."""
    geo_cfg = settings.load_geo_settings()
    rows = []
    for i, r in enumerate(to_process):
        if job.cancel_event.is_set():
            break
        job.done = i
        job.current_name = r.get("filename", "")

        lat, lon = r.get("lat"), r.get("lon")
        geo = None
        if r.get("fixed_geo"):
            geo = _geo_from_dict(r["fixed_geo"])   # fixed place: no network lookup
        elif lat is not None and lon is not None:
            geo = geocore.reverse_geocode(
                lat, lon, photon_server=geo_cfg.photon_server, nominatim_server=geo_cfg.nominatim_server,
                pelias_server=geo_cfg.pelias_server, maritime_server=geo_cfg.maritime_server, lang=geo_cfg.lang,
                nocache=bool(r.get("regeocode")),
            )

        warn = ""
        if lat is None:
            warn = "Keine GPS-Position gefunden (weder im File noch im Track) — kann nicht reverse-geocodiert werden."
        elif geo is None or geo.is_empty:
            warn = "Reverse-Geocoding lieferte für diese Position kein Ergebnis."

        rows.append({**r, "geo": geo.to_dict() if geo else None, "warn": warn})
    else:
        job.done = len(to_process)

    if job.cancel_event.is_set():
        # Hands the whole scanned set back as an editable selection list (not an error
        # page) -- whatever was already geocoded before Abbrechen was clicked is simply
        # not kept, but nothing has to be re-scanned: adjust the selection and click
        # "Geocoding starten" again.
        return {
            "rows": scanned_rows, "error": None, "skipped_done": 0,
            "track_fetch_errors": [], "track_points": 0,
            "all_rows_json": json.dumps(scanned_rows),
            "selected_paths": selected_set, "suggested_tz": None,
            "form_state": {"embed_in_image": embed_in_image, "delete_srt": delete_srt},
            "notice": f"⏹ Geocoding abgebrochen nach {job.done} von {job.total} Datei(en). "
                      f"Auswahl ggf. anpassen und „Geocoding starten“ erneut klicken.",
            "handoff_path": handoff_path, "handoff_root_path": handoff_root_path, **step_ctx,
        }

    return {
        "rows": rows, "error": None, "skipped_done": 0,
        "track_fetch_errors": [], "track_points": 0,
        "all_rows_json": json.dumps(rows), "suggested_tz": None,
        "form_state": {"embed_in_image": embed_in_image, "delete_srt": delete_srt},
        "handoff_path": handoff_path, "handoff_root_path": handoff_root_path, **step_ctx,
    }


@router.post("/geocode/start", response_class=HTMLResponse)
def geocode_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_scanned_rows: str = Form(...),
    embed_in_image: bool = Form(False),
    delete_srt: bool = Form(False),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
    step_json: str = Form(""),
):
    """Second, separately cancelable job: per-file reverse-geocoding (network calls) for
    only the rows still selected on the scan list -- everything else (metadata, GPS
    position) was already resolved locally in /browse/start, so this step has nothing
    left to do for a deselected file."""
    try:
        scanned_rows = json.loads(all_scanned_rows)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")

    try:
        step_d = json.loads(step_json) if step_json else None
    except json.JSONDecodeError:
        step_d = None
    step_ctx = {"step": step_d, "step_url": "/gps/browse/start", "step_json": step_json}
    selected_set = set(selected)
    to_process = [r for r in scanned_rows if r["path"] in selected_set]

    job = jobs.create_job()
    job.total = len(to_process)
    job.phase = "Geocoding"


    jobs.run_job(job, lambda: geocode_start_work(job, delete_srt=delete_srt, embed_in_image=embed_in_image, handoff_path=handoff_path, handoff_root_path=handoff_root_path, scanned_rows=scanned_rows, selected_set=selected_set, step_ctx=step_ctx, to_process=to_process))
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(),
        "poll_url": f"/gps/geocode/progress/{job.id}", "cancel_url": f"/gps/geocode/cancel/{job.id}",
    })


@router.get("/geocode/progress/{job_id}", response_class=HTMLResponse)
def geocode_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.",
            "rows": [], "skipped_done": 0,
        })
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/gps/geocode/progress/{job.id}", "cancel_url": f"/gps/geocode/cancel/{job.id}",
        })
    if job.status == "cancelled":
        # job.result was still populated on cancel (see work() above) -- an editable
        # scan list, not a dead end.
        return templates.TemplateResponse(request, "_scan_list.html", job.result or {})
    if job.status == "error":
        return templates.TemplateResponse(request, "_file_list.html", {
            "error": f"Fehler beim Geocoding: {job.error}", "rows": [], "skipped_done": 0,
        })
    # done
    return templates.TemplateResponse(request, "_file_list.html", job.result or {})


@router.post("/geocode/cancel/{job_id}")
def geocode_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


def _format_seconds_diff(secs: int | None) -> str:
    if not secs:
        return ""
    secs = abs(int(secs))
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _parse_rows(all_rows: str):
    try:
        return json.loads(all_rows)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Ungültige Vorschau-Daten, bitte neu laden.")


def _run_execute(rows, selected_set, embed_in_image, delete_srt, handoff_path, handoff_root_path,
                 job=None) -> dict:
    """Per-file write (sidecar, optional image GPS/embed, optional .srt delete). With `job`,
    updates progress per file and stops cleanly between files on cancel."""
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
        try:
            results.append(_execute_one(row, embed_in_image, delete_srt))
        except Exception as e:      # noqa: BLE001 -- one broken file must not abort the rest (a year run would lose every later file)
            import traceback
            tb = traceback.extract_tb(e.__traceback__)[-1]
            results.append({"path": row["path"], "xmp_written": False, "image_written": False, "image_error": "",
                            "xmp_error": f"{type(e).__name__}: {e} ({os.path.basename(tb.filename)}:{tb.lineno})"})
        if job is not None:
            job.done += 1
    ctx = _finish_execute(results, handoff_path, handoff_root_path)
    if cancelled:
        ctx["cancelled_info"] = (f"Abgebrochen nach {len(results)} von {len(todo)} Datei(en) "
                                 f"– bereits geschriebene Dateien bleiben geändert.")
    return ctx


def _execute_one(row, embed_in_image, delete_srt) -> dict:
    file = row["path"]
    pathsafety.check_allowed(file)
    is_video = bool(row.get("is_video"))
    lat, lon = row.get("lat"), row.get("lon")
    geo = _geo_from_dict(row.get("geo"))
    xmp_path = file + ".xmp"
    result = {"path": file, "xmp_written": False, "xmp_error": "", "image_written": False, "image_error": ""}

    if not os.path.exists(file):
        result["xmp_error"] = "Datei nicht mehr gefunden -- Vorschau neu laden."
        return result

    sc_ok, sc_msg = geocore.ensure_xmp_sidecar_ex(file, xmp_path, is_video)
    if sc_ok:
        # the sidecar copy of a video's QuickTime CreateDate is raw UTC without offset -> give it the
        # resolved local time + offset (same value Timestamp & Rename writes); never touches a dated-with-offset one
        result["date_offset_added"] = geocore.ensure_xmp_dates_with_offset(xmp_path, row.get("capture_display") or "")
        cam = geocore.write_camera_name(xmp_path)      # readable name from camera_names.json etc. -> XMP-mon:CameraName
        result["camera_name"] = cam["name"] if cam["ok"] else ""
        result["camera_name_source"] = cam["source"] if cam["ok"] else ""
        if cam["ok"]:
            result["camera_raw"] = f'{cam["make"]} {cam["model"]}'.strip()
            result["camera_source_label"] = devicenames.SOURCE_LABELS.get(cam["source"], cam["source"])
            if cam["source"] == devicenames.SOURCE_RAW:     # no rule matched -> say why, so a typo/missing file is visible
                st = devicenames.overrides_status()
                result["camera_hint"] = (
                    f"keine Regel gefunden – camera_names.json: {st['path']} "
                    + ("(Datei fehlt)" if not st["exists"] else f"(ungültiges JSON: {st['error']})" if st["error"]
                       else f"({st['rules']} Regeln, keine passt zu „{cam['model'] or cam['make']}“)"))
        ok, msg = geocore.write_location_to_xmp(xmp_path, lat, lon, geo)
        # height, time zone and IPTC are context: the separate step "Kontext nachfüllen" (/ctxfill/) writes them
        result["xmp_written"] = ok
        if not ok:
            result["xmp_error"] = msg or "Keine Daten zu schreiben (weder GPS noch Location)."
    else:
        result["xmp_error"] = "Konnte XMP-Sidecar nicht erstellen/aktualisieren" + (f": {sc_msg}" if sc_msg else ".")

    # GPS replaced from track/.srt/fixed place on a photo: the image's own EXIF GPS would
    # otherwise stay stale (Immich etc. read the file too), so overwrite it right away.
    # Videos only ever get the sidecar.
    if not is_video and row.get("override_applied") and lat is not None and lon is not None:
        ok, msg = geocore.write_gps_to_image(file, lat, lon)
        result["image_written"] = ok
        if not ok:
            result["image_error"] = msg or "GPS konnte nicht ins Bild geschrieben werden."

    if not is_video and embed_in_image:
        # Embeds the full CURRENT .xmp snapshot (GPS+Location, and any AI description
        # already there) -- see xmpsync.py. Default OFF: AI-Beschreibung usually follows.
        ok, msg = xmpsync.sync_xmp_to_image(file, xmp_path)
        result["image_written"] = ok or result["image_written"]
        if not ok:
            result["image_error"] = msg or "Keine Daten zu schreiben (weder GPS noch Location)."

    # DJI .srt: removed only after the sidecar write is verified (GPS read back).
    srt = row.get("srt_path")
    if delete_srt and srt and result["xmp_written"] and os.path.isfile(srt) and lat is not None and lon is not None:
        # whatever the position came from (.srt, track, file, fixed place): once the sidecar verifiably
        # holds it, the telemetry .srt is redundant
        vlat, vlon, _n = geocore.read_xmp_gps(xmp_path)
        if vlat is not None and abs(vlat - float(lat)) < 1e-4 and abs(vlon - float(lon)) < 1e-4:
            try:
                os.remove(srt)
                result["srt_deleted"] = True
            except OSError as e:
                result["xmp_error"] = f".srt konnte nicht gelöscht werden: {e}"
    # DJI .lrf (low-res proxy video, no value of its own): removed with the .srt once the sidecar is written
    if delete_srt and is_video and result["xmp_written"]:
        lrf = geocore.find_dji_lrf(file)
        if lrf:
            try:
                os.remove(lrf)
                result["lrf_deleted"] = True
            except OSError as e:
                result["xmp_error"] = f".lrf konnte nicht gelöscht werden: {e}"
    return result


def _finish_execute(results, handoff_path, handoff_root_path) -> dict:
    # Every write went through exiftool -overwrite_original, which bumps mtime to "now" --
    # offered as an opt-in button on the results screen, or run automatically here when
    # RESTORE_TIMESTAMP=true.
    written_paths = []
    for r in results:
        if r["xmp_written"]:
            written_paths.append(r["path"] + ".xmp")
        if r["image_written"]:
            written_paths.append(r["path"])

    auto_mtime_results = None
    if written_paths and settings.restore_timestamp_enabled():
        auto_mtime_results = [r.__dict__ for r in mtimecore.restore_many(written_paths)]

    # "Weiter zu AI-Beschreibung" gets the EXACT media files written (not the .xmp, not the
    # whole folder) -- see core/handoff.py.
    touched_file_paths = [r["path"] for r in results if r["xmp_written"]]
    handoff_id = handoff.stash(handoff_path, handoff_root_path, touched_file_paths) if touched_file_paths else ""
    return {
        "results": results, "written_paths": written_paths, "auto_mtime_results": auto_mtime_results,
        "handoff_path": handoff_path, "handoff_root_path": handoff_root_path, "handoff_id": handoff_id,
    }


def _exec_progress_ctx(job) -> dict:
    return {"job_id": job.id, "job": job.to_progress_dict(),
            "poll_url": f"/gps/execute/progress/{job.id}", "cancel_url": f"/gps/execute/cancel/{job.id}"}


@router.post("/execute/start", response_class=HTMLResponse)
def execute_start(
    request: Request,
    selected: list[str] = Form(default=[]),
    all_rows: str = Form(...),
    embed_in_image: bool = Form(False),
    delete_srt: bool = Form(False),
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
    jobs.run_job(job, lambda: _run_execute(rows, selected_set, embed_in_image, delete_srt,
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
    delete_srt: bool = Form(False),
    handoff_path: str = Form(""),
    handoff_root_path: str = Form(""),
):
    """Synchronous variant (scripts/tests); the UI uses /execute/start."""
    ctx = _run_execute(_parse_rows(all_rows), set(selected), embed_in_image, delete_srt,
                       handoff_path, handoff_root_path)
    return templates.TemplateResponse(request, "_results.html", ctx)


mtime_routes.add_restore_routes(router, templates, "/gps", lambda p: pathsafety.check_allowed(p))


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
