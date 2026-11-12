"""SQLite index (fileindex): speeds up the scans but the FILE stays the master -- a changed file (even with a restored mtime), a deleted
database, FILE_INDEX=off or an expired entry all lead to the same results as without it. Plus: run history, GPS pre-filter."""
import os, subprocess as sp, sys, tempfile, time

SCR = tempfile.mkdtemp(prefix="fidx_")
MR = os.path.join(SCR, "root"); os.makedirs(MR)
os.environ["MEDIA_ROOT"] = MR
os.environ["GEO_SERVICE_URL"] = "http://geo-service.invalid:8000"
os.environ["FILE_INDEX_DB"] = os.path.join(SCR, "idx.sqlite")
os.environ.pop("FILE_INDEX", None); os.environ.pop("FILE_INDEX_DAYS", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import fileindex, ctxfill, geoservice, geocore, jobs, fillcore, aicore
from app.modules.ctx_fill import router as cr

fails = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {extra}"))
    if not cond: fails.append(name)

geoservice._get = lambda path, params, timeout_s=None: ({"place_version": "pv"}, "ok") if path == "/sights/status" else (None, "down")

def sidecar(media, extra=()):
    sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-exif:GPSLatitude=48.1591", "-XMP-exif:GPSLongitude=16.3328",
            "-XMP-exif:DateTimeOriginal=2026:10:11 16:10:58+02:00", *extra, media + ".xmp"], check=True)

paths = []
for i in range(6):
    p = os.path.join(MR, f"2026.10.11-16.10.{i:02d}.jpg"); open(p, "wb").write(b"x"); sidecar(p); paths.append(p)

class Step:
    def to_dict(self): return {}
def scan(**kw):
    job = jobs.create_job()
    return cr.scan_start_work(job, path=MR, paths=paths, step=Step(), **kw)

# ---- first scan reads everything, second comes from the index ----------------------------------------------------------------------
r1 = scan()
check("first scan: nothing from the index", r1["counts"]["from_index"] == 0 and len(r1["rows"]) == 6, r1["counts"])
r2 = scan()
check("second scan: all from the index, same rows", r2["counts"]["from_index"] == 6 and [x["path"] for x in r2["rows"]] == [x["path"] for x in r1["rows"]]
      and [x["missing"] for x in r2["rows"]] == [x["missing"] for x in r1["rows"]], r2["counts"])
check("cached rows keep the position", all(x["lat"] is not None for x in r2["rows"]), r2["rows"][:1])
r3 = scan(use_index=False)
check("use_index=False reads the files again (recheck)", r3["counts"]["from_index"] == 0 and len(r3["rows"]) == 6)

# ---- the file is the master: content change with RESTORED mtime is noticed (ctime), different env too ----------------------------------
scan()                                                      # warm
x = paths[0] + ".xmp"
st = os.stat(x)
sp.run(["exiftool", "-config", geocore.MON_CONFIG, "-overwrite_original", "-q", "-XMP-mon:CtxSchema=2", "-XMP-mon:CtxDaypart=Nacht", "-XMP-mon:CtxWeather=klar", "-XMP-mon:CtxTempC=9",
        "-XMP-mon:CtxDaylight=Nacht", "-XMP-mon:CtxElevation=1", "-XMP-mon:CtxTimezone=Europe/Vienna", x], check=True)
os.utime(x, ns=(st.st_atime_ns, st.st_mtime_ns))            # what the Jahreslauf's timestamp step does
r4 = scan()
first = next(r for r in r4["rows"] if r["path"] == paths[0])
check("rewritten sidecar with restored mtime is re-read (stale cache impossible)", r4["counts"]["from_index"] == 5 and "wetter" not in first["missing"] and "land" not in first["missing"], (r4["counts"], first["missing"]))
os.environ["IPTC_CREDIT"] = "Familie X"
r5 = scan()
check("changed environment (IPTC credit) invalidates the cached results", r5["counts"]["from_index"] == 0, r5["counts"])
os.environ.pop("IPTC_CREDIT")

