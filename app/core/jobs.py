"""
jobs.py — minimal in-memory background-job tracking for long-running browse/scan
operations, so the web UI can show real per-file progress and a working Cancel button
instead of one opaque "lädt…" spinner blocking the whole request until it's done.

Single-process, in-memory only -- the same assumption this app already makes elsewhere
(config/presets.json, MEDIA_ROOT: one container, one process). Jobs don't survive a
restart, which is fine: they're just in-flight UI state, nothing durable is lost if one
disappears (the person just re-clicks "Vorschau laden").
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

_jobs: dict[str, "Job"] = {}
_lock = threading.Lock()

# Finished jobs older than this are dropped on the next sweep, so a browser tab left open
# (or abandoned) doesn't leak memory forever -- the UI never needs a finished job's state
# once it has rendered the final result once.
_MAX_FINISHED_JOB_AGE_SECS = 600


@dataclass
class Job:
    id: str
    status: str = "running"          # running | done | cancelled | error
    total: int = 0
    done: int = 0
    current_name: str = ""
    phase: str = ""  # optional label for a multi-phase job (e.g. GPS Sidecar's
                      # "Metadaten lesen" vs. "Geocoding" passes); empty = single phase
    result: object = None            # whatever the worker function returns, once done
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_progress_dict(self) -> dict:
        pct = int(100 * self.done / self.total) if self.total else 0
        return {
            "id": self.id, "status": self.status, "total": self.total,
            "done": self.done, "current_name": self.current_name, "pct": pct,
            "error": self.error, "phase": self.phase,
        }


def _sweep_finished_jobs_locked() -> None:
    now = time.time()
    stale = [jid for jid, j in _jobs.items()
             if j.finished_at is not None and now - j.finished_at > _MAX_FINISHED_JOB_AGE_SECS]
    for jid in stale:
        _jobs.pop(jid, None)


def create_job() -> Job:
    with _lock:
        _sweep_finished_jobs_locked()
        job = Job(id=uuid.uuid4().hex)
        _jobs[job.id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(job_id)


def run_job(job: Job, work_fn: Callable[[], object]) -> None:
    """Runs work_fn() in a background thread. work_fn is expected to update
    job.total/job.done/job.current_name as it goes (typically via a progress_cb passed
    into timecore.resolve_batch/resolve_many/scan_folder) and to check job.cancel_event
    itself (also typically by passing it straight into those same functions) so it can
    stop early. Whatever work_fn returns becomes job.result once status flips to "done"."""
    def _runner():
        try:
            result = work_fn()
            job.result = result
            job.status = "cancelled" if job.cancel_event.is_set() else "done"
        except Exception as e:  # noqa: BLE001 -- surfaced to the UI as a plain error row
            job.status = "error"
            job.error = str(e)
        finally:
            job.finished_at = time.time()

    threading.Thread(target=_runner, daemon=True).start()


def cancel_job(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None:
        return False
    job.cancel_event.set()
    return True
