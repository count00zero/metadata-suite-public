"""app/core/trackmath.py (suite module 'GPS füllen') must stay byte-identical to the geo-service's copies:
service and suite have to agree on interpolation and confidence."""
import hashlib, os, sys
R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
h = lambda *p: hashlib.sha256(open(os.path.join(R, *p), "rb").read()).hexdigest()
a = h("app", "core", "trackmath.py")
for other in (("geo-service", "app", "trackmath.py"), ("geo-service", "clients", "trackmath.py")):
    assert a == h(*other), f"app/core/trackmath.py differs from {'/'.join(other)} -- copy the edited file over the others"
print("PASS")
