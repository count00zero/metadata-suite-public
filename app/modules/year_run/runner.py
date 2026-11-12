"""
runner.py -- the engine of the "Jahreslauf" module: the web version of Media_actualyear_nfo_Creator.sh.

For one year it works through up to four folders (photo/<YEAR>, photo/Urlaube/<YEAR>, video/<YEAR>, video/Urlaube/<YEAR>) and
runs, per folder, the same background steps the single modules run -- unattended, with the defaults of the old script:

    GPS Sidecar -> AI-Beschreibung -> NFO Maker (videos) -> Kontext nachfuellen -> Datei-Zeitstempel      (+ Collage for photo/Urlaube)

The steps call the extracted `*_work` functions / `_run_execute` of the module routers, so web module and Jahreslauf share one
implementation. Everything runs in ONE background thread; state (log, per-step summary, progress) lives in `RUNS` and is kept for
24 h so the page can be reopened later. Cancel stops between files (the work functions check the shared cancel event).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime

from ...core import ctxfill, fileindex, fillcore, geoservice, jobs, settings, skipdirs, stepper, timecore

STEP_LABELS = {
    "rename": "Umbenennen", "gps": "GPS Sidecar", "ai": "AI-Beschreibung", "nfo": "NFO Maker", "ctx": "Kontext nachfüllen",
    "mtime": "Datei-Zeitstempel", "collage": "Collage",
}
STEP_ORDER = ("rename", "gps", "ctx", "ai", "nfo", "collage", "mtime")          # mtime is always last (see steps_for)
DEFAULT_STEPS = ("rename", "gps", "ctx", "ai", "nfo", "collage", "mtime")      # old script: gps+ai (+nfo for videos, collage for photo/Urlaube); + ctx, mtime on request
_KEEP_SECS = 24 * 3600


@dataclass
class Opts:
    year: int
    kind: str = "all"          # all | video | image
    scope: str = "all"         # all | regular | urlaub
    depth: int = 7
    aim: bool = False          # also fill/repair missing AI descriptions folder-wide + NFO repair-ai
    steps: tuple = DEFAULT_STEPS
    default_tz: str = "Europe/Vienna"
    fixbad: bool = False       # find unusable AI descriptions (refusals, <think>, loops, English, cut off ...) and regenerate them
    recheck: bool = False      # ignore the remembered per-folder state and check everything again
    backfill: bool = False     # add data that is still missing (weather, currency ...) AND redo AI text + .nfo for the files that got new data
    month: int = 0             # 1-12: only the month folders <Y>/<Y.MM> of photo AND video (no Urlaube); 0 = the whole year

    @property
    def title(self) -> str:
        """'2026' or, for a month run, '2026.11'."""
        return f"{self.year}.{self.month:02d}" if 1 <= self.month <= 12 else str(self.year)


@dataclass
class Folder:
    kind: str                  # IMAGE | VIDEO
    scope: str                 # regular | urlaub
    path: str
    root_path: str
    exists: bool = False
    files: int = 0
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------------------------
# planning / preview
# ---------------------------------------------------------------------------------------------------------------------
def _root(kind: str) -> str:
    for rf in settings.load_root_folders():
        if rf.type_filter == kind:
            return rf.path
    return settings.photo_root() if kind == "IMAGE" else settings.video_root()


def _join(base: str, *parts: str) -> str:
    return os.path.join(base, *parts) + "/"


def plan(opts: Opts) -> list[Folder]:
    """The folders of the old script, in its order: photo/<Y>, photo/Urlaube/<Y>, video/<Y>, video/Urlaube/<Y>."""
    y = str(opts.year)
    out: list[Folder] = []
    for kind, label in (("IMAGE", "Fotos"), ("VIDEO", "Videos")):
        if opts.kind == "video" and kind == "IMAGE" or opts.kind == "image" and kind == "VIDEO":
            continue
        base = _root(kind)
        if 1 <= opts.month <= 12:
            # month run: photo/<Y>/<Y.MM> and video/<Y>/<Y.MM> together (Urlaube folders have no month level)
            out.append(Folder(kind, "regular", _join(base, y, opts.title), base, label=f"{label} {opts.title}"))
            continue
        if opts.scope in ("all", "regular"):
            out.append(Folder(kind, "regular", _join(base, y), base, label=f"{label} {y}"))
        if opts.scope in ("all", "urlaub"):
            out.append(Folder(kind, "urlaub", _join(base, "Urlaube", y), base, label=f"{label} Urlaube {y}"))
    return out


def preview(opts: Opts) -> list[Folder]:
    folders = plan(opts)
    for f in folders:
        f.exists = os.path.isdir(f.path)
        if f.exists:
            f.files = len(timecore.find_files(f.path, opts.depth, f.kind))
    return folders


def steps_for(folder: Folder, opts: Opts) -> list[str]:
    """Which of the selected steps apply to this folder."""
    order = list(STEP_ORDER)
    selected = set(opts.steps)
    if opts.backfill:
        selected.add("ctx")                      # ctx runs right after gps, before ai/nfo, so AI text and .nfo are written with its data
    out = []
    for s in order:
        if s not in selected and s != "mtime":      # the timestamp step ALWAYS runs, last: no run may leave wrong mtimes behind
            continue
        if s == "nfo" and folder.kind != "VIDEO":
            continue
        if s == "collage" and not (folder.kind == "IMAGE" and folder.scope == "urlaub"):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------------------------------------------------
# run state
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Run:
    id: str
    opts: Opts
    folders: list
    status: str = "running"            # running | done | cancelled | error
    error: str = ""
    phase: str = ""
    folder_index: int = 0
    step_label: str = ""
    sub: object = None                 # the Job of the step that is running right now (progress source)
    log: list = field(default_factory=list)          # [(time, level, text)]
    summary: list = field(default_factory=list)      # [{folder, step, ok, text}]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    fix_pending: bool = False          # cancelled/failed run: modified files still await the timestamp fix (confirmed by the user)
    fix_count: int = 0                 # files modified since the run started (when fix_pending)
    fix_done: str = ""                 # result text of the confirmed fix
    db_id: object = None               # row in the SQLite run history (fileindex), None when the index is off
    step_clean: bool = False           # set by a step that verified nothing is left to do (-> remembered per folder)

    def say(self, text: str, level: str = "info") -> None:
        self.log.append((datetime.now().strftime("%H:%M:%S"), level, text))

    def progress(self) -> dict:
        sub = self.sub
        total, done = (getattr(sub, "total", 0), getattr(sub, "done", 0)) if sub else (0, 0)
        return {"status": self.status, "phase": self.phase, "folder_index": self.folder_index + 1,
                "folder_total": len(self.folders), "step": self.step_label,
                "done": done, "total": total, "pct": int(100 * done / total) if total else 0,
                "current_name": getattr(sub, "current_name", "") if sub else "", "error": self.error}

    def new_sub(self, phase: str) -> jobs.Job:
        j = jobs.Job(id=uuid.uuid4().hex)
        j.cancel_event = self.cancel_event          # one cancel for the whole run
        j.phase = phase
        self.sub = j
        self.say(f"    ↳ {phase}")
        return j


RUNS: dict[str, Run] = {}
_lock = threading.Lock()


def get(run_id: str):
    return RUNS.get(run_id)


def _gc() -> None:
    now = time.time()
    for k in [k for k, r in RUNS.items() if r.finished and now - r.finished > _KEEP_SECS]:
        RUNS.pop(k, None)


def active_run():
    return next((r for r in RUNS.values() if r.status == "running"), None)


def start(opts: Opts):
    """Starts the run in a background thread. Only one run at a time (returns the running one if there is one)."""
    with _lock:
        _gc()
        cur = active_run()
        if cur is not None:
            return cur, False
        run = Run(id=uuid.uuid4().hex, opts=opts, folders=preview(opts))
        RUNS[run.id] = run
    threading.Thread(target=_main, args=(run,), daemon=True).start()
    return run, True


# ---------------------------------------------------------------------------------------------------------------------
# Remembered state per folder: a step that found nothing left to do is skipped next time as long as the folder is unchanged.
# Fingerprint = every file under the folder (relative path, size, mtime); stored in <config>/year_run_state.json.
# ---------------------------------------------------------------------------------------------------------------------
_state_lock = threading.Lock()


def state_days() -> int:
    """YEAR_RUN_STATE_DAYS: how long a remembered result counts (default 30; 0 = never skip, always check)."""
    try:
        return max(0, int(os.environ.get("YEAR_RUN_STATE_DAYS", "30") or 30))
    except ValueError:
        return 30


def _state_path() -> str:
    return os.path.join(settings.config_dir(), "year_run_state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _state_path())
    except OSError:
        pass            # remembering is an optimisation only


def folder_fingerprint(path: str, depth: int) -> str:
    """Hash over (relative path, size, mtime) of all files below `path` up to `depth` levels."""
    h = hashlib.sha1()
    base = path.rstrip("/")
    base_depth = base.count("/")
    for root, dirs, files in os.walk(path):
        dirs.sort()
        if root.rstrip("/").count("/") - base_depth >= depth:
            dirs[:] = []
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            h.update(f"{os.path.relpath(fp, base)}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def files_modified_since(path: str, depth: int, since: float) -> list:
    """Every file below `path` (up to depth) whose mtime is >= `since` -- i.e. written during the run."""
    out = []
    base = path.rstrip("/")
    base_depth = base.count("/")
    for root, dirs, files in os.walk(path):
        if root.rstrip("/").count("/") - base_depth >= depth:
            dirs[:] = []
        sibling_dirs = list(dirs)
        skipdirs.prune(dirs)
        for f in files:
            if skipdirs.is_generated_collage(f, sibling_dirs):
                continue            # the Collage module sets these mtimes itself (capture date of a source photo)
            fp = os.path.join(root, f)
            try:
                if os.stat(fp).st_mtime >= since - 2:
                    out.append(fp)
            except OSError:
                continue
    return sorted(out)


def fix_modified_since(run: "Run", folder: "Folder", since: float) -> tuple:
    """Sets the mtime of every file written since `since` (media, .xmp, .nfo, thumbs ...) to its capture time
    (filename first, EXIF as fallback). Returns (changed, unresolved, errors)."""
    from ...core import mtimecore
    files = files_modified_since(folder.path, run.opts.depth, since)
    if not files:
        return 0, 0, 0
    job = run.new_sub("Zeitstempel: seit Laufstart geänderte Dateien")
    res = mtimecore.restore_many(files, job=job)
    return (sum(1 for r in res if r.changed), sum(1 for r in res if r.skipped and not r.changed), sum(1 for r in res if r.error))


def prepare_fix(run: "Run") -> None:
    """Called when a run ends early (cancel/error): counts what still needs a timestamp fix and asks for confirmation."""
    n = 0
    for i, folder in enumerate(run.folders):
        if i > run.folder_index or not folder.exists:
            continue
        n += len(files_modified_since(folder.path, run.opts.depth, run.started))
    run.fix_count = n
    run.fix_pending = n > 0
    if run.fix_pending:
        run.say(f"⚠ {n} Datei(en) wurden in diesem Lauf geändert und haben noch keinen richtigen Zeitstempel – bitte mit „OK“ bestätigen.", "warn")


def finish_fix(run: "Run") -> bool:
    """Confirmed by the user ("OK"): runs the timestamp fix for the folders this (cancelled) run touched."""
    with _lock:
        if not run.fix_pending or run.status == "running" or active_run() is not None:
            return False
        run.status, run.fix_pending = "running", False
        run.cancel_event.clear()
    threading.Thread(target=_finish_main, args=(run,), daemon=True).start()
    return True


def _finish_main(run: "Run") -> None:
    hb_stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(run, hb_stop), daemon=True).start()
    try:
        run.step_label = "Zeitstempel richtigstellen"
        tot = [0, 0, 0]
        last = run.folder_index
        for i, folder in enumerate(run.folders):
            if i > last or not folder.exists or run.cancel_event.is_set():
                continue
            run.folder_index = i
            run.phase = folder.label
            run.say(f"[{folder.label}] Zeitstempel richtigstellen …")
            c, u, e = fix_modified_since(run, folder, run.started)
            tot = [tot[0] + c, tot[1] + u, tot[2] + e]
            run.say(f"[{folder.label}] Zeitstempel: {c} gesetzt" + (f", {u} ohne ermittelbares Datum" if u else "") + (f", {e} Fehler" if e else ""))
        run.fix_done = f"{tot[0]} Zeitstempel gesetzt" + (f", {tot[1]} ohne ermittelbares Datum (unverändert)" if tot[1] else "") + (f", {tot[2]} Fehler" if tot[2] else "")
        _rec(run, {"folder": "(abgebrochener Lauf)", "step": "Zeitstempel richtigstellen", "ok": not tot[2], "text": run.fix_done})
        run.say("=== Zeitstempel richtiggestellt ===")
        run.folder_index = last
    except Exception as e:      # noqa: BLE001
        run.fix_done = f"Fehler: {e}"
        run.say(f"Zeitstempel-Korrektur fehlgeschlagen: {e}", "error")
    finally:
        hb_stop.set()
        run.sub = None
        run.status = "cancelled"
        run.finished = time.time()


def _sig(opts: Opts) -> str:
    return f"aim={int(opts.aim)}|fixbad={int(opts.fixbad)}|bf={int(opts.backfill)}|tz={opts.default_tz}|depth={opts.depth}"


HEARTBEAT_SECS = 20


def _heartbeat(run: Run, stop: threading.Event) -> None:
    """Writes a progress line to the log every few seconds while a step works, so the log shows life between steps."""
    last = None
    while not stop.wait(HEARTBEAT_SECS):
        sub = run.sub
        if sub is None or not getattr(sub, "total", 0):
            continue
        state = (id(sub), sub.done)
        if state == last:
            continue
        last = state
        name = getattr(sub, "current_name", "")
        run.say(f"      … {getattr(sub, 'phase', '')}: {sub.done} / {sub.total}" + (f" · {name}" if name else ""))


def _rec(run: Run, entry: dict) -> None:
    """Result line of a step: into the run summary and into the SQLite history."""
    run.summary.append(entry)
    fileindex.run_step(run.db_id, entry["folder"], entry["step"], entry["ok"], entry["text"])


def _main(run: Run) -> None:
    hb_stop = threading.Event()
    run.db_id = fileindex.run_begin(f"{'Monat' if run.opts.month else 'Jahr'} {run.opts.title}",
                                    {"kind": run.opts.kind, "scope": run.opts.scope, "depth": run.opts.depth, "aim": run.opts.aim, "fixbad": run.opts.fixbad,
                                     "backfill": run.opts.backfill, "recheck": run.opts.recheck, "steps": list(run.opts.steps)})
    threading.Thread(target=_heartbeat, args=(run, hb_stop), daemon=True).start()
    try:
        run.say(f"=== {'Monat' if run.opts.month else 'Jahr'} {run.opts.title}: Typ={run.opts.kind}, Scope={run.opts.scope}, Tiefe={run.opts.depth}, "
                f"AIM={'ja' if run.opts.aim else 'nein'}, KI-Müll reparieren={'ja' if run.opts.fixbad else 'nein'}, Neue Daten nachtragen={'ja' if run.opts.backfill else 'nein'}, Schritte={', '.join(STEP_LABELS[s] for s in run.opts.steps)} ===")
        for i, folder in enumerate(run.folders):
            run.folder_index = i
            if run.cancel_event.is_set():
                break
            if not folder.exists:
                run.say(f"[{folder.label}] Ordner nicht vorhanden – übersprungen: {folder.path}", "warn")
                continue
            run.phase = folder.label
            run.say(f"--- {folder.label}: {folder.path} ({folder.files} Datei(en)) ---")
            touched: list = []
            use_state = state_days() > 0
            fp0 = folder_fingerprint(folder.path, run.opts.depth) if use_state else ""
            with _state_lock:
                known = (_load_state().get(folder.path, {}) if use_state else {})
            sig = _sig(run.opts)
            clean_steps: dict = {}
            for step in steps_for(folder, run.opts):
                if run.cancel_event.is_set():
                    break
                run.step_label = STEP_LABELS[step]
                mem = known.get(step) or {}
                if (use_state and not run.opts.recheck and mem.get("fp") == fp0 and mem.get("sig") == sig
                        and time.time() - mem.get("at", 0) < state_days() * 86400):
                    when = datetime.fromtimestamp(mem["at"]).strftime("%d.%m.%Y %H:%M")
                    text = f"unverändert seit dem letzten Lauf ({when}) – übersprungen"
                    _rec(run, {"folder": folder.label, "step": STEP_LABELS[step], "ok": True, "text": text})
                    run.say(f"[{folder.label}] {STEP_LABELS[step]}: {text}")
                    clean_steps[step] = mem["at"]
                    continue
                run.say(f"[{folder.label}] {STEP_LABELS[step]} …")
                run.step_clean = False
                try:
                    text = STEP_FUNCS[step](run, folder, touched)
                    _rec(run, {"folder": folder.label, "step": STEP_LABELS[step], "ok": True, "text": text})
                    run.say(f"[{folder.label}] {STEP_LABELS[step]}: {text}")
                    if run.step_clean and not run.cancel_event.is_set():
                        clean_steps[step] = time.time()
                except Exception as e:      # noqa: BLE001 -- one failing step must not stop the rest of the year
                    _rec(run, {"folder": folder.label, "step": STEP_LABELS[step], "ok": False, "text": str(e)})
                    tb = traceback.extract_tb(e.__traceback__)[-1]
                    run.say(f"[{folder.label}] {STEP_LABELS[step]} FEHLER: {type(e).__name__}: {e}  (in {os.path.basename(tb.filename)}:{tb.lineno}, {tb.name})", "error")
                    run.log.append((datetime.now().strftime("%H:%M:%S"), "debug", traceback.format_exc(limit=4)))
                    logging.getLogger("year_run").error("Jahreslauf %s / %s: %s", folder.label, STEP_LABELS[step], e, exc_info=True)   # also in `docker compose logs`
            if use_state and clean_steps and not run.cancel_event.is_set():
                fp1 = folder_fingerprint(folder.path, run.opts.depth)      # after all writes of this run
                with _state_lock:
                    st = _load_state()
                    st[folder.path] = {k: {"fp": fp1, "sig": sig, "at": at} for k, at in clean_steps.items()}
                    _save_state(st)
        run.status = "cancelled" if run.cancel_event.is_set() else "done"
        run.say("=== Abgebrochen ===" if run.status == "cancelled" else f"=== Fertig mit {'Monat' if run.opts.month else 'Jahr'} {run.opts.title} ===")
        if run.status == "cancelled":
            prepare_fix(run)
    except Exception as e:          # noqa: BLE001
        run.status, run.error = "error", str(e)
        run.say(f"Abbruch: {e}", "error")
        try:
            prepare_fix(run)
        except Exception:           # noqa: BLE001
            pass
    finally:
        hb_stop.set()
        run.sub = None
        run.finished = time.time()
        fileindex.run_end(run.db_id, run.status, run.error)


# ---------------------------------------------------------------------------------------------------------------------
# steps -- each returns a one-line summary text; `touched` collects media paths written so far in this folder
# ---------------------------------------------------------------------------------------------------------------------
def _all_step(paths: list) -> stepper.Step:
    """One 'step' holding every path (the Jahreslauf never pages in batches)."""
    return stepper.Step(paths=list(paths), total=len(paths), first=1 if paths else 0, last=len(paths), next_offset=len(paths))


def step_rename(run: Run, folder: Folder, touched: list) -> str:
    """Timestamp & Rename with the module's defaults: rename to YYYY.MM.DD-HH.MM.SS + pin the .xmp date (no EXIF rewrite)."""
    import os
    from ..timestamp_tool import router as t
    cands, _named = timecore.scan_candidates(folder.path, run.opts.depth, folder.kind, False)
    cands = [p for p in cands
             if not ((b := timecore.generated_companion_base_stem(p)) is not None and timecore.has_sibling_media(p, b))
             and not (os.path.splitext(p)[1].lower() == ".nfo"
                      and timecore.has_sibling_media(p, os.path.splitext(os.path.basename(p))[0]))]
    if not cands:
        run.step_clean = True
        return "nichts umzubenennen"
    job = run.new_sub("Umbenennen: Zeit ermitteln")
    job.total = len(cands)

    def cb(done, total, name):
        job.done, job.total, job.current_name = done, total, name
    res = timecore.resolve_batch(cands, "EXIF", run.opts.default_tz, "ALL", folder.kind, progress_cb=cb, cancel_event=run.cancel_event)
    timecore.assign_unique_targets(res)
    rows = t._rows_from_resolutions(res, folder.path)
    sel = {r["path"] for r in rows if r.get("row_type") == "primary"}
    ex = run.new_sub("Umbenennen: schreiben")
    out = t._run_execute(rows, sel, True, False, 120, "", "", job=ex, do_xmpdate=True, do_rotate=True)    # rotation default-on like the page
    results = out.get("results", [])
    renamed = sum(1 for r in results if r.get("renamed_to"))
    errs = sum(1 for r in results if r.get("error"))
    run.step_clean = not errs
    return f"{len(sel)} Datei(en) geprüft, {renamed} umbenannt" + (f", {errs} Fehler" if errs else "")