# ---- database deleted / disabled / expired -------------------------------------------------------------------------------------------
fileindex.close(); os.remove(os.environ["FILE_INDEX_DB"])
check("database deleted: scan still works (re-reads, recreates)", scan()["counts"]["from_index"] == 0 and scan()["counts"]["from_index"] == 6)
os.environ["FILE_INDEX"] = "off"; fileindex.close()
check("FILE_INDEX=off: nothing cached, results identical", scan()["counts"]["from_index"] == 0 and len(scan()["rows"]) == len(r1["rows"]) and not fileindex.enabled())
os.environ.pop("FILE_INDEX"); fileindex.close()
scan()
os.environ["FILE_INDEX_DAYS"] = "0"
check("FILE_INDEX_DAYS=0: entries are never trusted", scan()["counts"]["from_index"] == 0)
os.environ.pop("FILE_INDEX_DAYS")
fileindex._exec("UPDATE ctx_cache SET seen = seen - 400*86400")
check("entries older than the 365-day default are ignored", scan()["counts"]["from_index"] == 0)

# ---- AI scan: 'has a description' is cached, the file stays master -----------------------------------------------------------------
from app.modules.ai_description import router as ar
TEXT = ["Ein langer schöner Beschreibungstext vom Bild."]
sp.run(["exiftool", "-overwrite_original", "-q", f"-XMP-dc:Description={TEXT[0]}", paths[1] + ".xmp"], check=True)
reads = []
orig = aicore.get_existing_ai_description
aicore.get_existing_ai_description = lambda f, strict=False: (reads.append(f), orig(f, strict))[1]
geoservice.context_info = lambda *a, **k: ("", {})
def ai_scan(**kw):
    job = jobs.create_job()
    return ar.browse_start_work(job, camera_model="", candidates=[paths[1]], depth=3, embed_in_image=True, overwrite_existing=False, path=MR, root_path=MR, step=Step(), type_filter="IMAGE", **kw)
a1 = ai_scan(); n1 = len(reads)
a2 = ai_scan()
check("AI scan: a known description is not read again", a1["skipped_done"] == 1 and a2["skipped_done"] == 1 and len(reads) == n1, (n1, len(reads)))
sp.run(["exiftool", "-overwrite_original", "-q", "-XMP-dc:Description=", paths[1] + ".xmp"], check=True)
a3 = ai_scan()
check("description removed from the file -> the file wins (row offered again)", len(a3["rows"]) == 1 and len(reads) > n1, (a3["skipped_done"], len(reads)))
a4 = ai_scan(use_index=False)
check("recheck bypasses the AI cache", len(a4["rows"]) == 1)

# ---- run history -------------------------------------------------------------------------------------------------------------------------
rid = fileindex.run_begin("Jahr 2026", {"aim": True})
fileindex.run_step(rid, "Fotos 2026", "GPS Sidecar", True, "3 Sidecar geschrieben")
fileindex.run_end(rid, "done")
rr = fileindex.recent_runs(5); st = fileindex.run_steps(rid)
check("run history: run + steps stored", rr and rr[0]["title"] == "Jahr 2026" and rr[0]["status"] == "done" and st and st[0]["text"] == "3 Sidecar geschrieben", (rr, st))

# ---- GPS pre-filter: files whose sidecar already has a place never reach the expensive media read -------------------------------------
from app.modules.gps_sidecar import router as gr
from app.core import timecore
g_done, g_new = paths[2], paths[3]
sp.run(["exiftool", "-overwrite_original", "-q", "-XMP-photoshop:Country=Österreich", "-XMP-photoshop:City=Wien", g_done + ".xmp"], check=True)
seen = []
real = timecore.resolve_batch
timecore.resolve_batch = lambda files, *a, **k: (seen.extend(files), real(files, *a, **k))[1]
def gps_scan(skip, **kw):
    seen.clear()
    return gr.browse_start_work(jobs.create_job(), camera_model="", candidate_paths=[g_done, g_new], default_tz="Europe/Vienna", delete_srt=False, depth=3,
                                embed_in_image=True, fixed_location="", override_gps_from_track=False, path=MR, root_path=MR, remember_no_gps=False,
                                retry_no_gps=False, skip_already_done=skip, step=Step(), traccar_device_ids=[], type_filter="IMAGE",
                                use_dawarich=False, use_dawarich_import=False, use_dji_srt=False, **kw)
res = gps_scan(True)
check("GPS: only the file without a place is read from the media", seen == [g_new] and res["skipped_done"] == 1, (seen, res.get("skipped_done")))
res = gps_scan(False)
check("GPS: without 'skip already done' both are read", sorted(seen) == sorted([g_done, g_new]))
res = gps_scan(True, regeocode=True)
check("GPS: regeocode reads the files that already have a place", g_done in seen)

print("\n%s" % ("ALL OK" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
