"""Weather shadow archive: backfill from tracks + client points.   python tests/test_weather_backfill.py"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client, gpx  # noqa: E402

DAY0 = 1_759_968_000            # 2025-10-09 00:00 UTC
BASE = DAY0 + 12 * 3600


def _traccar(points):
    return lambda u: Resp(200, content=gpx(points), headers={"content-type": "application/gpx+xml"})


def _hourly(u):
    day = u.split("start_date=")[1][:10]
    return Resp(200, {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(24)], "weather_code": [3] * 24,
                                 "temperature_2m": [9.0] * 24, "precipitation": [0.0] * 24, "cloud_cover": [90] * 24}})


def wait(c, ok=("done", "capped", "rate_limited", "cancelled", "error")):
    for _ in range(300):
        j = c.get("/weather/backfill").json()
        if j["status"] in ok:
            return j
        time.sleep(0.05)
    raise AssertionError("backfill did not finish")


def test_off_by_default():
    c = client(World())
    assert c.post("/weather/backfill", json={"points": [[BASE, 47.0, 15.0]]}).status_code == 409


def test_points_and_tracks_then_context_offline():
    w = World()
    w.on("archive-api.open-meteo.com", _hourly)
    # track: Graz on day 1, Vienna on day 2 (two cells), plus a second point in the SAME cell/day
    w.on("http://traccar/api/positions/gpx", _traccar([(BASE, 47.07, 15.43), (BASE + 600, 47.071, 15.431),
                                                      (BASE + 86400, 48.21, 16.37)]))
    c = client(w, weather_provider="open-meteo", weather_backfill_delay=0.0)
    # a photo at a third place on day 1 (client sends it as a point)
    r = c.post("/weather/backfill", json={"points": [[BASE + 100, 46.62, 14.31]], "from": DAY0, "to": DAY0 + 2 * 86400 - 1})
    assert r.status_code == 200, r.text
    j = wait(c)
    assert j["status"] == "done" and j["needed_cell_days"] == 3 and j["counts"]["fetched"] == 3, j
    assert w.count("open-meteo") == 3
    st = c.get("/weather/stats").json()["archive"]
    assert st["archived_days"] == 3 and st["cells"] == 3 and st["first_day"] == "2025-10-09" and st["last_day"] == "2025-10-10", st

    # again: everything is archived -> zero upstream requests
    c.post("/weather/backfill", json={"points": [[BASE + 100, 46.62, 14.31]], "from": DAY0, "to": DAY0 + 2 * 86400 - 1})
    j = wait(c)
    assert j["counts"]["cached"] == 3 and j["counts"]["fetched"] == 0 and w.count("open-meteo") == 3, j

    # /context now works without any network
    w.routes = [(s, f) for s, f in w.routes if "open-meteo" not in s]
    ctx = c.get("/context", params={"lat": 46.62, "lon": 14.31, "time": BASE + 100}).json()
    assert ctx["weather"] and ctx["weather"]["summary_de"] == "bedeckt", ctx


def test_dry_run_recent_and_permanence():
    w = World(); w.on("archive-api.open-meteo.com", _hourly); w.on("api.open-meteo.com", _hourly)
    c = client(w, weather_provider="open-meteo", weather_backfill_delay=0.0)
    now = int(time.time()); future = now + 3 * 86400
    pts = [[BASE, 47.0, 15.0], [now, 47.0, 15.0], [future, 47.0, 15.0]]
    r = c.post("/weather/backfill", json={"points": pts, "tracks": False, "dry_run": True})
    j = wait(c)
    # today counts like any other day now; only the future is skipped
    assert j["dry_run"] and j["counts"]["missing"] == 2 and j["counts"]["recent"] == 1 and w.count("open-meteo") == 0, j
    c.post("/weather/backfill", json={"points": pts, "tracks": False})
    j = wait(c)
    assert j["counts"]["fetched"] == 2 and j["counts"]["recent"] == 1 and w.count("open-meteo") == 2, "today is archived, the future is not"
    # archived rows never expire
    import sqlite3
    db = c.app.state.st.cache.path
    ttl = sqlite3.connect(db).execute("SELECT ttl FROM weather").fetchall()
    assert ttl == [(0,), (0,)], ttl


def test_rate_limit_and_cap_resume():
    w = World()
    calls = {"n": 0}

    def limited(u):
        calls["n"] += 1
        return Resp(429) if calls["n"] > 2 else _hourly(u)
    w.on("archive-api.open-meteo.com", limited)
    c = client(w, weather_provider="open-meteo", weather_backfill_delay=0.0)
    pts = [[DAY0 + i * 86400 + 3600, 47.0, 15.0] for i in range(5)]
    c.post("/weather/backfill", json={"points": pts, "tracks": False})
    j = wait(c)
    assert j["status"] == "rate_limited" and j["counts"]["fetched"] == 2, j
    calls["n"] = -100                                          # limit lifted: continues, skipping the 2 archived days
    c.post("/weather/backfill", json={"points": pts, "tracks": False})
    j = wait(c)
    assert j["status"] == "done" and j["counts"]["cached"] == 2 and j["counts"]["fetched"] == 3, j

    c2 = client(World(), weather_provider="open-meteo", weather_backfill_delay=0.0, weather_backfill_max=2)
    c2.app.state.st.weather  # noqa
    w2 = World(); w2.on("archive-api.open-meteo.com", _hourly); c2 = client(w2, weather_provider="open-meteo", weather_backfill_delay=0.0, weather_backfill_max=2)
    c2.post("/weather/backfill", json={"points": pts, "tracks": False})
    j = wait(c2)
    assert j["status"] == "capped" and j["counts"]["fetched"] == 2, j


def test_huge_range_skips_empty_blocks():
    """1900 -> today: clamped to 1940, empty 90-day blocks cost ONE request per source, not one per day."""
    from datetime import datetime, timezone
    w = World()
    w.on("archive-api.open-meteo.com", _hourly)

    def tr(u):
        def ep(k):
            v = u.split(k + "=")[1].split("&")[0]
            return int(datetime.strptime(v[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        a, b = ep("from"), ep("to")
        pts = [(BASE, 47.07, 15.43)] if a <= BASE <= b else []
        return Resp(200, content=gpx(pts), headers={"content-type": "application/gpx+xml"})
    w.on("http://traccar/api/positions/gpx", tr)
    c = client(w, weather_provider="open-meteo", weather_backfill_delay=0.0)
    r = c.post("/weather/backfill", json={"points": [], "from": -2208988800, "to": int(time.time()) + 10 * 86400})
    assert r.status_code == 200, r.text
    j = wait(c)
    assert j["status"] == "done" and j["counts"]["fetched"] == 1 and j["track_points"] == 1, j
    assert j["blocks_skipped"] > 250 and j["track_windows"]["done"] == j["track_windows"]["total"], j
    # ~300 blocks probed + 18 five-day windows x 5 days only in the one block with data
    assert w.count("traccar") < 450, w.count("traccar")


def test_validation_and_auth():
    w = World(); w.on("archive-api.open-meteo.com", _hourly)
    c = client(w, weather_provider="open-meteo", token="s")
    assert c.post("/weather/backfill", json={"points": [[BASE, 47.0, 15.0]]}).status_code == 401
    h = {"x-api-key": "s"}
    assert c.post("/weather/backfill", json={}, headers=h).status_code == 422
    assert c.post("/weather/backfill", json={"from": DAY0}, headers=h).status_code == 422
    assert c.get("/weather/stats", headers=h).json()["enabled"] is True


def test_refresh_bad_and_all():
    """refresh=bad re-fetches only stored days that are empty/incomplete/old; refresh=all every requested day."""
    w = World()
    w.on("archive-api.open-meteo.com", _hourly)
    c = client(w, weather_provider="open-meteo", weather_backfill_delay=0.0)
    pts = [[BASE, 47.07, 15.43], [BASE + 86400, 48.21, 16.37]]
    c.post("/weather/backfill", json={"points": pts, "tracks": False}); j = wait(c)
    assert j["counts"]["fetched"] == 2 and w.count("open-meteo") == 2, j
    c.post("/weather/backfill", json={"points": pts, "tracks": False}); j = wait(c)       # default: stored days are skipped
    assert j["counts"]["cached"] == 2 and w.count("open-meteo") == 2, j
    # damage one stored day (old row version, i.e. one the service would not trust any more)
    st = c.app.state.st
    key = st.weather.key(47.1, 15.4, "2025-10-09")
    st.cache.put("weather", key, {"time": ["2025-10-09T00:00"], "weather_code": [None], "_v": 2}, 0)
    c.post("/weather/backfill", json={"points": pts, "tracks": False, "dry_run": True, "refresh": "bad"}); j = wait(c)
    assert j["counts"]["missing"] == 1 and j["counts"]["cached"] == 1, j
    c.post("/weather/backfill", json={"points": pts, "tracks": False, "refresh": "bad"}); j = wait(c)
    assert j["counts"]["fetched"] == 1 and w.count("open-meteo") == 3, j
    c.post("/weather/backfill", json={"points": pts, "tracks": False, "refresh": "all"}); j = wait(c)
    assert j["counts"]["fetched"] == 2 and w.count("open-meteo") == 5, j
    assert c.post("/weather/backfill", json={"points": pts, "refresh": "bogus"}).status_code == 422


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except Exception as e:                       # noqa: BLE001
                import traceback; traceback.print_exc()
                fails += 1; print("FAIL", name, "->", repr(e))
    sys.exit(1 if fails else 0)
