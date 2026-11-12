"""A maker-notes WARNING must not make sidecar creation fail; a real error still does."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.core import geocore
w = "Warning: Error rebuilding maker notes (may be corrupt) - /x/a.jpg"
assert not geocore._has_hard_error(w)
assert geocore._has_hard_error("Error: File not found - /x/a.jpg")
assert geocore._has_hard_error(w + "\nError: Bad format")
assert not geocore._has_hard_error("")
print("all ok")
