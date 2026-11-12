"""exifpool: protocol correctness, unicode, errors, timeout/cancel recovery, concurrency, fallback."""
import os, sys, tempfile, threading, time, subprocess as sp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import exifpool

d = tempfile.mkdtemp(prefix="xp_")
def mk(name, color="red"):
    p = os.path.join(d, name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i",f"color=c={color}:s=32x32","-frames:v","1",p], check=True)
    return p
a = mk("a.jpg"); u = mk("Grüße ü 日本.jpg"); h = mk("x#y.jpg")

# read, -T (suppresses exiftool's native {ready}), -q (same), plain: none may hang or bleed into the next call
r = exifpool.run(["exiftool","-s3","-FileName",a]); assert r.returncode == 0 and r.stdout.strip() == "a.jpg", r
r = exifpool.run(["exiftool","-m","-n","-T","-Make",a]); assert r.returncode == 0 and r.stdout.strip() == "-", r
r = exifpool.run(["exiftool","-q","-s3","-FileName",a]); assert r.stdout.strip() == "a.jpg", r
r = exifpool.run(["exiftool","-s3","-FileName",a]); assert r.stdout.strip() == "a.jpg", r    # no leftovers from before
# unicode / special names
assert exifpool.run(["exiftool","-s3","-FileName",u]).stdout.strip() == "Grüße ü 日本.jpg"
assert exifpool.run(["exiftool","-s3","-FileName",h]).stdout.strip() == "x#y.jpg"
# errors: status + stderr, stdout empty
r = exifpool.run(["exiftool","-j",os.path.join(d,"nope.jpg")]); assert r.returncode == 1 and "File not found" in r.stderr, r
# write round trip incl. non-zero status on failure
r = exifpool.run(["exiftool","-m","-q","-overwrite_original","-Make=PoolCam",a]); assert r.returncode == 0, r
assert exifpool.run(["exiftool","-s3","-Make",a]).stdout.strip() == "PoolCam"
r = exifpool.run(["exiftool","-m","-q","-overwrite_original","-Make=X",os.path.join(d,"nope.jpg")]); assert r.returncode != 0
# newline in an argument -> transparent one-shot fallback (the pool protocol is line based)
desc = "zeile eins\nzeile zwei"
r = exifpool.run(["exiftool","-m","-overwrite_original",f"-XMP-dc:Description={desc}",a]); assert r.returncode == 0, r
import json
assert json.loads(exifpool.run(["exiftool","-j","-XMP-dc:Description",a]).stdout)[0]["Description"].replace("\r","") == desc
# legacy "timeout N exiftool" shape works, and a timeout there yields rc 124 instead of raising
assert exifpool.run(["timeout","30","exiftool","-s3","-Make",a]).stdout.strip() == "PoolCam"

# timeout kills only that worker, pool recovers; TimeoutExpired like subprocess.run
big = os.path.join(d, "many"); os.makedirs(big)
for i in range(300): open(os.path.join(big, f"{i}.txt"), "w").write("x")
try:
    exifpool.run(["exiftool","-r","-j","-G","-a","-ee",big], timeout=0.001)
    raise SystemExit("expected TimeoutExpired")
except sp.TimeoutExpired:
    pass
r = exifpool.run(["timeout","0.001","exiftool","-r","-j","-a","-ee",big]); assert r.returncode == 124, r
assert exifpool.run(["exiftool","-s3","-FileName",a]).stdout.strip() == "a.jpg"           # pool still fine

# cancel_event: kills the call quickly
ev = threading.Event(); threading.Timer(0.3, ev.set).start()
t0 = time.time()
try:
    exifpool.run(["exiftool","-r","-j","-G","-a","-ee","/usr"], timeout=60, cancel_event=ev)
    raise SystemExit("expected Cancelled")
except exifpool.Cancelled:
    assert time.time() - t0 < 5
assert exifpool.run(["exiftool","-s3","-FileName",a]).stdout.strip() == "a.jpg"

# worker killed while idle -> next call transparently retried on a new one
w = exifpool._pool.idle[0]; w.proc.kill(); time.sleep(0.3)
assert exifpool.run(["exiftool","-s3","-FileName",a]).stdout.strip() == "a.jpg"

# concurrency: many threads, each gets ITS OWN answer (no cross-talk), pool size is respected
files = [mk(f"c{i}.jpg") for i in range(24)]
bad = []
def work(chunk):
    for f in chunk * 3:
        out = exifpool.run(["exiftool","-s3","-FileName",f]).stdout.strip()
        if out != os.path.basename(f): bad.append((f, out))
ths = [threading.Thread(target=work, args=(files[i::12],)) for i in range(12)]
[t.start() for t in ths]; [t.join() for t in ths]
assert not bad, bad[:3]
st = exifpool.stats(); assert st["running"] <= max(1, st["max"]), st

# pool off -> same results through plain subprocess
os.environ["EXIFTOOL_POOL_SIZE"] = "0"
assert exifpool.run(["exiftool","-s3","-FileName",u]).stdout.strip() == "Grüße ü 日本.jpg"
assert exifpool.run(["exiftool","-j",os.path.join(d,"nope.jpg")]).returncode == 1
del os.environ["EXIFTOOL_POOL_SIZE"]

# speed sanity: pooled calls must be far cheaper than process starts
n = 40
t = time.time()
for _ in range(n): exifpool.run(["exiftool","-s3","-FileName",a])
pooled = (time.time() - t) / n
t = time.time()
for _ in range(10): sp.run(["exiftool","-s3","-FileName",a], capture_output=True)
oneshot = (time.time() - t) / 10
assert pooled < oneshot / 4, (pooled, oneshot)
print(f"PASS  pooled {pooled*1000:.1f} ms vs one-shot {oneshot*1000:.1f} ms per call")