def step_gps(run: Run, folder: Folder, touched: list) -> str:
    from ..gps_sidecar import router as g
    is_video = folder.kind == "VIDEO"
    paths = timecore.find_files(folder.path, run.opts.depth, folder.kind)
    if not paths:
        run.step_clean = True
        return "keine Dateien"
    geo_cfg = settings.load_geo_settings()
    devices = [d for d, _label in (geo_cfg.traccar_devices or [])]
    scan_job = run.new_sub("GPS: Metadaten + Track")
    scan_job.total = len(paths)
    scan = g.browse_start_work(
        scan_job, camera_model="", candidate_paths=paths, default_tz=run.opts.default_tz, delete_srt=True,
        depth=run.opts.depth, embed_in_image=not is_video, fixed_location="", override_gps_from_track=False,
        path=folder.path, remember_no_gps=False, retry_no_gps=False, root_path=folder.root_path,
        skip_already_done=True, step=_all_step(paths), traccar_device_ids=devices, type_filter=folder.kind,
        use_dawarich=True, use_dawarich_import=bool(geo_cfg.dawarich_server and geo_cfg.dawarich_import_api_key),
        use_dji_srt=is_video)   # Dawarich archive account: automatically, as soon as DAWARICH_IMPORT_API_KEY is set
    for err in scan.get("track_fetch_errors", []) or []:
        run.say(f"  GPS: {err}", "warn")
    rows = scan.get("rows", [])
    if not rows:
        run.step_clean = True
        return f"nichts zu tun ({scan.get('skipped_done', 0)} bereits fertig)"
    geo_job = run.new_sub("GPS: Geocoding")
    geo_job.total = len(rows)
    ctx = g.geocode_start_work(
        geo_job, delete_srt=True, embed_in_image=not is_video, handoff_path=folder.path, handoff_root_path=folder.root_path,
        scanned_rows=rows, selected_set={r["path"] for r in rows}, step_ctx={}, to_process=rows)
    rows = ctx.get("rows", rows) if not run.cancel_event.is_set() else []
    usable = [r for r in rows if r.get("geo") or r.get("lat") is not None]
    nogps = len(rows) - len(usable)
    if not usable:
        return f"{len(rows)} Datei(en) ohne GPS (weder Datei noch Track) – nichts geschrieben"
    ex_job = run.new_sub("GPS: Sidecar schreiben")
    out = g._run_execute(usable, {r["path"] for r in usable}, not is_video, True, folder.path, folder.root_path, job=ex_job)
    results = out.get("results", [])
    ok = [r for r in results if r.get("xmp_written")]
    touched.extend(r["path"] for r in ok)
    errs = [r for r in results if r.get("xmp_error") or r.get("image_error")]
    for r in errs[:10]:
        run.say(f"  GPS: {os.path.basename(r['path'])}: {r.get('xmp_error') or r.get('image_error')}", "error")
    if len(errs) > 10:
        run.say(f"  GPS: … und {len(errs) - 10} weitere Fehler", "error")
    run.step_clean = not nogps and not errs      # files without a position stay open: a track may arrive later
    return f"{len(ok)} Sidecar geschrieben" + (f", {nogps} ohne GPS" if nogps else "") + (f", {len(errs)} mit Fehlern" if errs else "")


