import os, sys, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="hh_"); os.environ["MEDIA_ROOT"] = MR
from app.core import timecore, executor
import inspect
p = os.path.join(MR, "2026.05.05-10.00.00.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
for tz, short in (("Asia/Kolkata",""),("Europe/Vienna","+2"),("Asia/Kathmandu","")):
    r = timecore.resolve_one(p, "EXIF", tz)
    print(tz, r.offset, repr(r.offset_short))
    assert r.offset_short == short or (short=="+2" and r.offset_short in ("+2","+02")), (tz, r.offset_short)
print("PASS")
