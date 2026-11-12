"""Automatic refresh of the sights index: downloads (unchanged / new / resume / invalid), keep newest + last used,
pipeline steps, schedule, previous database kept.  python tests/test_sights_refresh.py"""
import gzip
import io
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import sightsindex as SI  # noqa: E402
from app import sightsrefresh as RF  # noqa: E402


class FakeHttp:
    """requests-like: head/get for ONE url; `body`, `etag` can be changed between calls."""
    def __init__(self, body: bytes, etag="v1"):
        self.body, self.etag, self.gets, self.ranges = body, etag, 0, []

    def head(self, url, **kw):
        return type("R", (), {"status_code": 200, "headers": {"ETag": self.etag, "Content-Length": str(len(self.body)), "Last-Modified": "x"}})()

    def get(self, url, stream=True, headers=None, **kw):
        self.gets += 1
        rng = (headers or {}).get("Range", "")
        self.ranges.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        data = self.body[start:]
        outer = self

        class R:
            status_code = 206 if start else 200

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def iter_content(s, n):
                for i in range(0, len(data), 4):
                    yield data[i:i + 4]
        return R()


def gz(text: str) -> bytes:
    b = io.BytesIO()
    with gzip.GzipFile(fileobj=b, mode="wb") as f:
        f.write(text.encode())
    return b.getvalue()


URL = "https://example.org/data/wikimedia-importance.csv.gz"


def files(d):
    return sorted(f for f in os.listdir(d) if f.startswith("wikimedia"))


def test_download_unchanged_new_and_keep_newest_plus_last_used():
    d = tempfile.mkdtemp()
    h = FakeHttp(gz("a,b\n1,2\n"), "v1")
    r = RF.download("importance", URL, d, http=h, now=datetime(2026, 1, 1))
    assert r["changed"] and files(d) == ["wikimedia-importance.2026-01-01.csv.gz"], files(d)
    assert not RF.download("importance", URL, d, http=h, now=datetime(2026, 1, 2))["changed"] and h.gets == 1      # same ETag: nothing to do
    RF.mark_used(d, "importance", r["file"])                                                                     # v1 was used successfully
    h.body, h.etag = gz("a,b\n3,4\n"), "v2"
    r2 = RF.download("importance", URL, d, http=h, now=datetime(2026, 2, 1))
    assert files(d) == ["wikimedia-importance.2026-01-01.csv.gz", "wikimedia-importance.2026-02-01.csv.gz"], files(d)   # newest + last used
    RF.mark_used(d, "importance", r2["file"])
    h.body, h.etag = gz("a,b\n5,6\n"), "v3"
    RF.download("importance", URL, d, http=h, now=datetime(2026, 3, 1))
    assert files(d) == ["wikimedia-importance.2026-02-01.csv.gz", "wikimedia-importance.2026-03-01.csv.gz"], files(d)   # v1 gone, v2 (used) + v3
    # v3 is never used, v4 arrives: v4 + v2 (the last one known to work) stay, v3 goes
    h.body, h.etag = gz("a,b\n7,8\n"), "v4"
    RF.download("importance", URL, d, http=h, now=datetime(2026, 4, 1))
    assert files(d) == ["wikimedia-importance.2026-02-01.csv.gz", "wikimedia-importance.2026-04-01.csv.gz"], files(d)
    assert RF.latest(d, "importance").endswith("2026-04-01.csv.gz")


