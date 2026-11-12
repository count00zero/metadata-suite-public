"""Module 'Kontext nachfüllen': scan reads only the .xmp, fills only what is missing (height/zone, weather, light, date, IPTC),
patches an existing .nfo, embeds into photos, keeps existing values (geo-service faked)."""
import html, os, re, subprocess as sp, sys, tempfile, time

SCRATCH = tempfile.mkdtemp(prefix="ctxfill_")
MR = os.path.join(SCRATCH, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
os.environ["BATCH_SIZE_CTXFILL"] = "0"
os.environ.pop("AI_CONTEXT", None)                     # the module must work with the AI switch OFF
os.environ["IPTC_CREDIT"] = "Familie Test"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app
from app.core import geoservice, nfocore, ctxfill, geocore
from fastapi.testclient import TestClient
c = TestClient(app)

CTX = {"daypart_de": "Nachmittag", "season_de": "Herbst", "month_de": "Oktober", "year": 2026, "weekday_de": "Sonntag",
       "local_time": "2026-10-11T16:10:58+02:00", "tz": "Europe/Vienna", "elevation_m": 345.0,
       "weather": {"summary_de": "bedeckt", "temp_c": 14, "wind_kmh": 14, "wind_de": "leichter Wind", "precip_mm": 0.0,
                   "cloud_pct": 100, "humidity_pct": 60},
       "daylight": {"label": "Tageslicht", "sunrise": "07:10", "sunset": "18:17"}, "moon": {"label": "Neumond (1 %)"},
       "extra_de": "x", "parts_de": {}}
calls = []
PV = ["2026-10-01T00:00:00|r3"]
TIMEOUTS = []
def fake_get(path, params, timeout_s=None):
    TIMEOUTS.append(timeout_s)
    calls.append(path)
    if path == "/context":
        return dict(CTX), "ok"
    if path == "/sights/status":
        return {"place_version": PV[0]}, "ok"
    if path == "/reverse":
        return {"found": True, "country": "Adriatisches Meer", "country_a": "", "state": "", "county": "", "city": "",
                "locality": "Golf von Triest", "street": "", "source": "maritime"}, "ok"
    if path == "/elevation":
        return {"elevation": 345.0}, "ok"
    if path == "/timezoneJSON":
        return {"timezoneId": "Europe/Vienna"}, "ok"
    return None, "down"
geoservice._get = fake_get
geoservice.health = lambda: {"status": "ok"}

def sidecar(media, extra=()):
    a = ["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=48.1591", "-XMP-exif:GPSLongitude=16.3328",
         "-XMP-exif:DateTimeOriginal=2026:10:11 16:10:58+02:00", *extra, media + ".xmp"]
    sp.run(a, check=True)

def img(name):
    p = os.path.join(MR, name)
    sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x48", "-frames:v", "1", p, "-loglevel", "error"], check=True)
    sp.run(["exiftool", "-overwrite_original", "-q", "-DateTimeOriginal=2026:10:11 16:10:58", "-OffsetTimeOriginal=+02:00", "-Make=Foo", "-Model=Bar", p], check=True)
    return p

def xmp_text(p):
    return open(p + ".xmp", encoding="utf-8").read()

def poll(resp):
    src = resp.text
    while True:
        m = re.search(r'hx-get="(/ctxfill/(?:scan|apply)/progress/[0-9a-f]+)"', src)
        if not m:
            return src
        time.sleep(0.05)
        src = c.get(m.group(1)).text

a = img("a.jpg"); sidecar(a)                                   # empty sidecar except position
b = img("b.jpg"); sidecar(b, ["-XMP-mon:CtxWeather=Regen (alt)", "-XMP-mon:CtxTempC=3"])   # weather already there
nopos = img("nopos.jpg")
sp.run(["exiftool", "-overwrite_original", "-q", "-XMP-dc:Description=nur Text", nopos + ".xmp"], check=True)
noxmp = img("noxmp.jpg")
v = os.path.join(MR, "clip.mp4")
sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1", v, "-loglevel", "error"], check=True)
sidecar(v)
nfo = os.path.join(MR, "clip.nfo")
open(nfo, "w", encoding="utf-8").write("<movie><plot>Cam: [COLOR green]X[/COLOR] [CR]In: [COLOR yellow]a[/COLOR], [CR][COLOR white]KI Text[/COLOR]"
                                       "[CR][COLOR lightblue]Temp: 1 °C[/COLOR]</plot></movie>")

# --- scan (xmp only) -----------------------------------------------------------------------------
idx = c.get("/ctxfill/")
assert idx.status_code == 200 and "Kontext nachfüllen" in idx.text
form = {"path": MR, "root_path": MR, "depth": 3}
page = poll(c.post("/ctxfill/scan/start", data=form))
assert "a.jpg" in page and "b.jpg" in page and "clip.mp4" in page, page[:800]
assert "nopos.jpg" not in page.split("hx-post=\"/ctxfill/apply\"")[0].split("class=\"rows\"")[-1], "file without position must not be listed"
assert "1 ohne Position" in page and "1 ohne .xmp" in page, re.sub(r"<[^>]+>", " ", page)[:600]
rows = ctxfill.inspect(a, 48.1591, 16.3328)
assert set(rows["missing"]) == {"hoehe", "wetter", "licht", "tag", "land", "ort", "iptc"}, rows
rb = ctxfill.inspect(b, 48.1591, 16.3328)
assert "CtxWeather" in rb["present"]
assert "wetter" not in rb["missing"], rb
rv = ctxfill.inspect(v, 48.1591, 16.3328)
assert "nfo" not in rv["missing"], rv      # nothing known yet that the NFO could lack

# --- apply: all groups, embed ----------------------------------------------------------------------
sel = [a, b, v]
r = poll(c.post("/ctxfill/apply", data={"selected": sel, "groups": list(ctxfill.GROUPS), "embed": "1"}))
assert "Datei(en) bearbeitet" in r and "3 Datei" in r, re.sub(r"<[^>]+>", " ", r)[:500]
ta = xmp_text(a)
for needle in ("CtxWeather>bedeckt", "CtxTempC>14", "CtxDaylight>Tageslicht", "CtxSunrise>07:10", "CtxMoon>Neumond", "CtxDaypart>Nachmittag",
               "CtxElevation>345", "CtxTimezone>Europe/Vienna", "GPSAltitude>", "AltitudeSource>dem", "CtxWindText>leichter Wind", "CtxFilledBy>"):
    assert needle in ta, (needle, ta)
assert "Credit>Familie Test<" in ta and "photoshop:DateCreated" in ta, ta
# existing weather of b stays (no overwrite), the rest is added
tb = xmp_text(b)
assert "Regen (alt)" in tb and "CtxTempC>3<" in tb and "bedeckt" not in tb and "CtxDaylight>Tageslicht" in tb, tb
# embedded into the photo: altitude + IPTC
emb = sp.run(["exiftool", "-j", "-n", "-GPSAltitude", "-IPTC:Credit", "-IPTC:Source", a], capture_output=True, text=True).stdout
assert '"GPSAltitude": 345' in emb and "Familie Test" in emb, emb
# video: sidecar only, .nfo patched (old Temp line replaced, AI line kept)
tn = open(nfo, encoding="utf-8").read()
assert "Temp: 14 °C" in tn and "Temp: 1 °C" not in tn and "KI Text" in tn and "Licht: Tageslicht (Aufgang 07:10 · Untergang 18:17)" in tn and tn.count("</plot>") == 1, tn
assert sp.run(["exiftool", "-q", "-GPSAltitude", v], capture_output=True, text=True).stdout.strip() == "", "video itself must stay untouched"

# an XMP that is complete but a .nfo without the block gets flagged
open(nfo, "w", encoding="utf-8").write("<movie><plot>Cam: [COLOR green]X[/COLOR]</plot></movie>")
lat, lon, _m = geocore.read_xmp_gps(v + ".xmp")
assert ctxfill.inspect(v, lat, lon)["missing"] == ["nfo"]
poll(c.post("/ctxfill/apply", data={"selected": [v], "groups": ["nfo"]}))
assert "Temp: 14 °C" in open(nfo, encoding="utf-8").read() and ctxfill.inspect(v, lat, lon)["missing"] == []

# --- second scan: nothing left to fill ----------------------------------------------------------------
page = poll(c.post("/ctxfill/scan/start", data=form))
assert "Nichts nachzufüllen" in page, re.sub(r"<[^>]+>", " ", page)[:600]

# --- overwrite + group selection -----------------------------------------------------------------------
CTX["weather"]["temp_c"] = 20
poll(c.post("/ctxfill/apply", data={"selected": [b], "groups": ["wetter"], "overwrite": "1"}))
tb = xmp_text(b)
assert "CtxWeather>bedeckt" in tb and "CtxTempC>20<" in tb, tb

# --- why there is no weather is named in the result
w = img("w.jpg"); sidecar(w)
saved = CTX.pop("weather"); CTX["weather_status"] = "failed"
res = ctxfill.fill_one(w, {"wetter"})
assert any("fehlgeschlagen" in n for n in res["notes"]), res
CTX["weather"] = saved; CTX.pop("weather_status")

assert [t for t, cl in zip(TIMEOUTS, calls) if cl == "/context"] and all(t >= 40 for t, cl in zip(TIMEOUTS, calls) if cl == "/context"), TIMEOUTS   # slow first /context call must not hit the 8 s timeout

# --- Ort & Gewaesser: sidecar without place gets sea + nearest water feature, other keywords stay, NFO Ort line follows ---
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
o = img("sea.jpg"); sidecar(o, ["-XMP-dc:Subject+=Urlaub", "-XMP-dc:Subject+=Boot"])
assert "ort" in ctxfill.inspect(o, 48.1591, 16.3328)["missing"]
poll(c.post("/ctxfill/apply", data={"selected": [o], "groups": ["ort"]}))
to = xmp_text(o)
assert "Adriatisches Meer" in to and "Golf von Triest" in to, to
subj = sp.run(["exiftool", "-j", "-XMP-dc:Subject", o + ".xmp"], capture_output=True, text=True).stdout
assert "Urlaub" in subj and "Boot" in subj and "Golf von Triest" in subj and "geotagged" in subj, subj
assert "ort" not in ctxfill.inspect(o, 48.1591, 16.3328)["missing"]
# a complete sidecar is left alone without overwrite, re-resolved with it
sp.run(["exiftool", "-overwrite_original", "-q", "-XMP-iptcCore:Location=Alter Ort", o + ".xmp"], check=True)
poll(c.post("/ctxfill/apply", data={"selected": [o], "groups": ["ort"]}))
assert "Alter Ort" in xmp_text(o)
poll(c.post("/ctxfill/apply", data={"selected": [o], "groups": ["ort"], "overwrite": "1"}))
assert "Golf von Triest" in xmp_text(o) and "Alter Ort" not in xmp_text(o)
# refresh: complete files are offered on request
pg = poll(c.post("/ctxfill/scan/start", data={**form, "refresh": "1"}))
assert "sea.jpg" in pg and "Auffrischen" in pg, re.sub(r"<[^>]+>", " ", pg)[:600]
# video: the "Ort:" line of the .nfo is rewritten, the rest stays
v2 = os.path.join(MR, "sea.mp4")
sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1", v2, "-loglevel", "error"], check=True)
sidecar(v2)
n2 = os.path.join(MR, "sea.nfo")
open(n2, "w", encoding="utf-8").write("<movie><plot>Ort: [COLOR yellow]No Location[/COLOR] [CR]Cam: [COLOR green]X[/COLOR]</plot></movie>")
poll(c.post("/ctxfill/apply", data={"selected": [v2], "groups": ["ort"]}))
tn2 = open(n2, encoding="utf-8").read()
assert "Ort: [COLOR yellow]Adriatisches Meer, Golf von Triest[/COLOR]" in tn2 and "Cam: [COLOR green]X[/COLOR]" in tn2, tn2

# --- data version: stamped when the place is resolved, older stamps are offered again ----------------------------------
geoservice._PV.update(at=0.0, v="")
poll(c.post("/ctxfill/apply", data={"selected": [o], "groups": ["ort"], "overwrite": "1"}))
assert "CtxPlaceVersion>2026-10-01T00:00:00|r3<" in xmp_text(o), xmp_text(o)
assert not ctxfill.inspect(o, 48.1591, 16.3328, PV[0])["stale"]
PV[0] = "2026-11-01T00:00:00|r3"                                  # index rebuilt later
st = ctxfill.inspect(o, 48.1591, 16.3328, PV[0])
assert st["stale"] and {"ort", "tag"} <= set(st["missing"]), st
geoservice._PV.update(at=0.0, v="")
pg = poll(c.post("/ctxfill/scan/start", data=form))
assert "sea.jpg" in pg and "älterem Datenstand" in pg, re.sub(r"<[^>]+>", " ", pg)[:500]
# an unstamped (legacy) file with a place is NOT flagged
lg = img("legacy.jpg"); sidecar(lg, ["-XMP-photoshop:Country=Österreich", "-XMP-photoshop:City=Musterdorf"])
assert "ort" not in ctxfill.inspect(lg, 48.1591, 16.3328, PV[0])["missing"]

# --- schema stamp (CtxSchema) + stale flags (AiStale / NfoStale) -----------------------------------------------------------
ta = xmp_text(a)
assert "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in ta, ta                          # complete -> stamped
assert "AiStale>%d<" % ctxfill.CTX_SCHEMA in ta and "NfoStale" not in ta, ta     # new data after the AI text -> AI stale; a photo has no .nfo
assert "NfoStale>%d<" % ctxfill.CTX_SCHEMA in xmp_text(v) and "AiStale>" in xmp_text(v)
sa = ctxfill.inspect(a, 48.1591, 16.3328)
assert sa["schema"] == ctxfill.CTX_SCHEMA and sa["ai_stale"] and not sa["nfo_stale"] and "wetter" not in sa["missing"] and "land" not in sa["missing"], sa
assert ctxfill.inspect(v, 48.1591, 16.3328)["nfo_stale"]
# stamp is only the data groups: a stamp does not count as "added data"
assert not any(t in ctxfill._ALL_CTX for t in ctxfill.STAMP_TAGS)
# a sidecar stamped with an OLDER schema gets exactly the newer groups (country / currency) added -- nothing else is asked again
CTX.update(country_info={"name": "Kroatien", "text_de": "Währung: Euro (EUR)"})
old = img("old_schema.jpg"); sidecar(old, ["-XMP-mon:CtxDaypart=Vormittag", "-XMP-mon:CtxSchema=1", "-XMP-exif:GPSAltitude=1", "-XMP-mon:CtxWeather=sonnig", "-XMP-mon:CtxTempC=20",
                                       "-XMP-mon:CtxDaylight=Tag", "-XMP-mon:CtxElevation=1", "-XMP-mon:CtxTimezone=Europe/Vienna", "-XMP-photoshop:Country=Kroatien", "-XMP-photoshop:City=Rovinj"])
si = ctxfill.inspect(old, 48.1591, 16.3328)
assert si["schema"] == 1 and "land" in si["missing"] and not {"wetter", "licht", "tag", "hoehe"} & set(si["missing"]), si
n_before = len(calls)
rr = ctxfill.fill_one(old, {"land"})
assert rr["ok"], rr
to_ = xmp_text(old)
assert "CtxCountryInfo>Währung: Euro (EUR)<" in to_ and "CtxCountry>Kroatien<" in to_ and "CtxWeather>sonnig" in to_, to_
assert "CtxSchema>1<" in to_ and "AiStale>" in to_, to_                          # a partial run (only 'land') must NOT claim the newer schema, but marks the AI text stale
assert "land" in ctxfill.inspect(old, 48.1591, 16.3328)["missing"]
ctxfill.fill_one(old, set(ctxfill.GROUPS))
assert "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in xmp_text(old) and ctxfill.inspect(old, 48.1591, 16.3328)["missing"] == ["ort"] or "land" not in ctxfill.inspect(old, 48.1591, 16.3328)["missing"]
# a domestic photo: nothing new to add, still stamped -> never asked again, AI text not marked stale
CTX.pop("country_info")
dom = img("domestic.jpg"); sidecar(dom, ["-XMP-mon:CtxDaypart=Vormittag", "-XMP-mon:CtxWeather=sonnig", "-XMP-mon:CtxTempC=20", "-XMP-mon:CtxDaylight=Tag",
                                        "-XMP-mon:CtxElevation=1", "-XMP-mon:CtxTimezone=Europe/Vienna"])
ctxfill.fill_one(dom, set(ctxfill.GROUPS), mark_stale=True)
td = xmp_text(dom)
assert "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in td, td
# weather that exists never (stamped) is not re-flagged for ever; an INconclusive answer (rate limit) is not stamped, so it is retried
nw = img("noweather.jpg"); sidecar(nw)
saved = CTX.pop("weather"); CTX["weather_status"] = "limited"
ctxfill.fill_one(nw, set(ctxfill.GROUPS))
assert "CtxSchema>" not in xmp_text(nw), "rate-limited weather must not be stamped as complete"
CTX["weather_status"] = "empty"
ctxfill.fill_one(nw, set(ctxfill.GROUPS))
assert "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in xmp_text(nw)
assert "wetter" not in ctxfill.inspect(nw, 48.1591, 16.3328)["missing"], "conclusively empty weather must not be asked again"
CTX["weather"] = saved; CTX.pop("weather_status")

# --- capture time: a video whose sidecar date has NO zone still gets time-of-day / weather via the zone-qualified FILE NAME ---------------
nt = os.path.join(MR, "2026.03.01-13.20.04.mp4")
sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1", nt, "-loglevel", "error"], check=True)
sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=48.2396", "-XMP-exif:GPSLongitude=16.4204",
        "-XMP-xmp:CreateDate=2026:03:01 12:20:04", "-XMP-photoshop:Country=Österreich", "-XMP-photoshop:City=Wien", nt + ".xmp"], check=True)
