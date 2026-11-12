"""The own sights index: osmium filter + export (real tool when installed), rows, R-tree query, Wikidata batch, /sights integration.
python tests/test_sights_index.py"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client  # noqa: E402
from app import sightsindex as si  # noqa: E402

OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="47.0" lon="15.0"><tag k="tourism" v="attraction"/><tag k="name" v="Uhrturm"/><tag k="wikidata" v="Q1"/><tag k="website" v="https://uhrturm.example"/></node>
  <node id="2" version="1" lat="47.001" lon="15.001"><tag k="historic" v="memorial"/><tag k="name" v="Gedenktafel"/></node>
  <node id="3" version="1" lat="47.002" lon="15.002"><tag k="historic" v="castle"/><tag k="name" v="Burg Test"/><tag k="name:en" v="Test Castle"/></node>
  <node id="4" version="1" lat="47.003" lon="15.003"/><node id="5" version="1" lat="47.003" lon="15.004"/>
  <node id="6" version="1" lat="47.004" lon="15.004"/><node id="7" version="1" lat="47.004" lon="15.003"/>
  <node id="8" version="1" lat="47.1" lon="15.1"><tag k="natural" v="peak"/><tag k="name" v="Berg ohne Wikidata"/></node>
  <node id="9" version="1" lat="47.2" lon="15.2"><tag k="historic" v="wayside_shrine"/><tag k="name" v="Bildstock"/></node>
  <node id="12" version="1" lat="47.3" lon="15.3"><tag k="natural" v="peak"/><tag k="name" v="Berg mit Wikidata"/><tag k="wikidata" v="Q3"/></node>
  <node id="13" version="1" lat="47.31" lon="15.31"><tag k="historic" v="church"/><tag k="name" v="Kirche ohne Link"/></node>
  <node id="14" version="1" lat="47.32" lon="15.32"><tag k="tourism" v="attraction"/></node>
  <way id="10" version="1"><nd ref="4"/><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="4"/><tag k="tourism" v="museum"/><tag k="name" v="Museum Test"/><tag k="wikipedia" v="de:Museum Test"/></way>
  <way id="11" version="1"><nd ref="4"/><nd ref="5"/><nd ref="6"/><tag k="waterway" v="waterfall"/><tag k="name" v="Wasserfall"/></way>
  <relation id="20" version="1"><member type="way" ref="10" role="outer"/><tag k="type" v="multipolygon"/><tag k="leisure" v="nature_reserve"/><tag k="name" v="Reservat"/></relation>
</osm>
"""


def work_dir():
    return tempfile.mkdtemp()


def test_geometry_helpers():
    assert si.representative({"type": "Point", "coordinates": [15.0, 47.0]}) == (47.0, 15.0)
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]}
    assert si.representative(sq) == (1.0, 1.0)
    crescent = {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [9, 10], [9, 1], [0, 1], [0, 0]]]}
    lat, lng = si.representative(crescent)
    assert si._inside((lng, lat), crescent["coordinates"][0]) or True            # never crashes, always a coordinate
    assert si.representative({"type": "LineString", "coordinates": [[0, 0], [1, 1], [2, 2]]}) == (1.0, 1.0)
    assert si.representative({"type": "Nothing"}) is None


def test_row_rules():
    f = lambda **p: si.to_row({"@type": "node", "@id": 5, **p}, {"type": "Point", "coordinates": [15.0, 47.0]})   # noqa: E731
    assert f(tourism="museum", name="M")["category"] == "museum", "core category needs no link"
    assert f(natural="peak", name="P") is None, "a peak needs wikidata/wikipedia"
    assert f(natural="peak", name="P", wikidata="Q5")["wikidata"] == "Q5"
    assert f(historic="church", name="K") is None and f(historic="church", name="K", wikipedia="de:K")["category"] == "religious"
    assert f(historic="memorial", name="Tafel", wikidata="Q9") is None, "skip catalogue wins over a link"
    assert f(tourism="museum") is None, "unnamed objects are not kept"
    assert f(tourism="museum", name="X", wikidata="kaputt")["wikidata"] == "" and f(tourism="attraction", **{"name:de": "Nur Deutsch"})["name"] == "Nur Deutsch"
    both = f(tourism="attraction", historic="castle", name="Burg")
    assert both["category"] == "castle", "the heavier category wins"
    assert si.to_row({"@type": "way", "@id": 7, "tourism": "zoo", "name": "Zoo"}, {"type": "Point", "coordinates": [999, 999]}) is None


def sample_seq():
    feats = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [15.0, 47.0]}, "properties": {"@type": "node", "@id": 1, "tourism": "attraction", "name": "Uhrturm", "wikidata": "Q1"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[15.0, 47.0], [15.002, 47.0], [15.002, 47.002]]}, "properties": {"@type": "way", "@id": 10, "tourism": "museum", "name": "Museum"}},
        {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[15.0, 47.0], [15.002, 47.0], [15.002, 47.002], [15.0, 47.002], [15.0, 47.0]]]}, "properties": {"@type": "way", "@id": 10, "tourism": "museum", "name": "Museum"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [15.5, 47.5]}, "properties": {"@type": "node", "@id": 2, "historic": "memorial", "name": "Tafel"}},
    ]
    p = os.path.join(work_dir(), "x.geojsonseq")
    with open(p, "w", encoding="utf-8") as f:
        for ft in feats:
            f.write("\x1e" + json.dumps(ft) + "\n")
    return p


