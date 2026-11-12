"""
mtime_routes.py -- shared "Datei-Zeitstempel wiederherstellen" endpoints with progress/cancel
(one exiftool call per file can take a while), used by Timestamp & Rename, GPS Sidecar and
AI-Beschreibung:  POST <prefix>/restore-mtime/start, GET .../progress/{id}, POST .../cancel/{id}.
The old synchronous POST <prefix>/restore-mtime stays in each module for scripts/tests.
"""
from __future__ import annotations

import html

from fastapi import Form, Request
from fastapi.responses import HTMLResponse

from . import jobs, mtimecore


def add_restore_routes(router, templates, prefix: str, check_allowed) -> None:
    def ctx(job):
        base = f"{prefix}/restore-mtime"
        return {"job_id": job.id, "job": job.to_progress_dict(),
                "poll_url": f"{base}/progress/{job.id}", "progress_url": f"{base}/progress/{job.id}",
                "cancel_url": f"{base}/cancel/{job.id}", "module_prefix": prefix}

    @router.post("/restore-mtime/start", response_class=HTMLResponse)
    def restore_start(request: Request, paths: list[str] = Form(default=[])):
        for p in paths:
            check_allowed(p)
        job = jobs.create_job()
        job.total = len(paths)
        job.phase = "Zeitstempel"
        jobs.run_job(job, lambda: [r.__dict__ for r in mtimecore.restore_many(paths, job=job)])
        return templates.TemplateResponse(request, "_progress.html", ctx(job))

    @router.get("/restore-mtime/progress/{job_id}", response_class=HTMLResponse)
    def restore_progress(request: Request, job_id: str):
        job = jobs.get_job(job_id)
        if job is None:
            return HTMLResponse('<div class="error">Vorgang nicht gefunden (abgelaufen oder Server neu gestartet).</div>')
        if job.status == "running":
            return templates.TemplateResponse(request, "_progress.html", ctx(job))
        if job.status == "error":
            return HTMLResponse(f'<div class="error">Fehler: {html.escape(job.error)}</div>')
        return templates.TemplateResponse(request, "_mtime_results.html", {"results": job.result or []})

    @router.post("/restore-mtime/cancel/{job_id}")
    def restore_cancel(job_id: str):
        jobs.cancel_job(job_id)
        return HTMLResponse(status_code=204, content="")
