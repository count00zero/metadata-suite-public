"""Folders named *Kalender* / *Weihnachtskarte* / *Osterkarte* are never descended into."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from PIL import Image
from app.core import timecore, collagecore, skipdirs

assert skipdirs.is_skipped_dir("Kalender 2024") and skipdirs.is_skipped_dir("Meine WEIHNACHTSKARTE") and skipdirs.is_skipped_dir("Osterkarte-2020")
assert not skipdirs.is_skipped_dir("Hochzeit")
root = tempfile.mkdtemp()
for d in ("Urlaub", "Kalender 2024", "Urlaub/Weihnachtskarte", "Osterkarte"):
    os.makedirs(os.path.join(root, d), exist_ok=True)
    Image.new("RGB", (20, 20)).save(os.path.join(root, d, "a.jpg"))
names = {os.path.relpath(p, root) for p in timecore.find_files(root, 3)}
assert names == {"Urlaub/a.jpg"}, names
imgs = {os.path.relpath(p, root) for p in collagecore.find_images(root)}
assert imgs == {"Urlaub/a.jpg"}, imgs
trips = [os.path.basename(t) for t in collagecore.discover_trip_folders(root)]
assert trips == ["Urlaub"], trips
print("all ok")

# trip collage "<folder name>.jpg" next to its folder is never a candidate
y = os.path.join(root, "Urlaube", "2023")
trip = "2023.02.05-2023.02.10 - Maria Alm - Tante Frida"
os.makedirs(os.path.join(y, trip))
Image.new("RGB", (20, 20)).save(os.path.join(y, trip + ".jpg"))
Image.new("RGB", (20, 20)).save(os.path.join(y, "lose.jpg"))
Image.new("RGB", (20, 20)).save(os.path.join(y, trip, "IMG_1.jpg"))
got = {os.path.relpath(p, y) for p in timecore.find_files(y, 3)}
assert got == {"lose.jpg", os.path.join(trip, "IMG_1.jpg")}, got
assert skipdirs.is_trip_collage(trip + ".JPG", [trip]) and not skipdirs.is_trip_collage("x.jpg", [trip])
print("ok   trip collages excluded")

# Kontext nachfüllen (fillcore) sees the same generated collages as the others
from app.core import fillcore
Image.new("RGB", (20, 20)).save(os.path.join(y, "2023.12.jpg")); Image.new("RGB", (20, 20)).save(os.path.join(y, "folder.jpg"))
fm = {os.path.relpath(p, y) for p in fillcore.find_media(y, 3, "image")}
assert fm == {"lose.jpg", os.path.join(trip, "IMG_1.jpg")}, fm
print("ok   fillcore skips folder.jpg / month / trip collages")

# Collage module: generated collages (folder.jpg, monthly, trip) are never source photos / never make a folder a "trip"
import tempfile
from app.core import collagecore
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "2023.02.05-2023.02.10 - Maria Alm"))
os.makedirs(os.path.join(d, "Kalender 2023"))
for n in ("2023.12.jpg", "folder.jpg", "2023.02.05-2023.02.10 - Maria Alm.jpg"):
    open(os.path.join(d, n), "w").close()
open(os.path.join(d, "Kalender 2023", "x.jpg"), "w").close()
open(os.path.join(d, "2023.02.05-2023.02.10 - Maria Alm", "a.jpg"), "w").close()
assert not collagecore.has_direct_images(d), "only generated collages -> not a trip folder"
assert [os.path.basename(x) for x in collagecore.find_images(d)] == ["a.jpg"], collagecore.find_images(d)
open(os.path.join(d, "IMG_1.jpg"), "w").close()
assert collagecore.has_direct_images(d)
print("ok   collage ignores generated collages")

# Year folder: month folders are ONE unit each (collage in the year folder), recursive photos, no per-day collages
y = os.path.join(tempfile.mkdtemp(), "2023")
for sub in ("2023.11/01", "2023.11/02", "2023.12", "Ausflug"):
    os.makedirs(os.path.join(y, sub))
for p in ("2023.11/01/a.jpg", "2023.11/02/b.jpg", "2023.12/c.jpg", "Ausflug/d.jpg"):
    open(os.path.join(y, p), "w").close()
os.makedirs(os.path.join(y, "leer"))
got = [os.path.relpath(t, y) for t in collagecore.discover_trip_folders(y)]
assert got == ["2023.11", "2023.12", "Ausflug"], got
tr = collagecore.discover_trips(y)
assert {t.name: t.image_count for t in tr} == {"2023.11": 2, "2023.12": 1, "Ausflug": 1}
assert all(os.path.dirname(t.output_path) == y for t in tr)
print("ok   year folder: month folders are units")
