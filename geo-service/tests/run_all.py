#!/usr/bin/env python3
"""Runs every test file in its own process.  PATH needs exiftool + ffmpeg for the end-to-end ones
(they print SKIP otherwise).      python tests/run_all.py"""
import glob
import os
import signal
import subprocess
import sys
import time

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
files = sorted(glob.glob(os.path.join(root, "tests", "test_*.py")) + glob.glob(os.path.join(root, "clients", "tests", "test_*.py")))
failed = 0


class _R:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_one(cmd, cwd, timeout):
    """One test in its own process GROUP; on timeout the whole group (test + servers/browsers it started) is killed, so a
    hanging test fails by name instead of blocking the run forever."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return _R(p.returncode, out, err)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out = err = ""
        return _R(124, out or "", (err or "") + f"\nTIMEOUT after {timeout}s -- test killed")


for f in files:
    t = time.time()
    print(f"START {os.path.relpath(f, root)}", flush=True)
    r = run_one([sys.executable, "-u", "-I", f], root, 300)
    skipped = "SKIP" in r.stdout
    print(f"{'SKIP' if skipped else 'PASS' if r.returncode == 0 else 'FAIL'}  {time.time() - t:5.1f}s  {os.path.relpath(f, root)}", flush=True)
    if r.returncode:
        failed += 1
        print("      " + "\n      ".join((r.stdout + r.stderr).strip().splitlines()[-8:]))
print(f"\n{len(files) - failed}/{len(files)} passed")
sys.exit(1 if failed else 0)