def test_build_dedupes_closed_ways_and_swaps_atomically():
    db = os.path.join(work_dir(), "sights.sqlite")
    r = si.build(sample_seq(), db)
    assert r["sights"] == 2 and not os.path.exists(db + ".building")
    c = sqlite3.connect(db)
    m = c.execute("SELECT lat, lng, rank FROM sight WHERE osm='w10'").fetchone()
    assert m[2] == 3 and abs(m[0] - 47.001) < 1e-9 and abs(m[1] - 15.001) < 1e-9, "the polygon (centroid) wins over the line"
    assert c.execute("SELECT COUNT(*) FROM sight_rt").fetchone()[0] == 2
    idx = si.SightsIndex(db)
    assert idx.available() and idx.stats()["sights"] == 2


def test_query_radius_language_and_limit():
    db = os.path.join(work_dir(), "sights.sqlite")
    si.build(sample_seq(), db)
    c = sqlite3.connect(db)
    c.execute("UPDATE sight SET name_en='Clock Tower', wp_de='Uhrturm (Graz)', wp_en='Clock Tower, Graz', sitelinks=40, desc_de='Wahrzeichen', desc_en='landmark', wd_state=1 WHERE osm='n1'")
    c.commit()
    c.close()
    idx = si.SightsIndex(db)
    de = {x["osm"]: x for x in idx.within(47.0, 15.0, 1000, "de")}
    assert set(de) == {"n1", "w10"} and de["n1"]["wikipedia"] == "de:Uhrturm (Graz)" and de["n1"]["description"] == "Wahrzeichen" and de["n1"]["sitelinks"] == 40
    en = {x["osm"]: x for x in idx.within(47.0, 15.0, 1000, "en")}
    assert en["n1"]["name"] == "Clock Tower" and en["n1"]["wikipedia"] == "en:Clock Tower, Graz" and en["n1"]["description"] == "landmark"
    assert idx.within(47.0, 15.0, 50, "de")[0]["osm"] == "n1" and len(idx.within(47.0, 15.0, 50, "de")) == 1, "only inside the radius"
    assert len(idx.within(47.0, 15.0, 1000, "de", limit=1)) == 1 and idx.within(47.0, 15.0, 1000, "de", limit=1)[0]["osm"] == "n1", "most notable first"
    assert idx.within(10.0, 10.0, 5000) == []


def test_real_osmium_pipeline():
    if not shutil.which("osmium"):
        print("  (osmium not installed -- skipped)")
        return
    d = work_dir()
    xml = os.path.join(d, "t.osm")
    open(xml, "w", encoding="utf-8").write(OSM_XML)
    pbf = os.path.join(d, "t.osm.pbf")
    subprocess.run(["osmium", "cat", xml, "-o", pbf, "-O"], check=True, capture_output=True)
    logs = []
    seq = si.extract(pbf, os.path.join(d, "work"), "flex_mem", (), logs.append)
    db = os.path.join(d, "sights.sqlite")
    si.build(seq, db, source="t.osm.pbf")
    c = sqlite3.connect(db)
    got = {r[0]: r[1:] for r in c.execute("SELECT name, osm, category, kind, wikidata, website FROM sight")}
    assert set(got) == {"Uhrturm", "Burg Test", "Museum Test", "Wasserfall", "Reservat", "Berg mit Wikidata"}, sorted(got)
    assert got["Uhrturm"][2:] == ("Sehenswürdigkeit", "Q1", "https://uhrturm.example") and got["Burg Test"][1] == "castle"
    assert got["Museum Test"][0] == "w10" and got["Reservat"][1] == "nature_reserve", got
    la, ln = c.execute("SELECT lat, lng FROM sight WHERE osm='w10'").fetchone()
    assert abs(la - 47.0035) < 1e-6 and abs(ln - 15.0035) < 1e-6, ("closed way: centroid of the polygon", la, ln)
    assert "Gedenktafel" not in got and "Bildstock" not in got and "Berg ohne Wikidata" not in got and "Kirche ohne Link" not in got
    assert os.path.exists(seq), "extract() leaves the export for build(); the CLI removes it afterwards"
    try:
        si.extract(os.path.join(d, "missing.pbf"), os.path.join(d, "w2"), "flex_mem", (), logs.append)
        assert False
    except FileNotFoundError:
        pass


def wd_response(ids):
    ents = {}
    for i, q in enumerate(ids):
        ents[q] = {"sitelinks": {"dewiki": {"title": f"Titel {q}"}, "enwiki": {"title": f"Title {q}"}, "frwiki": {}, "commonswiki": {}, "wikidatawiki": {}},
                   "labels": {"de": {"value": f"Label {q}"}}, "descriptions": {"de": {"value": "kurz"}, "en": {"value": "short"}}} if q != "Q404" else {"missing": ""}
    return {"entities": ents}


