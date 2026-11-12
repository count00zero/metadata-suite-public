"""sightsindex.py -- an own sights database built from the OpenStreetMap planet file (the one your Nominatim uses), so
/sights does not depend on Photon's "nearest 50 objects" and finds the famous place 8 km away as well.

Two runs, both resumable and safe to repeat:

  python -m app.sightsindex build      planet.osm.pbf  ->  osmium tags-filter  ->  osmium export  ->  sights.sqlite (+ R-tree)
  python -m app.sightsindex wikidata   Wikidata batch (wbgetentities): sitelink count (notability), short description,
                                       Wikipedia titles; continues where it stopped
  python -m app.sightsindex importance Wikimedia importance (Nominatim's wikimedia-importance.sql.gz / .csv.gz, GEO_WIKIMEDIA_IMPORTANCE_FILE):
                                       how much attention a sight's Wikipedia article gets (0..1); feeds the ranking
  python -m app.sightsindex download | refresh | schedule   automatic updates -- see sightsrefresh.py
  python -m app.sightsindex wikitext   first sentences of the Wikipedia article (de/en) -> "summary" -- see sightswikitext.py
  python -m app.sightsindex translate  German descriptions via LLM (Ollama/Google) where Wikidata has none -- see sightstranslate.py
  python -m app.sightsindex all        build, then wikidata, then importance (when the file is there)
  python -m app.sightsindex status     what the database holds

The planet file is only READ (mount it read-only). `build` writes a filtered extract into the work folder first (a few
GB for the whole planet, deleted afterwards), then the database into <db>.building and swaps it in at the end.

What is kept (named objects only): tourism=attraction/museum/viewpoint/zoo/theme_park, historic=* without the skip
catalogue (memorials, wayside shrines, plain 'building' ... see sights.SKIP_HISTORIC and GEO_POI_SKIP), natural=peak,
waterway=waterfall, leisure=nature_reserve -- and of those only objects with a wikidata/wikipedia tag or in a core category
(attraction, museum, castle, ruins, zoo, theme park, waterfall, viewpoint, nature reserve). So a church or a peak
needs a Wikidata/Wikipedia link, a museum does not.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, Optional

import requests

from . import sights as S

CORE = {"attraction", "museum", "castle", "ruins", "zoo", "theme_park", "waterfall", "viewpoint", "nature_reserve"}
GEOM_RANK = {"Point": 1, "LineString": 2, "MultiLineString": 2, "Polygon": 3, "MultiPolygon": 3}
OTHER_WIKIS = {"commonswiki", "specieswiki", "metawiki", "mediawikiwiki", "wikidatawiki", "incubatorwiki", "outreachwiki", "foundationwiki",
               "sourceswiki", "wikimaniawiki", "strategywiki", "nostalgiawiki", "testwiki", "test2wiki", "labswiki", "loginwiki"}
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "geo-service-sights-index/1.0 (self-hosted; https://github.com/count00zero/metadata-suite)"


# ----------------------------------------------------------------------------------------------- osmium
def rw_connect(path: str) -> sqlite3.Connection:
    """Writer connection: WAL (readers -- /sights -- never block on a running import/translate, and vice versa) and a long
    busy timeout for the rare writer-vs-writer case. WAL is stored in the file, so it stays on for every later opening."""
    c = sqlite3.connect(path, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return c


def read_meta(db_path: str) -> dict:
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            return {k: v for k, v in c.execute("SELECT k, v FROM meta")}
        finally:
            c.close()
    except sqlite3.Error:
        return {}


def status_path(db_path: str) -> str:
    return db_path + ".refresh.json"


def read_status(db_path: str) -> dict:
    """Result of the last automatic refresh (written by `refresh`/`schedule`), {} when there was none."""
    try:
        with open(status_path(db_path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_status(db_path: str, data: dict) -> None:
    tmp = status_path(db_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, status_path(db_path))


def filter_expressions(extra_skip=()) -> list:
    skip = ",".join(sorted(set(S.SKIP_HISTORIC) | {x for x in extra_skip if x}))
    return ["nwr/tourism=attraction,museum,viewpoint,zoo,theme_park", f"nwr/historic!={skip}", "nwr/natural=peak",
            "nwr/waterway=waterfall", "nwr/leisure=nature_reserve"]


def run(cmd: list, log: Callable) -> None:
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1]} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[:600]}")


def extract(pbf: str, work_dir: str, index_type: str, extra_skip, log: Callable) -> str:
    """planet.osm.pbf -> filtered extract (with the nodes/ways the objects need) -> GeoJSON text sequence."""
    if not shutil.which("osmium"):
        raise RuntimeError("osmium-tool not found (it is part of the sights-import image)")
    if not os.path.isfile(pbf):
        raise FileNotFoundError(f"planet file not found: {pbf}")
    os.makedirs(work_dir, exist_ok=True)
    filtered = os.path.join(work_dir, "sights-filtered.osm.pbf")
    seq = os.path.join(work_dir, "sights.geojsonseq")
    t = time.time()
    run(["osmium", "tags-filter", pbf, *filter_expressions(extra_skip), "-o", filtered, "-O", "--no-progress"], log)
    log(f"filtered in {time.time() - t:.0f} s: {os.path.getsize(filtered) / 1e6:.0f} MB")
    t = time.time()
    run(["osmium", "export", filtered, "-f", "geojsonseq", "--geometry-types=point,linestring,polygon", "-a", "type,id",
         f"--index-type={index_type}", "-o", seq, "-O", "--no-progress"], log)
    log(f"exported in {time.time() - t:.0f} s: {os.path.getsize(seq) / 1e6:.0f} MB")
    os.remove(filtered)
    return seq


# ----------------------------------------------------------------------------------------------- geometry
def _ring_centroid(ring: list):
    """Centroid of a polygon ring (shoelace); falls back to the vertex mean for degenerate rings."""
    ox, oy = ring[0][0], ring[0][1]                      # relative to the first vertex: no cancellation with big coordinates
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        x1, y1, x2, y2 = x1 - ox, y1 - oy, x2 - ox, y2 - oy
        w = x1 * y2 - x2 * y1
        a += w
        cx += (x1 + x2) * w
        cy += (y1 + y2) * w
    if abs(a) < 1e-18:
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    return ox + cx / (3 * a), oy + cy / (3 * a)


def _inside(pt, ring) -> bool:
    x, y, ins = pt[0], pt[1], False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def representative(geom: dict) -> Optional[tuple]:
    """(lat, lng) for a GeoJSON geometry: the point itself, the middle vertex of a line, the centroid of the largest
    polygon (moved to its vertex mean when the centroid falls outside, e.g. a crescent-shaped park)."""
    t, c = geom.get("type"), geom.get("coordinates")
    try:
        if t == "Point":
            return float(c[1]), float(c[0])
        if t == "LineString":
            m = c[len(c) // 2]
            return float(m[1]), float(m[0])
        if t == "MultiLineString":
            line = max(c, key=len)
            m = line[len(line) // 2]
            return float(m[1]), float(m[0])
        if t in ("Polygon", "MultiPolygon"):
            polys = c if t == "MultiPolygon" else [c]
            ring = max((p[0] for p in polys), key=lambda r: (max(x for x, _ in r) - min(x for x, _ in r)) * (max(y for _, y in r) - min(y for _, y in r)))
            x, y = _ring_centroid(ring)
            if not _inside((x, y), ring):
                x, y = sum(p[0] for p in ring[:-1]) / max(1, len(ring) - 1), sum(p[1] for p in ring[:-1]) / max(1, len(ring) - 1)
            return float(y), float(x)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None
    return None


# ----------------------------------------------------------------------------------------------- one OSM object -> row
def _first(props: dict, *keys) -> str:
    for k in keys:
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


CASTLE_TYPE_DE = {"palace": "Palast", "stately": "Schloss", "manor": "Herrenhaus", "fortress": "Festung", "defensive": "Burg",
                  "kremlin": "Kreml", "shiro": "Burg (Shiro)", "citadel": "Zitadelle", "castrum": "Römerkastell"}


def classify(props: dict, extra_skip=()) -> Optional[tuple]:
    """(category, kind label) of the best matching tag, or None. historic=castle + castle_type says what it really is
    (palace -> Palast, stately -> Schloss, fortress -> Festung ...), so a Palais is no longer called "Burg/Schloss"."""
    best = None
    for key in ("tourism", "historic", "natural", "waterway", "leisure"):
        v = props.get(key)
        if not v or not S.wanted(key, v, extra_skip):
            continue
        cat = S.category_of(key, v)
        w = S.CATEGORIES[cat][1]
        if best is None or w > best[0]:
            kind = S.kind_de(key, v)
            if key == "historic" and v == "castle":
                kind = CASTLE_TYPE_DE.get((props.get("castle_type") or "").strip().lower(), kind)
            best = (w, cat, kind)
    return (best[1], best[2]) if best else None


def to_row(props: dict, geom: dict, extra_skip=()) -> Optional[dict]:
    name = _first(props, "name", "name:de", "name:en", "int_name")
    if not name:
        return None
    cls = classify(props, extra_skip)
    if cls is None:
        return None
    wikidata = _first(props, "wikidata")
    wikipedia = _first(props, "wikipedia")
    if not (wikidata or wikipedia or cls[0] in CORE):
        return None
    pos = representative(geom)
    if pos is None or not (-90 <= pos[0] <= 90 and -180 <= pos[1] <= 180):
        return None
    typ = {"node": "n", "way": "w", "relation": "r"}.get(str(props.get("@type")), "?")
    return {"osm": f"{typ}{props.get('@id')}", "name": name, "name_de": _first(props, "name:de"), "name_en": _first(props, "name:en"),
            "category": cls[0], "kind": cls[1], "lat": pos[0], "lng": pos[1], "rank": GEOM_RANK.get(geom.get("type"), 0),
            "wikidata": wikidata if re.fullmatch(r"Q\d+", wikidata or "") else "", "wikipedia": wikipedia,
            "website": _first(props, "website", "contact:website", "url"), "opening_hours": _first(props, "opening_hours"),
            "description": _first(props, "description:de", "description", "description:en"), "phone": _first(props, "phone", "contact:phone"),
            "castle_type": (props.get("castle_type") or "").strip().lower()}


def iter_features(path: str) -> Iterator[dict]:
    """GeoJSON text sequence (RFC 8142: records start with 0x1e, `osmium export` writes them)."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\x1e").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


