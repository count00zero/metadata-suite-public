"""sightsrefresh.py -- keeps the sights index up to date by itself.

  python -m app.sightsindex download   fetch the source files that changed (Wikimedia importance, optionally the planet PBF)
  python -m app.sightsindex refresh    download -> build (when the PBF is newer) -> wikidata -> importance -> wikitext -> translate
  python -m app.sightsindex schedule   long-running: runs `refresh` every GEO_SIGHTS_REFRESH_DAYS days at GEO_SIGHTS_REFRESH_AT

Downloads go to GEO_SIGHTS_DOWNLOAD_DIR (/downloads) as <name>.<date>.<ext> with a manifest.json (ETag / Last-Modified, "used" =
a build / import with that file succeeded). Nothing is replaced before the new file is complete and checked. Kept per source:
the NEWEST file and the latest older file that was used successfully (so there is always a version that is known to work);
everything older is deleted. The previous sights.sqlite stays as sights.sqlite.prev (hard link) when a build replaces it.
The result of every run is written to <db>.refresh.json (shown by GET /sights/status and in the suite's Info page).
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Callable, Optional

import requests

IMPORTANCE_URLS = ("https://downloads.opencagedata.com/public/wikimedia_importance/%YYYYMM%/wikimedia_importance.tsv.gz,"
                   "https://nominatim.org/data/wikimedia-importance.csv.gz")

SOURCES = {          # kind -> (env name of the URL, default URL, check)
    # several mirrors, comma separated: the one with the newest Last-Modified is downloaded (see pick_url)
    "importance": ("GEO_SIGHTS_IMPORTANCE_URL", IMPORTANCE_URLS, "gz"),
    "pbf": ("GEO_SIGHTS_PBF_URL", "", "pbf"),
}


# ----------------------------------------------------------------------------------------------- manifest
def manifest_path(dl_dir: str) -> str:
    return os.path.join(dl_dir, "manifest.json")


def load_manifest(dl_dir: str) -> dict:
    try:
        with open(manifest_path(dl_dir), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_manifest(dl_dir: str, m: dict) -> None:
    os.makedirs(dl_dir, exist_ok=True)
    tmp = manifest_path(dl_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, manifest_path(dl_dir))


def latest(dl_dir: str, kind: str) -> Optional[str]:
    """Path of the newest complete file of this source, None if there is none."""
    for e in reversed((load_manifest(dl_dir).get(kind) or [])):
        p = os.path.join(dl_dir, e["file"])
        if os.path.isfile(p):
            return p
    return None


def importance_source(dl_dir: str, configured: str = "") -> Optional[str]:
    """The importance file to use: the newest of the downloaded one, the configured file and every self-built
    'wikimedia_importance*.tsv.gz' next to the configured file (the wikimedia-build container writes them there)."""
    cand = []
    d = latest(dl_dir, "importance")
    if d:
        cand.append(d)
    if configured:
        if os.path.isfile(configured):
            cand.append(configured)
        import glob
        cand += glob.glob(os.path.join(os.path.dirname(configured) or ".", "wikimedia_importance*.tsv.gz"))
    cand = [c for c in dict.fromkeys(cand) if os.path.isfile(c)]
    return max(cand, key=os.path.getmtime) if cand else None


def source_stamp(path: str) -> str:
    return f"{os.path.basename(path)}|{int(os.path.getmtime(path))}"


def mark_used(dl_dir: str, kind: str, path: str) -> None:
    m = load_manifest(dl_dir)
    for e in m.get(kind) or []:
        if e["file"] == os.path.basename(path):
            e["used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_manifest(dl_dir, m)
    prune(dl_dir, kind)


def prune(dl_dir: str, kind: str, log: Callable = lambda m: None) -> list:
    """Keeps the newest file and the latest OLDER one that was used successfully; deletes the rest. Returns the deleted names."""
    m = load_manifest(dl_dir)
    entries = m.get(kind) or []
    if len(entries) <= 1:
        return []
    keep = {entries[-1]["file"]}
    for e in reversed(entries[:-1]):
        if e.get("used"):
            keep.add(e["file"])
            break
    gone = []
    for e in entries:
        if e["file"] not in keep:
            try:
                os.remove(os.path.join(dl_dir, e["file"]))
            except OSError:
                pass
            gone.append(e["file"])
            log(f"  removed the old file {e['file']}")
    m[kind] = [e for e in entries if e["file"] in keep]
    save_manifest(dl_dir, m)
    return gone


# ----------------------------------------------------------------------------------------------- download
def _check(path: str, how: str) -> None:
    if how == "gz":
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 24):
                pass
    elif how == "pbf":
        with open(path, "rb") as fh:
            if b"OSMHeader" not in fh.read(512):
                raise ValueError("not an OpenStreetMap PBF file (no OSMHeader)")


def expand_months(url: str, log: Callable = lambda m: None, http=requests, now: Optional[datetime] = None, max_back: int = 24) -> Optional[str]:
    """A URL with %YYYYMM% (e.g. .../public/wikimedia_importance/%YYYYMM%/file.tsv.gz): the folders are named by month and some months are missing,
    so try this month, then each earlier one (HEAD only, no directory listing) and return the first that exists, else None."""
    if "%YYYYMM%" not in url:
        return url
    d = now or datetime.now()
    y, m = d.year, d.month
    for _ in range(max_back + 1):
        cand = url.replace("%YYYYMM%", f"{y}{m:02d}")
        try:
            if http.head(cand, allow_redirects=True, timeout=30).status_code < 400:
                return cand
        except Exception as e:                                   # noqa: BLE001
            log(f"  {cand}: not reachable ({type(e).__name__})")
            return None                                          # network down: no use in probing 24 months
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    log(f"  {url}: no folder for the last {max_back} months")
    return None


def drop_wrong_kind(kind: str, spec: str, log: Callable = lambda m: None) -> str:
    """Removes addresses that cannot be a file of this kind (a planet .pbf listed for the importance file would win the
    'newest' comparison and start an 80 GB download that then fails the gzip check)."""
    bad = {"importance": (".pbf",), "pbf": (".gz", ".csv", ".tsv", ".sql")}.get(kind, ())
    keep = []
    for u in [u for u in re.split(r"[,\s]+", (spec or "").strip()) if u]:
        if any(u.split("?")[0].lower().endswith(b) for b in bad):
            log(f"  ignoring {u}: not a '{kind}' file")
        else:
            keep.append(u)
    return ",".join(keep)


def pick_url(spec: str, log: Callable = lambda m: None, http=requests, now: Optional[datetime] = None) -> str:
    """`spec` = one URL or several separated by comma/space. With several, the mirror whose file was modified last wins
    (HEAD Last-Modified); mirrors that do not answer are skipped. If none answers, the first URL is returned so that
    download() reports the real error."""
    urls = [u for u in re.split(r"[,\s]+", (spec or "").strip()) if u]
    first = urls[0] if urls else ""
    urls = [u2 for u in urls if (u2 := expand_months(u, log, http, now))]
    if len(urls) <= 1:
        return urls[0] if urls else first.replace("%YYYYMM%", (now or datetime.now()).strftime("%Y%m"))
    best, best_t = None, None
    for u in urls:
        try:
            h = http.head(u, allow_redirects=True, timeout=30)
            if h.status_code >= 400:
                log(f"  {u}: HTTP {h.status_code} -- skipped")
                continue
            lm = h.headers.get("Last-Modified", "")
            try:
                t = parsedate_to_datetime(lm).timestamp() if lm else 0.0
            except (TypeError, ValueError):
                t = 0.0
        except Exception as e:                                   # noqa: BLE001
            log(f"  {u}: not reachable ({type(e).__name__}) -- skipped")
            continue
        log(f"  {u}: modified {lm or 'unknown'}")
        if best is None or t > best_t:
            best, best_t = u, t
    return best or urls[0]


def _name(url: str, day: str) -> str:
    base = os.path.basename(url.split("?")[0]) or "download"
    stem, _, ext = base.partition(".")
    return f"{stem}.{day}.{ext}" if ext else f"{stem}.{day}"


def download(kind: str, url: str, dl_dir: str, log: Callable = lambda m: None, http=requests, now: Optional[datetime] = None) -> dict:
    """{'changed': bool, 'file': path | None, 'reason': str}. Raises on a failed or invalid download (the old files stay)."""
    check = SOURCES[kind][2]
    os.makedirs(dl_dir, exist_ok=True)
    m = load_manifest(dl_dir)
    entries = m.setdefault(kind, [])
    h = http.head(url, allow_redirects=True, timeout=60)
    if h.status_code >= 400:
        raise RuntimeError(f"{url}: HTTP {h.status_code}")
    etag, lastmod, size = h.headers.get("ETag", ""), h.headers.get("Last-Modified", ""), int(h.headers.get("Content-Length", "0") or 0)
    if entries:
        top = entries[-1]
        same = (etag and top.get("etag") == etag) or (not etag and lastmod and top.get("last_modified") == lastmod
                                                      and (not size or top.get("size") == size))
        if same and os.path.isfile(os.path.join(dl_dir, top["file"])):
            return {"changed": False, "file": os.path.join(dl_dir, top["file"]), "reason": "unchanged on the server"}
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    final = os.path.join(dl_dir, _name(url, day))
    if os.path.exists(final):                                    # second download on the same day: keep the first, name the new one apart
        final = os.path.join(dl_dir, _name(url, day + (now or datetime.now()).strftime("-%H%M%S")))
    part, pmeta = final + ".part", final + ".part.json"
    have = 0
    if os.path.exists(part):
        try:
            with open(pmeta, encoding="utf-8") as fh:
                pm = json.load(fh)
        except (OSError, ValueError):
            pm = {}
        if pm.get("etag") == etag and pm.get("last_modified") == lastmod and etag + lastmod:
            have = os.path.getsize(part)
    headers = {"Range": f"bytes={have}-"} if have else {}
    log(f"downloading {url} ({size / 1e6:.0f} MB){' -- continuing at ' + str(have // 1000000) + ' MB' if have else ''}")
    with http.get(url, stream=True, headers=headers, allow_redirects=True, timeout=120) as r:
        if r.status_code == 200:
            have = 0
        elif r.status_code != 206:
            raise RuntimeError(f"{url}: HTTP {r.status_code}")
        with open(pmeta, "w", encoding="utf-8") as fh:
            json.dump({"etag": etag, "last_modified": lastmod}, fh)
        with open(part, "ab" if have else "wb") as out:
            for chunk in r.iter_content(1 << 20):
                out.write(chunk)
    got = os.path.getsize(part)
    if size and got != size:
        raise RuntimeError(f"incomplete download ({got} of {size} bytes) -- run again to continue")
    try:
        _check(part, check)
    except Exception as e:                                       # noqa: BLE001
        for junk in (part, pmeta):
            if os.path.exists(junk):
                os.remove(junk)
        raise RuntimeError(f"downloaded file is not valid ({e}); removed") from e
    os.replace(part, final)
    if os.path.exists(pmeta):
        os.remove(pmeta)
    entries.append({"file": os.path.basename(final), "date": day, "etag": etag, "last_modified": lastmod, "size": got, "used": ""})
    save_manifest(dl_dir, m)
    prune(dl_dir, kind, log)
    return {"changed": True, "file": final, "reason": "downloaded"}


# ----------------------------------------------------------------------------------------------- refresh
def next_due(last_finished: Optional[datetime], days: int, at: str, now: Optional[datetime] = None) -> datetime:
    """Next run: `days` after the last finished run, at HH:MM; without a previous run: the next HH:MM from now."""
    now = now or datetime.now()
    try:
        hh, mm = (int(x) for x in (at or "03:30").split(":"))
    except ValueError:
        hh, mm = 3, 30
    if last_finished is None:
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return t if t > now else t + timedelta(days=1)
    return (last_finished + timedelta(days=max(1, days))).replace(hour=hh, minute=mm, second=0, microsecond=0)


def refresh(db_path: str, dl_dir: str, env: Callable = os.environ.get, log: Callable = lambda m: None, steps_override: Optional[dict] = None,
            http=requests) -> dict:
    """The whole pipeline. Each step runs on its own: a failing one is recorded and the next ones still run."""
    from . import sightsindex as SI
    started = datetime.now()
    result = {"started": started.strftime("%Y-%m-%dT%H:%M:%S"), "steps": [], "ok": True}
    ov = steps_override or {}

    def step(name: str, fn: Callable):
        t0 = time.time()
        try:
            detail = (ov.get(name) or fn)()
            result["steps"].append({"name": name, "status": "ok", "detail": detail or "", "seconds": round(time.time() - t0)})
            log(f"[refresh] {name}: ok {detail or ''}")
            return True
        except Exception as e:                                    # noqa: BLE001
            result["steps"].append({"name": name, "status": "error", "detail": f"{type(e).__name__}: {e}"[:300], "seconds": round(time.time() - t0)})
            result["ok"] = False
            log(f"[refresh] {name}: FAILED {type(e).__name__}: {e}")
            return False

    got = {}
    for kind, (env_name, default, _chk) in SOURCES.items():
        url = (env(env_name) or default).strip()
        if not url or url.lower() in ("off", "none", "0", "false"):
            continue
        url = pick_url(drop_wrong_kind(kind, url, log), log, http)
        if not url:
            continue
        def dl(kind=kind, url=url):
            r = download(kind, url, dl_dir, log, http)
            got[kind] = r
            return r["reason"] + (f": {os.path.basename(r['file'])}" if r["file"] else "")
        step(f"download {kind}", dl)

    pbf = latest(dl_dir, "pbf") if (env("GEO_SIGHTS_PBF_URL") or "").strip() else env("GEO_OSM_PBF_FILE", "/osm/planet-latest.osm.pbf")
    built = SI.read_meta(db_path) if os.path.exists(db_path) else {}
    rebuilt = False
    if pbf and os.path.isfile(pbf):
        mt = f"{os.path.getmtime(pbf):.0f}"
        if not built or built.get("source_mtime") != mt or env("GEO_SIGHTS_REFRESH_FORCE_BUILD") == "1":
            def do_build():
                nonlocal rebuilt
                work = env("GEO_SIGHTS_WORK_DIR") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "sights-work")
                seq = SI.extract(pbf, work, env("GEO_SIGHTS_OSMIUM_INDEX", "flex_mem"), tuple(), log)
                try:
                    SI.build(seq, db_path, tuple(), log, source=os.path.basename(pbf), source_path=pbf)
                finally:
                    if os.path.exists(seq):
                        os.remove(seq)
                rebuilt = True
                if (env("GEO_SIGHTS_PBF_URL") or "").strip():
                    mark_used(dl_dir, "pbf", pbf)
                return "rebuilt from " + os.path.basename(pbf)
            step("build", do_build)
        else:
            result["steps"].append({"name": "build", "status": "skipped", "detail": "the PBF has not changed", "seconds": 0})
    else:
        result["steps"].append({"name": "build", "status": "skipped", "detail": f"no PBF ({pbf or 'GEO_OSM_PBF_FILE not set'})", "seconds": 0})

    if os.path.exists(db_path):
        step("wikidata", lambda: SI.wikidata(db_path, delay=float(env("GEO_SIGHTS_WD_DELAY", "1.0") or 1.0),
                                             workers=int(env("GEO_SIGHTS_WD_WORKERS", "2") or 2), log=log))
        imp = importance_source(dl_dir, env("GEO_WIKIMEDIA_IMPORTANCE_FILE", ""))
        if imp and os.path.isfile(imp):
            if rebuilt or SI.read_meta(db_path).get("importance_src") != source_stamp(imp) or not SI.read_meta(db_path).get("importance_run"):
                def do_imp():
                    r = SI.importance(db_path, imp, log=log)
                    if latest(dl_dir, "importance") == imp:
                        mark_used(dl_dir, "importance", imp)
                    return f"{r['sights_with_importance']:,} sights have a value"
                step("importance", do_imp)
            else:
                result["steps"].append({"name": "importance", "status": "skipped", "detail": "file unchanged", "seconds": 0})
        if env("GEO_SIGHTS_REFRESH_WIKITEXT", "1") != "0":
            from . import sightswikitext as W
            step("wikitext", lambda: W.wikitext(db_path, scope=env("GEO_SIGHTS_WP_SCOPE", "notable") or "notable", log=log))
        if env("GEO_SIGHTS_REFRESH_TRANSLATE", "1") != "0" and (env("AI_PROVIDER") or "").strip():
            from . import sightstranslate as T
            step("translate", lambda: T.translate(db_path, T.Providers.from_env(env), scope=env("GEO_SIGHTS_TR_SCOPE", "notable") or "notable",
                                                  batch=int(env("GEO_SIGHTS_TR_BATCH", "10") or 10), workers=int(env("GEO_SIGHTS_TR_WORKERS", "2") or 2),
                                                  verbose=False, log=log))
    finished = datetime.now()
    result["finished"] = finished.strftime("%Y-%m-%dT%H:%M:%S")
    days = int(env("GEO_SIGHTS_REFRESH_DAYS", "0") or 0)
    if days > 0:
        result["next_due"] = next_due(finished, days, env("GEO_SIGHTS_REFRESH_AT", "03:30")).strftime("%Y-%m-%dT%H:%M")
    result["downloads"] = {k: [{"file": e["file"], "date": e["date"], "used": e.get("used", "")} for e in v] for k, v in load_manifest(dl_dir).items()}
    SI.write_status(db_path, result)
    return result


def schedule(db_path: str, dl_dir: str, env: Callable = os.environ.get, log: Callable = lambda m: None, sleep: Callable = time.sleep,
             now: Callable = datetime.now, max_runs: int = 0, runner: Callable = refresh) -> int:
    """Runs `refresh` every GEO_SIGHTS_REFRESH_DAYS days (HH:MM = GEO_SIGHTS_REFRESH_AT). 0 days = idle (exits with a hint)."""
    from . import sightsindex as SI
    days = int(env("GEO_SIGHTS_REFRESH_DAYS", "0") or 0)
    if days <= 0:
        log("GEO_SIGHTS_REFRESH_DAYS is 0 -- automatic refresh is off")
        return 0
    runs = 0
    first_due: Optional[datetime] = None            # without a previous run the target stays fixed (it must not roll on to the next day)
    while True:
        st = SI.read_status(db_path)
        try:
            last = datetime.strptime(st["finished"], "%Y-%m-%dT%H:%M:%S") if st.get("finished") else None
        except ValueError:
            last = None
        if last is None:
            first_due = first_due or next_due(None, days, env("GEO_SIGHTS_REFRESH_AT", "03:30"), now())
            due = first_due
        else:
            due = next_due(last, days, env("GEO_SIGHTS_REFRESH_AT", "03:30"), now())
        if now() >= due:
            log(f"refresh starts ({due:%Y-%m-%d %H:%M} was due)")
            runner(db_path, dl_dir, env, log)
            runs += 1
            if max_runs and runs >= max_runs:
                return 0
            continue
        log(f"next refresh {due:%Y-%m-%d %H:%M}")
        sleep(min(3600, max(30, (due - now()).total_seconds())))