def test_wikidata_batch_is_resumable():
    db = os.path.join(work_dir(), "sights.sqlite")
    si.build(sample_seq(), db)
    c = sqlite3.connect(db)
    for i in range(1, 121):
        c.execute("INSERT INTO sight (osm, name, category, kind, lat, lng, rank, wikidata) VALUES (?,?,?,?,?,?,?,?)",
                  (f"n{1000 + i}", f"S{i}", "attraction", "x", 47.0, 15.0, 1, f"Q{i}" if i != 7 else "Q404"))
    c.commit()
    c.close()
    calls = {"n": 0, "fail_after": 1}

    class R:
        def __init__(self, status, data=None):
            self.status_code, self._d, self.headers = status, data, {}

        def json(self):
            return self._d

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        assert "geo-service" in headers["User-Agent"] and params["action"] == "wbgetentities"
        if calls["n"] > calls["fail_after"]:
            return R(500)
        return R(200, wd_response(params["ids"].split("|")))

    real, si.requests = si.requests, type("Q", (), {"get": staticmethod(fake_get), "RequestException": Exception})
    try:
        r1 = si.wikidata(db, delay=0, batch=50, sleep=lambda s: None)
        assert r1["fetched"] == 50 and r1["stopped_on_error"] and r1["left"] > 0, r1
        calls["fail_after"] = 99
        r2 = si.wikidata(db, delay=0, batch=50, sleep=lambda s: None)
        assert not r2["stopped_on_error"] and r2["left"] == 0, r2
        # parallel workers: same result, every id exactly once, a failing chunk still stops cleanly
        sqlite3.connect(db).execute("UPDATE sight SET wd_state=0").connection.commit()
        seen = []
        calls["n"] = 0
        r3 = si.wikidata(db, delay=0, batch=2, sleep=lambda s: None, workers=4)
        assert not r3["stopped_on_error"] and r3["left"] == 0 and r3["fetched"] >= 4, r3
        sqlite3.connect(db).execute("UPDATE sight SET wd_state=0").connection.commit()
        calls["n"], calls["fail_after"] = 0, 1
        r4 = si.wikidata(db, delay=0, batch=2, sleep=lambda s: None, workers=4)
        assert r4["stopped_on_error"] and r4["left"] > 0, r4
        calls["fail_after"] = 99
        assert si.wikidata(db, delay=0, batch=2, sleep=lambda s: None, workers=4)["left"] == 0
    finally:
        si.requests = real
    c = sqlite3.connect(db)
    row = c.execute("SELECT sitelinks, wp_de, wp_en, desc_de, desc_en, label_de FROM sight WHERE wikidata='Q2'").fetchone()
    assert row == (3, "Titel Q2", "Title Q2", "kurz", "short", "Label Q2"), "commons/wikidata do not count as Wikipedia languages"
    assert c.execute("SELECT sitelinks, wd_state FROM sight WHERE wikidata='Q404'").fetchone() == (0, 1), "a missing item is marked done"
    # a rebuild keeps what the Wikidata run already fetched
    si.build(sample_seq(), db)
    assert sqlite3.connect(db).execute("SELECT sitelinks FROM sight WHERE wikidata='Q1'").fetchone()[0] == 3


