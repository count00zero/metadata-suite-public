"""
exifpool.py -- a small pool of long-running exiftool processes (``-stay_open``).

Why: starting exiftool (Perl) costs ~90 ms per call, while parsing a file takes ~4 ms. Every scan
used to spawn exiftool several times per file. Here a handful of workers stay alive and take
commands over stdin, so the start-up cost is paid once per worker instead of once per call.

Drop-in for ``subprocess.run`` for exiftool calls::

    res = exifpool.run(["exiftool", "-j", "-m", path], timeout=60)      # res.stdout / .stderr / .returncode

  * Same command lists as before (leading "exiftool" and the old ``timeout N`` prefix are understood).
  * Raises ``subprocess.TimeoutExpired`` like subprocess.run; on timeout the worker is killed and replaced.
  * ``cancel_event`` (threading.Event): polled every 0.2 s; the worker is killed and ``Cancelled`` raised.
  * Anything the protocol can't carry (newline in an argument), pool disabled (EXIFTOOL_POOL_SIZE=0) or
    a broken pool falls back transparently to a normal one-shot subprocess -- behaviour never depends on the pool.

Env: EXIFTOOL_POOL_SIZE (default 6, 0 = off), EXIFTOOL_POOL_IDLE_SECS (default 300).
"""
from __future__ import annotations

import atexit
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

_READY = "{ready}"      # printed natively by exiftool after -execute (NOT when -q is given)
_DONE = "{pdone}"        # our own end marker (-echo3), always printed
_STATUS_TAG = "{status}"
DEFAULT_POOL_SIZE = 6
DEFAULT_IDLE_SECS = 300


class Cancelled(Exception):
    """cancel_event was set while a call was in flight (the worker has been killed)."""


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    args: list


def pool_size() -> int:
    raw = (os.environ.get("EXIFTOOL_POOL_SIZE") or "").strip().strip("\"'")
    try:
        return max(0, int(raw)) if raw else DEFAULT_POOL_SIZE
    except ValueError:
        return DEFAULT_POOL_SIZE


def _idle_secs() -> int:
    try:
        return max(10, int((os.environ.get("EXIFTOOL_POOL_IDLE_SECS") or "").strip() or DEFAULT_IDLE_SECS))
    except ValueError:
        return DEFAULT_IDLE_SECS


