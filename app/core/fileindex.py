"""
fileindex.py -- a SQLite index in front of the per-file scans (config folder, file_index.sqlite).

THE FILE IS ALWAYS THE MASTER. The index only remembers what a scan found out about a file, keyed by the file's stat signature
(size + mtime + ctime of the sidecar / .nfo / media file). As soon as a signature differs, the entry is ignored and the file is read
again -- so the index can be deleted, restored from a backup or be wrong without ever changing a result, only the speed. Entries older
than FILE_INDEX_DAYS (default 365) are ignored too; "Alles neu prüfen" (recheck) bypasses it for the run.

Used for:  ctx_cache  the result of ctxfill.inspect() per sidecar (saves the exiftool pass over the sidecars + the text parsing)
           ai_known   "this file has an AI description" (saves one exiftool call per file in the AI scan; never caches "no")
           runs / run_steps   history of every Jahreslauf (what ran when, with which options, what each step did)

FILE_INDEX=off disables all of it (every function then behaves as "not found" / does nothing).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Optional

from . import settings

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_conn_path = ""


def enabled() -> bool:
    return os.environ.get("FILE_INDEX", "on").strip().lower() not in ("off", "0", "false", "no")


def max_age_days() -> int:
    try:
        return max(0, int(os.environ.get("FILE_INDEX_DAYS", "365") or 365))
    except ValueError:
        return 365


def db_path() -> str:
    return os.environ.get("FILE_INDEX_DB") or os.path.join(settings.config_dir(), "file_index.sqlite")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ctx_cache (path TEXT PRIMARY KEY, sig TEXT NOT NULL, env TEXT NOT NULL, info TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ai_known (path TEXT PRIMARY KEY, sig TEXT NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL NOT NULL, finished REAL, title TEXT, opts TEXT, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS run_steps (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, folder TEXT, step TEXT, ok INTEGER, text TEXT, at REAL);
"""


def _db() -> Optional[sqlite3.Connection]:
    """One shared connection (WAL); None when disabled or the file cannot be opened -- callers then simply work without the index."""
    global _conn, _conn_path
    if not enabled():
        return None
    path = db_path()
    with _lock:
        if _conn is not None and _conn_path == path:
            return _conn
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            c = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(_SCHEMA)
            _conn, _conn_path = c, path
            return c
        except (sqlite3.Error, OSError):
            return None


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None


def _stat_sig(*paths: str) -> str:
    """'size:mtime_ns:ctime_ns' per file ('-' if absent), joined. ctime cannot be set by touch/utime, so a restored mtime
    (the Jahreslauf resets mtimes to the capture time) still changes the signature when the content was rewritten."""
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append(f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}")
        except OSError:
            out.append("-")
    return "|".join(out)


def _fresh(seen: float) -> bool:
    d = max_age_days()
    return d > 0 and time.time() - seen < d * 86400


def _one(sql: str, args: tuple = ()):
    c = _db()
    if c is None:
        return None
    try:
        with _lock:
            return c.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


def _exec(sql: str, args: tuple = ()) -> None:
    c = _db()
    if c is None:
        return
    try:
        with _lock:
            c.execute(sql, args)
    except sqlite3.Error:
        pass


# ---- ctx scan cache -------------------------------------------------------------------------------------------------------------
def ctx_sig(media_path: str, is_video: bool) -> str:
    return _stat_sig(media_path + ".xmp", os.path.splitext(media_path)[0] + ".nfo") if is_video else _stat_sig(media_path + ".xmp")


def ctx_get(media_path: str, is_video: bool, env: str) -> Optional[dict]:
    row = _one("SELECT sig, env, info, seen FROM ctx_cache WHERE path=?", (media_path,))
    if not row or row[0] != ctx_sig(media_path, is_video) or row[1] != env or not _fresh(row[3]):
        return None
    try:
        return json.loads(row[2])
    except ValueError:
        return None


def ctx_put_many(items: list) -> None:
    """items: [(media_path, is_video, env, info_dict)] -- one transaction for a whole scan."""
    c = _db()
    if c is None or not items:
        return
    now = time.time()
    try:
        with _lock:
            c.execute("BEGIN")
            c.executemany("INSERT OR REPLACE INTO ctx_cache(path, sig, env, info, seen) VALUES (?,?,?,?,?)",
                          [(p, ctx_sig(p, v), env, json.dumps(info), now) for p, v, env, info in items])
            c.execute("COMMIT")
    except sqlite3.Error:
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass


# ---- "has an AI description" ----------------------------------------------------------------------------------------------------
def ai_sig(media_path: str) -> str:
    return _stat_sig(media_path + ".xmp", media_path)        # a photo may carry the text embedded (fallback), so the media file counts


def ai_known(media_path: str) -> bool:
    row = _one("SELECT sig, seen FROM ai_known WHERE path=?", (media_path,))
    return bool(row and row[0] == ai_sig(media_path) and _fresh(row[1]))


def ai_remember(media_path: str) -> None:
    _exec("INSERT OR REPLACE INTO ai_known(path, sig, seen) VALUES (?,?,?)", (media_path, ai_sig(media_path), time.time()))


# ---- run history ----------------------------------------------------------------------------------------------------------------
def run_begin(title: str, opts: dict) -> Optional[int]:
    c = _db()
    if c is None:
        return None
    try:
        with _lock:
            cur = c.execute("INSERT INTO runs(started, title, opts, status) VALUES (?,?,?,'running')", (time.time(), title, json.dumps(opts, ensure_ascii=False)))
            return cur.lastrowid
    except sqlite3.Error:
        return None


def run_step(run_id: Optional[int], folder: str, step: str, ok: bool, text: str) -> None:
    if run_id is not None:
        _exec("INSERT INTO run_steps(run_id, folder, step, ok, text, at) VALUES (?,?,?,?,?,?)", (run_id, folder, step, int(bool(ok)), text, time.time()))


def run_end(run_id: Optional[int], status: str, note: str = "") -> None:
    if run_id is not None:
        _exec("UPDATE runs SET finished=?, status=?, note=? WHERE id=?", (time.time(), status, note, run_id))


def recent_runs(limit: int = 20) -> list:
    c = _db()
    if c is None:
        return []
    try:
        with _lock:
            rows = c.execute("SELECT id, started, finished, title, status, note FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(id=r[0], started=r[1], finished=r[2], title=r[3], status=r[4], note=r[5]) for r in rows]
    except sqlite3.Error:
        return []


def run_steps(run_id: int) -> list:
    c = _db()
    if c is None:
        return []
    try:
        with _lock:
            rows = c.execute("SELECT folder, step, ok, text, at FROM run_steps WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(folder=r[0], step=r[1], ok=bool(r[2]), text=r[3], at=r[4]) for r in rows]
    except sqlite3.Error:
        return []


def stats() -> dict:
    out = {"enabled": enabled(), "path": db_path(), "ctx_cache": 0, "ai_known": 0, "runs": 0}
    for t in ("ctx_cache", "ai_known", "runs"):
        row = _one(f"SELECT COUNT(*) FROM {t}")
        out[t] = row[0] if row else 0
    return out