def test_invalid_and_incomplete_downloads_leave_everything_as_it_was():
    d = tempfile.mkdtemp()
    h = FakeHttp(b"this is not gzip at all", "v1")
    try:
        RF.download("importance", URL, d, http=h, now=datetime(2026, 1, 1))
        raise AssertionError("invalid gzip accepted")
    except RuntimeError as e:
        assert "not valid" in str(e)
    assert files(d) == [] and RF.latest(d, "importance") is None
    pbf = FakeHttp(b"\x00\x00\x00\rXXXX", "p1")
    try:
        RF.download("pbf", "https://example.org/planet-latest.osm.pbf", d, http=pbf, now=datetime(2026, 1, 1))
        raise AssertionError("not a pbf accepted")
    except RuntimeError as e:
        assert "OSMHeader" in str(e)
    ok = FakeHttp(b"\x00\x00\x00\x0eOSMHeader....", "p1")
    assert RF.download("pbf", "https://example.org/planet-latest.osm.pbf", d, http=ok, now=datetime(2026, 1, 1))["changed"]


def test_interrupted_download_resumes_with_a_range_request():
    d = tempfile.mkdtemp()
    body = gz("a,b\n" + "".join(f"{i},{i * 7919 % 10007}\n" for i in range(400)))
    h = FakeHttp(body, "v1")
    final = os.path.join(d, "wikimedia-importance.2026-01-01.csv.gz")
    with open(final + ".part", "wb") as fh:
        fh.write(body[:40])
    with open(final + ".part.json", "w") as fh:
        fh.write('{"etag": "v1", "last_modified": "x"}')
    r = RF.download("importance", URL, d, http=h, now=datetime(2026, 1, 1))
    assert r["changed"] and h.ranges == ["bytes=40-"] and open(final, "rb").read() == body


def test_next_due():
    assert RF.next_due(None, 30, "03:30", datetime(2026, 1, 1, 1, 0)) == datetime(2026, 1, 1, 3, 30)
    assert RF.next_due(None, 30, "03:30", datetime(2026, 1, 1, 5, 0)) == datetime(2026, 1, 2, 3, 30)
    assert RF.next_due(datetime(2026, 1, 1, 4, 10), 30, "03:30") == datetime(2026, 1, 31, 3, 30)


def make_db(path, source_mtime="1"):
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.executescript(SI.SCHEMA)
    c.execute("INSERT INTO meta VALUES ('source_mtime', ?)", (source_mtime,))
    c.execute("INSERT INTO meta VALUES ('built', '2026-01-01T00:00:00')")
    c.commit()
    c.close()


def test_refresh_pipeline_steps_and_failure_isolation():
    d, dl = tempfile.mkdtemp(), tempfile.mkdtemp()
    db = os.path.join(d, "sights.sqlite")
    make_db(db, "1")
    pbf = os.path.join(d, "planet.osm.pbf")
    open(pbf, "wb").write(b"x")
    os.utime(pbf, (1000, 1000))
    calls = []
    SI_extract, SI_build, SI_wd, SI_imp = SI.extract, SI.build, SI.wikidata, SI.importance
    SI.extract = lambda *a, **k: (calls.append("extract"), os.path.join(d, "seq"))[1]
    SI.build = lambda *a, **k: (calls.append("build"), make_db(db, "1000"))[0]
    SI.wikidata = lambda *a, **k: (calls.append("wikidata"), (_ for _ in ()).throw(RuntimeError("Wikidata down")))[0]
    SI.importance = lambda *a, **k: (calls.append("importance"), {"sights_with_importance": 5})[1]
    env = {"GEO_OSM_PBF_FILE": pbf, "GEO_SIGHTS_IMPORTANCE_URL": "off", "GEO_SIGHTS_REFRESH_DAYS": "30", "GEO_SIGHTS_REFRESH_TRANSLATE": "0",
           "GEO_SIGHTS_REFRESH_WIKITEXT": "0", "GEO_WIKIMEDIA_IMPORTANCE_FILE": os.path.join(d, "imp.csv.gz")}
    open(env["GEO_WIKIMEDIA_IMPORTANCE_FILE"], "wb").write(gz("a\n"))
    try:
        r = RF.refresh(db, dl, env.get, lambda m: None)
        names = {s["name"]: s["status"] for s in r["steps"]}
        assert names["build"] == "ok" and names["wikidata"] == "error" and names["importance"] == "ok" and not r["ok"], r["steps"]   # a failing step does not stop the next ones
        assert calls == ["extract", "build", "wikidata", "importance"], calls
        assert r["next_due"] and SI.read_status(db)["steps"], "status file written"
        calls.clear()
        r2 = RF.refresh(db, dl, env.get, lambda m: None)                       # PBF unchanged -> no build
        assert {s["name"]: s["status"] for s in r2["steps"]}["build"] == "skipped" and "build" not in calls, r2["steps"]
    finally:
        SI.extract, SI.build, SI.wikidata, SI.importance = SI_extract, SI_build, SI_wd, SI_imp