def _ai_backfill(run: Run, folder: Folder, touched: list) -> str:
    """Redo the AI text of every file whose sidecar says 'new context data arrived after the text' (AiStale), then mark the
    .nfo of a video as stale too. The flag lives in the sidecar, so a run that was interrupted yesterday is picked up today."""
    from ..ai_description import router as a
    is_video = folder.kind == "VIDEO"
    stale = [p for p in ctxfill.find_flagged(a._candidate_files(folder.path, run.opts.depth, folder.kind), "ai") if os.path.exists(p)]
    if not stale:
        return ""
    run.say(f"AI: {len(stale)} Beschreibung(en) sind älter als die neuen Daten – werden neu erzeugt")
    scan_job = run.new_sub("AI: veraltete Beschreibungen vorbereiten")
    scan_job.total = len(stale)
    scan = a.browse_start_work(scan_job, camera_model="", candidates=stale, depth=run.opts.depth, embed_in_image=not is_video,
                               overwrite_existing=True, path=folder.path, root_path=folder.root_path,
                               step=_all_step(stale), type_filter=folder.kind, fix_bad=False)
    rows = scan.get("rows", [])
    if not rows or run.cancel_event.is_set():
        return ""
    gen_job = run.new_sub("AI: veraltete Beschreibungen neu erzeugen")
    gen_job.total = len(rows)
    gen = a.generate_start_work(gen_job, embed_in_image=not is_video, path=folder.path, root_path=folder.root_path,
                                scanned_rows=rows, selected_set={r["path"] for r in rows}, step_ctx={}, to_process=rows)
    rows = gen.get("rows", []) if not run.cancel_event.is_set() else []
    todo = [r for r in rows if r.get("new_description")]
    fails = len(stale) - len(todo)
    if not todo:
        return f"neue Daten: {fails} ohne KI-Ergebnis (bleiben markiert)"
    ex_job = run.new_sub("AI: veraltete Beschreibungen schreiben")
    out = a._run_execute(todo, {r["path"] for r in todo}, not is_video, folder.path, folder.root_path, job=ex_job)
    ok = [r for r in out.get("results", []) if r.get("xmp_written")]
    for r in ok:
        ctxfill.clear_flag(r["path"], "ai")
        if is_video:
            ctxfill.set_flag(r["path"], "nfo")         # the .nfo carries the AI text -> must follow
    touched.extend(r["path"] for r in ok if r["path"] not in touched)
    return f"neue Daten: {len(ok)} Beschreibung(en) neu" + (f", {fails} ohne Ergebnis (bleiben markiert)" if fails else "")