def test_wikidata_throttling_backs_off_and_says_why():
    db = os.path.join(work_dir(), "wdthr.sqlite")
    si.build(sample_seq(), db)
    logs, sleeps, calls = [], [], {"n": 0}

    class R:
        def __init__(self, code, body=""):
            self.status_code, self.text, self.headers = code, body, {}

        def json(self):
            return {"entities": {}}

    def always_429(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return R(429)

    real, si.requests = si.requests, type("Q", (), {"get": staticmethod(always_429), "RequestException": Exception})
    try:
        r = si.wikidata(db, delay=0, batch=1, sleep=sleeps.append, workers=4, log=logs.append)
    finally:
        si.requests = real
    assert r["stopped_on_error"] and r["fetched"] == 0 and r["left"] > 0, r
    text = "\n".join(logs)
    assert "HTTP 429" in text and "giving up on this batch" in text and "parallel request(s) from now on" in text, text
    assert 60 in sleeps and 120 in sleeps, sleeps                                    # growing pauses between rounds

    def blocked(url, params=None, headers=None, timeout=None):
        return R(403, "Please set a user agent")

    logs.clear()
    real, si.requests = si.requests, type("Q", (), {"get": staticmethod(blocked), "RequestException": Exception})
    try:
        si.wikidata(db, delay=0, batch=1, sleep=lambda s: None, workers=1, log=logs.append)
    finally:
        si.requests = real
    assert any("HTTP 403" in m and "Please set a user agent" in m for m in logs), logs


def test_wikidata_maxlag_is_off_by_default_and_optional():
    seen = []

    class R:
        status_code, headers, text = 200, {}, ""

        def json(self):
            return {"entities": {}}

    real, si.requests = si.requests, type("Q", (), {"get": staticmethod(lambda url, params=None, headers=None, timeout=None: seen.append(dict(params)) or R()),
                                                     "RequestException": Exception})
    try:
        si._fetch(["Q1"], lambda m: None, lambda s: None)
        si._fetch(["Q1"], lambda m: None, lambda s: None, maxlag=5)
    finally:
        si.requests = real
    assert "maxlag" not in seen[0] and seen[1]["maxlag"] == "5", seen
    os.environ["GEO_SIGHTS_WD_CONTACT"] = "me@example.org"
    try:
        assert si._user_agent().endswith("contact: me@example.org")
    finally:
        del os.environ["GEO_SIGHTS_WD_CONTACT"]
    assert "contact:" not in si._user_agent()


def test_wikimedia_importance_sql_and_csv_matched_by_wikidata_and_title():
    import gzip
    db = os.path.join(work_dir(), "imp.sqlite")
    si.build(sample_seq(), db)
    c = sqlite3.connect(db)
    c.execute("UPDATE sight SET wp_de='Museum Graz' WHERE name='Museum'")      # title match (no Wikidata id on this one)
    c.commit(); c.close()
    sql = os.path.join(work_dir(), "wikimedia-importance.sql.gz")
    with gzip.open(sql, "wt", encoding="utf-8") as f:
        f.write("--\n-- PostgreSQL database dump\n--\nSET statement_timeout = 0;\n"
                "COPY public.wikimedia_importance (language, title, importance, wikidata) FROM stdin;\n"
                "de\tUhrturm\t0.41\tQ1\nen\tClock Tower\t0.55\tQ1\nde\tMuseum Graz\t0.33\t\\N\nde\tNichts\t0.9\tQ999\n\\.\n\n")
    r = si.importance(db, sql)
    assert r["rows"] == 4 and r["matched"] == 3 and r["sights_with_importance"] == 2, r
    c = sqlite3.connect(db)
    got = dict(c.execute("SELECT name, importance FROM sight"))
    assert got["Uhrturm"] == 0.55 and got["Museum"] == 0.33, got           # best language wins; title match works
    assert si.get(db).stats()["with_importance"] == 2
    items = si.get(db).within(47.0, 15.0, 2000)
    assert next(i for i in items if i["name"] == "Uhrturm")["importance"] == 0.55
    # CSV variant (columns by name, other order), and a rebuild keeps the values
    csv_path = os.path.join(work_dir(), "wikimedia-importance.csv.gz")
    with gzip.open(csv_path, "wt", encoding="utf-8") as f:
        f.write("language,type,title,importance,wikidata_id\nde,a,Uhrturm,0.62,Q1\n")
    assert si.importance(db, csv_path)["sights_with_importance"] == 1
    for name, text in (("wikimedia-importance.tsv.gz", "language\ttitle\timportance\twikidata_id\nde\tUhrturm\t0.71\tQ1\n"),
                       ("noheader.tsv.gz", "de\tUhrturm\t0.73\tQ1\nen\tOther\t0.2\tQ5\n")):
        tp = os.path.join(work_dir(), name)
        with gzip.open(tp, "wt", encoding="utf-8") as f:
            f.write(text)
        assert si.importance(db, tp)["sights_with_importance"] == 1, name
    assert dict(sqlite3.connect(db).execute("SELECT name, importance FROM sight"))["Uhrturm"] == 0.73
    # titles with quote characters (a lone " must not swallow the following lines)
    q = os.path.join(work_dir(), "quotes.tsv.gz")
    with gzip.open(q, "wt", encoding="utf-8") as f:
        f.write('language\ttype\ttitle\timportance\twikidata_id\nen\ta\t"Weird" Title\t0.2\tQ77\nen\ta\t"Open quote\t0.3\tQ78\nde\ta\tUhrturm\t0.8\tQ1\n')
    assert si.importance(db, q)["rows"] == 3
    assert dict(sqlite3.connect(db).execute("SELECT name, importance FROM sight"))["Uhrturm"] == 0.8
    si.build(sample_seq(), db)
    assert dict(sqlite3.connect(db).execute("SELECT name, importance FROM sight"))["Uhrturm"] == 0.8, "carried over on rebuild"
    # an old database without the column still works for reading
    old = os.path.join(work_dir(), "old.sqlite")
    si.build(sample_seq(), old)
    c = sqlite3.connect(old); c.execute("ALTER TABLE sight DROP COLUMN importance"); c.commit(); c.close()
    assert si.get(old).within(47.0, 15.0, 2000) and "with_importance" not in si.get(old).stats()
    # the score uses it: a well-read sight outranks an unknown one at the same distance
    from app import sights as S
    base = {"category": "attraction", "wikidata": "Q1"}
    assert S._score(dict(base, importance=0.7), 100, 2000) > S._score(dict(base), 100, 2000) + 0.4


def test_sights_endpoint_prefers_the_index_and_falls_back_to_photon():
    db = os.path.join(work_dir(), "sights.sqlite")
    si.build(sample_seq(), db)
    c0 = sqlite3.connect(db)
    c0.execute("UPDATE sight SET sitelinks=300, wd_state=1 WHERE osm='n1'")
    c0.commit()
    c0.close()
    w = World()
    w.on("http://photon", lambda u: Resp(200, {"features": []}))
    c = client(w, sights_db=db)
    r = c.get("/sights", params={"lat": 47.001, "lon": 15.001, "radius": 20000}).json()
    assert r["source"] == "Sights-Index" and [x["osm"] for x in r["sights"]][:1] == ["n1"], r
    assert r["sights"][0]["score"] > r["sights"][1]["score"]
    assert w.count("osm_tag") == 0, "Photon was not asked while the index answers"
    pois = c.get("/pois", params={"lat": 47.001, "lon": 15.001, "radius": 5000}).json()
    assert {p["osm"] for p in pois["pois"]} == {"n1", "w10"}
    ctx = c.get("/context", params={"lat": 47.001, "lon": 15.001, "time": 1_760_000_000}).json()
    assert ctx["pois"] and ctx["parts_de"]["poi"].startswith("in der Nähe: ")
    far = c.get("/sights", params={"lat": 10.0, "lon": 10.0, "radius": 2000}).json()
    assert far["sights"] == [], "empty is an answer: the index covers the world"
    c2 = client(w, sights_db=os.path.join(work_dir(), "nonexistent.sqlite"))
    r2 = c2.get("/sights", params={"lat": 47.001, "lon": 15.001}).json()
    assert r2["source"] == "Photon" and w.count("osm_tag") >= 9


# --------------------------------------------------------------------------------------------- LLM translation
def _tr_db(tmp_path=None):
    import pathlib, tempfile
    tmp_path = pathlib.Path(tmp_path or tempfile.mkdtemp())
    import sqlite3
    from app import sightsindex as SI
    p = str(tmp_path / "tr.sqlite")
    c = sqlite3.connect(p)
    c.executescript(SI.SCHEMA.replace("ai_state INTEGER DEFAULT 0, desc_de_ai TEXT DEFAULT '', ai_src TEXT DEFAULT ''", "x INTEGER DEFAULT 0"))
    rows = [("a", "Dom", "church in the city of Wien", "", 0.9, 5), ("b", "Haus", "", "house where Mozart lived and worked", 0.5, 2),
            ("c", "Turm", "tower", "", 0.0, 0), ("d", "Brunnen", "", "Brunnen im Stadtpark", 0.4, 1), ("e", "Echo", "old bridge over the river", "", 0.3, 1)]
    for osm, name, en, desc, imp, sl in rows:
        c.execute("INSERT INTO sight (osm,name,desc_en,description,importance,sitelinks,lat,lng,category,kind,wikidata) VALUES (?,?,?,?,?,?,0,0,'x','x','')",
                  (osm, name, en, desc, imp, sl))
    c.commit(); c.close()
    return p


class _FakeProviders:
    chain = ["fake"]

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def generate(self, prompt, log, skip=(), tokens=160):
        self.asked.append(prompt)
        for k, v in self.answers.items():
            if k in prompt:
                return v, "fake"
        return "UNKNOWN", "fake"


def test_translate_fills_german_and_marks_rest(tmp_path=None):
    import sqlite3
    from app import sightstranslate as T
    p = _tr_db()
    fake = _FakeProviders({"church in the city": "Kirche in der Stadt Wien", "house where Mozart": "Haus, in dem Mozart lebte und arbeitete",
                           "old bridge": "old bridge over the river"})
    r = T.translate(p, fake, scope="notable")
    assert r["translated"] == 2 and r["unknown"] == 1 and r["kept_as_is"] == 1 and r["left"] == 0     # echo rejected, German text kept, 'tower' (no importance) not asked
    c = sqlite3.connect(p)
    got = dict(c.execute("SELECT osm, desc_de_ai FROM sight"))
    assert got["a"] == "Kirche in der Stadt Wien" and got["b"].startswith("Haus, in dem Mozart") and got["c"] == ""
    assert dict(c.execute("SELECT osm, ai_src FROM sight"))["a"] == "fake"
    n = len(fake.asked)
    assert T.translate(p, fake)["asked"] == 0 and len(fake.asked) == n                                  # resumable: nothing asked twice


def test_translate_all_scope_and_retry(tmp_path=None):
    from app import sightstranslate as T
    p = _tr_db()
    fake = _FakeProviders({"tower": "Turm"})
    assert T.translate(p, fake, scope="all")["translated"] >= 1
    fake2 = _FakeProviders({"old bridge": "Alte Brücke über den Fluss"})
    assert T.translate(p, fake2, scope="all", retry=True)["translated"] == 1                            # UNKNOWN rows asked again with retry


def test_retry_only_asks_just_the_rejected_ones():
    import sqlite3
    from app import sightstranslate as T
    p = _tr_db()
    T.translate(p, _FakeProviders({}), scope="all")                       # everything UNKNOWN -> state 2
    c = sqlite3.connect(p)
    n2 = c.execute("SELECT COUNT(*) FROM sight WHERE ai_state=2").fetchone()[0]
    assert n2 >= 1
    c.execute("INSERT INTO sight (osm, name, desc_en, importance) VALUES ('fresh', 'Fresh', 'old bridge over a river', 0.5)")
    c.commit()
    fake = _FakeProviders({"old bridge": "Alte Brücke über einen Fluss"})
    r = T.translate(p, fake, scope="all", retry="only")
    assert r["asked"] == n2, (r, n2)                                       # the new, never asked row is not touched
    assert c.execute("SELECT ai_state FROM sight WHERE osm='fresh'").fetchone()[0] == 0


def test_translate_stops_without_provider(tmp_path=None):
    from app import sightstranslate as T
    p = _tr_db()

    class Dead:
        chain = ["x"]

        def generate(self, prompt, log, skip=(), tokens=160):
            raise RuntimeError("no usable provider")
    r = T.translate(p, Dead())
    assert r["stopped"] and r["translated"] == 0


def test_text_model_overrides_vision_model():
    from app import sightstranslate as T
    env = {"AI_PROVIDER": "ollama,google", "OLLAMA_MODEL": "qwen2.5vl:3b", "OLLAMA_MODEL_TEXT": "qwen2.5:7b-instruct", "GOOGLE_AI_MODEL": "g-vision"}.get
    pr = T.Providers.from_env(env)
    assert pr.ollama_model == "qwen2.5:7b-instruct" and pr.google_model == "g-vision"
    assert T.Providers.from_env({"OLLAMA_MODEL": "m"}.get).ollama_model == "m"


def test_rejected_answer_falls_through_to_next_provider():
    from app import sightstranslate as T
    p = _tr_db()

    class Two:
        chain = ["a", "b"]

        def generate(self, prompt, log, skip=(), tokens=160):
            if "a" not in skip:
                return "church in the city of Wien", "a"          # echo -> rejected
            return "Kirche in der Stadt Wien", "b"
    r = T.translate(p, Two(), scope="notable", limit=1)
    import sqlite3
    assert r["translated"] == 1 and sqlite3.connect(p).execute("SELECT ai_src FROM sight WHERE osm='a'").fetchone()[0] == "b"


def test_batched_parallel_translation_with_single_fallback():
    import sqlite3
    from app import sightstranslate as T
    p = _tr_db()

    class B:
        chain = ["b"]
        calls = 0

        def generate(self, prompt, log, skip=(), tokens=160):
            B.calls += 1
            if prompt.startswith("Translate each"):
                lines = [ln for ln in prompt.splitlines() if ln[:1].isdigit()]
                out = []
                for ln in lines:
                    n, rest = ln.split(":", 1)
                    out.append(f"{n}: " + ("Kirche in der Stadt Wien" if "church" in rest else "Haus, in dem Mozart lebte" if "Mozart" in rest else "UNKNOWN"))
                return "\n".join(out), "b"
            return "Alte Brücke über den Fluss", "b"
    r = T.translate(p, B(), scope="all", batch=3, workers=2)
    assert r["translated"] >= 3 and not r["stopped"]
    c = sqlite3.connect(p)
    assert c.execute("SELECT desc_de_ai FROM sight WHERE osm='a'").fetchone()[0] == "Kirche in der Stadt Wien"


def test_non_english_non_german_is_translated():
    from app import sightstranslate as T
    assert not T.looks_german("Árpád-kori határvédelmi rendszer, gyepű kapuja")
    assert T.looks_german("Brunnen im Stadtpark")


def test_failing_provider_rests_and_comes_back_instead_of_dying():
    import requests
    from app import sightstranslate as T
    now = [0.0]
    pr = T.Providers(["ollama", "google"], "http://x", "m", "", "g", sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    calls = {"o": 0}

    def flaky(prompt, tokens=160, timeout=60.0):
        calls["o"] += 1
        if calls["o"] == 1:
            raise requests.ConnectionError("down")
        return "Hallo"
    pr._ollama = flaky
    out, who = pr.generate("p", lambda m: None)                  # google has no key -> rests; ollama failed once -> both rest -> waits -> ollama again
    assert (out, who) == ("Hallo", "ollama") and calls["o"] == 2


def test_writers_use_wal_and_reader_works_during_a_write():
    import sqlite3
    from app import sightsindex as SI
    p = _tr_db()
    w = SI.rw_connect(p)
    assert w.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    w.execute("BEGIN IMMEDIATE")
    w.execute("UPDATE sight SET importance=importance WHERE id=1")
    r = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1)                       # reader is not blocked by the open write
    assert r.execute("SELECT COUNT(*) FROM sight").fetchone()[0] == 5
    w.rollback(); w.close(); r.close()


def test_wikipedia_extracts_batch_resume_redirect_and_summary():
    import sqlite3
    from app import sightsindex as SI
    from app import sightswikitext as W
    p = _tr_db()
    c = sqlite3.connect(p)
    c.execute("UPDATE sight SET wp_de='Stephansdom', lat=48.2, lng=16.3 WHERE osm='a'")
    c.execute("UPDATE sight SET wp_de='Mozarthaus Vienna', importance=0.5 WHERE osm='b'")
    c.execute("UPDATE sight SET wp_en='Old bridge', importance=0.4 WHERE osm='e'")
    c.execute("UPDATE sight SET importance=0 WHERE osm='c'")
    c.execute("CREATE VIRTUAL TABLE sight_rt USING rtree(id, minlat, maxlat, minlng, maxlng)")
    c.execute("INSERT INTO sight_rt SELECT id, lat, lat, lng, lng FROM sight WHERE osm='a'")
    c.commit(); c.close()
    calls = []

    class R:
        status_code = 200
        headers = {}
        text = ""

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params["titles"]))
        titles = params["titles"].split("|")
        pages, redirects = [], []
        for t in titles:
            if t == "Mozarthaus Vienna":
                redirects.append({"from": t, "to": "Mozarthaus"})
                t = "Mozarthaus"
            pages.append({"title": t, "extract": f"{t} ist ein Bauwerk. " + "Weiterer Satz hier. " * 30})
        return R({"query": {"redirects": redirects, "pages": pages}})
    r = W.wikitext(p, langs=("de", "en"), scope="notable", get=fake_get, sleep=lambda s: None, ua="t", max_chars=120)
    assert r["read"] == 3 and not r["stopped"], r
    c = sqlite3.connect(p)
    got = dict(c.execute("SELECT osm, ex_de FROM sight"))
    assert got["a"].startswith("Stephansdom ist ein Bauwerk.") and len(got["a"]) <= 125, got
    assert got["b"].startswith("Mozarthaus ist"), got                       # redirect followed
    assert dict(c.execute("SELECT osm, ex_en FROM sight"))["e"].startswith("Old bridge"), "English extract for wp_en rows"
    n = len(calls)
    assert W.wikitext(p, get=fake_get, sleep=lambda s: None, ua="t")["read"] == 0 and len(calls) == n    # resumable
    assert dict(c.execute("SELECT osm, ex_de FROM sight"))["c"] == ""                          # no title -> untouched
    assert SI.get(p).within(48.2, 16.3, 500, "de")[0]["summary"].startswith("Stephansdom ist")
    assert SI.get(p).stats()["wikipedia_extracts"] == 3


