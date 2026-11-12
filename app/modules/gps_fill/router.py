"""
router.py -- the "GPS füllen" web module (#8): gives photos/videos that have NO GPS a position from
the GPS track (Traccar/Dawarich through the geo-service), with interpolation between the two
surrounding track points, a confidence per file and automatic camera-clock drift correction
(engine: core/fillcore.py, maths: core/trackmath.py).

Difference to "GPS Sidecar": that module takes the NEAREST track point inside a window; this one
interpolates, tells how sure it is, corrects a wrong camera clock and never needs Traccar/Dawarich
credentials itself -- all track access goes through the geo-service (GEO_SERVICE_URL), so tracks live
in one place.

Two phases like every module: /scan/start (read, plan, preview with confidence) and /apply (write
the .xmp sidecars of the selected files). The plan is computed server-side and kept under a token;
/apply only ever writes what the server planned itself.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
import threading
import time
import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...core import fillcore, geocore, geoservice, jobs, pathsafety, settings, stepper

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
SHARED_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates_shared")
templates = Jinja2Templates(directory=[TEMPLATES_DIR, SHARED_TEMPLATES_DIR])

router = APIRouter()

_PLAN_TTL = 6 * 3600
_plans: dict[str, dict] = {}
_plans_lock = threading.Lock()


def _store_plans(plans: list) -> str:
    token = uuid.uuid4().hex[:16]
    with _plans_lock:
        now = time.time()
        for k in [k for k, v in _plans.items() if now - v["ts"] > _PLAN_TTL]:
            _plans.pop(k, None)
        while len(_plans) > 30:
            _plans.pop(min(_plans, key=lambda k: _plans[k]["ts"]), None)
        _plans[token] = {"ts": now, "plans": {p.media.path: p for p in plans}}
    return token


def _get_plans(token: str):
    with _plans_lock:
        e = _plans.get(token)
        return e["plans"] if e else None


def _opts(drift, min_conf, gap_min, gap2_min, force, retry, include_import, tz, neighbor=True, neighbor_h=6.0) -> fillcore.FillOpts:
    geo_cfg = settings.load_geo_settings()
    gap1 = max(1, int(gap_min)) * 60 if gap_min else geo_cfg.max_gap_secs
    gap2 = max(1, int(gap2_min)) * 60 if gap2_min else geo_cfg.max_gap_secs_tier2
    return fillcore.FillOpts(
        drift=(drift or "auto").strip() or "auto", min_conf=max(0.0, min(1.0, float(min_conf))),
        gap1=gap1, gap2=max(gap1, gap2), tz=(tz or "").strip() or os.environ.get("MTIME_TIMEZONE", "Europe/Vienna"),
        force=bool(force), retry=bool(retry), include_import=bool(include_import),
        neighbor=bool(neighbor), neighbor_s=int(max(0.0, float(neighbor_h)) * 3600))


@router.get("/", response_class=HTMLResponse)
def index(request: Request, path: str = "", root_path: str = ""):
    geo_cfg = settings.load_geo_settings()
    return templates.TemplateResponse(request, "index.html", {
        "media_root": pathsafety.MEDIA_ROOT,
        "root_folders": [rf.to_dict() for rf in settings.load_root_folders()],
        "prefill_path": path, "prefill_root_path": root_path,
        "service_url": geoservice.url(), "service_enabled": geoservice.enabled(),
        "gap_min": geo_cfg.max_gap_secs // 60, "gap2_min": geo_cfg.max_gap_secs_tier2 // 60,
        "default_tz": os.environ.get("MTIME_TIMEZONE", "Europe/Vienna"),
        "today": datetime.now().strftime("%Y-%m-%d"),
    })


@router.get("/subfolders", response_class=HTMLResponse)
def subfolders(path: str = "", root_path: str = ""):
    options = "".join(f'<option value="{p}"></option>' for p in pathsafety.list_subfolders(path, root_path))
    return HTMLResponse(options)


def _err(request: Request, msg: str):
    return templates.TemplateResponse(request, "_scan_list.html", {"error": msg, "rows": []})


@router.post("/scan/start", response_class=HTMLResponse)
def scan_start(
    request: Request,
    path: str = Form(...), root_path: str = Form(""), depth: int = Form(3),
    camera: str = Form(""), drift: str = Form("auto"), min_conf: float = Form(0.3),
    gap_min: int = Form(0), gap2_min: int = Form(0), tz: str = Form(""),
    force: str = Form(""), retry: str = Form(""), include_import: str = Form(""),
    neighbor: str = Form(""), neighbor_h: float = Form(6.0),
    scan_id: str = Form(""), offset: int = Form(0),
):
    pathsafety.check_allowed(path)
    pathsafety.check_within_root(path, root_path)
    depth = max(0, min(7, depth))
    # Same rule as every other module: the Basis-Ordner decides what is looked at (VIDEOS -> only
    # videos, PHOTOS -> only photos), derived server-side, never from the form.
    root_folder = pathsafety.root_for_path(root_path)
    types = {"VIDEO": "video", "IMAGE": "image"}.get(root_folder.type_filter if root_folder else "ALL", "all")
    if not os.path.exists(path):
        return _err(request, f"Pfad nicht gefunden: {path}")
    if not geoservice.enabled():
        return _err(request, "GPS füllen holt die Tracks über den geo-service: GEO_SERVICE_URL ist nicht gesetzt "
                             "(in der mitgelieferten docker-compose.yml ist es standardmäßig http://geo-service:8000).")
    try:
        o = _opts(drift, min_conf, gap_min, gap2_min, force, retry, include_import, tz, neighbor, neighbor_h)
        fillcore.parse_manual_drift(o.drift)
    except ValueError:
        return _err(request, f"Uhr-Abweichung: „{drift}“ ist weder auto, off noch eine Zahl in Sekunden.")
    try:
        geoservice.health()
    except geoservice.ServiceError as e:
        return _err(request, f"geo-service nicht erreichbar: {e}")

    step = stepper.resume("gpsfill", scan_id, offset)
    if step is None:
        step = stepper.start("gpsfill", fillcore.find_media(path, depth, types), meta={"cache": {}})
    cache = step.meta.setdefault("cache", {})      # shared with the snapshot: filled once in step 1
    paths = step.paths

    job = jobs.create_job()
    job.total = max(1, len(paths))
    job.phase = "Metadaten lesen"
    geo_cfg = settings.load_geo_settings()

    def work():
        def prog(done, total):
            job.done, job.total = done, max(1, total)

        media = fillcore.collect(paths, camera, job.cancel_event, prog)
        fillcore.resolve_times(media, o.tz)
        if job.cancel_event.is_set():
            return {}
        auto = fillcore.parse_manual_drift(o.drift) is None and o.drift.strip().lower() != "off"

        # Track + drift: once for the WHOLE scan in step 1 (every file's GPS can serve as a clock
        # reference), cached for the following steps. With manual/off drift only this step's files count.
        if auto and "track" not in cache:
            job.phase = "Referenzdateien & Track (gesamter Scan)"
            all_media = media
            if len(paths) < step.total:
                all_paths = [p for p in stepper._sessions.get(step.scan_id, {}).get("paths", paths) if os.path.exists(p)] \
                    if step.scan_id else paths
                all_media = fillcore.collect(all_paths, camera, job.cancel_event, prog)
                fillcore.resolve_times(all_media, o.tz)
            track, windows = fillcore.load_track(all_media, o, geoservice.track_range, job.cancel_event)
            model = fillcore.estimate_drift(all_media, track, o)
            cache.update(track=track, windows=windows, model=model, files=len(all_media))
        if auto:
            track, windows, model = cache["track"], cache["windows"], cache["model"]
        else:
            job.phase = "Track laden"
            track, windows = fillcore.load_track(media, o, geoservice.track_range, job.cancel_event)
            model = None
        if job.cancel_event.is_set():
            return {}

        plans = fillcore.plan_files(media, track, model, o)
        if o.neighbor and any(p.status == "no-track-point" for p in plans):
            job.phase = "Nachbardateien im Ordner"
            dir_cache = cache.setdefault("dirs", {})        # folder -> its own media (times resolved), read once

            def load_dir(folder):
                files = fillcore.find_media(folder, 7, "all")         # the folder and everything below it
                for d in sorted({os.path.dirname(f) for f in files} - set(dir_cache)):
                    m = fillcore.collect([f for f in files if os.path.dirname(f) == d], "", job.cancel_event)
                    fillcore.resolve_times(m, o.tz)
                    dir_cache[d] = m
                return [m for d in sorted({os.path.dirname(f) for f in files}) for m in dir_cache.get(d, [])]
            fillcore.plan_neighbors(plans, o, load_dir)
        fills = [p for p in plans if p.status == "fill"]

        job.phase, job.done, job.total = "Ortsnamen", 0, max(1, len(fills))
        seen: dict = {}
        for i, p in enumerate(fills):
            if job.cancel_event.is_set():
                break
            job.done, job.current_name = i, os.path.basename(p.media.path)
            key = (round(p.loc["lat"], 4), round(p.loc["lon"], 4))
            if key not in seen:
                g = geocore.reverse_geocode(
                    key[0], key[1], photon_server=geo_cfg.photon_server, nominatim_server=geo_cfg.nominatim_server,
                    pelias_server=geo_cfg.pelias_server, maritime_server=geo_cfg.maritime_server, lang=geo_cfg.lang)
                seen[key] = g.to_dict() if g is not None and not g.is_empty else None
            p.geo = seen[key]
        job.done = job.total

        counts: dict = {}
        for p in plans:
            counts[p.status] = counts.get(p.status, 0) + 1
        token = _store_plans(plans)
        rows = []
        for p in plans:
            if p.status in ("has-gps", "sidecar-has-gps", "marked-no-gps"):
                continue
            loc, g = p.loc or {}, p.geo or {}
            place = ", ".join(x for x in (g.get("city") or g.get("locality"), g.get("country")) if x)
            rows.append({
                "path": p.media.path, "filename": os.path.basename(p.media.path), "status": p.status,
                "reason": p.reason, "camera": p.media.camera, "basis": p.media.basis,
                "assumed": "tz-assumed" in p.media.basis,
                "lat": loc.get("lat"), "lon": loc.get("lon"), "method": loc.get("method", ""),
                "neighbor": loc.get("neighbor", ""), "gap_s": loc.get("gap_s"), "confidence": loc.get("confidence"), "uncertainty_m": loc.get("uncertainty_m"),
                "conf_class": ("hi" if (loc.get("confidence") or 0) >= 0.7 else "mid" if (loc.get("confidence") or 0) >= 0.4 else "lo"),
                "drift_display": fillcore.fmt_dur(p.drift.seconds) if p.drift.seconds else "",
                "drift_method": p.drift.method, "place": place,
                "rel_dir": pathsafety.rel_subdir(path, p.media.path),
            })
        return {
            "rows": rows, "error": None, "counts": counts, "token": token, "windows": windows,
            "points": len(track), "drift_table": model.table() if model else [], "drift_mode": o.drift,
            "assumed_tz": o.tz, "assumed_n": sum(1 for p in fills if "tz-assumed" in p.media.basis),
            "drifted_n": sum(1 for p in fills if p.drift.seconds), "scanned_count": len(media),
            "min_conf": o.min_conf, "step": step.to_dict(), "step_url": "/gpsfill/scan/start",
        }

    jobs.run_job(job, work)
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(), "label": "Datei(en)",
        "poll_url": f"/gpsfill/scan/progress/{job.id}", "cancel_url": f"/gpsfill/scan/cancel/{job.id}",
    })


@router.get("/scan/progress/{job_id}", response_class=HTMLResponse)
def scan_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _err(request, "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut laden.")
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(), "label": "Datei(en)",
            "poll_url": f"/gpsfill/scan/progress/{job.id}", "cancel_url": f"/gpsfill/scan/cancel/{job.id}"})
    if job.status == "error":
        return _err(request, f"Fehler beim Scannen: {job.error}")
    if job.status == "cancelled" or not job.result:
        return templates.TemplateResponse(request, "_scan_list.html", {
            "rows": [], "cancelled_info": "Scan abgebrochen."})
    return templates.TemplateResponse(request, "_scan_list.html", job.result)


@router.post("/scan/cancel/{job_id}")
def scan_cancel(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")


@router.post("/apply", response_class=HTMLResponse)
def apply(request: Request, token: str = Form(""), selected: list[str] = Form(default=[]),
          mark_nogps: str = Form("")):
    plans = _get_plans(token)
    if plans is None:
        return templates.TemplateResponse(request, "_apply_results.html", {
            "error": "Der Plan ist abgelaufen oder der Server wurde neu gestartet — bitte „Scannen“ erneut klicken."})
    chosen = []
    for path in selected:
        try:
            pathsafety.check_allowed(path)
        except HTTPException:
            continue
        p = plans.get(path)
        if p is not None and p.status == "fill":
            chosen.append(p)
    todo_mark = [p for p in plans.values() if p.status == "no-track-point"] if mark_nogps else []

    job = jobs.create_job()
    job.total, job.phase = max(1, len(chosen)), "Sidecars schreiben"

    def work():
        results = []
        for i, p in enumerate(chosen):
            if job.cancel_event.is_set():
                break
            job.done, job.current_name = i, os.path.basename(p.media.path)
            ok, msg = fillcore.write_plan(p)
            results.append({"path": p.media.path, "filename": os.path.basename(p.media.path), "ok": ok,
                            "message": "" if ok else msg, "lat": p.loc["lat"], "lon": p.loc["lon"],
                            "confidence": p.loc["confidence"]})
        job.done = job.total
        marked = sum(1 for p in todo_mark if fillcore.mark_nogps(p))
        return {"results": results, "ok_n": sum(r["ok"] for r in results),
                "err_n": sum(not r["ok"] for r in results), "marked": marked, "error": None}

    jobs.run_job(job, work)
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(), "label": "Sidecar(s)",
        "poll_url": f"/gpsfill/apply/progress/{job.id}", "cancel_url": f"/gpsfill/scan/cancel/{job.id}"})


@router.get("/apply/progress/{job_id}", response_class=HTMLResponse)
def apply_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "_apply_results.html", {"error": "Vorgang nicht gefunden."})
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(), "label": "Sidecar(s)",
            "poll_url": f"/gpsfill/apply/progress/{job.id}", "cancel_url": f"/gpsfill/scan/cancel/{job.id}"})
    if job.status == "error":
        return templates.TemplateResponse(request, "_apply_results.html", {"error": f"Fehler: {job.error}"})
    return templates.TemplateResponse(request, "_apply_results.html", job.result or {"results": [], "ok_n": 0, "err_n": 0, "marked": 0})


# ---------------------------------------------------------------------------------------------
# Wetter-Archiv: fills the geo-service's weather "shadow database" ahead of time
# ---------------------------------------------------------------------------------------------
def _werr(request: Request, msg: str):
    return templates.TemplateResponse(request, "_weather_result.html", {"error": msg, "r": {}})


WEATHER_DEFAULT_FROM = "1900-01-01"


def _parse_day(s: str, end: bool = False):
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(d.timestamp()) + (86399 if end else 0)


@router.post("/weather/start", response_class=HTMLResponse)
def weather_start(
    request: Request,
    path: str = Form(""), root_path: str = Form(""), depth: int = Form(3),
    use_media: str = Form(""), use_tracks: str = Form(""), date_from: str = Form(""), date_to: str = Form(""),
    dry_run: str = Form(""), tz: str = Form(""),
    include_import: str = Form(""), all_devices: str = Form(""), refresh: str = Form("none"),
):
    if not geoservice.enabled():
        return _werr(request, "Das Wetter-Archiv liegt im geo-service: GEO_SERVICE_URL ist nicht gesetzt.")
    try:
        h = geoservice.health()
    except geoservice.ServiceError as e:
        return _werr(request, f"geo-service nicht erreichbar: {e}")
    if h.get("weather") != "open-meteo":
        return _werr(request, "Wetter ist im geo-service aus: dort WEATHER_PROVIDER=open-meteo setzen "
                             "(es gehen nur auf 0,1° gerundete Koordinaten und der Tag an Open-Meteo).")
    if not use_media and not use_tracks:
        return _werr(request, "Bitte mindestens eine Quelle wählen: Fotos/Videos und/oder Tracks.")
    files: list = []
    if use_media:
        if not path:
            return _werr(request, "Für „Fotos/Videos“ bitte oben einen Pfad angeben.")
        try:
            pathsafety.check_allowed(path)
            pathsafety.check_within_root(path, root_path)
        except HTTPException as e:         # htmx swallows 4xx bodies -> show the reason in the panel
            return _werr(request, f"Pfad nicht erlaubt: {e.detail}")
        if not os.path.exists(path):
            return _werr(request, f"Pfad nicht gefunden: {path}")
        root_folder = pathsafety.root_for_path(root_path)
        types = {"VIDEO": "video", "IMAGE": "image"}.get(root_folder.type_filter if root_folder else "ALL", "all")
        files = fillcore.find_media(path, max(0, min(7, depth)), types)
    # Defaults: from 01.01.1900, to = today (always the current day, also if the field was cleared/stale)
    date_from = (date_from or "").strip() or WEATHER_DEFAULT_FROM
    date_to = datetime.now(timezone.utc).strftime("%Y-%m-%d") if not (date_to or "").strip() else date_to
    t0 = _parse_day(date_from)
    t1 = _parse_day(date_to, True)
    if use_tracks and (t0 is None or t1 is None):
        return _werr(request, "Datum bitte als JJJJ-MM-TT angeben.")
    tzname = (tz or "").strip() or os.environ.get("MTIME_TIMEZONE", "Europe/Vienna")

    job = jobs.create_job()
    job.total, job.phase = max(1, len(files)), "Metadaten lesen"

    def work():
        import time as _t
        points: list = []
        if files:
            def prog(done, total):
                job.done, job.total = done, max(1, total)
            media = fillcore.collect(files, "", job.cancel_event, prog)
            fillcore.resolve_times(media, tzname)
            if job.cancel_event.is_set():
                return {}
            points = fillcore.weather_points(media)
        body = {"points": points, "tracks": bool(use_tracks), "dry_run": bool(dry_run),
                "include_import": bool(include_import), "devices": "all" if all_devices else "default",
                "refresh": refresh if refresh in ("none", "bad", "all") else "none"}
        if use_tracks:
            lo, hi = t0, t1
            if lo is None or hi is None:                    # no dates: the span of the media itself, +-1 day
                eps = [p[0] for p in points]
                if not eps:
                    return {"files": len(files), "points": 0, "service": None, "stats": None, "dry": bool(dry_run),
                            "note": "Keine Datei mit Zeit und GPS gefunden -- für Tracks bitte Von/Bis angeben."}
                lo, hi = min(eps) - 86400, max(eps) + 86400
            body.update({"from": lo, "to": hi})
        elif not points:
            return {"files": len(files), "points": 0, "service": None, "stats": None, "dry": bool(dry_run),
                    "note": "Keine Datei mit Zeit und GPS im Sidecar/EXIF gefunden."}
        job.phase, job.done, job.total, job.current_name = "Wetter-Archiv", 0, 1, ""
        geoservice.weather_backfill(body)
        st = {}
        while True:
            if job.cancel_event.is_set():
                try:
                    geoservice.weather_cancel()
                except geoservice.ServiceError:
                    pass
            st = geoservice.weather_status()
            if st.get("phase") == "tracks":
                tw = st.get("track_windows") or {}
                job.phase, job.done, job.total = "Tracks lesen (Zeitfenster)", tw.get("done", 0), max(1, tw.get("total", 1))
            else:
                job.phase, job.done, job.total = "Wetter-Archiv", st.get("done", 0), max(1, st.get("needed_cell_days", 1))
            if st.get("status") != "running":
                break
            _t.sleep(1.0)
        return {"files": len(files), "points": len(points), "service": st, "stats": geoservice.weather_stats(),
                "dry": bool(dry_run), "note": ""}

    jobs.run_job(job, work)
    return templates.TemplateResponse(request, "_progress.html", {
        "job_id": job.id, "job": job.to_progress_dict(), "label": "",
        "poll_url": f"/gpsfill/weather/progress/{job.id}", "cancel_url": f"/gpsfill/weather/cancel/{job.id}",
    })


@router.get("/weather/progress/{job_id}", response_class=HTMLResponse)
def weather_progress(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        return _werr(request, "Vorgang nicht gefunden (abgelaufen oder Server neu gestartet) — bitte erneut starten.")
    if job.status == "running":
        return templates.TemplateResponse(request, "_progress.html", {
            "job_id": job.id, "job": job.to_progress_dict(), "label": "",
            "poll_url": f"/gpsfill/weather/progress/{job.id}", "cancel_url": f"/gpsfill/weather/cancel/{job.id}"})
    if job.status == "error":
        return _werr(request, f"Fehler beim Wetter-Archiv: {job.error}")
    return templates.TemplateResponse(request, "_weather_result.html", {"r": job.result or {}, "cancelled": job.status == "cancelled"})


@router.post("/weather/cancel/{job_id}")
def weather_cancel_route(job_id: str):
    jobs.cancel_job(job_id)
    return HTMLResponse(status_code=204, content="")