def step_ai(run: Run, folder: Folder, touched: list) -> str:
    bf = _ai_backfill(run, folder, touched) if run.opts.backfill else ""
    text = _step_ai_main(run, folder, touched)
    return (bf + "; " if bf else "") + text


def _step_ai_main(run: Run, folder: Folder, touched: list) -> str:
    from ..ai_description import router as a
    is_video = folder.kind == "VIDEO"
    if run.opts.aim or run.opts.fixbad or not touched:
        cand = a._candidate_files(folder.path, run.opts.depth, folder.kind)      # fill-missing over the whole folder
    else:
        cand = [p for p in touched if os.path.exists(p)]                          # old script: only what this run just geotagged
    if not cand:
        run.step_clean = True
        return "keine Dateien"
    scan_job = run.new_sub("AI: vorhandene Beschreibungen prüfen")
    scan_job.total = len(cand)
    scan = a.browse_start_work(scan_job, camera_model="", candidates=cand, depth=run.opts.depth, embed_in_image=not is_video,
                               overwrite_existing=False, path=folder.path, root_path=folder.root_path,
                               step=_all_step(cand), type_filter=folder.kind, fix_bad=run.opts.fixbad, use_index=not run.opts.recheck)
    rows = scan.get("rows", [])
    bad = [r for r in rows if r.get("bad_reason")]
    if bad:
        run.say(f"AI: {len(bad)} unbrauchbare Beschreibung(en) gefunden – werden neu erzeugt", "warn")
        for r in bad[:10]:
            run.say(f"   {r['filename']}: {r['bad_reason']}")
    if run.opts.fixbad and not run.opts.aim:
        rows = [r for r in rows if r.get("bad_reason") or r["path"] in set(touched)]      # without AIM: no folder-wide filling
    if not rows:
        run.step_clean = True
        return f"nichts zu tun ({scan.get('skipped_done', 0)} haben schon eine Beschreibung)"
    gen_job = run.new_sub("AI: Beschreibung generieren")
    gen_job.total = len(rows)
    gen = a.generate_start_work(gen_job, embed_in_image=not is_video, path=folder.path, root_path=folder.root_path,
                                scanned_rows=rows, selected_set={r["path"] for r in rows}, step_ctx={}, to_process=rows)
    rows = gen.get("rows", []) if not run.cancel_event.is_set() else []
    fails = [r for r in rows if not r.get("new_description")]
    todo = [r for r in rows if r.get("new_description") or r.get("context_fields")]
    if not todo:
        return f"{len(fails)} ohne Ergebnis (KI-Provider nicht erreichbar?)"
    ex_job = run.new_sub("AI: Beschreibung schreiben")
    out = a._run_execute(todo, {r["path"] for r in todo}, not is_video, folder.path, folder.root_path, job=ex_job)
    results = out.get("results", [])
    ok = [r for r in results if r.get("xmp_written")]
    touched.extend(r["path"] for r in ok if r["path"] not in touched)
    for r in ok:                                       # a text made now already uses the current data
        if ctxfill.read_flags(r["path"])["ai"]:
            ctxfill.clear_flag(r["path"], "ai")
    run.step_clean = not fails and len(ok) == len(results)
    return f"{len(ok)} Beschreibung(en) geschrieben" + (f", {len(fails)} ohne KI-Ergebnis" if fails else "")


