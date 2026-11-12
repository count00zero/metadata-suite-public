import os, sys, tempfile, subprocess as sp, re, json, time, html as _h
MR = tempfile.mkdtemp(prefix="te_"); os.environ["MEDIA_ROOT"] = MR
from app.core import timecore, geocore
from datetime import datetime
H = timecore.filename_time_hint
assert H("2026.10.03-09.14.27") is None
assert H("20261003_091427") is None
assert H("IMG_20261003091427") is None
assert H("2026.10.03") == ("dateonly", datetime(2026,10,3,12,0,0))
assert H("20261003") == ("dateonly", datetime(2026,10,3,12,0,0))
assert H("2026") == ("yearonly", None)
assert H("2026.10.03-09.00.00 - 2026.10.03-11.00.00") == ("range", datetime(2026,10,3,10,0,0)), H("2026.10.03-09.00.00 - 2026.10.03-11.00.00")
assert H("20261003-20261005") == ("range", datetime(2026,10,4,12,0,0))
assert H("20261003090000-20261003110000") == ("range", datetime(2026,10,3,10,0,0))
assert H("Urlaub Sommer") is None
# epoch / video UTC
f = os.path.join(MR,"d"); os.makedirs(f)
v = os.path.join(f,"clip.mp4")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-t","1","-metadata","creation_time=2026-10-04T10:29:33Z",v],check=True)
r = timecore.resolve_one(v,"EXIF","Europe/Vienna")
ep, note = timecore.track_match_time(r, v)
assert ep == int(datetime(2026,10,4,10,29,33,tzinfo=__import__("datetime").timezone.utc).timestamp()) and "CreateDate" in note, (ep,note,r.source)
v2 = os.path.join(f,"2026.10.04-12.29.33.mp4"); os.replace(v, v2)
r2 = timecore.resolve_one(v2,"EXIF","Europe/Vienna"); ep2, n2 = timecore.track_match_time(r2, v2)
assert ep2 == r2.epoch and n2 == "", (ep2, n2)
y = os.path.join(f,"2026.mp4"); sp.run(["cp",v2,y])
r3 = timecore.resolve_one(y,"EXIF","Europe/Vienna"); assert timecore.track_match_time(r3,y)[0] is None
d = os.path.join(f,"2026.05.06.mp4"); sp.run(["cp",v2,d])
r4 = timecore.resolve_one(d,"EXIF","Europe/Vienna"); e4,_ = timecore.track_match_time(r4,d)
assert datetime.fromtimestamp(e4, tz=__import__("zoneinfo").ZoneInfo("Europe/Vienna")).hour == 12
# xmp mtime reset
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
j = os.path.join(f,"2026.10.04-09.18.06.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",j],check=True)
sp.run(["exiftool","-q","-o",j+".xmp",j],check=True)
rows = [{"path": j, **{k:getattr(timecore.resolve_one(j,"EXIF","Europe/Vienna"),k) for k in ("source","local_str","exif_dt","offset","offset_short","epoch","warn")}}]
res = timecore.resolve_one(j,"EXIF","Europe/Vienna")
from app.modules.timestamp_tool import router as tr
rows = [res.to_dict()] if hasattr(res,"to_dict") else None
out = tr._run_execute(rows, {j}, False, True, 60, "", "", None, True)
assert any(x.get("xmp_touched") for x in out["results"]), out["results"]
assert int(os.path.getmtime(j+".xmp")) == res.epoch, (os.path.getmtime(j+".xmp"), res.epoch)
assert j+".xmp" in out["written_paths"]
print("PASS")