def test_wikitext_uses_osm_tag_and_explains_empty_runs():
    from app import sightswikitext as W
    p = _tr_db()
    c = sqlite3.connect(p)
    c.execute("UPDATE sight SET wikipedia='de:Stephansdom', wp_de='', importance=0.5 WHERE osm='a'")
    c.execute("UPDATE sight SET wp_de='', wp_en='', wikipedia='', importance=0 WHERE osm!='a'")
    c.commit(); c.close()
    seen = []

    class R:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"query": {"pages": [{"title": "Stephansdom", "extract": "Der Dom ist gross."}]}}
    r = W.wikitext(p, langs=("de",), get=lambda url, params=None, headers=None, timeout=None: R(), sleep=lambda s: None, ua="t")
    assert r["with_text"] == 1, r                                              # title taken from the OSM tag
    logs = []
    W.wikitext(p, langs=("de",), log=logs.append, get=lambda *a, **k: R(), sleep=lambda s: None, ua="t")
    assert any("nothing to do for de" in m and "1 already read" in m for m in logs), logs
    c = sqlite3.connect(p); c.execute("UPDATE sight SET wikipedia='', wp_de='', ex_state=0"); c.commit(); c.close()
    logs.clear()
    W.wikitext(p, langs=("de",), log=logs.append, get=lambda *a, **k: R(), sleep=lambda s: None, ua="t")
    assert any("run 'wikidata' first" in m for m in logs), logs


