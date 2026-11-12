import html as _h, os, re, json, subprocess as sp, tempfile, time, sys
MR = tempfile.mkdtemp(prefix="gpsfeat_"); os.environ["MEDIA_ROOT"] = MR
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
from app.core import geocore
c = TestClient(app)
f = os.path.join(MR, "d"); os.makedirs(f)
def img(name, **tags):
    p = os.path.join(f, name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=blue:s=64x64","-frames:v","1",p],check=True)
    if tags:
        sp.run(["exiftool","-q","-overwrite_original",*[f"-{k}={v}" for k,v in tags.items()],p],check=True)
    return p
def vid(name):
    p = os.path.join(f, name)
    sp.run(["ffmpeg","-y","-loglevel","error","-f","lavfi","-i","color=c=red:s=64x64","-t","1",p],check=True)
    return p
def exif(p, *t):
    return sp.run(["exiftool","-n","-s3",*t,p],capture_output=True,text=True).stdout.split()
def run(data, path=f):
    r = c.post("/gps/browse/start", data={"path":path,"root_path":MR,"depth":"0",**data})
    jid = re.search(r'progress/([a-f0-9]+)', r.text).group(1)
    for _ in range(600):
        x = c.get(f"/gps/browse/progress/{jid}")
        if "progress-panel" not in x.text: return x.text
        time.sleep(0.05)
def rows_of(html):
    return json.loads(_h.unescape(re.search(r"name=\"all_scanned_rows\" value=.(.*?).>", html, re.S).group(1)))

# --- parsing
s1 = os.path.join(MR,"a.srt"); open(s1,"w").write("1\n00:00:00,000 --> 00:00:00,033\n<font>[iso : 100] [latitude: 48.12345] [longitude: 16.54321] [altitude: 1]</font>\n")
s2 = os.path.join(MR,"b.srt"); open(s2,"w").write("1\n...\nGPS (16.5, 48.25, 22) BAROMETER: 3\n")
assert geocore.parse_dji_srt(s1) == (48.12345, 16.54321), geocore.parse_dji_srt(s1)
assert geocore.parse_dji_srt(s2) == (48.25, 16.5)
# fixed place "7" (normally one of the geo-service's "Meine Orte") -- faked, no service in the test
geocore.all_places = lambda: [geocore.FixedPlace("7", "Calle X 12, Santiago", -33.5, -70.25, geocore.GeoResult(
    country="Chile", country_a="CHL", state="RM", county="Santiago", city="Santiago", locality="Centro",
    street="Calle X 12", source="Mein Ort: 7"))]

# --- camera filter
img("2026.01.01-10.00.00.jpg", Make="Foo", Model="Alpha")
img("2026.01.02-10.00.00.jpg", Make="Bar", Model="Beta")
h = run({"camera_model":"Foo"}); r = rows_of(h)
assert len(r)==1 and "Alpha" in r[0]["camera"], r
assert "Kamera-Filter" in h
h = run({}); assert len(rows_of(h))==2

# --- no-GPS marker
p = vid("2026.02.02-10.00.00.mp4")
assert geocore.mark_no_gps(p, p+".xmp", True)
assert geocore.read_xmp_gps(p+".xmp") == (None, None, True)
h = run({}); assert not any(x["filename"].endswith("mp4") for x in rows_of(h)) and "gemerkt" in h
h = run({"retry_no_gps":"true"}); assert any(x["filename"].endswith("mp4") for x in rows_of(h))
os.remove(p+".xmp"); os.remove(p)

# --- DJI srt + delete
v = vid("2026.03.03-10.00.00.mp4"); os.replace(s1, v[:-4]+".srt")
h = run({"camera_model":"", "use_dji_srt":"true"}); rr=[x for x in rows_of(h) if x["filename"].endswith("mp4")]
assert rr and rr[0]["gps_source"]=="DJI-.srt" and rr[0]["lat"]==48.12345, rr
h0 = run({}); assert [x for x in rows_of(h0) if x["filename"].endswith("mp4")][0]["lat"] is None
rr[0]["geo"]=None
ex = c.post("/gps/execute", data={"selected":rr[0]["path"],"all_rows":json.dumps(rr),"delete_srt":"true"})
assert "DJI-.srt gelöscht" in ex.text, ex.text[-600:]
assert not os.path.exists(v[:-4]+".srt")
assert geocore.read_xmp_gps(v+".xmp")[:2]==(48.12345,16.54321)
assert "geotagged" in open(v+".xmp").read()

# --- fixed location + override on a photo with existing GPS (N/E)
g = img("2026.04.04-10.00.00.jpg", GPSLatitude="10", GPSLatitudeRef="N", GPSLongitude="20", GPSLongitudeRef="E")
h = run({"fixed_location":"7"}); rs={x["filename"]:x for x in rows_of(h)}
x = rs["2026.04.04-10.00.00.jpg"]; assert x["gps_source"]=="Datei (vorhandenes GPS)" and not x["override_applied"], x
assert rs["2026.01.01-10.00.00.jpg"]["gps_source"]=="Fester Ort: 7"
h = run({"fixed_location":"7","override_gps_from_track":"true"}); rs={x["filename"]:x for x in rows_of(h)}
x = rs["2026.04.04-10.00.00.jpg"]; assert x["override_applied"] and x["lat"]==-33.5 and x["old_lat"]==10, x
# geocode start (fixed geo, no network)
sel=[x["path"] for x in rs.values()]
r = c.post("/gps/geocode/start", data={"selected":sel,"all_scanned_rows":json.dumps(list(rs.values()))})
jid = re.search(r'progress/([a-f0-9]+)', r.text).group(1)
for _ in range(600):
    y = c.get(f"/gps/geocode/progress/{jid}")
    if "progress-panel" not in y.text: break
    time.sleep(0.05)
rows2 = json.loads(_h.unescape(re.search(r"name=\"all_rows\" value='(.*?)'", y.text, re.S).group(1))) if "all_rows" in y.text else None
assert rows2 and all(r_["geo"] and r_["geo"]["country"]=="Chile" for r_ in rows2), y.text[:500]
ex = c.post("/gps/execute", data={"selected":sel,"all_rows":json.dumps(rows2)})
assert exif(g,"-GPSLatitude","-GPSLongitude")==["-33.5","-70.25"], exif(g,"-GPSLatitude","-GPSLongitude")
assert exif(g+".xmp","-XMP-exif:GPSLatitude")==["-33.5"] or exif(g+".xmp","-XMP-exif:GPSLatitude")[0].startswith("-33.5")
assert "Chile" in open(g+".xmp").read() and "geotagged" in open(g+".xmp").read()
print("PASS")