def _nfo_rows(run: Run, folder: Folder, mode: str, label: str) -> list:
    from ..nfo_maker import router as n
    rows, scan_id, offset = [], "", 0
    while True:
        job = run.new_sub(f"NFO: {label} suchen")
        res = n.scan_start_work(job, depth=max(1, run.opts.depth), handoff_entry=None, include_existing=False,
                                mode=mode, offset=offset, path=folder.path, scan_id=scan_id)
        rows += res.get("rows", [])
        st = res.get("step") or {}
        if run.cancel_event.is_set() or not st.get("has_more"):
            return rows
        scan_id, offset = st.get("scan_id", ""), st.get("next_offset", 0)


def _nfo_backfill(run: Run, folder: Folder, defaults) -> str:
    """Rewrite the .nfo of every video whose sidecar says 'new data / new AI text arrived after the .nfo' (NfoStale)."""
    from ..nfo_maker import router as n
    videos, _msg = n._candidate_videos(folder.path, run.opts.depth, "normal")
    stale = [v for v in ctxfill.find_flagged(videos, "nfo") if os.path.exists(v)]
    if not stale:
        return ""
    run.say(f"NFO: {len(stale)} .nfo sind älter als die neuen Daten – werden neu geschrieben")
    scan_job = run.new_sub("NFO: veraltete .nfo vorbereiten")
    res = n.scan_start_work(scan_job, depth=max(1, run.opts.depth), handoff_entry={"file_paths": stale}, include_existing=True,
                            mode="normal", offset=0, path=folder.path, scan_id="")
    rows = res.get("rows", [])
    if not rows or run.cancel_event.is_set():
        return ""
    job = run.new_sub("NFO: veraltete .nfo schreiben")
    job.total = len(rows)
    out = n.generate_start_work(job, ai_description_enabled=defaults.ai_description_enabled, defaults=defaults, force=True, force_jpg=False,
                                force_timestamp=False, mode="normal", older_val=defaults.older, skip_location=defaults.skip_location, to_process=rows)
    done = [r for r in out.get("results", []) if r.get("nfo_written")]
    for r in done:
        ctxfill.clear_flag(r["path"], "nfo")
    err = [r for r in out.get("results", []) if r.get("error")]
    return f"neue Daten: {len(done)} .nfo neu geschrieben" + (f", {len(err)} Fehler (bleiben markiert)" if err else "")