# ----------------------------------------------------------------------------------------------- build
SCHEMA = """
CREATE TABLE sight (id INTEGER PRIMARY KEY, osm TEXT UNIQUE, name TEXT, name_de TEXT, name_en TEXT, category TEXT, kind TEXT,
                    lat REAL, lng REAL, rank INTEGER, wikidata TEXT, wikipedia TEXT, website TEXT, opening_hours TEXT,
                    description TEXT, phone TEXT, castle_type TEXT DEFAULT '', sitelinks INTEGER DEFAULT 0, wd_state INTEGER DEFAULT 0,
                    wp_de TEXT DEFAULT '', wp_en TEXT DEFAULT '', desc_de TEXT DEFAULT '', desc_en TEXT DEFAULT '',
                    label_de TEXT DEFAULT '', label_en TEXT DEFAULT '', importance REAL DEFAULT 0,
                    ai_state INTEGER DEFAULT 0, desc_de_ai TEXT DEFAULT '', ai_src TEXT DEFAULT '',
                    ex_de TEXT DEFAULT '', ex_en TEXT DEFAULT '', ex_state INTEGER DEFAULT 0);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""
COLS = ("osm", "name", "name_de", "name_en", "category", "kind", "lat", "lng", "rank", "wikidata", "wikipedia", "website",
        "opening_hours", "description", "phone", "castle_type")


def _mtime(path: str) -> str:
    try:
        return f"{os.path.getmtime(path):.0f}"
    except OSError:
        return ""


def build(geojson: str, db_path: str, extra_skip=(), log: Callable = lambda m: None, source: str = "", source_path: str = "") -> dict:
    """GeoJSON sequence -> database. Built next to the target and swapped in at the end (readers keep the old file until then).
    A wikidata state of an older database is carried over, so a rebuild does not start the Wikidata run again."""
    tmp = db_path + ".building"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    c = sqlite3.connect(tmp)
    c.executescript(SCHEMA)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    seen = kept = 0
    q = f"INSERT INTO sight ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))}) " \
        "ON CONFLICT(osm) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, rank=excluded.rank WHERE excluded.rank > sight.rank"
    batch: list = []
    for ft in iter_features(geojson):
        seen += 1
        row = to_row(ft.get("properties") or {}, ft.get("geometry") or {}, extra_skip)
        if row is None:
            continue
        kept += 1
        batch.append([row[k] for k in COLS])
        if len(batch) >= 20000:
            c.executemany(q, batch)
            batch.clear()
            log(f"  {seen:,} read, {kept:,} kept")
    if batch:
        c.executemany(q, batch)
    c.commit()
    log("building the R-tree ...")
    c.executescript("""
        CREATE VIRTUAL TABLE sight_rt USING rtree(id, minlat, maxlat, minlng, maxlng);
        INSERT INTO sight_rt SELECT id, lat, lat, lng, lng FROM sight;
        CREATE INDEX sight_wd ON sight(wd_state, wikidata);
        CREATE INDEX sight_cat ON sight(category);
    """)
    _carry_over_wikidata(c, db_path, log)
    n = c.execute("SELECT COUNT(*) FROM sight").fetchone()[0]
    for k, v in (("built", time.strftime("%Y-%m-%dT%H:%M:%S")), ("source", source), ("source_mtime", _mtime(source_path)), ("n_sight", str(n)), ("schema", "1")):
        c.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
    c.commit()
    c.close()
    for ext in ("-wal", "-shm"):                 # leftovers of the old file (WAL mode) must not meet the new one
        if os.path.exists(db_path + ext):
            os.remove(db_path + ext)
    if os.path.exists(db_path) and os.environ.get("GEO_SIGHTS_KEEP_PREV", "1") != "0":
        prev = db_path + ".prev"                      # the last database that served: a hard link, no copy, atomic
        try:
            if os.path.exists(prev):
                os.remove(prev)
            os.link(db_path, prev)
        except OSError:
            pass
    os.replace(tmp, db_path)
    log(f"done: {n:,} sights ({seen:,} OSM objects looked at)")
    return {"sights": n, "seen": seen}


CARRY_COLS = ("ai_state", "desc_de_ai", "ai_src", "ex_de", "ex_en", "ex_state")


def _carry_over_wikidata(c: sqlite3.Connection, db_path: str, log: Callable) -> None:
    if not os.path.exists(db_path):
        return
    try:
        c.execute("ATTACH DATABASE ? AS old", (db_path,))
        c.execute("""UPDATE sight SET (sitelinks, wd_state, wp_de, wp_en, desc_de, desc_en, label_de, label_en) =
                     (SELECT o.sitelinks, o.wd_state, o.wp_de, o.wp_en, o.desc_de, o.desc_en, o.label_de, o.label_en
                      FROM old.sight o WHERE o.wikidata = sight.wikidata AND o.wd_state = 1 LIMIT 1)
                     WHERE wikidata != '' AND EXISTS (SELECT 1 FROM old.sight o WHERE o.wikidata = sight.wikidata AND o.wd_state = 1)""")
        c.commit()
        if "importance" in {r[1] for r in c.execute("PRAGMA old.table_info(sight)")}:
            # Only the rows that have an importance, copied to an indexed temp table: the old table has no index on
            # (wikidata) alone, and an OR over it made this UPDATE scan the whole old table for every new row (hours).
            c.execute("CREATE TEMP TABLE old_imp AS SELECT osm, wikidata, importance FROM old.sight WHERE importance > 0")
            c.execute("CREATE INDEX temp.old_imp_wd ON old_imp(wikidata)")
            c.execute("CREATE INDEX temp.old_imp_osm ON old_imp(osm)")
            c.execute("""UPDATE sight SET importance = MAX(
                         COALESCE((SELECT MAX(o.importance) FROM old_imp o WHERE o.wikidata != '' AND o.wikidata = sight.wikidata), 0),
                         COALESCE((SELECT MAX(o.importance) FROM old_imp o WHERE o.osm = sight.osm), 0))""")
            c.commit()
            c.execute("DROP TABLE temp.old_imp")
        old_cols = {r[1] for r in c.execute("PRAGMA old.table_info(sight)")}
        carry = [k for k in CARRY_COLS if k in old_cols]            # LLM translations and Wikipedia extracts survive a rebuild
        if carry:
            cond = " OR ".join(f"o.{k} > 0" for k in ("ai_state", "ex_state") if k in carry)
            c.execute(f"UPDATE sight SET ({', '.join(carry)}) = (SELECT {', '.join('o.' + k for k in carry)} FROM old.sight o "
                      f"WHERE o.osm = sight.osm) WHERE EXISTS (SELECT 1 FROM old.sight o WHERE o.osm = sight.osm AND ({cond or '0'}))")
            c.commit()
        c.execute("DETACH DATABASE old")
        log("carried over the Wikidata data of the previous database")
    except sqlite3.Error as e:
        log(f"(could not carry over the old Wikidata data: {e})")


# ----------------------------------------------------------------------------------------------- Wikidata
def _count_sitelinks(sitelinks: dict) -> int:
    return sum(1 for k in sitelinks if re.fullmatch(r"[a-z_]{2,}wiki", k) and k not in OTHER_WIKIS)


def wikidata(db_path: str, delay: float = 1.0, batch: int = 50, limit: int = 0, log: Callable = lambda m: None,
             sleep: Callable = time.sleep, workers: int = 1, maxlag: int = 0) -> dict:
    """Fills sitelinks / Wikipedia titles / labels / short descriptions for every sight with a wikidata id, 50 per request.
    `workers` requests run in parallel (each worker waits `delay` after its request). Stops cleanly on errors; run it again
    to continue (rows done are marked wd_state=1)."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"{db_path} does not exist -- run 'build' first")
    workers = max(1, int(workers))
    c = rw_connect(db_path)
    todo = c.execute("SELECT COUNT(DISTINCT wikidata) FROM sight WHERE wikidata != '' AND wd_state = 0").fetchone()[0]
    log(f"{todo:,} Wikidata items to fetch ({workers} parallel)")
    done = errors = 0

    def work(ids):
        ents = _fetch(ids, log, sleep, maxlag)
        if ents is not None:
            sleep(delay)
        return ents

    cur = workers                  # on trouble: fewer parallel requests, longer pauses; stop only after repeated failures
    bad_rounds = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while True:
            ids = [r[0] for r in c.execute("SELECT DISTINCT wikidata FROM sight WHERE wikidata != '' AND wd_state = 0 LIMIT ?", (batch * cur,))]
            if not ids or (limit and done >= limit):
                break
            chunks = [ids[i:i + batch] for i in range(0, len(ids), batch)]
            results = list(pool.map(work, chunks))
            failed = 0
            for chunk, ents in zip(chunks, results):
                if ents is None:
                    failed += 1
                    continue
                for qid in chunk:
                    e = ents.get(qid) or {}
                    sl = e.get("sitelinks") or {}
                    lab, des = e.get("labels") or {}, e.get("descriptions") or {}
                    c.execute("UPDATE sight SET sitelinks=?, wd_state=1, wp_de=?, wp_en=?, desc_de=?, desc_en=?, label_de=?, label_en=? WHERE wikidata=?",
                              (_count_sitelinks(sl), (sl.get("dewiki") or {}).get("title", ""), (sl.get("enwiki") or {}).get("title", ""),
                               (des.get("de") or {}).get("value", ""), (des.get("en") or {}).get("value", ""),
                               (lab.get("de") or {}).get("value", ""), (lab.get("en") or {}).get("value", ""), qid))
                done += len(chunk)
            c.commit()
            log(f"  {done:,} / {todo:,}")
            if failed:
                errors += failed
                bad_rounds += 1
                if bad_rounds >= 4:
                    log("stopping (Wikidata not reachable / throttled) -- run again later to continue")
                    break
                cur = max(1, cur // 2)
                pause = 60 * bad_rounds
                log(f"  trouble: {cur} parallel request(s) from now on, waiting {pause} s ({bad_rounds}/4)")
                sleep(pause)
            else:
                bad_rounds = 0
                errors = 0
    c.execute("INSERT OR REPLACE INTO meta VALUES ('wikidata_run', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    c.commit()
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    left = c.execute("SELECT COUNT(DISTINCT wikidata) FROM sight WHERE wikidata != '' AND wd_state = 0").fetchone()[0]
    c.close()
    return {"fetched": done, "left": left, "stopped_on_error": bool(errors)}


def _open_text(path: str):
    import gzip
    return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="") if path.endswith(".gz") else open(path, encoding="utf-8", errors="replace", newline="")


def _importance_rows(path: str) -> Iterator[tuple]:
    """(language, title, importance, wikidata) from Nominatim's wikimedia-importance dump: a PostgreSQL `COPY ... FROM stdin`
    block (.sql.gz) or a CSV / TSV (.csv.gz / .tsv.gz, with or without a header line). Columns are found by name, so their order does not matter."""
    import csv
    want = {"language": "language", "title": "title", "importance": "importance", "wikidata": "wikidata", "wikidata_id": "wikidata"}

    def idx(cols):
        m = {}
        for i, c in enumerate(cols):
            k = want.get(c.strip().strip('"').lower())
            if k and k not in m:
                m[k] = i
        return m if "importance" in m and ("wikidata" in m or ("language" in m and "title" in m)) else None

    def row(m, f):
        def g(k):
            i = m.get(k)
            return f[i] if i is not None and i < len(f) and f[i] not in ("\\N", "") else ""
        try:
            return g("language"), g("title"), float(g("importance")), g("wikidata")
        except ValueError:
            return None

    with _open_text(path) as fh:
        first = True
        m = None
        in_copy = False
        for line in fh:
            if in_copy:
                if line.startswith("\\."):
                    in_copy = False
                    continue
                r = row(m, line.rstrip("\r\n").split("\t"))
                if r:
                    yield r
                continue
            if line.startswith("COPY "):
                a, b = line.find("("), line.find(")")
                cm = idx(line[a + 1:b].split(",")) if 0 < a < b else None
                if cm:
                    m, in_copy = cm, True
                continue
            if first:
                first = False
                delim = "\t" if "\t" in line else ","
                cm = idx(line.rstrip("\r\n").split("\t") if delim == "\t" else next(csv.reader([line], delimiter=delim), []))
                if not cm:
                    # no header line: language, title, importance, wikidata (Q...) in that order
                    f = line.rstrip("\r\n").split("\t") if delim == "\t" else next(csv.reader([line], delimiter=delim), [])
                    try:
                        if len(f) >= 4 and 0 <= float(f[2]) <= 1 and f[3].strip().startswith("Q"):
                            cm = {"language": 0, "title": 1, "importance": 2, "wikidata": 3}
                            r = row(cm, f)
                            if r:
                                yield r
                    except ValueError:
                        cm = None
                if cm:                               # CSV / TSV: the rest of the file are rows
                    m = cm
                    if delim == "\t":               # TSV: plain split -- titles may contain quote characters
                        for ln in fh:
                            r = row(m, ln.rstrip("\r\n").split("\t"))
                            if r:
                                yield r
                    else:
                        csv.field_size_limit(1 << 24)
                        for f in csv.reader(fh, delimiter=delim):
                            r = row(m, f)
                            if r:
                                yield r
                    return


def importance(db_path: str, file: str, log: Callable = lambda m: None) -> dict:
    """Wikimedia importance (Nominatim's wikimedia-importance dump, 0..1: how much attention a Wikipedia article gets) per sight,
    matched by Wikidata id or by (language, article title). Needs the database from `build`; best after `wikidata` (adds titles)."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"{db_path} does not exist -- run 'build' first")
    if not os.path.exists(file):
        raise FileNotFoundError(f"{file} not found (GEO_WIKIMEDIA_IMPORTANCE_FILE / GEO_WIKIMEDIA_HOST_DIR)")
    c = rw_connect(db_path)
    if "importance" not in {r[1] for r in c.execute("PRAGMA table_info(sight)")}:
        c.execute("ALTER TABLE sight ADD COLUMN importance REAL DEFAULT 0")
        c.commit()
    by_qid: dict = {}
    by_title: dict = {}
    for qid, wp, wpde, wpen in c.execute("SELECT wikidata, wikipedia, wp_de, wp_en FROM sight"):
        if qid:
            by_qid.setdefault(qid, 0.0)
        for lang, title in (("de", wpde), ("en", wpen), *([tuple(wp.split(":", 1))] if wp and ":" in wp else [])):
            if title:
                by_title.setdefault((lang, title.replace("_", " ")), 0.0)
    log(f"matching {len(by_qid):,} Wikidata ids and {len(by_title):,} article titles against {file}")
    n = hit = 0
    for lang, title, imp, qid in _importance_rows(file):
        n += 1
        if n % 5_000_000 == 0:
            log(f"  {n:,} rows read, {hit:,} matched")
        if qid and qid in by_qid:
            if imp > by_qid[qid]:
                by_qid[qid] = imp
            hit += 1
        elif lang and title:
            k = (lang, title.replace("_", " "))
            if k in by_title:
                if imp > by_title[k]:
                    by_title[k] = imp
                hit += 1
    if n == 0:
        c.close()
        raise ValueError("no importance rows found (expected a PostgreSQL COPY dump or a CSV with an 'importance' column)")
    log(f"{n:,} rows read, {hit:,} matched; writing ...")
    rows = list(c.execute("SELECT id, wikidata, wikipedia, wp_de, wp_en FROM sight"))
    upd = []
    for sid, qid, wp, wpde, wpen in rows:
        best = by_qid.get(qid, 0.0) if qid else 0.0
        for lang, title in (("de", wpde), ("en", wpen), *([tuple(wp.split(":", 1))] if wp and ":" in wp else [])):
            if title:
                best = max(best, by_title.get((lang, title.replace("_", " ")), 0.0))
        upd.append((best, sid))
    c.executemany("UPDATE sight SET importance=? WHERE id=?", upd)
    c.execute("INSERT OR REPLACE INTO meta VALUES ('importance_run', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    c.execute("INSERT OR REPLACE INTO meta VALUES ('importance_src', ?)", (f"{os.path.basename(file)}|{int(os.path.getmtime(file))}",))
    c.commit()
    with_imp = c.execute("SELECT COUNT(*) FROM sight WHERE importance > 0").fetchone()[0]
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    return {"rows": n, "matched": hit, "sights_with_importance": with_imp}


def _user_agent() -> str:
    """Wikimedia asks API clients to name a contact: GEO_SIGHTS_WD_CONTACT (e-mail or URL) is appended when set."""
    c = (os.environ.get("GEO_SIGHTS_WD_CONTACT") or "").strip()
    return f"{USER_AGENT} contact: {c}" if c else USER_AGENT


def _fetch(ids: list, log: Callable, sleep: Callable, maxlag: int = 0) -> Optional[dict]:
    params = {"action": "wbgetentities", "ids": "|".join(ids), "props": "sitelinks|labels|descriptions", "languages": "de|en",
              "format": "json"}
    if maxlag > 0:                       # optional: back off while Wikidata's replication lag is above this many seconds
        params["maxlag"] = str(maxlag)
    why = ""
    for attempt in range(5):
        try:
            r = requests.get(WIKIDATA_API, params=params, headers={"User-Agent": _user_agent()}, timeout=60)
        except requests.RequestException as e:
            why = f"request failed ({type(e).__name__})"
            log(f"  {why}; retry {attempt + 1}/5")
            sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 503) or r.status_code >= 500:
            wait = int(r.headers.get("Retry-After", "0") or 0) or 10 * (attempt + 1)
            why = f"HTTP {r.status_code}"
            log(f"  HTTP {r.status_code}, waiting {wait} s")
            sleep(wait)
            continue
        if r.status_code != 200:
            snippet = ""
            try:
                snippet = (r.text or "")[:160].replace("\n", " ")
            except Exception:                                # noqa: BLE001
                pass
            log(f"  HTTP {r.status_code} from Wikidata -- {snippet or 'no body'} (403 = blocked address / user agent)")
            return None
        try:
            data = r.json()
        except ValueError:
            log("  Wikidata answered 200 but not with JSON (a proxy or block page?)")
            return None
        if isinstance(data, dict) and (data.get("error") or {}).get("code") == "maxlag":
            info = str((data.get("error") or {}).get("info") or "")[:140]
            why = f"maxlag answer from Wikidata ({'we sent maxlag=' + str(maxlag) if maxlag > 0 else 'we sent no maxlag parameter'}): {info}"
            log(f"  {why}; waiting {10 * (attempt + 1)} s")
            sleep(10 * (attempt + 1))
            continue
        return (data or {}).get("entities") or {}
    log(f"  giving up on this batch after 5 attempts: {why}")
    return None


# ----------------------------------------------------------------------------------------------- read side
class SightsIndex:
    """Read side: one read-only connection per thread; a rebuilt file (atomic swap) is picked up automatically."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()

    def _db(self) -> Optional[sqlite3.Connection]:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return None
        loc = self._local
        if getattr(loc, "conn", None) is None or getattr(loc, "mtime", None) != m:
            if getattr(loc, "conn", None) is not None:
                try:
                    loc.conn.close()
                except sqlite3.Error:
                    pass
            loc.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10)
            loc.conn.row_factory = sqlite3.Row
            loc.mtime = m
        return loc.conn

    def available(self) -> bool:
        db = self._db()
        if db is None:
            return False
        try:
            return db.execute("SELECT 1 FROM sight LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def meta(self) -> dict:
        db = self._db()
        try:
            return {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM meta")} if db is not None else {}
        except sqlite3.Error:
            return {}

    def stats(self) -> dict:
        db = self._db()
        if db is None:
            return {"available": False}
        n, wd, done = db.execute("SELECT COUNT(*), SUM(wikidata != ''), SUM(wd_state = 1) FROM sight").fetchone()
        out = {"available": True, "sights": n, "with_wikidata": wd or 0, "wikidata_done": done or 0, **self.meta()}
        if self._has_importance(db):
            out["with_importance"] = db.execute("SELECT COUNT(*) FROM sight WHERE importance > 0").fetchone()[0]
        cols = {r[1] for r in db.execute("PRAGMA table_info(sight)")}
        if "ex_state" in cols:
            out["wikipedia_extracts"] = db.execute("SELECT COUNT(*) FROM sight WHERE ex_de != '' OR ex_en != ''").fetchone()[0]
        if "desc_de_ai" in cols:
            out["descriptions_translated"] = db.execute("SELECT COUNT(*) FROM sight WHERE ai_state = 1").fetchone()[0]
        return out

    @staticmethod
    def _has_importance(db) -> bool:
        return "importance" in {r[1] for r in db.execute("PRAGMA table_info(sight)")}

    def within(self, lat: float, lon: float, radius_m: int, lang: str = "de", limit: int = 3000) -> list:
        """Raw sights inside the radius in the shape sights.search expects; most notable first when more than `limit`."""
        db = self._db()
        if db is None:
            return []
        dlat = radius_m / 111_320
        dlon = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.05))
        order = "s.importance DESC, " if self._has_importance(db) else ""
        rows = db.execute(f"""SELECT s.* FROM sight_rt rt JOIN sight s ON s.id = rt.id
                             WHERE rt.minlat >= ? AND rt.maxlat <= ? AND rt.minlng >= ? AND rt.maxlng <= ?
                             ORDER BY {order}s.sitelinks DESC, (s.wikidata != '') DESC LIMIT ?""",
                          (lat - dlat, lat + dlat, lon - dlon, lon + dlon, limit)).fetchall()
        lg = (lang or "de")[:2].lower()
        out = []
        for r in rows:
            name = (r["name_de"] or r["label_de"] or r["name"]) if lg == "de" else (r["name_en"] or r["label_en"] or r["name"]) if lg == "en" else r["name"]
            wp = ""
            if lg == "de" and r["wp_de"]:
                wp = "de:" + r["wp_de"]
            elif r["wp_en"] and lg != "de":
                wp = "en:" + r["wp_en"]
            elif r["wikipedia"]:
                wp = r["wikipedia"]
            elif r["wp_de"]:
                wp = "de:" + r["wp_de"]
            item = {"name": name, "kind": r["kind"], "category": r["category"], "lat": r["lat"], "lon": r["lng"], "osm": r["osm"],
                    "sitelinks": r["sitelinks"]}
            if "importance" in r.keys() and r["importance"]:
                item["importance"] = round(r["importance"], 4)
            # Wikidata's short description in the asked language first (OSM description tags are often English or long prose)
            wd_lang = r["desc_de"] if lg == "de" else r["desc_en"] if lg == "en" else ""
            ai = r["desc_de_ai"] if lg == "de" and "desc_de_ai" in r.keys() else ""
            desc = wd_lang or ai or r["description"] or r["desc_de"] or r["desc_en"]
            ex = ""
            if "ex_de" in r.keys():
                ex = (r["ex_de"] if lg == "de" else (r["ex_en"] or r["ex_de"]))
            if ex:
                item["summary"] = ex                       # first sentences of the Wikipedia article
            for k, v in (("wikidata", r["wikidata"]), ("wikipedia", wp), ("website", r["website"]), ("opening_hours", r["opening_hours"]),
                         ("description", desc), ("phone", r["phone"])):
                if v:
                    item[k] = v
            out.append(item)
        return out


