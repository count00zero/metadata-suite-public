#!/usr/bin/env python3
"""Runs every tests/test_*.py in its own process (each builds its own temp media tree and
sets MEDIA_ROOT itself) and prints a summary. Needs ffmpeg + exiftool on PATH.

    python tests/run_all.py            # all
    python tests/run_all.py nfo gps    # only files whose name contains one of the words
"""
import glob, os, signal, subprocess, sys, time

here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
words = sys.argv[1:]
files = sorted(glob.glob(os.path.join(here, "test_*.py")))
if words:
    files = [f for f in files if any(w in os.path.basename(f) for w in words)]
env = dict(os.environ, PYTHONPATH=root, ANDROID_DEVICES_FILE=os.path.join(here, "data", "android_devices.json"))
failed = []
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT", "180"))      # seconds per test file


class _R:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_one(cmd, cwd, env, timeout):
    """Runs one test in its own process GROUP; on timeout the whole group (test + browsers + servers it started) is killed,
    so one hanging test fails by name instead of blocking the run forever (a grandchild holding the output pipe)."""
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
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
    print(f"START   {os.path.basename(f)}", flush=True)
    r = run_one([sys.executable, "-u", f], root, env, TEST_TIMEOUT)
    ok = r.returncode == 0
    print(f"{'PASS' if ok else 'FAIL'}  {time.time()-t:5.1f}s  {os.path.basename(f)}", flush=True)
    if not ok:
        failed.append(f)
        print("      " + "\n      ".join((r.stdout + r.stderr).strip().splitlines()[-6:]))
print(f"\n{len(files)-len(failed)}/{len(files)} passed")
sys.exit(1 if failed else 0)
