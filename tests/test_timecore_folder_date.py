"""Camera counters are not dates; a single-day folder date wins and starts at 13:00."""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.core import timecore as tc

root = tempfile.mkdtemp()
day = os.path.join(root, "2023.04", "2023.04.29 - Hochzeit Ahmed & Charlotte"); os.makedirs(day)
def mk(name, folder=day):
    p = os.path.join(folder, name); open(p, "wb").write(b"x"); return p

r = tc._resolve_core(mk("D85_1872.jpg"), "NAME", "Europe/Vienna")
assert r["local_naive"].strftime("%Y.%m.%d-%H.%M.%S") == "2023.04.29-13.00.00", r["local_naive"]
assert r["source"].startswith("Ordner"), r["source"]
print("ok   camera counter -> folder day 13:00", r["source"])

r = tc._resolve_core(mk("IMG_2026.jpg"), "NAME", "Europe/Vienna")
assert r["local_naive"].strftime("%Y.%m.%d") == "2023.04.29", r["local_naive"]
print("ok   year-only name does not beat full-day folder")

r = tc._resolve_core(mk("20230429_101500.jpg"), "NAME", "Europe/Vienna")
assert r["local_naive"].strftime("%H.%M.%S") == "10.15.00" and r["source"].startswith("Dateiname"), r
print("ok   real timestamp in the name still wins")
print("all ok")

# monthly collages (YYYY.MM.jpg) are never candidates of a rename run
m = os.path.join(root, "2023.04"); open(os.path.join(m, "2023.04.jpg"), "wb").write(b"x"); open(os.path.join(m, "2023.12.JPG"), "wb").write(b"x")
open(os.path.join(m, "2023.04.29-10.00.00.jpg"), "wb").write(b"x")
names = {os.path.basename(x) for x in tc.find_files(m, 2)}
assert "2023.04.jpg" not in names and "2023.12.JPG" not in names, names
assert "2023.04.29-10.00.00.jpg" in names and "D85_1872.jpg" in names, names
print("ok   month collages excluded")

# force_zone: a (wrong) Dubai GPS position must not decide the zone
f = os.path.join(day, "2023.04.29-13.00.00_09.jpg"); open(f, "wb").write(b"x")
tc.get_exif = lambda path, timeout=120, cancel=None: {"GPSLatitude": 25.2, "GPSLongitude": 55.27}
if tc._timezonefinder() is not None:
    a = tc.resolve_batch([f], "EXIF", "Europe/Vienna")[0]
    assert "Dubai" in a.source and a.offset_str.endswith("+04:00") if hasattr(a, "offset_str") else "Dubai" in a.source, a.source
b = tc.resolve_batch([f], "EXIF", "Europe/Vienna", force_zone=True)[0]
assert "Europe/Vienna" in b.source and "Dubai" not in b.source, b.source
assert b.zieldate.endswith("+02:00"), b.zieldate
print("ok   force_zone ->", b.source, b.zieldate)

# force_zone also beats an embedded wrong offset on a file WITHOUT GPS (name 13:00, file says 15:00+04:00)
g = os.path.join(day, "2023.04.29-13.00.00_12.jpg"); open(g, "wb").write(b"x")
tc.get_exif = lambda path, timeout=120, cancel=None: {"DateTimeOriginal": "2023:04:29 15:00:00", "OffsetTimeOriginal": "+04:00"}
a = tc.resolve_batch([g], "EXIF", "Europe/Vienna")[0]
b = tc.resolve_batch([g], "EXIF", "Europe/Vienna", force_zone=True)[0]
assert b.zieldate == "2023:04:29 13:00:00+02:00", (a.zieldate, b.zieldate, b.source)
print("ok   force_zone overrides embedded offset ->", b.zieldate, "| without:", a.zieldate)

# write-time zone override: same wall clock, new offset/epoch
row = {"exif_dt": "2023:04:29 13:00:00", "offset": "+04:00", "offset_short": "+4", "epoch": 1682758800, "tz_name": "Asia/Dubai"}
n = tc.row_with_zone(row, "Europe/Vienna")
assert n["exif_dt"] == row["exif_dt"] and n["offset"] == "+02:00" and n["offset_short"] == "+2" and n["tz_name"] == "Europe/Vienna", n
assert n["epoch"] == 1682766000 and row["offset"] == "+04:00", n
print("ok   row_with_zone ->", n["offset"], n["epoch"])

# Zeitkorrektur
assert tc.parse_shift("") == 0 and tc.parse_shift("+01:30") == 5400 and tc.parse_shift("-0:05:30") == -330
assert tc.parse_shift("+2h") == 7200 and tc.parse_shift("-90m") == -5400 and tc.parse_shift("45s") == 45
assert tc.parse_shift("+1d 02:00") == 93600 and tc.parse_shift("1d") == 86400 and tc.parse_shift("-1d 00:30") == -88200
for bad in ("abc", "1:2:3:4", "+999d"):
    try:
        tc.parse_shift(bad); assert False, bad
    except ValueError:
        pass
assert tc.format_shift(5400) == "+01:30:00" and tc.format_shift(-330) == "-00:05:30"
h = os.path.join(day, "2023.04.29-10.00.00_77.jpg"); open(h, "wb").write(b"x")
tc.get_exif = lambda path, timeout=120, cancel=None: {}
a = tc.resolve_batch([h], "EXIF", "Europe/Vienna")[0]
b = tc.resolve_batch([h], "EXIF", "Europe/Vienna", time_shift_s=5400)[0]
assert a.local_str == "2023.04.29-10.00.00" and b.local_str == "2023.04.29-11.30.00", (a.local_str, b.local_str)
assert b.shift == "+01:30:00" and "Zeitkorrektur +01:30:00" in b.source_display and not b.warn, (b.shift, b.source_display, b.warn)
assert b.to_dict()["shift"] == "+01:30:00" and tc.Resolution(**{k: v for k, v in b.to_dict().items() if k in {f.name for f in __import__("dataclasses").fields(tc.Resolution) if f.init}}).shift == "+01:30:00"
# a time that comes from the GPS UTC stamp is not shifted
tc.get_exif = lambda path, timeout=120, cancel=None: {"GPSLatitude": 47.9, "GPSLongitude": 16.2, "GPSDateTime": "2023:04:29 08:00:00Z"}
if tc._timezonefinder() is not None:
    g1 = tc.resolve_batch([h], "EXIF", "Europe/Vienna", time_shift_s=3600)[0]
    assert g1.shift == "" and g1.source.startswith("GPS-UTC"), (g1.shift, g1.source)
print("ok   Zeitkorrektur")
