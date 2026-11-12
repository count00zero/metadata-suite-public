import os, tempfile, subprocess as sp
MR = tempfile.mkdtemp(prefix="gd_"); os.environ["MEDIA_ROOT"] = MR
from app.core import timecore
def mk(name, dto, gpsdt, lat="24.45", lon="54.64"):
    p=os.path.join(MR,name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    d,t=gpsdt.split(" ")
    sp.run(["exiftool","-overwrite_original","-q",f"-DateTimeOriginal={dto}",f"-GPSLatitude={lat}","-GPSLatitudeRef=N",f"-GPSLongitude={lon}","-GPSLongitudeRef=E",f"-GPSDateStamp={d}",f"-GPSTimeStamp={t}",p],check=True)
    return p
# Xiaomi case: GPS stamp 2h behind wall clock
p=mk("2026.08.05-19.55.13_08.jpg","2026:08:05 19:55:13","2026:08:05 17:55:13")
r=timecore.resolve_one(p,"EXIF","Europe/Vienna")
assert r.target_name=="2026.08.05-19.55.13_08.jpg", r.target_name
assert r.offset_str=="+04:00" if hasattr(r,"offset_str") else True
d=r.to_dict(); print(d.get("source"), d.get("offset"), d.get("warn"))
assert "GPS-Zone" in d["source"], d["source"]
# consistent GPS stamp -> GPS wins
p2=mk("x2.jpg","2026:08:05 19:55:13","2026:08:05 15:55:13")
r2=timecore.resolve_one(p2,"EXIF","Europe/Vienna"); assert "GPS-UTC" in r2.to_dict()["source"], r2.to_dict()["source"]
# gap of 30 h (other day) -> no distrust
p3=mk("x3.jpg","2026:08:05 19:55:13","2026:08:04 07:55:13")
r3=timecore.resolve_one(p3,"EXIF","Europe/Vienna"); assert "GPS-UTC" in r3.to_dict()["source"]
# env override
os.environ["GPS_UTC_TRUST"]="always"
r4=timecore.resolve_one(p,"EXIF","Europe/Vienna"); assert "GPS-UTC" in r4.to_dict()["source"]
print("PASS")

os.environ.pop("GPS_UTC_TRUST",None)
# Samsung in flight mode: clock on Vienna (+02:00), GPS UTC correct -> GPS wins, 18:07:44 +04:00
p5=mk("2026.08.05-16.07.44_15.jpg","2026:08:05 16:07:44","2026:08:05 14:07:44")
sp.run(["exiftool","-overwrite_original","-q","-OffsetTime=+02:00","-OffsetTimeOriginal=+02:00",p5],check=True)
r5=timecore.resolve_one(p5,"EXIF","Europe/Vienna"); d5=r5.to_dict(); print(d5["source"], d5.get("offset"))
assert r5.target_name=="2026.08.05-18.07.44_15.jpg", r5.target_name
assert "GPS-UTC" in d5["source"]
# same without offset tags would be ambiguous -> still distrusted (Xiaomi case)
assert "GPS-Zone" in timecore.resolve_one(mk("2026.08.05-19.55.13_09.jpg","2026:08:05 19:55:13","2026:08:05 17:55:13"),"EXIF","Europe/Vienna").to_dict()["source"]
print("PASS2")
# DJI drone, GPS void (clock written as UTC, +2h Vienna): same result, but no warning when
# filename and metadata agree
pd=mk("2026.08.19-16.46.56_683.jpg","2026:08:19 16:46:56","2026:08:19 14:46:56","4.6338","73.5627")
rd=timecore.resolve_one(pd,"EXIF","Europe/Vienna"); dd=rd.to_dict()
assert rd.target_name=="2026.08.19-16.46.56_683.jpg" and dd["zieldate"].endswith("+05:00"), dd["zieldate"]
assert not dd["warn"], dd["warn"]
print("PASS3")
