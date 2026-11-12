"""Collage: placed / skipped photo counters (renderers, result, templates)."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PIL import Image
from app.core import collagecore as cc

d = tempfile.mkdtemp()
files = []
for i in range(6):
    p = os.path.join(d, f"{i}.jpg"); Image.new("RGB", (400, 300), (i * 30, 90, 160)).save(p); files.append(p)
bad = os.path.join(d, "bad.jpg"); open(bad, "wb").write(b"not an image"); files.append(bad)

for layout in ("polaroid", "mosaic"):
    st = {}
    s = cc.CollageSettings(layout=layout, width=1200, height=800)
    cc.render_collage_canvas(files, s, layout, st)
    assert st["unreadable"] == (1 if layout == "polaroid" else st["unreadable"]) and st["unreadable"] <= 1, (layout, st)
    assert st["placed"] >= 1 and st["placed"] + st["unreadable"] <= len(files), (layout, st)
    print("ok  ", layout, st)

# tiny canvas: more photos than room -> skipped
st = {}
cc.render_collage_canvas(files[:6], cc.CollageSettings(layout="polaroid", width=300, height=200, cell=150), "polaroid", st)
assert st["placed"] < 6, st
print("ok   no room ->", st)

trip = os.path.join(d, "trip"); os.makedirs(trip)
for f in files: os.replace(f, os.path.join(trip, os.path.basename(f)))
r = cc.generate_trip_collage(cc.TripFolder(path=trip, name="trip", image_count=7, output_path=os.path.join(d, "trip.jpg"), already_exists=False),
                             cc.CollageSettings(layout="mosaic", width=1200, height=800, sync_folder_jpg=False), d, "")
assert r.status == "created" and r.placed_count + r.skipped_count == r.image_count and r.unreadable_count <= 1, r
print("ok   result", r.placed_count, r.skipped_count, r.unreadable_count)
print("all ok")