def test_schedule_runs_when_due_and_waits_otherwise():
    d = tempfile.mkdtemp()
    db = os.path.join(d, "sights.sqlite")
    ran, slept = [], []
    clock = [datetime(2026, 1, 1, 5, 0)]
    env = {"GEO_SIGHTS_REFRESH_DAYS": "30", "GEO_SIGHTS_REFRESH_AT": "03:30"}

    def runner(db_path, dl, e, log):
        ran.append(clock[0])
        SI.write_status(db_path, {"finished": clock[0].strftime("%Y-%m-%dT%H:%M:%S")})
    clock_fn = lambda: clock[0]

    def sleep(s):
        slept.append(s)
        clock[0] = clock[0] + __import__("datetime").timedelta(seconds=s)
    RF.schedule(db, d, env.get, lambda m: None, sleep, clock_fn, max_runs=1, runner=runner)
    assert ran and ran[0] >= datetime(2026, 1, 2, 3, 30) and slept, (ran, slept)       # first run at the next 03:30
    assert RF.schedule(db, d, {"GEO_SIGHTS_REFRESH_DAYS": "0"}.get, lambda m: None) == 0   # 0 = off


def test_build_keeps_the_previous_database():
    from test_sights_index import sample_seq  # noqa: E402
    d = tempfile.mkdtemp()
    db = os.path.join(d, "sights.sqlite")
    SI.build(sample_seq(), db)
    assert not os.path.exists(db + ".prev")
    SI.build(sample_seq(), db)
    assert os.path.exists(db + ".prev") and os.path.getsize(db + ".prev") > 0


def test_self_built_importance_file_wins_when_newer():
    d, w = tempfile.mkdtemp(), tempfile.mkdtemp()
    h = FakeHttp(gz("a,b\n1,2\n"), "v1")
    r = RF.download("importance", URL, d, http=h, now=datetime(2026, 1, 1))
    cfg = os.path.join(w, "wikimedia-importance.sql.gz")                                  # configured name, not there
    assert RF.importance_source(d, cfg) == os.path.join(d, r["file"])                    # only the download exists
    built = os.path.join(w, "wikimedia_importance.tsv.gz")
    open(built, "wb").write(gz("language\ttype\ttitle\timportance\twikidata_id\n"))
    os.utime(os.path.join(d, r["file"]), (1000, 1000))
    assert RF.importance_source(d, cfg) == built                                          # self-built file is newer
    os.utime(built, (500, 500))
    assert RF.importance_source(d, cfg) == os.path.join(d, r["file"])                    # an older self-built file loses
    assert RF.importance_source(tempfile.mkdtemp(), "") is None
    assert RF.source_stamp(built) == "wikimedia_importance.tsv.gz|500"


