import os, re, sys, tempfile
MR = tempfile.mkdtemp(prefix="info_"); os.environ["MEDIA_ROOT"] = MR
os.environ.update({"TRACCAR_TOKEN": "supersecret-token-123", "DAWARICH_API_KEY": "another-secret-key",
                   "TRACCAR_URL": "http://127.0.0.1:9", "NFO_TYPO_VARIABLE": "x",
                   "BUILD_DATE": "2026-10-06T12:00:00Z", "GIT_COMMIT": "abcdef1234567", "APP_VERSION": "1.2",
                   "EXIFTOOL_VERSION": "99.99", "FFMPEG_VERSION": "1.2.3", "MEDIAINFO_VERSION": "0.1"})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)

r = c.get("/info/"); assert r.status_code == 200
h = r.text
# secrets never leave the server, in any representation
for url in ("/info/", "/info/report.txt", "/info/data.json", "/info/services"):
    t = c.get(url).text
    assert "supersecret-token-123" not in t and "another-secret-key" not in t, url
assert "●●●●●●●● (21 Zeichen)" in h
# build identity, tools, env, paths
assert "v1.2" in h and "2026-10-06T12:00:00Z" in h and "abcdef1234" in h
assert re.search(r"exiftool.*?\d+\.\d+", h, re.S)
# pinned (99.99) != installed -> warning note
assert "Image wurde mit 99.99 gebaut" in h
assert "Image wurde mit 1.2.3 gebaut" in h and "Image wurde mit 0.1 gebaut" in h   # ffmpeg/ffprobe + mediainfo pin checks
assert "NFO_TYPO_VARIABLE" in h and "Unbekannte Variablen" in h
assert "MEDIA_ROOT" in h and MR in h
# service check: unreachable service is reported, not crashing
s = c.get("/info/services").text
assert "nicht erreichbar" in s and "127.0.0.1:9" in s
# sights index fragment: no service -> a hint; with the service -> numbers, refresh steps, download list
assert "GEO_SERVICE_URL" in c.get("/info/sights").text
from app.core import geoservice
os.environ["GEO_SERVICE_URL"] = "http://geo.invalid:8000"
geoservice._get = lambda path, params, timeout_s=None: ({"available": True, "sights": 962545, "with_wikidata": 316561, "wikidata_done": 316561,
    "with_importance": 135614, "descriptions_translated": 7585, "wikipedia_extracts": 1200, "built": "2026-10-20T10:00:00", "source": "planet.osm.pbf",
    "place_version": "2026-10-20T10:00:00|r3", "refresh": {"started": "2026-10-24T03:30:00", "finished": "2026-10-24T05:00:00", "ok": False, "next_due": "2026-11-23T03:30",
    "steps": [{"name": "wikidata", "status": "error", "detail": "RuntimeError: down", "seconds": 5}, {"name": "build", "status": "skipped", "detail": "the PBF has not changed", "seconds": 0}],
    "downloads": {"importance": [{"file": "wikimedia-importance.2026-10-01.csv.gz", "date": "2026-10-01", "used": "2026-10-02T01:00:00"}]}}}, "ok")
sg = c.get("/info/sights").text
assert "962.545" in sg and "7.585" in sg and "Fehler in einem Schritt" in sg and "2026-11-23T03:30" in sg and "wikimedia-importance.2026-10-01.csv.gz" in sg and "2026-10-20T10:00:00|r3" in sg, re.sub(r"<[^>]+>", " ", sg)[:700]
os.environ["GEO_SERVICE_URL"] = ""
# report + json
rep = c.get("/info/report.txt").text
assert "exiftool" in rep and "TRACCAR_URL=http://127.0.0.1:9" in rep
j = c.get("/info/data.json").json()
assert {"build", "tools", "packages", "env_groups", "paths"} <= set(j)
# every module page carries the info icon as the LAST pill of its nav (far right)
for u in ("/timestamp/", "/gps/", "/ai/", "/nfo/", "/collage/", "/mtime/", "/shortvideos/", "/gpsfill/"):
    nav = re.search(r'<nav class="modules-nav">(.*?)</nav>', c.get(u).text, re.S).group(1)
    pills = re.findall(r'<(?:a|span) class="module-pill[^"]*"', nav)
    assert 'info-pill' in pills[-1], (u, pills[-1])
    assert 'href="/info/"' in nav, u
print("PASS")

# automatic version label when APP_VERSION is empty
os.environ["APP_VERSION"] = ""
from app.core import infocore
v = infocore.build_info()["version"]
assert v == "2026.10.06+abcdef1", v

# env table shows the computed value for an empty APP_VERSION
rows = {r["name"]: r for g in infocore.env_groups() for r in g["rows"]}
assert rows["APP_VERSION"]["effective"] == "2026.10.06+abcdef1", rows["APP_VERSION"]
assert rows["BUILD_DATE"]["state"] == "set" and rows["BUILD_DATE"]["effective"] == ""
assert "2026.10.06+abcdef1" in c.get("/info/").text and "(automatisch)" in c.get("/info/").text

# commit comment on the Info page (below the commit)
os.environ["GIT_MESSAGE"] = "folder.jpg wird nie umbenannt oder verarbeitet (Ordnerliste ausgenommen)"
assert infocore.build_info()["git_message"].startswith("folder.jpg wird nie")
html = c.get("/info/").text
assert "Letzter Commit:" in html and "folder.jpg wird nie umbenannt oder verarbeitet (Ordnerliste ausgenommen)" in html
assert "GIT_MESSAGE" in html
os.environ["GIT_MESSAGE"] = ""
assert "Letzter Commit:" not in c.get("/info/").text
print("PASS commit message")

# exiftran shows its (apt package) version, not just "vorhanden"
import shutil as _sh
if _sh.which("exiftran") and _sh.which("dpkg-query"):
    infocore.tool_versions.cache_clear()
    _t = {t["name"]: t for t in infocore.tool_versions()}["exiftran"]
    assert _t["version"] not in ("vorhanden", "?") and _t["version"][0].isdigit(), _t
    print("PASS exiftran version", _t["version"])