_INDEXES: dict = {}


def get(path: str) -> SightsIndex:
    if path not in _INDEXES:
        _INDEXES[path] = SightsIndex(path)
    return _INDEXES[path]


# ----------------------------------------------------------------------------------------------- CLI
def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main(argv: Optional[list] = None) -> int:
    from .config import load
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "status"
    s = load()
    db = s.sights_index_path
    env = os.environ.get
    extra_skip = tuple(getattr(s, "poi_skip", ()))
    if cmd in ("build", "all"):
        pbf = env("GEO_OSM_PBF_FILE", "/osm/planet-latest.osm.pbf")
        work = env("GEO_SIGHTS_WORK_DIR") or os.path.join(os.path.dirname(os.path.abspath(db)), "sights-work")
        seq = extract(pbf, work, env("GEO_SIGHTS_OSMIUM_INDEX", "flex_mem"), extra_skip, _log)
        try:
            build(seq, db, extra_skip, _log, source=os.path.basename(pbf), source_path=pbf)
        finally:
            if os.path.exists(seq):
                os.remove(seq)
    imp_file = env("GEO_WIKIMEDIA_IMPORTANCE_FILE", "/wikimedia/wikimedia-importance.sql.gz")
    if cmd == "importance":
        r = importance(db, imp_file, log=_log)
        _log(f"Importance: {r['sights_with_importance']:,} sights have a value ({r['matched']:,} of {r['rows']:,} rows matched)")
        return 0
    dl_dir = env("GEO_SIGHTS_DOWNLOAD_DIR", "/downloads") or "/downloads"
    if cmd in ("download", "refresh", "schedule"):
        from . import sightsrefresh as RF
        if cmd == "schedule":
            rc = RF.schedule(db, dl_dir, os.environ.get, _log)
            if int(env("GEO_SIGHTS_REFRESH_DAYS", "0") or 0) <= 0:      # idle instead of exiting: restart: unless-stopped would loop
                while True:
                    time.sleep(86400)
            return rc
        if cmd == "refresh":
            r = RF.refresh(db, dl_dir, os.environ.get, _log)
            _log("refresh finished: " + ("all steps ok" if r["ok"] else "some steps FAILED (see above and /sights/status)"))
            return 0 if r["ok"] else 1
        bad = 0
        for kind, (env_name, default, _c) in RF.SOURCES.items():
            url = (env(env_name) or default).strip()
            if not url or url.lower() in ("off", "none", "0", "false"):
                continue
            try:
                r = RF.download(kind, url, dl_dir, _log)
                _log(f"{kind}: {r['reason']} {os.path.basename(r['file'] or '')}")
            except Exception as e:                                # noqa: BLE001
                bad += 1
                _log(f"{kind}: FAILED {type(e).__name__}: {e}")
        return 1 if bad else 0
    if cmd == "wikitext":
        from . import sightswikitext as W
        r = W.wikitext(db, langs=tuple(x.strip() for x in (env("GEO_SIGHTS_WP_LANGS", "de,en") or "de,en").split(",") if x.strip()),
                       scope=env("GEO_SIGHTS_WP_SCOPE", "notable") or "notable", limit=int(env("GEO_SIGHTS_WP_MAX", "0") or 0),
                       delay=float(env("GEO_SIGHTS_WP_DELAY", "0.5") or 0.5), workers=int(env("GEO_SIGHTS_WP_WORKERS", "2") or 2),
                       sentences=int(env("GEO_SIGHTS_WP_SENTENCES", "3") or 3), max_chars=int(env("GEO_SIGHTS_WP_MAXCHARS", "400") or 400), log=_log)
        _log(f"Wikipedia: {r['read']:,} articles read, {r['with_text']:,} with text" + (" (stopped: not reachable)" if r["stopped"] else ""))
        return 1 if r["stopped"] else 0
    if cmd == "translate":
        from . import sightstranslate as T
        r = T.translate(db, T.Providers.from_env(), scope=env("GEO_SIGHTS_TR_SCOPE", "notable") or "notable",
                        limit=int(env("GEO_SIGHTS_TR_MAX", "0") or 0), retry=("only" if env("GEO_SIGHTS_TR_RETRY", "0").lower() == "only" else env("GEO_SIGHTS_TR_RETRY", "0") == "1"),
                        sleep_s=float(env("GEO_SIGHTS_TR_SLEEP", "0") or 0), verbose=env("GEO_SIGHTS_TR_LOG", "1") != "0", log=_log,
                        batch=int(env("GEO_SIGHTS_TR_BATCH", "10") or 10), workers=int(env("GEO_SIGHTS_TR_WORKERS", "2") or 2))
        _log(f"Translation: {r['asked']:,} asked, {r['translated']:,} translated, {r['unknown']:,} unknown/rejected, "
             f"{r['kept_as_is']:,} kept (not English), {r['left']:,} left" + (" (stopped: no usable provider)" if r["stopped"] else ""))
        return 1 if r["stopped"] else 0
    if cmd in ("wikidata", "all"):
        r = wikidata(db, delay=float(env("GEO_SIGHTS_WD_DELAY", "1.0") or 1.0),
                     workers=int(env("GEO_SIGHTS_WD_WORKERS", "2") or 2), maxlag=int(env("GEO_SIGHTS_WD_MAXLAG", "0") or 0), log=_log)
        _log(f"Wikidata: {r['fetched']:,} fetched, {r['left']:,} left" + (" (stopped on an error)" if r["stopped_on_error"] else ""))
        if r["stopped_on_error"]:
            return 1
        if cmd == "all" and os.path.exists(imp_file):
            ri = importance(db, imp_file, log=_log)
            _log(f"Importance: {ri['sights_with_importance']:,} sights have a value")
        return 0
    if cmd == "status":
        print(json.dumps(get(db).stats(), indent=2, ensure_ascii=False))
    elif cmd not in ("build", "all", "wikidata", "importance", "translate", "wikitext", "download", "refresh", "schedule"):
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
