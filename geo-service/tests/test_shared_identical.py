"""trackmath.py / textde.py exist twice (service + clients, so each side stays self-contained).
They must stay byte-identical -- the service and gps_fill must agree on interpolation and confidence."""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bad = []
for name in ("trackmath.py", "textde.py"):
    a, b = (hashlib.sha256(open(os.path.join(ROOT, d, name), "rb").read()).hexdigest() for d in ("app", "clients"))
    print(("PASS" if a == b else "FAIL"), name, a[:12], b[:12])
    if a != b:
        bad.append(name)
sys.exit(1 if bad else 0)
