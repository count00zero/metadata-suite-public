"""cache.py -- one small SQLite file for everything worth remembering.

Five key/value tables (reverse-geocoding results, per-UTC-day track chunks, per-day weather, GeoNames-style answers, elevations).
Every call opens its own short-lived connection (WAL mode), so the threadpool FastAPI uses
for sync endpoints never shares a connection between threads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

TABLES = ("reverse", "trackday", "weather", "geonames", "elevation")


class Cache:
    def __init__(self, path: str):
        self.path = path
        self.hits = 0
        self.misses = 0
        self._lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            for t in TABLES:
                c.execute(f"CREATE TABLE IF NOT EXISTS {t} (k TEXT PRIMARY KEY, v TEXT NOT NULL, ts INTEGER NOT NULL, ttl INTEGER NOT NULL)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, table: str, key: str) -> Optional[Any]:
        assert table in TABLES
        with self._conn() as c:
            row = c.execute(f"SELECT v, ts, ttl FROM {table} WHERE k=?", (key,)).fetchone()
        if row is None or (row[2] > 0 and time.time() - row[1] > row[2]):
            with self._lock:
                self.misses += 1
            return None
        with self._lock:
            self.hits += 1
        return json.loads(row[0])

    def get_with_ts(self, table: str, key: str):
        """(value, stored_epoch) like get(), or (None, None)."""
        assert table in TABLES
        with self._conn() as c:
            row = c.execute(f"SELECT v, ts, ttl FROM {table} WHERE k=?", (key,)).fetchone()
        if row is None or (row[2] > 0 and time.time() - row[1] > row[2]):
            with self._lock:
                self.misses += 1
            return None, None
        with self._lock:
            self.hits += 1
        return json.loads(row[0]), row[1]

    def has(self, table: str, key: str) -> bool:
        """Existence check without touching the hit/miss counters (used by the weather backfill)."""
        assert table in TABLES
        with self._conn() as c:
            row = c.execute(f"SELECT ts, ttl FROM {table} WHERE k=?", (key,)).fetchone()
        return row is not None and not (row[1] > 0 and time.time() - row[0] > row[1])

    def put(self, table: str, key: str, value: Any, ttl_s: int) -> None:
        """ttl_s <= 0 means 'never expires'."""
        assert table in TABLES
        with self._lock, self._conn() as c:
            c.execute(f"INSERT OR REPLACE INTO {table} (k, v, ts, ttl) VALUES (?,?,?,?)",
                      (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), int(time.time()), int(ttl_s)))

    def purge_expired(self) -> int:
        n = 0
        now = int(time.time())
        with self._lock, self._conn() as c:
            for t in TABLES:
                n += c.execute(f"DELETE FROM {t} WHERE ttl > 0 AND ts + ttl < ?", (now,)).rowcount
        return n

    def counts(self) -> dict:
        with self._conn() as c:
            return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def weather_span(self) -> dict:
        """What the weather archive holds: rows, permanent rows, first/last UTC day, distinct cells."""
        with self._conn() as c:
            keys = [r[0] for r in c.execute("SELECT k FROM weather WHERE ttl = 0")]
            total = c.execute("SELECT COUNT(*) FROM weather").fetchone()[0]
        days = sorted(k.split("|")[1] for k in keys if "|" in k)
        return {"rows": total, "archived_days": len(keys), "first_day": days[0] if days else None,
                "last_day": days[-1] if days else None, "cells": len({k.split("|")[0] for k in keys})}

    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