CTXREQ = []
_fg = geoservice._get
def _spy(path, params, timeout_s=None):
    if path == "/context": CTXREQ.append(dict(params))
    return _fg(path, params, timeout_s)
geoservice._get = _spy
rn = ctxfill.fill_one(nt, set(ctxfill.GROUPS))
tn_ = xmp_text(nt)
check_epoch = CTXREQ and CTXREQ[0]["time"] == 1772367604          # 2026-03-01 13:20:04 Europe/Vienna (UTC+1) = 12:20:04 UTC
assert check_epoch, CTXREQ
assert "CtxDaypart>" in tn_ and "CtxWeather>" in tn_ and "CtxTimeBasis>Dateiname<" in tn_ and "CtxSchema>%d<" % ctxfill.CTX_SCHEMA in tn_, tn_
assert "CtxClock" not in tn_, "a time taken from the file name must not feed the camera-clock check"
# a file with NO usable time at all: not stamped (inconclusive, retried later), and the reason is named
nn = os.path.join(MR, "IMG_without_time.mp4")
sp.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1", nn, "-loglevel", "error"], check=True)
sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=48.2396", "-XMP-exif:GPSLongitude=16.4204",
        "-XMP-mon:CtxTrip=Ordner X", "-XMP-mon:CtxTripSource=folder", nn + ".xmp"], check=True)
rz = ctxfill.fill_one(nn, set(ctxfill.GROUPS))
assert "CtxSchema>" not in xmp_text(nn), "no known capture time -> must NOT be stamped complete"
assert any("Aufnahmezeit" in n for n in rz["notes"]), rz
# a sidecar stamped with the OLD schema 2 that lacks time-of-day / weather is checked by its marker tags again (the stamp was wrong)
oldst = img("stamp2.jpg"); sidecar(oldst, ["-XMP-mon:CtxSchema=2", "-XMP-mon:CtxTrip=X"])
assert {"wetter", "licht", "tag"} <= set(ctxfill.inspect(oldst, 48.1591, 16.3328)["missing"]), ctxfill.inspect(oldst, 48.1591, 16.3328)
geoservice._get = _fg

# --- no service / bad input -------------------------------------------------------------------------------
r = c.post("/ctxfill/apply", data={"selected": [a], "groups": []})
assert "Nichts ausgewählt" in r.text
os.environ["GEO_SERVICE_URL"] = ""
assert "GEO_SERVICE_URL" in c.post("/ctxfill/apply", data={"selected": [a], "groups": ["wetter"]}).text
print("PASS")