def step_nfo(run: Run, folder: Folder, touched: list) -> str:
    from ..nfo_maker import router as n
    defaults = settings.load_nfo_defaults()
    parts = []
    if run.opts.backfill:
        bf = _nfo_backfill(run, folder, defaults)
        if bf:
            parts.append(bf)
    passes = [("normal", "neue .nfo")]
    if run.opts.fixbad:
        passes.append(("repair-bad-ai", "unbrauchbare AI-Beschreibung"))
    if run.opts.aim:
        passes.append(("repair-ai", "AI-Reparatur"))          # old script: --repair-ai together with -AIM
        passes.append(("repair-missing-ai", "fehlende AI-Beschreibung"))      # ... and --repair-missing-ai
    if defaults.ai_description_enabled:
        passes.append(("repair-missing", "fehlende Beschreibung/Ort"))       # old script after -GM/-LM: location now known
    clean = True
    for mode, label in passes:
        rows = _nfo_rows(run, folder, mode, label)
        todo = [r for r in rows if not r.get("would_skip")]
        if not todo:
            parts.append(f"{label}: nichts zu tun")
            continue
        job = run.new_sub(f"NFO: {label} schreiben")
        job.total = len(todo)
        ai_on = True if mode in ("repair-ai", "repair-bad-ai", "repair-missing-ai") else defaults.ai_description_enabled     # like the NFO module's own checkbox
        out = n.generate_start_work(job, ai_description_enabled=ai_on, defaults=defaults, force=False, force_jpg=False,
                                    force_timestamp=False, mode=mode, older_val=defaults.older,
                                    skip_location=defaults.skip_location, to_process=todo)
        res = out.get("results", [])
        ok = [r for r in res if r.get("nfo_written")]
        err = [r for r in res if r.get("error")]
        parts.append(f"{label}: {len(ok)} geschrieben" + (f", {len(err)} Fehler" if err else ""))
        clean = clean and not err
    run.step_clean = clean and not run.cancel_event.is_set()
    return "; ".join(parts)


