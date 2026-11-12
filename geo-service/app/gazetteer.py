"""gazetteer.py -- your own GeoNames database (SQLite) instead of asking Pelias.

Built from the GeoNames dumps in the GeoNames volume (GEONAMES_HOST_DIR -> /geonames, read-only):

  allCountries(.txt|.zip)        every place on earth          -> table place (+ R*Tree place_rt for "what is near here")
  alternateNamesV2(.txt|.zip)    names in other languages      -> table alt + the full-text index (Wien = Vienna = Vídeň)
  admin1CodesASCII.txt           names of states               -> table admin  (key AT.03)
  admin2Codes.txt                names of districts            -> table admin  (key AT.03.307)
  hierarchy(.txt|.zip)           parent/child of admin units   -> table hierarchy  (exact childrenJSON)
  postal_allCountries(.txt|.zip) postal codes (export/zip)     -> table postal (+ R*Tree)
  featureCodes_en.txt            names of fcode                -> table fcode
  countryInfo.txt                (read by geonames_ws.Countries)

Only the first file is required; whatever is missing is simply not available (the endpoints then use their old
sources). The database is written next to the cache (GN_GAZETTEER_DB, default <data dir>/gazetteer.sqlite) in a
temporary file and swapped in when complete, so a half-finished import never replaces a working database.

  docker compose exec geo-service python -m app.gazetteer            # build / rebuild
  docker compose exec geo-service python -m app.gazetteer --status

Real geonames.org ids, real feature class/code, real population -- unlike the guesses made from OSM/Pelias data.
GeoNames data is CC-BY 4.0 (attribution: https://www.geonames.org).
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
import sys
import threading
import time
import zipfile
from typing import Callable, Iterator, Optional

csv.field_size_limit(1 << 24)

SCHEMA_VERSION = "1"
BATCH = 20000

PLACE_COLS = ("id", "name", "ascii", "lat", "lng", "fcl", "fcode", "cc", "a1", "a2", "a3", "a4", "pop", "elev", "tz")
_SKIP_ALT_LANGS = {"link", "wkdt", "post", "iata", "icao", "faac", "fr_1793", "unlc", "nuts", "geoid", "uic", "tcid", "lauc", "gnis", "ncbi"}


# ------------------------------------------------------------------ reading the dump files
def _find(dir_: str, names: list) -> Optional[str]:
    for n in names:
        p = os.path.join(dir_, n)
        if os.path.isfile(p):
            return p
    return None


def open_text(path: str, member_hint: str = "") -> io.TextIOBase:
    """Text stream of a dump: plain .txt, or the .txt inside a .zip (readme.txt is skipped)."""
    if path.lower().endswith(".zip"):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".txt") and "readme" not in n.lower()]
        pick = next((n for n in names if member_hint and member_hint.lower() in n.lower()), names[0] if names else None)
        if pick is None:
            raise ValueError(f"{path}: no .txt inside")
        return io.TextIOWrapper(zf.open(pick), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def rows(path: str, member_hint: str = "") -> Iterator[list]:
    with open_text(path, member_hint) as fh:
        for r in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
            if r and not r[0].startswith("#"):
                yield r


def sources(dir_: str) -> dict:
    return {
        "places": _find(dir_, ["allCountries.txt", "allCountries.zip"]),
        "alt": _find(dir_, ["alternateNamesV2.txt", "alternateNamesV2.zip", "alternateNames.txt", "alternateNames.zip"]),
        "admin1": _find(dir_, ["admin1CodesASCII.txt"]),
        "admin2": _find(dir_, ["admin2Codes.txt"]),
        "hierarchy": _find(dir_, ["hierarchy.txt", "hierarchy.zip"]),
        "postal": _find(dir_, ["postal_allCountries.txt", "postal_allCountries.zip", "postalCodes.txt", "postalCodes.zip"]),
        "fcodes": _find(dir_, ["featureCodes_en.txt"]),
    }


# ------------------------------------------------------------------ building
def _num(v, cast=int, default=0):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return default


def _fts_text(*parts) -> str:
    return " ".join(p for p in parts if p)


def build(dir_: str, db_path: str, langs=("de", "en"), fts_classes: str = "", log: Callable[[str], None] = print) -> dict:
    """Builds db_path from the dumps in dir_. Returns {table: rows}. Raises if allCountries is missing."""
    src = sources(dir_)
    if not src["places"]:
        raise FileNotFoundError(f"{dir_}: allCountries.txt (or .zip) not found")
    tmp = db_path + ".building"
    for p in (tmp, tmp + "-journal", tmp + "-wal"):
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    t0 = time.time()
    c = sqlite3.connect(tmp)
    c.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-400000;
        CREATE TABLE place (id INTEGER PRIMARY KEY, name TEXT, ascii TEXT, lat REAL, lng REAL, fcl TEXT, fcode TEXT, cc TEXT,
                            a1 TEXT, a2 TEXT, a3 TEXT, a4 TEXT, pop INTEGER, elev INTEGER, tz TEXT);
        CREATE VIRTUAL TABLE place_rt USING rtree(id, minlat, maxlat, minlng, maxlng);
        CREATE TABLE alt (id INTEGER, lang TEXT, name TEXT, pref INTEGER, short INTEGER);
        CREATE TABLE admin (code TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE hierarchy (parent INTEGER, child INTEGER, type TEXT);
        CREATE TABLE fcode (code TEXT PRIMARY KEY, name TEXT, descr TEXT);
        CREATE TABLE postal (id INTEGER PRIMARY KEY, cc TEXT, code TEXT, place TEXT, a1n TEXT, a1c TEXT, a2n TEXT, a2c TEXT,
                             a3n TEXT, a3c TEXT, lat REAL, lng REAL);
        CREATE VIRTUAL TABLE postal_rt USING rtree(id, minlat, maxlat, minlng, maxlng);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    counts = {}

    def flush(sql, buf):
        if buf:
            c.executemany(sql, buf)
            buf.clear()

    # --- places
    log(f"places: {src['places']}")
    buf, rt, n = [], [], 0
    for r in rows(src["places"], "allcountries"):
        if len(r) < 18:
            continue
        pid = _num(r[0])
        lat, lng = _num(r[4], float, 0.0), _num(r[5], float, 0.0)
        buf.append((pid, r[1], r[2], lat, lng, r[6], r[7], r[8], r[10], r[11], r[12], r[13], _num(r[14]), _num(r[15], int, None), r[17]))
        rt.append((pid, lat, lat, lng, lng))
        n += 1
        if len(buf) >= BATCH:
            flush("INSERT INTO place VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            flush("INSERT INTO place_rt VALUES (?,?,?,?,?)", rt)
            if n % 1000000 < BATCH:
                log(f"  {n:,} places")
    flush("INSERT INTO place VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
    flush("INSERT INTO place_rt VALUES (?,?,?,?,?)", rt)
    counts["place"] = n
    c.execute("CREATE INDEX place_cc_fcode ON place(cc, fcode)")
    c.execute("CREATE INDEX place_admin ON place(cc, a1, a2, fcode)")
    c.execute("CREATE INDEX place_pop ON place(pop)")
    log(f"  {n:,} places in {time.time() - t0:.0f}s")

    # --- alternate names (only the wanted languages; no historic names)
    want = {l.strip().lower() for l in langs if l.strip()}
    if src["alt"] and want:
        log(f"alternate names ({','.join(sorted(want))}): {src['alt']}")
        buf, n = [], 0
        for r in rows(src["alt"], "alternatenames"):
            if len(r) < 4:
                continue
            lang = r[2].lower()
            if lang not in want or (len(r) > 7 and r[7] == "1"):
                continue
            buf.append((_num(r[1]), lang, r[3], 1 if len(r) > 4 and r[4] == "1" else 0, 1 if len(r) > 5 and r[5] == "1" else 0))
            n += 1
            if len(buf) >= BATCH:
                flush("INSERT INTO alt VALUES (?,?,?,?,?)", buf)
        flush("INSERT INTO alt VALUES (?,?,?,?,?)", buf)
        c.execute("CREATE INDEX alt_id ON alt(id, lang)")
        counts["alt"] = n
        log(f"  {n:,} alternate names")

    # --- full-text index: name, ascii name, alternate names of the wanted languages
    classes = {x.strip().upper() for x in fts_classes.split(",") if x.strip()}
    log("full-text index ...")
    c.execute("CREATE VIRTUAL TABLE place_fts USING fts5(names, tokenize='unicode61 remove_diacritics 2', prefix='3')")
    where = f"WHERE p.fcl IN ({','.join('?' * len(classes))})" if classes else ""
    c.execute(f"""INSERT INTO place_fts(rowid, names)
                  SELECT p.id, p.name || ' ' || p.ascii || ' ' || COALESCE((SELECT group_concat(a.name, ' ') FROM alt a WHERE a.id = p.id), '')
                  FROM place p {where}""", sorted(classes))
    counts["fts"] = c.execute("SELECT count(*) FROM place_fts").fetchone()[0]

    # --- admin names, feature codes
    for key in ("admin1", "admin2"):
        if src[key]:
            buf = [(r[0], r[1]) for r in rows(src[key]) if len(r) >= 2]
            c.executemany("INSERT OR REPLACE INTO admin VALUES (?,?)", buf)
            counts[key] = len(buf)
    if src["fcodes"]:
        buf = [(r[0], r[1], r[2] if len(r) > 2 else "") for r in rows(src["fcodes"]) if len(r) >= 2 and r[0] != "null"]
        c.executemany("INSERT OR REPLACE INTO fcode VALUES (?,?,?)", buf)
        counts["fcode"] = len(buf)

    # --- hierarchy
    if src["hierarchy"]:
        buf = [(_num(r[0]), _num(r[1]), r[2] if len(r) > 2 else "") for r in rows(src["hierarchy"], "hierarchy") if len(r) >= 2]
        c.executemany("INSERT INTO hierarchy VALUES (?,?,?)", buf)
        c.execute("CREATE INDEX hier_parent ON hierarchy(parent)")
        c.execute("CREATE INDEX hier_child ON hierarchy(child)")
        counts["hierarchy"] = len(buf)

    # --- postal codes
    if src["postal"]:
        log(f"postal codes: {src['postal']}")
        buf, rt, n = [], [], 0
        for r in rows(src["postal"], "allcountries"):
            if len(r) < 11:
                continue
            n += 1
            lat, lng = _num(r[9], float, 0.0), _num(r[10], float, 0.0)
            buf.append((n, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], lat, lng))
            rt.append((n, lat, lat, lng, lng))
            if len(buf) >= BATCH:
                flush("INSERT INTO postal VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", buf)
                flush("INSERT INTO postal_rt VALUES (?,?,?,?,?)", rt)
        flush("INSERT INTO postal VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", buf)
        flush("INSERT INTO postal_rt VALUES (?,?,?,?,?)", rt)
        c.execute("CREATE INDEX postal_code ON postal(cc, code)")
        c.execute("CREATE INDEX postal_place ON postal(place COLLATE NOCASE)")
        counts["postal"] = n

    meta = {"schema": SCHEMA_VERSION, "built": str(int(time.time())), "langs": ",".join(sorted(want)),
            **{f"src_{k}": os.path.basename(v) for k, v in src.items() if v}, **{f"n_{k}": str(v) for k, v in counts.items()}}
    c.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
    c.commit()
    c.execute("ANALYZE")
    c.commit()
    c.close()
    os.replace(tmp, db_path)
    log(f"done in {time.time() - t0:.0f}s: {counts}")
    return counts


# ------------------------------------------------------------------ querying
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


def _bbox(lat, lng, km):
    dlat = km / 111.32
    dlng = km / (111.32 * max(math.cos(math.radians(lat)), 0.05))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def fts_query(term: str, prefix: bool) -> str:
    toks = re.findall(r"\w+", term or "", flags=re.UNICODE)
    if not toks:
        return ""
    parts = [f'"{t}"' for t in toks]
    if prefix:
        parts[-1] += "*"
    return " ".join(parts)


class Gazetteer:
    """Read side. Opens the database lazily and notices a rebuilt file (the import swaps it in atomically)."""

    def __init__(self, path: str, country_name: Optional[Callable[[str], str]] = None):
        self.path = path
        self._country_name = country_name          # countryInfo.txt names ("Austria"); else the country's own record
        self._cn: dict = {}
        self._local = threading.local()            # one connection per thread: a shared sqlite3 connection is not safe
        self._mtime = 0.0                          # for concurrent cursors (rows got mixed up under load)
        self._lock = threading.Lock()
        self._fcodes: dict = {}
        self._admin: dict = {}

    # -- connection
    def _db(self) -> Optional[sqlite3.Connection]:
        try:
            m = os.path.getmtime(self.path)
        except OSError:
            return None
        loc = self._local
        conn = getattr(loc, "conn", None)
        if conn is None or getattr(loc, "mtime", None) != m:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            loc.conn, loc.mtime = conn, m
            with self._lock:
                if m != self._mtime:               # the file was rebuilt: forget what was cached from the old one
                    self._mtime = m
                    self._fcodes, self._admin, self._cn = {}, {}, {}
        return conn

    def available(self) -> bool:
        db = self._db()
        if db is None:
            return False
        try:
            return db.execute("SELECT count(*) FROM place LIMIT 1").fetchone()[0] > 0
        except sqlite3.Error:
            return False

    def meta(self) -> dict:
        db = self._db()
        if db is None:
            return {}
        try:
            return {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM meta")}
        except sqlite3.Error:
            return {}

    def has(self, table: str) -> bool:
        return bool(self.meta().get(f"n_{table}"))

    # -- records
    def country_name(self, cc: str) -> str:
        n = self._country_name(cc) if self._country_name else ""
        if n:
            return n
        if cc not in self._cn:
            db = self._db()
            r = db.execute("SELECT name FROM place INDEXED BY place_cc_fcode WHERE cc=? AND fcode IN ('PCLI','PCLD','PCL','PCLF','PCLS') ORDER BY pop DESC LIMIT 1", (cc,)).fetchone() if db is not None else None
            self._cn[cc] = r["name"] if r else ""
        return self._cn[cc]

    def country_local(self, cc: str, lang: str) -> str:
        db = self._db()
        r = db.execute("SELECT id FROM place INDEXED BY place_cc_fcode WHERE cc=? AND fcode IN ('PCLI','PCLD','PCL','PCLF','PCLS') ORDER BY pop DESC LIMIT 1", (cc,)).fetchone() if db is not None else None
        return (self.localized([r["id"]], lang).get(r["id"]) if r else "") or self.country_name(cc)

    def _fcode_name(self, fcl, fcode) -> str:
        db = self._db()
        if not self._fcodes and db is not None:
            try:
                self._fcodes = {r["code"]: r["name"] for r in db.execute("SELECT code, name FROM fcode")}
            except sqlite3.Error:
                self._fcodes = {}
        return self._fcodes.get(f"{fcl}.{fcode}", "")

    def _admin_name(self, key: str) -> str:
        if key not in self._admin:
            db = self._db()
            r = db.execute("SELECT name FROM admin WHERE code=?", (key,)).fetchone() if db is not None else None
            self._admin[key] = r["name"] if r else ""
        return self._admin[key]

    def localized(self, ids: list, lang: str) -> dict:
        """{id: name} in `lang` (preferred, non-short first) for the ids that have one."""
        db = self._db()
        if not ids or not lang or db is None:
            return {}
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in db.execute(f"SELECT id, name FROM alt WHERE lang=? AND id IN ({q}) ORDER BY pref ASC, short DESC", [lang.lower()[:2], *chunk]):
                out[r["id"]] = r["name"]            # last write wins = preferred (pref=1 sorts last), non-short
        return out

    def record(self, r: sqlite3.Row, lang: Optional[str] = None, names: Optional[dict] = None) -> dict:
        a1k = f"{r['cc']}.{r['a1']}" if r["a1"] else ""
        a2k = f"{a1k}.{r['a2']}" if a1k and r["a2"] else ""
        name = (names or {}).get(r["id"]) or r["name"]
        return {"geonameId": r["id"], "name": name, "toponymName": r["name"],
                "lat": f"{r['lat']:.5f}", "lng": f"{r['lng']:.5f}", "countryCode": r["cc"], "countryName": self.country_name(r["cc"]),
                "fcl": r["fcl"], "fcode": r["fcode"], "fclName": "", "fcodeName": self._fcode_name(r["fcl"], r["fcode"]),
                "population": r["pop"] or 0, "adminCode1": r["a1"] or "", "adminName1": self._admin_name(a1k) if a1k else "",
                "adminCode2": r["a2"] or "", "adminName2": self._admin_name(a2k) if a2k else ""}

    def _records(self, rows_: list, lang: Optional[str], dist: Optional[tuple] = None) -> list:
        names = self.localized([r["id"] for r in rows_], lang) if lang else {}
        out = []
        for r in rows_:
            rec = self.record(r, lang, names)
            if dist:
                rec["distance"] = round(haversine_km(dist[0], dist[1], r["lat"], r["lng"]), 5)
            out.append(rec)
        return out

    def get(self, geoname_id: int, lang: Optional[str] = None) -> Optional[dict]:
        db = self._db()
        if db is None:
            return None
        r = db.execute("SELECT * FROM place WHERE id=?", (geoname_id,)).fetchone()
        return self._records([r], lang)[0] if r else None

    # -- near a point
    def nearby(self, lat, lng, limit=10, radius_km=None, fclasses=None, fcodes=None, min_pop=0, lang=None) -> list:
        """Nearest places, nearest first. radius_km=None widens the circle (2, 5, 12, 25, 50, 100, 200, 400, 700, 1000 km) until `limit` are found."""
        db = self._db()
        if db is None:
            return []
        ladder = [radius_km] if radius_km else [2, 5, 12, 25, 50, 100, 200, 400, 700, 1000]     # small steps: the first box with hits is never much bigger than needed
        cond, args = [], []
        if fclasses:
            cond.append(f"p.fcl IN ({','.join('?' * len(fclasses))})")
            args += [c.upper() for c in fclasses]
        if fcodes:
            cond.append(f"p.fcode IN ({','.join('?' * len(fcodes))})")
            args += [c.upper() for c in fcodes]
        if min_pop:
            cond.append("p.pop >= ?")
            args.append(min_pop)
        extra = (" AND " + " AND ".join(cond)) if cond else ""
        best: list = []
        cos = max(math.cos(math.radians(lat)), 0.05)
        keep = max(limit * 4, 20)                      # SQLite sorts by the (flat) distance in C; Python only sees a handful of rows
        for km in ladder:
            s, n, w, e = _bbox(lat, lng, km)
            got = db.execute(f"""SELECT p.* FROM place_rt rt JOIN place p ON p.id = rt.id
                                 WHERE rt.minlat >= ? AND rt.maxlat <= ? AND rt.minlng >= ? AND rt.maxlng <= ?{extra}
                                 ORDER BY (p.lat - ?) * (p.lat - ?) + ((p.lng - ?) * ?) * ((p.lng - ?) * ?) LIMIT ?""",
                             [s, n, w, e, *args, lat, lat, lng, cos, lng, cos, keep]).fetchall()
            best = sorted(((haversine_km(lat, lng, r["lat"], r["lng"]), r) for r in got), key=lambda x: x[0])
            best = [x for x in best if x[0] <= km]
            if len(best) >= limit:
                break
        return self._records([r for _, r in best[:limit]], lang, (lat, lng))

    # -- search
    def search(self, term: str, limit=10, offset=0, countries=None, fclasses=None, fcodes=None, startswith="", equals="",
               bbox=None, min_pop=0, order_pop=False, lang=None) -> tuple:
        """(records, total). total is counted up to 10 000. Whole words first; the last word is tried as a prefix when
        nothing matched (or when startswith is given)."""
        db = self._db()
        if db is None:
            return [], 0
        cond, args = [], []
        if countries:
            cond.append(f"p.cc IN ({','.join('?' * len(countries))})")
            args += [c.upper() for c in countries]
        if fclasses:
            cond.append(f"p.fcl IN ({','.join('?' * len(fclasses))})")
            args += [c.upper() for c in fclasses]
        if fcodes:
            cond.append(f"p.fcode IN ({','.join('?' * len(fcodes))})")
            args += [c.upper() for c in fcodes]
        if min_pop:
            cond.append("p.pop >= ?")
            args.append(min_pop)
        if bbox:
            w, s, e, n = bbox
            cond.append("p.lat BETWEEN ? AND ? AND p.lng BETWEEN ? AND ?")
            args += [s, n, w, e]
        if startswith:
            cond.append("(lower(p.name) LIKE ? OR lower(p.ascii) LIKE ? OR p.id IN (SELECT id FROM alt WHERE lower(name) LIKE ?))")
            args += [startswith.lower() + "%"] * 3
        if equals:
            cond.append("(lower(p.name) = ? OR lower(p.ascii) = ? OR p.id IN (SELECT id FROM alt WHERE lower(name) = ?))")
            args += [equals.lower()] * 3
        extra = (" AND " + " AND ".join(cond)) if cond else ""
        order = "p.pop DESC" if order_pop else "(lower(p.name) = ?) DESC, (lower(p.ascii) = ?) DESC, p.pop DESC"
        for prefix in ((True,) if startswith else (False, True)):
            q = fts_query(term, prefix)
            if not q:
                return [], 0
            try:
                total = db.execute(f"SELECT count(*) FROM (SELECT 1 FROM place_fts f JOIN place p ON p.id = f.rowid "
                                   f"WHERE place_fts MATCH ?{extra} LIMIT 10000)", [q, *args]).fetchone()[0]
                if not total:
                    continue
                oargs = [] if order_pop else [term.lower(), term.lower()]
                got = db.execute(f"SELECT p.* FROM place_fts f JOIN place p ON p.id = f.rowid WHERE place_fts MATCH ?{extra} "
                                 f"ORDER BY {order} LIMIT ? OFFSET ?", [q, *args, *oargs, limit, offset]).fetchall()
            except sqlite3.Error:
                return [], 0
            return self._records(got, lang), total
        return [], 0

    # -- hierarchy
    def parents(self, geoname_id: int) -> list:
        """Ancestors, top first (country > ADM1 > ADM2 > ...), from hierarchy.txt, else rebuilt from the admin codes."""
        db = self._db()
        p = self.get(geoname_id)
        if db is None or p is None:
            return []
        chain, seen, cur = [], {geoname_id}, geoname_id
        if self.has("hierarchy"):
            for _ in range(10):
                r = db.execute("SELECT parent FROM hierarchy WHERE child=? ORDER BY (type='ADM') DESC LIMIT 1", (cur,)).fetchone()
                if not r or r["parent"] in seen:
                    break
                seen.add(r["parent"])
                cur = r["parent"]
                chain.append(cur)
            chain.reverse()
        else:
            raw = db.execute("SELECT * FROM place WHERE id=?", (geoname_id,)).fetchone()
            ids = []
            c = db.execute("SELECT id FROM place WHERE cc=? AND fcode LIKE 'PCL%' AND fcode != 'PCLH' ORDER BY (fcode='PCLI') DESC LIMIT 1", (raw["cc"],)).fetchone()
            if c:
                ids.append(c["id"])
            for fcode, cols in (("ADM1", ("a1",)), ("ADM2", ("a1", "a2")), ("ADM3", ("a1", "a2", "a3")), ("ADM4", ("a1", "a2", "a3", "a4"))):
                if any(not raw[k] for k in cols):
                    break
                r = db.execute(f"SELECT id FROM place WHERE cc=? AND fcode=? AND {' AND '.join(k + '=?' for k in cols)} LIMIT 1",
                               [raw["cc"], fcode, *[raw[k] for k in cols]]).fetchone()
                if r:
                    ids.append(r["id"])
            chain = [i for i in ids if i != geoname_id]
        out = []
        for i in chain:
            rec = self.get(i)
            if rec:
                out.append(rec)
        return out

    def children(self, geoname_id: int, limit=200, lang=None) -> tuple:
        """(records, total) administrative children."""
        db = self._db()
        if db is None:
            return [], 0
        if self.has("hierarchy"):
            got = db.execute("SELECT p.* FROM hierarchy h JOIN place p ON p.id = h.child WHERE h.parent = ? ORDER BY p.name COLLATE NOCASE", (geoname_id,)).fetchall()
        else:
            raw = db.execute("SELECT * FROM place WHERE id=?", (geoname_id,)).fetchone()
            if raw is None:
                return [], 0
            if raw["fcode"].startswith("PCL"):
                got = db.execute("SELECT * FROM place WHERE cc=? AND fcode='ADM1' ORDER BY name COLLATE NOCASE", (raw["cc"],)).fetchall()
            else:
                nxt = {"ADM1": ("ADM2", ("a1",)), "ADM2": ("ADM3", ("a1", "a2")), "ADM3": ("ADM4", ("a1", "a2", "a3"))}.get(raw["fcode"])
                if not nxt:
                    return [], 0
                got = db.execute(f"SELECT * FROM place WHERE cc=? AND fcode=? AND {' AND '.join(k + '=?' for k in nxt[1])} ORDER BY name COLLATE NOCASE",
                                 [raw["cc"], nxt[0], *[raw[k] for k in nxt[1]]]).fetchall()
        return self._records(got[:limit], lang), len(got)

    # -- postal codes
    def _postal_rec(self, r: sqlite3.Row, dist: Optional[float] = None) -> dict:
        rec = {"code": r["code"], "placeName": r["place"], "countryCode": r["cc"], "lat": f"{r['lat']:.5f}", "lng": f"{r['lng']:.5f}",
               "adminCode1": r["a1c"] or "", "adminName1": r["a1n"] or "", "adminCode2": r["a2c"] or "", "adminName2": r["a2n"] or "",
               "adminCode3": r["a3c"] or "", "adminName3": r["a3n"] or ""}
        if dist is not None:
            rec["distance"] = round(dist, 5)
        return rec

    def postal_near(self, lat, lng, radius_km=10.0, limit=5) -> list:
        db = self._db()
        if db is None or not self.has("postal"):
            return []
        s, n, w, e = _bbox(lat, lng, radius_km)
        got = db.execute("SELECT p.* FROM postal_rt rt JOIN postal p ON p.id = rt.id "
                         "WHERE rt.minlat >= ? AND rt.maxlat <= ? AND rt.minlng >= ? AND rt.maxlng <= ? LIMIT 5000", (s, n, w, e)).fetchall()
        best = sorted(((haversine_km(lat, lng, r["lat"], r["lng"]), r) for r in got), key=lambda x: x[0])
        return [self._postal_rec(r, d) for d, r in best if d <= radius_km][:limit]

    def postal_search(self, code="", place="", country="", limit=10) -> list:
        """code = prefix match; place = name prefix (case-insensitive)."""
        db = self._db()
        if db is None or not self.has("postal"):
            return []
        cond, args = [], []
        if code:
            cond.append("code LIKE ?")
            args.append(code + "%")
        if place:
            cond.append("place LIKE ? COLLATE NOCASE")
            args.append(place + "%")
        if country:
            cond.append("cc = ?")
            args.append(country.upper())
        if not cond:
            return []
        got = db.execute(f"SELECT * FROM postal WHERE {' AND '.join(cond)} ORDER BY code, place LIMIT ?", [*args, limit]).fetchall()
        return [self._postal_rec(r) for r in got]


# ------------------------------------------------------------------ command line
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from .config import load
    s = load()
    dump_dir = os.environ.get("GN_DUMP_DIR") or os.path.dirname(s.gn_country_file) or "/geonames"
    db_path = s.gn_gazetteer_db or os.path.join(os.path.dirname(s.cache_db) or ".", "gazetteer.sqlite")
    if "--status" in argv:
        g = Gazetteer(db_path)
        print(f"database: {db_path}  available={g.available()}")
        for k, v in sorted(g.meta().items()):
            print(f"  {k} = {v}")
        for k, v in sources(dump_dir).items():
            print(f"  dump {k}: {v or '-'}")
        return 0
    try:
        build(dump_dir, db_path, s.gn_gazetteer_langs, s.gn_gazetteer_fts_classes)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())


_INSTANCES: dict = {}


def get(path: str, country_name: Optional[Callable[[str], str]] = None) -> Gazetteer:
    """One Gazetteer per database file (shared by the GeoNames endpoints and the reverse chain)."""
    g = _INSTANCES.get(path)
    if g is None:
        g = _INSTANCES[path] = Gazetteer(path, country_name)
    elif country_name is not None and g._country_name is None:
        g._country_name = country_name
    return g
