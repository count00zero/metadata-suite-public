"""backfill.py -- fill the weather archive ("shadow database") ahead of time.

Which (0.1-degree cell, UTC day) pairs does the archive need? Exactly those where you were:
  * every cell/day that occurs in the Traccar/Dawarich tracks of the requested range, and
  * every cell/day of the points the client sends along (photos/videos with GPS in their sidecar).
Each pair is one Open-Meteo request (a whole day of hourly values) the first time, never again --
finished days are stored permanently (weather.py). Every requested day up to and including today is archived (values of the last days may be
revised slightly later -- accepted); only days in the future are skipped. Rate limits are respected: a pause between requests, a cap per
run, and an HTTP 429 stops the run cleanly; starting it again continues where it stopped.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

CHUNK_S = 5 * 86400
PROBE_S = 90 * 86400                     # empty blocks of this size are skipped with ONE request per source
FLOOR_EPOCH = -946771200                 # 1940-01-01 UTC: the Open-Meteo archive starts there; earlier days are pointless


def _day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


class Job:
    def __init__(self, dry_run: bool):
        self.id = uuid.uuid4().hex[:12]
        self.status = "running"          # running | done | cancelled | rate_limited | capped | error
        self.phase = "start"
        self.dry_run = dry_run
        self.error = ""
        self.cancel = threading.Event()
        self.started = time.time()
        self.finished: Optional[float] = None
        self.needed = 0                  # distinct (cell, day) pairs
        self.done = 0
        self.counts = {"fetched": 0, "cached": 0, "failed": 0, "empty": 0, "recent": 0, "missing": 0}
        self.track_windows = [0, 0]      # done, total
        self.points_in = 0
        self.track_points = 0
        self.blocks_skipped = 0          # empty 90-day blocks skipped by the probe
        self.sources: dict = {}

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "phase": self.phase, "dry_run": self.dry_run, "error": self.error,
                "needed_cell_days": self.needed, "done": self.done, "counts": dict(self.counts),
                "track_windows": {"done": self.track_windows[0], "total": self.track_windows[1]},
                "blocks_skipped": self.blocks_skipped, "points_sent": self.points_in, "track_points": self.track_points, "sources": self.sources,
                "elapsed_s": int((self.finished or time.time()) - self.started)}


class Backfill:
    def __init__(self, st):
        self.st = st
        self.job: Optional[Job] = None
        self._lock = threading.Lock()

    def current(self) -> Optional[Job]:
        return self.job

    def start(self, points, t_from, t_to, use_tracks, sources, devices, include_import, dry_run, refresh: str = "none") -> Job:
        with self._lock:
            if self.job and self.job.status == "running":
                raise RuntimeError("a backfill is already running")
            job = Job(dry_run)
            self.job = job
        threading.Thread(target=self._run, daemon=True, name="weather-backfill",
                         args=(job, points, t_from, t_to, use_tracks, sources, devices, include_import, refresh)).start()
        return job

    # ------------------------------------------------------------------------------
    def _run(self, job, points, t_from, t_to, use_tracks, sources, devices, include_import, refresh="none"):
        st = self.st
        try:
            wx = st.weather
            need: dict = {}                      # (la, lo, day) -> None
            for p in points:
                try:
                    ep, lat, lon = int(p[0]), float(p[1]), float(p[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    la, lo = wx.cell(lat, lon)
                    need[(la, lo, _day(ep))] = None
                    job.points_in += 1
            if use_tracks and t_from is not None and t_to is not None and t_to >= t_from:
                job.phase = "tracks"
                wanted = tuple(x.strip() for x in sources.split(",") if x.strip())
                t_from = max(t_from, FLOOR_EPOCH)
                t_to = min(t_to, int(time.time()))
                windows = list(range(t_from, t_to + 1, CHUNK_S))
                job.track_windows[1] = len(windows)
                per_block = PROBE_S // CHUNK_S
                for b in range(0, len(windows), per_block):
                    if job.cancel.is_set():
                        job.status = "cancelled"
                        return
                    block = windows[b:b + per_block]
                    if len(windows) > per_block:       # big range: look at the whole block first
                        has, ok = st.tracks.probe(block[0], min(block[-1] + CHUNK_S - 1, t_to), wanted, devices, include_import)
                        if ok and not has:
                            job.track_windows[0] += len(block)
                            job.blocks_skipped += 1
                            continue
                    for t0 in block:
                        if job.cancel.is_set():
                            job.status = "cancelled"
                            return
                        pts, status = st.tracks.points(t0, min(t0 + CHUNK_S - 1, t_to), wanted, devices, include_import)
                        for k, v in (status or {}).items():
                            job.sources[k] = job.sources.get(k, False) or bool(v)
                        for pt in pts:
                            la, lo = wx.cell(pt.lat, pt.lon)
                            need[(la, lo, _day(pt.epoch))] = None
                        job.track_points += len(pts)
                        job.track_windows[0] += 1
            job.needed = len(need)
            job.phase = "weather"
            fetched_upstream = 0
            for (la, lo, day) in sorted(need, key=lambda k: (k[2], k[0], k[1])):
                if job.cancel.is_set():
                    job.status = "cancelled"
                    return
                if job.dry_run:
                    if wx.age_days(day) <= wx.ARCHIVE_MIN_AGE_DAYS:
                        job.counts["recent"] += 1
                    elif not wx.needs_fetch(la, lo, day, refresh):
                        job.counts["cached"] += 1
                    else:
                        job.counts["missing"] += 1
                    job.done += 1
                    continue
                if fetched_upstream >= st.s.weather_backfill_max and wx.needs_fetch(la, lo, day, refresh) \
                        and wx.age_days(day) > wx.ARCHIVE_MIN_AGE_DAYS:
                    job.status = "capped"        # run again to continue
                    return
                res = wx.fetch_for_archive(la, lo, day, refresh)
                if res == "limited":
                    job.status = "rate_limited"
                    return
                job.counts[res] = job.counts.get(res, 0) + 1
                if res in ("fetched", "failed", "empty"):
                    fetched_upstream += 1
                    if st.s.weather_backfill_delay > 0:
                        time.sleep(st.s.weather_backfill_delay)
                job.done += 1
            job.status = "done"
        except Exception as e:              # noqa: BLE001
            job.status, job.error = "error", f"{type(e).__name__}: {e}"
        finally:
            job.phase = job.phase if job.status == "running" else job.phase
            job.finished = time.time()
