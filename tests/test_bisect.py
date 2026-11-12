import os,sys,subprocess,tempfile,shutil
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.core import executor, timecore, exifpool
import subprocess as sp
d=tempfile.mkdtemp(); f=os.path.join(d,"2026.10.03-09.14.27.jpg")
sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-frames:v","1",f],check=True)
real=exifpool.run
def fake(cmd,*a,**k):
    if cmd[:3]==["timeout",cmd[1],"exiftool"]:
        joined=" ".join(cmd)
        # whole-batch and the quicktime group fail; others succeed
        if joined.count("=")>3 or "quicktime:time:all" in joined:
            return exifpool.Result(1,"","Error: Not a HASH reference",cmd)
    return real(cmd,*a,**k)
executor.exifpool.run=fake
res=timecore.resolve_batch([f],"ALL","Europe/Vienna")[0] if hasattr(timecore,"resolve_batch") else None
o=executor.apply_resolution(res,False,True,30)
print(o.exif_written,o.exif_note,o.exif_error)
assert o.exif_written and "quicktime:time:all" in o.exif_note
executor.exifpool.run=lambda c,*a,**k: exifpool.Result(1,"","Error: boom",c)
o=executor.apply_resolution(res,False,True,30); assert not o.exif_written and "boom" in o.exif_error
print("PASS")
