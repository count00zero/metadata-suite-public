"""folder.jpg (generated folder cover) is never a rename/processing candidate."""
import os, sys, tempfile
from app.core import timecore
d = tempfile.mkdtemp()
for n in ("folder.jpg", "Folder.JPG", "a.jpg", "sub_folder.jpg"):
    open(os.path.join(d, n), "wb").write(b"x")
got = sorted(os.path.basename(p) for p in timecore.find_files(d, 0, "IMAGE"))
c, _ = timecore.scan_candidates(d, 0, "IMAGE", True)
ok = got == ["a.jpg", "sub_folder.jpg"] and sorted(os.path.basename(p) for p in c) == got
print(("ok   " if ok else "FAIL ") + "folder.jpg excluded", got)
sys.exit(0 if ok else 1)