def step_ctx(run: Run, folder: Folder, touched: list) -> str:
    from ..ctx_fill import router as c
    if not geoservice.enabled():
        return "übersprungen (GEO_SERVICE_URL nicht gesetzt)"
    types = {"VIDEO": "video", "IMAGE": "image"}[folder.kind]
    paths = fillcore.find_media(folder.path, max(1, run.opts.depth), types)
    if not paths:
        run.step_clean = True
        return "keine Dateien"
    scan_job = run.new_sub("Kontext: Sidecars lesen")
    scan_job.total = len(paths)
    scan = c.scan_start_work(scan_job, path=folder.path, paths=paths, step=_all_step(paths), use_index=not run.opts.recheck)
    rows = (scan or {}).get("rows", [])
    if not rows:
        run.step_clean = True
        return "nichts zu tun – alles vollständig"
    job = run.new_sub("Kontext: nachfüllen")
    job.total = len(rows)
    out = c.apply_work(job, chosen=[r["path"] for r in rows], embed=(folder.kind == "IMAGE"), gset=set(ctxfill.GROUPS), overwrite=False)
    # ok_n only means "no error" -- a file for which the geo-service delivered nothing is ok as well; count what was really added
    res = out.get("results", [])
    reasons: dict = {}
    for r in res:
        for note in r.get("notes", []):
            reasons[note] = reasons.get(note, 0) + 1
    for note, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]:
        run.say(f"  Kontext: {n}× {note}", "warn")
    empty = sum(1 for r in res if r["ok"] and not r["added"] and not r["nfo"])
    return (f"{out['added_n']} Datei(en) mit neuen Kontext-Daten, {out['nfo_n']} .nfo ergänzt"
            + (f", {empty} ohne neue Daten (Gründe oben im Log)" if empty else "")
            + (f", {out['err_n']} Fehler" if out["err_n"] else ""))


