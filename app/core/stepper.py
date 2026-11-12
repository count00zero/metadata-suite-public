"""
stepper.py — "stepped loading" for the list modules (Timestamp, GPS Sidecar, AI-Beschreibung,
NFO Maker, Datei-Zeitstempel, Kurzvideos): instead of reading/resolving EVERY file of a huge
folder before the first row appears, only the first N files are processed ("Schritt 1"); the
whole normal flow (filters, selection buttons, execute, ...) then works on that step, and a
"Nächste N laden" button -- available with or without having executed anything -- loads the next.

How it fits together
  * The cheap part of a scan (walking the folder, filename checks) still covers everything and
    produces the full ordered candidate list. start() snapshots that list server-side under a
    scan_id (in memory, TTL) and returns the first N paths.
  * "Nächste N laden" posts the same browse form plus scan_id + offset; resume() then continues
    from the SNAPSHOT (no re-walk, so already processed/renamed files can neither be skipped nor
    appear twice; paths that no longer exist -- e.g. renamed in the previous step -- are skipped).
  * "Alle verbleibenden laden" is the same request with offset = -(start+1) (see Step.all_offset).
  * N comes from the environment: BATCH_SIZE (global, default 200, 0 = no stepping) with optional
    per-module overrides BATCH_SIZE_TIMESTAMP / _GPS / _AI / _NFO / _MTIME / _SHORTVIDEOS.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_BATCH_SIZE = 200
_TTL_SECS = 6 * 3600
_MAX_SESSIONS = 40

_ENV_SUFFIX = {"timestamp": "TIMESTAMP", "gps": "GPS", "ai": "AI", "nfo": "NFO",
               "mtime": "MTIME", "shortvideos": "SHORTVIDEOS", "gpsfill": "GPSFILL", "ctxfill": "CTXFILL"}

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def batch_size(module: str) -> int:
    """Files per step for a module; 0 = unlimited (stepping off). Read fresh on every call."""
    def _int(name: str) -> Optional[int]:
        raw = (os.environ.get(name) or "").strip().strip("\"'")
        if not raw:
            return None
        try:
            return max(0, int(raw))
        except ValueError:
            return None

    suffix = _ENV_SUFFIX.get(module, module.upper())
    v = _int(f"BATCH_SIZE_{suffix}")
    if v is None:
        v = _int("BATCH_SIZE")
    return DEFAULT_BATCH_SIZE if v is None else v


@dataclass
class Step:
    paths: list
    scan_id: str = ""
    size: int = 0           # configured batch size (0 = stepping off)
    total: int = 0          # candidates in the snapshot
    first: int = 0          # 1-based position of the first file of this step
    last: int = 0           # position of the last file of this step
    next_offset: int = 0    # where the next step starts in the snapshot
    index: int = 1          # step number (1-based)
    meta: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return bool(self.size and self.total > self.size) or self.first > 1

    @property
    def has_more(self) -> bool:
        return self.next_offset < self.total

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.next_offset)

    @property
    def next_count(self) -> int:
        return min(self.size or self.remaining, self.remaining)

    @property
    def pct(self) -> int:
        return int(100 * self.last / self.total) if self.total else 100

    @property
    def all_offset(self) -> int:
        """Offset value that makes resume() load everything that is left (negative = 'all')."""
        return -(self.next_offset + 1)

    def to_dict(self) -> dict:
        return {"scan_id": self.scan_id, "size": self.size, "total": self.total, "first": self.first,
                "last": self.last, "next_offset": self.next_offset, "index": self.index,
                "has_more": self.has_more, "all_offset": self.all_offset, "remaining": self.remaining, "next_count": self.next_count,
                "pct": self.pct, "active": self.active}


def _gc() -> None:
    now = time.time()
    for k in [k for k, v in _sessions.items() if now - v["ts"] > _TTL_SECS]:
        _sessions.pop(k, None)
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.pop(min(_sessions, key=lambda k: _sessions[k]["ts"]), None)


def _slice(snapshot: list, offset: int, size: int) -> tuple[list, int]:
    """Up to `size` still-existing paths from `offset`; returns (paths, next_offset)."""
    out: list = []
    i = max(0, offset)
    while i < len(snapshot) and (size == 0 or len(out) < size):
        p = snapshot[i]
        i += 1
        if os.path.exists(p):
            out.append(p)
    return out, i


def start(module: str, all_paths: list, meta: Optional[dict] = None, unlimited: bool = False) -> Step:
    """New scan: snapshot the ordered candidate list, return step 1.
    unlimited: everything in one step (no "Nächste laden"). Used when the list comes from the previous module's
    "Weiter zu ..." handoff -- the user already chose those files there, so asking again would be noise."""
    size = 0 if unlimited else batch_size(module)
    snapshot = sorted(all_paths)
    paths, nxt = _slice(snapshot, 0, size)
    scan_id = ""
    if size and len(snapshot) > size:
        scan_id = uuid.uuid4().hex[:12]
        with _lock:
            _gc()
            _sessions[scan_id] = {"paths": snapshot, "meta": dict(meta or {}), "ts": time.time(),
                                  "module": module}
    return Step(paths=paths, scan_id=scan_id, size=size, total=len(snapshot), first=1 if paths else 0,
                last=nxt if paths else 0, next_offset=nxt, index=1, meta=dict(meta or {}))


def resume(module: str, scan_id: str, offset: int) -> Optional[Step]:
    """Next step of an earlier scan, or None (unknown/expired id -> caller does a fresh scan)."""
    if not scan_id:
        return None
    with _lock:
        _gc()
        sess = _sessions.get(scan_id)
        if sess:
            sess["ts"] = time.time()
    if not sess or sess.get("module") != module:
        return None
    size = batch_size(module)
    snapshot = sess["paths"]
    load_all = offset < 0               # "Alle verbleibenden laden": offset sent as -(start+1)
    if load_all:
        offset = -offset - 1
    paths, nxt = _slice(snapshot, offset, 0 if load_all else size)
    index = (offset // size) + 1 if size else 1
    return Step(paths=paths, scan_id=scan_id, size=size, total=len(snapshot),
                first=offset + 1 if paths else offset, last=nxt, next_offset=nxt, index=index,
                meta=dict(sess["meta"]))
