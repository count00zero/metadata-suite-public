"""fetch_many logs a progress line every N tiles; existing tiles are skipped."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import srtmfetch as s

d = tempfile.mkdtemp()
open(os.path.join(d, "N47E016.hgt"), "wb").close()
s.fetch_tile = lambda base, la, lo, out: "have" if (la, lo) == (47, 16) else ("new" if lo % 2 else "missing")
lines = []
res = s.fetch_many("x", [(47, lo) for lo in range(16, 26)], d, log=lines.append, every=4)
assert sum(res.values()) == 10 and res["new"] == 5, res
assert len(lines) == 2 and lines[0].startswith("srtm preload: 4/10"), lines
print("all ok")

# failed tiles are retried; what still fails is reported
calls = {}
def flaky(base, la, lo, out):
    calls[lo] = calls.get(lo, 0) + 1
    if lo == 1 and calls[lo] < 3: raise OSError("boom")
    if lo == 2: raise OSError("always")
    return "new"
s.fetch_tile = flaky
res = s.fetch_many("x", [(47, 0), (47, 1), (47, 2)], d, log=lambda m: None, retries=3, retry_wait=0)
assert res["new"] == 2 and res["failed"] == 1 and calls[1] == 3 and calls[2] == 4, (res, calls)
print("ok   retries")