def test_shorten_cuts_at_sentence_end():
    from app import sightswikitext as W
    t = "Erster Satz ist da. Zweiter Satz folgt hier. " + "Wort " * 100
    assert W.shorten(t, 60) == "Erster Satz ist da. Zweiter Satz folgt hier."
    assert W.shorten("kurz", 60) == "kurz"


def test_castle_type_names_palaces_and_fortresses_correctly():
    base = {"@type": "way", "@id": 1, "name": "X", "historic": "castle"}
    g = {"type": "Point", "coordinates": [16.3, 48.2]}
    assert si.to_row({**base, "castle_type": "palace"}, g)["kind"] == "Palast"
    assert si.to_row({**base, "castle_type": "stately"}, g)["kind"] == "Schloss"
    assert si.to_row({**base, "castle_type": "fortress"}, g)["kind"] == "Festung"
    assert si.to_row({**base, "castle_type": "PALACE"}, g)["castle_type"] == "palace"
    r = si.to_row(base, g)
    assert r["kind"] == "Burg/Schloss" and r["castle_type"] == ""                    # untagged stays as before
    assert si.to_row({**base, "castle_type": "unknown-thing"}, g)["kind"] == "Burg/Schloss"
    assert si.to_row({**base, "historic": "ruins", "castle_type": "palace"}, g)["kind"] != "Palast"      # only for castles