def step_mtime(run: Run, folder: Folder, touched: list) -> str:
    """Runs LAST, always: (1) the module's scan over all media + sidecars (anything with a wrong mtime), (2) every other file
    written during this run (.nfo, thumbs, ... which the scan does not cover) -> capture time of its name/EXIF."""
    from ..mtime_restore import router as m
    parts, clean = [], True
    cand = m._candidate_files(folder.path, run.opts.depth, folder.kind)
    if cand:
        job = run.new_sub("Zeitstempel: prüfen")
        job.total = len(cand)
        scan = m.browse_start_work(job, candidates=cand, find_rotation="", path=folder.path, step=_all_step(cand))
        rows = [r for r in (scan or {}).get("rows", []) if r.get("target") and r.get("differs", True)]
        if rows:
            results = m.apply_rows(rows, {r["path"] for r in rows}, set(), "")
            changed = sum(1 for r in results if r.get("changed"))
            errs = sum(1 for r in results if r.get("error"))
            clean = clean and not errs
            parts.append(f"{changed} Zeitstempel gesetzt" + (f", {errs} Fehler" if errs else ""))
        else:
            parts.append("alle Medien-Zeitstempel passen")
    if not run.cancel_event.is_set():
        c, u, e = fix_modified_since(run, folder, run.started)
        clean = clean and not e
        if c or e:
            parts.append(f"weitere in diesem Lauf geschriebene Dateien: {c} gesetzt" + (f", {e} Fehler" if e else ""))
    run.step_clean = clean and not run.cancel_event.is_set()
    return "; ".join(parts) or "keine Dateien"


def step_collage(run: Run, folder: Folder, touched: list) -> str:
    from ..collage import router as co
    if not any(e.is_dir() for e in os.scandir(folder.path)):
        return "kein Urlaubs-Unterordner – übersprungen"
    job = run.new_sub("Collage: Collagen suchen")
    found = co.discover_start_work(job, path=folder.path)
    trips = [t for t in found.get("trips", []) if not t.get("already_exists")]
    if not trips:
        run.step_clean = True
        return "alle Collagen sind schon vorhanden"
    d = settings.load_collage_defaults()
    d = d.to_dict() if hasattr(d, "to_dict") else asdict(d)
    cs = co._settings_from_dict({**d, "force": False})
    gjob = run.new_sub("Collage: erstellen")
    gjob.total = len(trips)
    out = co.generate_start_work(gjob, cs, trips)
    res = out.get("results", [])
    made = sum(1 for r in res if r.get("status") == "created")
    err = sum(1 for r in res if r.get("status") == "error")
    run.step_clean = not err
    return f"{made} Collage(n) erstellt" + (f", {err} Fehler" if err else "")


STEP_FUNCS = {"rename": step_rename, "gps": step_gps, "ai": step_ai, "nfo": step_nfo, "ctx": step_ctx, "mtime": step_mtime, "collage": step_collage}