def test_newest_mirror_is_chosen():
    A, B = "https://a.example/imp.tsv.gz", "https://b.example/imp.csv.gz"

    class H:
        def __init__(self, table):
            self.table = table

        def head(self, url, **kw):
            v = self.table[url]
            if isinstance(v, Exception):
                raise v
            code, lm = v
            return type("R", (), {"status_code": code, "headers": {"Last-Modified": lm} if lm else {}})()
    both = {A: (200, "Fri, 17 Oct 2026 19:12:00 GMT"), B: (200, "Sat, 04 Oct 2025 10:00:00 GMT")}
    assert RF.pick_url(f"{A},{B}", http=H(both)) == A                                   # newest Last-Modified wins
    assert RF.pick_url(f"{B} {A}", http=H(both)) == A                                   # order does not matter
    assert RF.pick_url(f"{A},{B}", http=H({A: (404, ""), B: both[B]})) == B             # a broken mirror is skipped
    assert RF.pick_url(f"{A},{B}", http=H({A: ConnectionError("x"), B: both[B]})) == B
    assert RF.pick_url(f"{A},{B}", http=H({A: (404, ""), B: (503, "")})) == A           # none answers: first, download() reports it
    assert RF.pick_url(f"{A},{B}", http=H({A: (200, ""), B: (200, "garbage")})) in (A, B)
    assert RF.pick_url(A) == A and RF.pick_url("") == ""
    assert "opencagedata" in RF.IMPORTANCE_URLS and "nominatim.org" in RF.IMPORTANCE_URLS


def test_month_folders_are_probed_backwards():
    from datetime import datetime
    T = "https://x.example/public/%YYYYMM%/imp.tsv.gz"
    seen = []

    class H:
        def __init__(self, have):
            self.have = have

        def head(self, url, **kw):
            seen.append(url)
            return type("R", (), {"status_code": 200 if any(h in url for h in self.have) else 404, "headers": {}})()
    now = datetime(2026, 11, 15)
    assert RF.expand_months(T, http=H(["202610"]), now=now) == "https://x.example/public/202610/imp.tsv.gz"     # Nov missing -> Oct
    assert seen == ["https://x.example/public/202611/imp.tsv.gz", "https://x.example/public/202610/imp.tsv.gz"], seen
    assert RF.expand_months(T, http=H(["202606"]), now=now).endswith("/202606/imp.tsv.gz")                      # gaps of several months
    assert RF.expand_months(T, http=H(["202512"]), now=datetime(2026, 2, 1)).endswith("/202512/imp.tsv.gz")      # year change
    assert RF.expand_months(T, http=H([]), now=now, max_back=3) is None
    assert RF.expand_months("https://x.example/plain.gz", http=H([]), now=now) == "https://x.example/plain.gz"   # no placeholder: untouched

    class Down:
        def head(self, *a, **k):
            raise ConnectionError("down")
    assert RF.expand_months(T, http=Down(), now=now) is None
    # inside pick_url: the template mirror and a plain one compete on Last-Modified
    class Both:
        def head(self, url, **kw):
            lm = "Fri, 17 Oct 2026 19:12:00 GMT" if "x.example" in url else "Sat, 04 Oct 2025 10:00:00 GMT"
            return type("R", (), {"status_code": 200, "headers": {"Last-Modified": lm}})()
    assert RF.pick_url(f"https://y.example/old.csv.gz,{T}", http=Both(), now=now) == "https://x.example/public/202611/imp.tsv.gz"
    assert "%YYYYMM%" in RF.IMPORTANCE_URLS and "{" not in RF.IMPORTANCE_URLS                                    # no braces: compose ${VAR:-default} would cut at '}'


def test_wrong_kind_of_address_is_ignored():
    logs = []
    spec = "https://o.example/public/%YYYYMM%/wikimedia_importance.tsv.gz, https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    assert RF.drop_wrong_kind("importance", spec, logs.append) == "https://o.example/public/%YYYYMM%/wikimedia_importance.tsv.gz"
    assert any("planet-latest.osm.pbf" in m and "not a 'importance'" in m for m in logs), logs
    assert RF.drop_wrong_kind("importance", "https://planet.example/x.osm.pbf") == ""
    assert RF.drop_wrong_kind("pbf", "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf") == "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    assert RF.drop_wrong_kind("pbf", "https://o.example/imp.csv.gz") == ""


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("PASS", name)
    print(f"\n{n} refresh checks passed")