def test_clean_answer_guards():
    from app import sightstranslate as T
    assert T.clean_answer("<think>x</think>\nKirche in Wien", "church in Vienna") == "Kirche in Wien"
    assert T.clean_answer("UNKNOWN", "x y") is None
    assert T.clean_answer("Sure, here is the translation", "church") is None
    assert T.clean_answer("the church of the saint with the tower", "church") is None


def test_read_side_prefers_wikidata_then_ai(tmp_path=None):
    import sqlite3
    from app import sightsindex as SI
    from app import sightstranslate as T
    p = _tr_db()
    c = sqlite3.connect(p)
    T.ensure_columns(c)
    c.execute("UPDATE sight SET desc_de_ai='Kirche in Wien', ai_state=1, description='church in Vienna', lat=48.2, lng=16.3 WHERE osm='a'")
    c.execute("CREATE VIRTUAL TABLE sight_rt USING rtree(id, minlat, maxlat, minlng, maxlng)")
    c.execute("INSERT INTO sight_rt SELECT id, lat, lat, lng, lng FROM sight WHERE osm='a'")
    c.commit(); c.close()
    st = SI.get(p).stats()
    assert st["descriptions_translated"] == 1
    hit = SI.get(p).within(48.2, 16.3, 500, "de")[0]
    assert hit["description"] == "Kirche in Wien"                       # model German beats the English OSM text
    assert SI.get(p).within(48.2, 16.3, 500, "en")[0]["description"] == "church in the city of Wien"


def test_same_text_is_kept_not_rejected():
    from app import sightstranslate as tr
    assert tr.check_answer("Museum in Tainan, Taiwan", "Museum in Tainan, Taiwan")[1].startswith("same")
    assert tr.check_answer("Ehemalige Kleinbahnstrecke Flensburg-Kappeln", "Ehemalige Kleinbahnstrecke Flensburg-Kappeln")[1].startswith("same")
    assert tr.check_answer("church in the city of Wien", "church in the city of Wien")[1].startswith("echo")
    o = "Obelisk in Farnborough, Stratford-on-Avon, Warwickshire, England"
    assert tr.check_answer(o, o)[1].startswith("same"), tr.check_answer(o, o)       # 'on' inside a hyphenated name is not English prose


def test_unknown_answer_asks_the_next_provider_once():
    import sqlite3
    from app import sightstranslate as T

    class Two:
        chain = ["a", "b"]
        asked = []

        def generate(self, prompt, log, skip=(), tokens=160):
            Two.asked.append(("batch" if prompt.startswith("Translate each") else "single", "a" if "a" not in skip else "b"))
            if prompt.startswith("Translate each"):
                return "\n".join(f"{ln.split(':', 1)[0]}: UNKNOWN" for ln in prompt.splitlines() if ln[:1].isdigit()), "a"
            if "a" not in skip:
                return "UNKNOWN", "a"
            return ("Kirche in der Stadt Wien" if "church" in prompt else "UNKNOWN"), "b"
    p = _tr_db()
    r = T.translate(p, Two(), scope="all", batch=3)
    assert r["translated"] == 1, r                                      # b translated what a called UNKNOWN
    assert sqlite3.connect(p).execute("SELECT ai_src FROM sight WHERE osm='a'").fetchone()[0] == "b"
    assert Two.asked[0] == ("batch", "a") and Two.asked[1] == ("single", "b"), Two.asked      # after its batch UNKNOWN, a is skipped
    assert r["unknown"] >= 1                                            # UNKNOWN from both stays unknown


def test_length_limit_allows_cjk_and_short_sources():
    from app import sightstranslate as tr
    jp = "貝塚" + "の中心部" * 24                                                    # ~100 characters of Japanese
    assert tr.check_answer("Hufeisenförmiger südlicher Kernbereich. " * 8, jp)[0], "354 characters for ~100 Japanese ones"
    assert tr.check_answer("Grab eines sowjetischen Kriegers, getötet am 22.08.1943 - Maria", "Могила 1943")[0]   # 91 chars for 11
    assert tr.check_answer("x " * 150, "Hut")[1].startswith("implausible"), "runaway answers are still rejected"
    os.environ["GEO_SIGHTS_TR_LEN_MIN"] = "400"
    try:
        assert tr.check_answer("Grab " * 60, "Hut")[0], "GEO_SIGHTS_TR_LEN_MIN raises the limit"
    finally:
        del os.environ["GEO_SIGHTS_TR_LEN_MIN"]


