"""
handoff.py — short-lived, in-memory store letting a "Weiter zu ..." link carry the
EXACT list of files a module's "Ausführen" step just touched, instead of only the
folder it was pointed at.

Why this exists: the original "Weiter zu ..." handoff only carried `path`/`root_path`
forward, so the TARGET module's auto-submitted scan re-walks the ENTIRE folder --
the maintainer's own report: renaming 1 of 22 files in a folder with Timestamp & Rename, then
clicking "Weiter zu GPS Sidecar", re-scans all 22 and reports "1 Datei(en) gefunden, 21
bereits abgeschlossen" -- the 21 are complete noise, since only the 1 file that was
JUST touched is actually relevant to carry forward. Every module's own execute() step
already knows exactly which files it just wrote (its own `written_paths`, or an
equivalent explicit list) -- stash() keeps that EXACT list, keyed by a short id, so the
next module's scan/browse can skip the folder walk entirely and use exactly those paths
as its only candidates, via fetch().

Deliberately NOT round-tripped through the browser (no giant hidden JSON field, no
sessionStorage): the "Weiter zu ..." link only ever carries the short id in its query
string: `?handoff_id=<id>`. The target module's own scan/browse endpoint looks the id
up itself, server-side, when it receives it as a form field.

Entries expire after a short TTL -- a handoff link is only ever meant to be clicked
once, right after the results page carrying it was rendered -- so this dict never grows
unbounded even when a batch's handoff link is never followed up on. Not popped on
read, so reloading the results page (or going back to it) and clicking again still
works within the TTL.
"""
from __future__ import annotations

import threading
import time
import uuid

_TTL_SECS = 30 * 60
_lock = threading.Lock()
_store: dict[str, dict] = {}


def _sweep_locked() -> None:
    now = time.time()
    stale = [k for k, v in _store.items() if now - v["created_at"] > _TTL_SECS]
    for k in stale:
        _store.pop(k, None)


def stash(path: str, root_path: str, file_paths: list) -> str:
    """Stores the exact file list a "Weiter zu ..." handoff should carry forward.
    Returns a short id to put in the link's `?handoff_id=...` query param. Callers
    should only do this when file_paths is non-empty -- an empty list means nothing
    was actually touched this run, so there is nothing more precise to hand off than
    the plain path/root_path folder reference the old handoff already offered."""
    with _lock:
        _sweep_locked()
        handoff_id = uuid.uuid4().hex[:12]
        _store[handoff_id] = {
            "path": path, "root_path": root_path, "file_paths": list(file_paths),
            "created_at": time.time(),
        }
    return handoff_id


def fetch(handoff_id: str) -> dict | None:
    """Looks up a stashed handoff by id -- None if missing, empty, or expired (the
    link was clicked too long after the results page that carried it was rendered;
    callers fall back to a normal folder scan in that case, same as before this
    feature existed)."""
    if not handoff_id:
        return None
    with _lock:
        _sweep_locked()
        return _store.get(handoff_id)