class _Worker:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["exiftool", "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.out_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.err_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.last_used = time.monotonic()
        self.alive = True
        for pipe, q in ((self.proc.stdout, self.out_q), (self.proc.stderr, self.err_q)):
            threading.Thread(target=self._pump, args=(pipe, q), daemon=True).start()

    @staticmethod
    def _pump(pipe, q) -> None:
        try:
            for line in iter(pipe.readline, b""):
                q.put(line)
        except Exception:
            pass
        q.put(None)  # EOF marker (process died)

    def kill(self) -> None:
        self.alive = False
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass
        for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                s.close()
            except Exception:
                pass

    def close(self) -> None:
        """Polite shutdown (-stay_open False), kill if it doesn't follow quickly."""
        self.alive = False
        try:
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=2)
        except Exception:
            pass
        self.kill()

    def execute(self, args: list, timeout: Optional[float], cancel_event) -> Result:
        # End detection: -echo3 {pdone} on stdout (always printed), -echo4 "{ready}<status>" on stderr.
        # exiftool's own native "{ready}" line on stdout comes right after {pdone} -- or not at all with
        # -q/-T/... -- so it is never waited for; any "{ready}" line seen on stdout is simply dropped
        # (it can't be real output), which also swallows a straggler that arrives during the next call.
        # Arguments go one per line; UTF-8 file names need an explicit charset for the argfile.
        payload = ["-charset", "filename=UTF8", "-charset", "exif=UTF8", *args,
                   "-echo3", _DONE, "-echo4", _READY + "${status}", "-execute"]
        data = ("\n".join(payload) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except Exception as e:
            self.alive = False
            raise _WorkerDied(str(e))

        out_lines: list[bytes] = []
        err_lines: list[bytes] = []
        out_done = err_done = False
        status = 0
        deadline = None if not timeout else time.monotonic() + timeout
        while not (out_done and err_done):
            if cancel_event is not None and cancel_event.is_set():
                self.kill()
                raise Cancelled()
            if deadline is not None and time.monotonic() > deadline:
                self.kill()
                raise subprocess.TimeoutExpired(["exiftool", *args], timeout)
            for q, lines, is_out in ((self.out_q, out_lines, True), (self.err_q, err_lines, False)):
                if (is_out and out_done) or (not is_out and err_done):
                    continue
                try:
                    line = q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line is None:
                    self.alive = False
                    raise _WorkerDied("exiftool-Prozess beendet")
                text = line.rstrip(b"\r\n")
                if is_out and text == _DONE.encode():
                    out_done = True
                elif is_out and text == _READY.encode():
                    pass
                elif not is_out and text.startswith(_READY.encode()):
                    err_done = True
                    try:
                        status = int(text[len(_READY):] or 0)
                    except ValueError:
                        status = 0
                else:
                    lines.append(line)
        self.last_used = time.monotonic()
        return Result(status, b"".join(out_lines).decode("utf-8", "replace"),
                      b"".join(err_lines).decode("utf-8", "replace"), ["exiftool", *args])



class _WorkerDied(Exception):
    pass


class _Pool:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.idle: "list[_Worker]" = []
        self.created = 0
        self.cond = threading.Condition(self.lock)
        self.reaper: Optional[threading.Thread] = None
        self.max = pool_size()

    def acquire(self) -> _Worker:
        with self.cond:
            self.max = pool_size()
            while True:
                while self.idle:
                    w = self.idle.pop()
                    if w.alive and w.proc.poll() is None:
                        return w
                    self.created -= 1
                if self.created < max(1, self.max):
                    self.created += 1
                    break
                self.cond.wait(timeout=1.0)
        try:
            w = _Worker()
        except Exception:
            with self.cond:
                self.created -= 1
                self.cond.notify()
            raise
        self._ensure_reaper()
        return w

    def release(self, w: _Worker) -> None:
        with self.cond:
            if w.alive and w.proc.poll() is None:
                self.idle.append(w)
            else:
                self.created -= 1
            self.cond.notify()

    def discard(self, w: _Worker) -> None:
        w.kill()
        with self.cond:
            self.created -= 1
            self.cond.notify()

    def _ensure_reaper(self) -> None:
        if self.reaper is None or not self.reaper.is_alive():
            self.reaper = threading.Thread(target=self._reap, daemon=True)
            self.reaper.start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            limit = _idle_secs()
            stale = []
            with self.cond:
                now = time.monotonic()
                keep = []
                for w in self.idle:
                    (stale if now - w.last_used > limit else keep).append(w)
                self.idle = keep
                self.created -= len(stale)
            for w in stale:
                w.close()

    def shutdown(self) -> None:
        with self.cond:
            ws, self.idle = self.idle, []
            self.created -= len(ws)
        for w in ws:
            w.close()

    def stats(self) -> dict:
        with self.lock:
            return {"max": pool_size(), "running": self.created, "idle": len(self.idle)}


_pool = _Pool()
atexit.register(_pool.shutdown)


def stats() -> dict:
    return _pool.stats()


def shutdown() -> None:
    _pool.shutdown()


def _normalize(cmd: list, timeout: Optional[float]) -> tuple[list, Optional[float], bool]:
    """Accept the historical command shapes: [exiftool, ...] and [timeout, N, exiftool, ...].
    The third value says the old ``timeout N`` prefix was used: callers of that shape expect exit
    code 124 on a timeout (like coreutils timeout) instead of an exception."""
    cmd = list(cmd)
    legacy = False
    if cmd and cmd[0] == "timeout" and len(cmd) > 2:
        try:
            t = float(cmd[1])
            timeout = t if timeout is None else min(timeout, t)
            cmd = cmd[2:]
            legacy = True
        except ValueError:
            pass
    if not cmd or cmd[0] != "exiftool":
        raise ValueError("exifpool.run: not an exiftool command")
    return cmd[1:], timeout, legacy


def _oneshot(args: list, timeout: Optional[float], cancel_event) -> Result:
    cmd = ["exiftool", *args]
    if cancel_event is None:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode, p.stdout or "", p.stderr or "", cmd)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=0.2)
            return Result(proc.returncode, out or "", err or "", cmd)
        except subprocess.TimeoutExpired:
            if cancel_event.is_set():
                proc.kill(); proc.communicate()
                raise Cancelled()
            if timeout and time.monotonic() - start > timeout:
                proc.kill(); proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)


def run(cmd: list, timeout: Optional[float] = None, cancel_event=None, **_ignored) -> Result:
    """Run one exiftool command (see module docstring). ``capture_output``/``text``/``check`` are
    accepted via **_ignored for drop-in use; output is always captured and returned as str."""
    args, timeout, legacy = _normalize(cmd, timeout)
    if legacy:
        try:
            return run(["exiftool", *args], timeout=timeout, cancel_event=cancel_event)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"Timeout nach {timeout}s", ["exiftool", *args])
    # "-config" is only honoured as the FIRST option of a real command line, never inside the -stay_open
    # argfile, so commands that load a config file (e.g. the XMP-mon namespace) run one-shot.
    if pool_size() == 0 or (args and args[0] == "-config") or any(("\n" in a or "\r" in a) for a in args):
        return _oneshot(args, timeout, cancel_event)
    for attempt in (1, 2):
        try:
            w = _pool.acquire()
        except FileNotFoundError:
            raise
        except Exception:
            return _oneshot(args, timeout, cancel_event)
        try:
            res = w.execute(args, timeout, cancel_event)
        except _WorkerDied:
            _pool.discard(w)
            if attempt == 2:
                return _oneshot(args, timeout, cancel_event)
            continue   # one retry on a fresh worker (a worker may have died idle)
        except BaseException:
            _pool.discard(w)   # timeout / cancel / anything unexpected: never reuse a doubtful worker
            raise
        _pool.release(w)
        return res
    return _oneshot(args, timeout, cancel_event)