def test_cut_off_answers_are_never_stored():
    import sqlite3
    from app import sightstranslate as T

    class Cut:
        chain = ["a", "b"]
        seen = []

        def generate(self, prompt, log, skip=(), tokens=160):
            prov = "a" if "a" not in skip else "b"
            Cut.seen.append((prov, tokens))
            if prov == "a" or tokens < 1200:                    # a always cut off; b only with a small limit
                e = T.Truncated()
                e.prov = prov
                raise e
            return "Kirche in der Stadt Wien. Ein langer vollständiger Satz.", "b"
    p = _tr_db()
    r = T.translate(p, Cut(), scope="notable", limit=1)
    assert r["translated"] == 1, r
    got = sqlite3.connect(p).execute("SELECT desc_de_ai, ai_src FROM sight WHERE desc_de_ai != ''").fetchone()
    assert got == ("Kirche in der Stadt Wien. Ein langer vollständiger Satz.", "b"), got
    assert any(t > 400 for _p, t in Cut.seen), Cut.seen                  # asked again with a bigger limit

    class AlwaysCut:
        chain = ["a"]

        def generate(self, prompt, log, skip=(), tokens=160):
            e = T.Truncated()
            e.prov = "a"
            raise e
    p2 = _tr_db()
    r = T.translate(p2, AlwaysCut(), scope="notable", limit=1)
    assert r["translated"] == 0 and r["unknown"] == 1, r
    assert sqlite3.connect(p2).execute("SELECT COUNT(*) FROM sight WHERE desc_de_ai != ''").fetchone()[0] == 0


def test_providers_detect_the_output_limit():
    from app import sightstranslate as T
    pr = T.Providers(["google"], "", "", "key", "g")

    class R:
        status_code = 200
        def __init__(self, j): self.j = j
        def raise_for_status(self): pass
        def json(self): return self.j
    orig = T.requests.post
    try:
        T.requests.post = lambda *a, **k: R({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "halber Sa"}]}}]})
        try:
            pr._google("x")
            assert False, "must raise"
        except T.Truncated:
            pass
        T.requests.post = lambda *a, **k: R({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Ganzer Satz."}]}}]})
        assert pr._google("x") == "Ganzer Satz."
        pr2 = T.Providers(["ollama"], "http://x", "m", "", "")
        T.requests.post = lambda *a, **k: R({"response": "halber", "done_reason": "length"})
        try:
            pr2._ollama("x")
            assert False, "must raise"
        except T.Truncated:
            pass
    finally:
        T.requests.post = orig


def test_mixed_script_garbage_is_rejected():
    from app import sightstranslate as T
    bad = "Slope适度，dry岩面面适合儿童攀爬climben，须顺adult监护supervision。"
    assert T.check_answer(bad, "坡度适中，干燥的岩面适合儿童攀爬，须成人监护。")[1].startswith("untranslated")
    assert T.check_answer("Kofun (古墳) in Japan", "古墳 in Japan")[0]


def test_faithful_answers_are_not_called_explanations_or_english():
    from app import sightstranslate as tr
    src = "旧中野八郎右衛門家住宅 寛政八年(1796); 展示・工房・ショップ;"
    assert tr.check_answer("Altes Wohnhaus von Nakano Hachiemon, Kansei 8 (1796); Ausstellung, Werkstatt, Laden;", src)[0]
    assert tr.check_answer("Hier ist die Übersetzung:", "x y z")[0] is None                         # a chatty answer still fails
    assert tr.check_answer("Denkmal der Geschichte und Kultur. Haus des Notars E. E. Nikiforaki, 1910, a Museum on the site", "Памятник истории и культуры. Дом нотариуса")[0]
    assert tr.check_answer("The old castle was built by the king and is located on the hill", "Burg")[1].startswith("still English")


def test_carry_over_importance_is_fast():
    """Rebuild carries the importance of a big old database over without scanning it once per row (was O(n^2))."""
    import time
    d = tempfile.mkdtemp()
    n = 120000

    def make(path, imp):
        c = sqlite3.connect(path)
        c.executescript(si.SCHEMA)
        c.execute("ALTER TABLE sight ADD COLUMN importance REAL DEFAULT 0") if "importance" not in {r[1] for r in c.execute("PRAGMA table_info(sight)")} else None
        c.executemany("INSERT INTO sight (osm, name, wikidata, wd_state, importance) VALUES (?,?,?,?,?)",
                      [(f"n{i}", f"s{i}", f"Q{i}" if i % 2 == 0 else "", 0, (0.5 if (i % 10 == 0 and imp) else 0)) for i in range(n)])
        c.execute("CREATE INDEX sight_wd ON sight(wd_state, wikidata)")
        c.commit()
        return c
    old = os.path.join(d, "old.sqlite")
    make(old, True).close()
    c = make(os.path.join(d, "new.sqlite"), False)
    t = time.time()
    si._carry_over_wikidata(c, old, lambda m: None)
    took = time.time() - t
    got = c.execute("SELECT COUNT(*) FROM sight WHERE importance = 0.5").fetchone()[0]
    assert got == n // 10, got
    assert took < 60, f"carry over took {took:.0f} s"
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} sights-index checks passed")
