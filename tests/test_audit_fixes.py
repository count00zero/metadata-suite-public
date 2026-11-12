import os, sys, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="audit_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from app.core import timecore, executor, nfocore, settings
def ex(*a): return sp.run(["exiftool","-s3",*a],capture_output=True,text=True).stdout.strip()
d = os.path.join(MR,"p"); os.makedirs(d)
def photo(name, dto, extra=()):
    p=os.path.join(d,name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-frames:v","1",p],check=True)
    sp.run(["exiftool","-q","-overwrite_original",f"-DateTimeOriginal={dto}",f"-CreateDate={dto}",*extra,p],check=True)
    return p
# 1) embedded camera offset (-05:00) abroad: file name = local wall clock, no GPS
p = photo("2026.10.03-16.28.29.jpg","2026:10:03 16:28:29",["-OffsetTimeOriginal=-05:00"])
r = timecore.resolve_batch([p],"EXIF","Europe/Vienna")[0]
print(r.source, r.offset, r.tz_confident)
assert r.offset == "-05:00" and r.tz_confident and "EXIF-Offset" in r.source
# 2) executor writes the 3 Offset tags for a confident photo, none for fallback guess
q = photo("2026.10.03-17.00.00.jpg","2026:10:03 17:00:00",["-OffsetTimeOriginal=+09:00"])
rq = timecore.resolve_batch([q],"EXIF","Europe/Vienna")[0]
o = executor.apply_resolution(rq, False, True, 60); assert o.exif_written, o.exif_error
assert ex("-OffsetTimeOriginal",q)=="+09:00" and ex("-OffsetTimeDigitized",q)=="+09:00" and ex("-OffsetTime",q)=="+09:00", ex("-a","-G1","-Offset*",q)
f = photo("clip_cam_zz.jpg","2026:10:03 18:00:00")    # no offset anywhere -> fallback zone guess
rf = timecore.resolve_batch([f],"EXIF","Europe/Vienna")[0]
assert not rf.tz_confident
o = executor.apply_resolution(rf, False, True, 60)
assert ex("-OffsetTimeOriginal",f)=="", "fallback guess must NOT be written as real offset"
# UTC-diff photo (confident) gets it
u = photo("2026.10.03-16.28.57.jpg","2026:10:03 14:28:57")  # CreateDate/DTO 2h behind filename
ru = timecore.resolve_batch([u],"EXIF","Europe/Vienna")[0]; print(ru.source, ru.tz_confident)
executor.apply_resolution(ru, False, True, 60)
print("after UTC-diff write:", ex("-OffsetTimeOriginal",u))
assert ex("-OffsetTimeOriginal",u)==ru.offset
# 3) NFO garbled scan no longer flags a normal NFO
x = "<movie>\n                <plot>fine</plot>\n</movie>"
assert not nfocore._nfo_plot_is_garbled(x)
assert nfocore._nfo_plot_is_garbled("<plot>"+"a"*20+"</plot>")
# 4) video prompt
from app.core import aicore
assert "Heimvideo" in aicore.build_prompts("", True)[1] and "Foto" in aicore.build_prompts("", False)[1]
assert ".m4v" in nfocore.NFO_VIDEO_EXT
print("PASS")
