"""GPS füllen: last resort = nearest file with its own GPS in the same folder on the same day (never another day / folder)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core import fillcore
from app.core.fillcore import Media, Plan, FillOpts, Sidecar

Z = ZoneInfo("Europe/Vienna")
ep = lambda *a: int(datetime(*a, tzinfo=Z).timestamp())
def med(name, e, lat=None, lon=None, folder="/m/2026/a"):
    return Media(path=f"{folder}/{name}", is_video=False, lat=lat, lon=lon, epoch=e)

target = med("t.jpg", ep(2026, 6, 1, 14, 0))
near = med("near.jpg", ep(2026, 6, 1, 14, 30), 47.1, 16.1)
far = med("far.jpg", ep(2026, 6, 1, 9, 0), 48.0, 17.0)
other_day = med("od.jpg", ep(2026, 6, 2, 14, 1), 10.0, 10.0)
sidecar_only = med("sc.jpg", ep(2026, 6, 1, 15, 0)); sidecar_only.sidecar = Sidecar(path="x", lat=47.5, lon=16.5)
nogps = med("ng.jpg", ep(2026, 6, 1, 14, 5))
folder_refs = [target, near, far, other_day, sidecar_only, nogps]

def run(opts, *files, refs=folder_refs):
    plans = [Plan(f, "no-track-point", "kein Track-Punkt") for f in files]
    n = fillcore.plan_neighbors(plans, opts, lambda folder: refs if folder == "/m/2026/a" else [])
    return plans, n

o = FillOpts()
(p,), n = run(o, target)
assert n == 1 and p.status == "fill" and (p.loc["lat"], p.loc["lon"]) == (47.1, 16.1), (p.status, p.loc)
assert p.loc["method"] == "neighbor" and p.loc["neighbor"] == "near.jpg" and p.loc["gap_s"] == 1800 and 0.3 <= p.loc["confidence"] <= 0.5, p.loc
# the chosen position is written with its provenance
args = " ".join(fillcore.sidecar_args(p, []))
assert "GpsSource=neighbor-file" in args and "GpsTrackSources=neighbor:near.jpg" in args, args

# a sidecar position counts, the nearest wins
t2 = med("t2.jpg", ep(2026, 6, 1, 15, 10))
(p2,), _ = run(o, t2)
assert p2.loc["neighbor"] == "sc.jpg" and p2.loc["lat"] == 47.5

# window: 20 min is too far -> stays unplaced; 0 = anywhere on that day (far.jpg is 5 h away, near.jpg closer)
(p3,), n3 = run(FillOpts(neighbor_s=20 * 60), target)
assert n3 == 0 and p3.status == "no-track-point" and "Nachbardatei" in p3.reason
# other day / other folder / no reference at all
alone = med("alone.jpg", ep(2026, 6, 3, 12, 0))
(p4,), n4 = run(o, alone)
assert n4 == 0 and p4.status == "no-track-point"
other_folder = med("x.jpg", ep(2026, 6, 1, 14, 0), folder="/m/2026/b")
(p5,), n5 = run(o, other_folder)
assert n5 == 0 and p5.status == "no-track-point"
# low confidence stays below min_conf
(p6,), _ = run(FillOpts(neighbor_s=6 * 3600, min_conf=0.45), med("t6.jpg", ep(2026, 6, 1, 12, 0)))
assert p6.status == "low-confidence" and p6.loc is not None, p6.status
# files the track placed / without time are untouched
pl = [Plan(target, "fill"), Plan(med("nt.jpg", None), "no-time")]
assert fillcore.plan_neighbors(pl, o, lambda f: folder_refs) == 0 and [x.status for x in pl] == ["fill", "no-time"]
# subfolders count too (same day), the same folder wins a tie, other days in subfolders do not
sub_near = med("s.jpg", ep(2026, 6, 1, 14, 10), 46.5, 15.5, folder="/m/2026/a/trip")
sub_other_day = med("so.jpg", ep(2026, 6, 4, 14, 0), 1.0, 1.0, folder="/m/2026/a/trip")
refs2 = folder_refs + [sub_near, sub_other_day]
(p7,), _ = run(o, target, refs=refs2)
assert p7.loc["neighbor"] == "trip/s.jpg" and p7.loc["lat"] == 46.5 and p7.loc["gap_s"] == 600, p7.loc
assert "GpsTrackSources=neighbor:trip/s.jpg" in " ".join(fillcore.sidecar_args(p7, []))
tie = med("tie.jpg", ep(2026, 6, 1, 14, 10), 47.7, 16.7)
(p8,), _ = run(o, target, refs=[target, sub_near, tie])
assert p8.loc["neighbor"] == "tie.jpg"
(p9,), n9 = run(o, med("late.jpg", ep(2026, 6, 4, 9, 0)), refs=refs2)
assert p9.loc["neighbor"] == "trip/so.jpg"             # only the subfolder file is from that day
# UI: option present and on by default, menu position
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
idx = open(os.path.join(base, "app/modules/gps_fill/templates/index.html"), encoding="utf-8").read()
assert 'name="neighbor" value="1" checked' in idx and 'name="neighbor_h"' in idx
nav = open(os.path.join(base, "app/modules/gps_sidecar/templates/index.html"), encoding="utf-8").read()
assert nav.index("GPS Sidecar</span>") < nav.index('href="/gpsfill/"') < nav.index('href="/geooverrides/"')
print("PASS")
