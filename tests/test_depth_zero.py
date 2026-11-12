"""Tiefe 0 = nur der angegebene Ordner; in JEDEM Modul zugelassen (Feld min=0, Server klemmt auf 0..7, nie auf 1)."""
import glob, os, re, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys; sys.path.insert(0, ROOT)
for p in glob.glob(os.path.join(ROOT, "app/modules/*/templates/index.html")):
    for m in re.finditer(r'<input type="number" name="depth"[^>]*>', open(p, encoding="utf-8").read()):
        assert 'min="0"' in m.group(0), (p, m.group(0))
for p in glob.glob(os.path.join(ROOT, "app/modules/*/router.py")):
    assert "max(1, min(7, depth" not in open(p, encoding="utf-8").read(), p
from app.core import fillcore, nfocore, timecore
d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, "sub"))
for f in ("a.jpg", "a.mp4", "sub/b.jpg", "sub/b.mp4"):
    open(os.path.join(d, f), "w").write("x")
rel = lambda l: sorted(os.path.relpath(x, d) for x in l)
assert rel(fillcore.find_media(d, 0)) == ["a.jpg", "a.mp4"]
assert len(fillcore.find_media(d, 1)) == 4
assert rel(nfocore.find_videos(d, 0)) == ["a.mp4"] and len(nfocore.find_videos(d, 1)) == 2
assert rel(timecore.find_files(d, 0, "ALL")) == ["a.jpg", "a.mp4"]
print("PASS")
