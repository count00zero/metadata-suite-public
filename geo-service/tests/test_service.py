"""Service tests against a fake upstream world (no network).   python tests/test_service.py"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import (EMPTY_FEATURES, SERVER_ERROR, Resp, World, client, gpx,  # noqa: E402
                   nominatim_graz, pelias_graz, photon_graz)

DAY0 = 1_759_968_000            # 2025-10-09 00:00 UTC
BASE = DAY0 + 12 * 3600         # noon UTC = 14:00 in Vienna (CEST)


def _all_empty(w):
    w.on("http://photon", EMPTY_FEATURES)
    w.on("http://nominatim", lambda u: Resp(200, {"error": "Unable to geocode"}))
    w.on("http://pelias", EMPTY_FEATURES)


def test_reverse_hit_and_cache():
    w = World(); w.on("http://photon", photon_graz)
    c = client(w)
    r = c.get("/reverse", params={"lat": 47.0707, "lon": 15.4395}).json()
    assert r["found"] and r["city"] == "Graz" and r["country_a"] == "AUT" and r["source"] == "Photon", r
    assert r["label"] == "Graz, Steiermark, Österreich" and r["cached"] is False
    r2 = c.get("/reverse", params={"lat": 47.07071, "lon": 15.43951}).json()      # same ~11 m cell
    assert r2["cached"] is True and r2["city"] == "Graz"
    assert w.count("http://photon") == 1, "second lookup must come from the cache"
    # where the cached value came from and when it entered the cache -- also for the first (uncached) answer
    assert r["upstream_source"] == "Photon" and r["cached_at"][:4].isdigit() and r["cached_age_s"] <= 5, r
    assert r2["upstream_source"] == "Photon" and r2["cached_at"] == r["cached_at"] and r2["cached_age_s"] >= 0, r2
    # an override replaces `source`, but the cached origin stays visible
    assert c.post("/overrides", json={"id": "o1", "lat": 47.0707, "lng": 15.4395, "radius_m": 30,
                                      "reverse": {"locality": "Daheim"}}).status_code == 200
    r3 = c.get("/reverse", params={"lat": 47.0707, "lon": 15.4395}).json()
    assert r3["source"] == "override" and r3["override"] == "o1" and r3["upstream_source"] == "Photon" \
        and r3["cached_at"] == r["cached_at"], r3
    c.delete("/overrides/o1")
    assert c.get("/reverse", params={"lat": 47.0707, "lon": 15.4395, "nocache": True}).json()["cached"] is False
    assert w.count("http://photon") == 2


def test_chain_falls_through_and_breaker():
    w = World(); w.on("http://photon", SERVER_ERROR); w.on("http://nominatim", nominatim_graz)
    c = client(w, breaker_failures=3, breaker_cooldown=60)
    for i in range(3):
        r = c.get("/reverse", params={"lat": 47.0 + i * 0.01, "lon": 15.4}).json()
        assert r["found"] and r["source"] == "Nominatim" and r["country_a"] == "AUT"
        assert r["tried"][0] == {"backend": "photon", "status": "unreachable"}
    r = c.get("/reverse", params={"lat": 47.5, "lon": 15.4}).json()
    assert r["tried"][0]["status"] == "skipped (circuit open)", r["tried"]
    assert w.count("http://photon") == 3, "breaker must stop asking a dead backend"
    assert c.get("/health").json()["geocoders"]["photon"]["open"] is True


def test_probe_shows_reason_and_recovers_open_circuit():
    w = World(); w.on("http://photon", SERVER_ERROR); w.on("http://nominatim", nominatim_graz)
    c = client(w, breaker_failures=1, breaker_cooldown=600)
    first = c.get("/reverse", params={"lat": 47.0, "lon": 15.4}).json()
    assert first["tried"][0]["status"] == "unreachable"
    assert c.get("/health").json()["geocoders"]["photon"]["open"] is True
    h = c.get("/health", params={"probe": True}).json()["geocoders"]["photon"]
    assert h["probe"]["reachable"] is False and h["probe"]["detail"] == "HTTP 500", h
    w.on("http://photon", photon_graz)                          # Photon is up now -- no restart, no waiting
    h = c.get("/health", params={"probe": True}).json()["geocoders"]["photon"]
    assert h["probe"]["reachable"] is True and h["open"] is False, h
    r = c.get("/reverse", params={"lat": 47.5, "lon": 15.4}).json()
    assert r["source"] == "Photon" and r["tried"] == [{"backend": "photon", "status": "hit"}], r
    # a cached answer does not replay the (possibly stale) 'unreachable' of the lookup that filled the cache
    again = c.get("/reverse", params={"lat": 47.0, "lon": 15.4}).json()
    assert again["cached"] is True and again["tried"] == [], again
    # connection refused is reported with its reason
    w2 = World(); c2 = client(w2)
    d = c2.get("/health", params={"probe": True}).json()["geocoders"]["photon"]["probe"]
    assert d["reachable"] is False and "ConnectionError" in d["detail"], d


def test_empty_is_cached_unreachable_is_not():
    w = World(); _all_empty(w)
    c = client(w)
    r = c.get("/reverse", params={"lat": 10.0, "lon": 20.0}).json()
    assert r["found"] is False and r["cached"] is False
    assert c.get("/reverse", params={"lat": 10.0, "lon": 20.0}).json()["cached"] is True

    w2 = World()                                                    # nothing reachable at all
    c2 = client(w2)
    assert c2.get("/reverse", params={"lat": 1.0, "lon": 2.0}).status_code == 503
    n = len(w2.calls)
    assert c2.get("/reverse", params={"lat": 1.0, "lon": 2.0}).status_code == 503
    assert len(w2.calls) > n, "an outage must not be remembered as 'no place here'"
    w2.on("http://photon", photon_graz)                              # backend comes back
    assert c2.get("/reverse", params={"lat": 1.0, "lon": 2.0}).json()["found"] is True


def test_maritime_inside_the_chain_comes_before_pelias():
    w = World(); _all_empty(w)
    w.on("http://maritime", lambda u: Resp(200, {"name": "Südpazifik"}))
    c = client(w, maritime="http://maritime", chain=["photon", "nominatim", "maritime", "pelias"])
    r = c.get("/reverse", params={"lat": -48.877, "lon": -123.393}).json()
    assert r["found"] and r["source"] == "Maritim-Backup" and r["label"] == "Südpazifik", r
    assert [t["backend"] for t in r["tried"]] == ["photon", "nominatim", "maritime"], r["tried"]
    assert w.count("http://pelias/v1/reverse") == 0, "Pelias was not asked for the open sea"
    w2 = World(); _all_empty(w2)
    w2.on("http://maritime", lambda u: Resp(200, {}))
    c2 = client(w2, maritime="http://maritime", chain=["photon", "nominatim", "maritime", "pelias"])
    r2 = c2.get("/reverse", params={"lat": 1, "lon": 2}).json()
    assert [t["backend"] for t in r2["tried"]] == ["photon", "nominatim", "maritime", "pelias"], r2["tried"]
    w3 = World(); _all_empty(w3)                    # not in the chain: still tried last, exactly once
    w3.on("http://maritime", lambda u: Resp(200, {"name": "Atlantik"}))
    r3 = client(w3, maritime="http://maritime").get("/reverse", params={"lat": 0, "lon": -30}).json()
    assert [t["backend"] for t in r3["tried"]][-1] == "maritime" and [t["backend"] for t in r3["tried"]].count("maritime") == 1


def test_maritime_only_when_others_empty_and_alpha3_supplement():
    w = World(); _all_empty(w)
    w.on("http://maritime", lambda u: Resp(200, {"name": "Indischer Ozean"}))
    c = client(w, maritime="http://maritime")
    r = c.get("/reverse", params={"lat": 4.0, "lon": 73.0}).json()
    assert r["found"] and r["source"] == "Maritim-Backup" and r["country"] == "Indischer Ozean"

    w = World(); w.on("http://photon", lambda u: Resp(200, {"features": [{"properties": {"country": "Österreich", "city": "Graz"}}]}))
    w.on("http://pelias", pelias_graz)                               # photon gave no countrycode -> ask Pelias
    r = client(w).get("/reverse", params={"lat": 47.0, "lon": 15.4}).json()
    assert r["source"] == "Photon" and r["country_a"] == "AUT"


def test_country_only_answer_is_upgraded_by_the_maritime_server():
    """Nominatim says just 'Spanien' for the Strait of Gibraltar: ask the maritime server, keep the country."""
    w = World()
    w.on("http://photon", lambda u: Resp(200, {"features": []}))
    w.on("http://nominatim", lambda u: Resp(200, {"address": {"country": "Spanien", "country_code": "es"}}))
    w.on("http://maritime", lambda u: Resp(200, {"name": "Straße von Gibraltar"}))
    r = client(w, maritime="http://maritime").get("/reverse", params={"lat": 35.96, "lon": -5.6}).json()
    assert r["country"] == "Spanien" and r["locality"] == "Straße von Gibraltar" and r["city"] == "", r
    assert r["label"] == "Straße von Gibraltar, Spanien" and "Maritim" in r["source"], r
    assert [t["backend"] for t in r["tried"]] == ["photon", "nominatim", "maritime"], r["tried"]
    # maritime does not know the place (land, desert): the country-only answer stays as it was
    w2 = World()
    w2.on("http://photon", lambda u: Resp(200, {"features": []}))
    w2.on("http://nominatim", lambda u: Resp(200, {"address": {"country": "Libyen", "country_code": "ly"}}))
    w2.on("http://maritime", lambda u: Resp(200, {}))
    r2 = client(w2, maritime="http://maritime").get("/reverse", params={"lat": 26.0, "lon": 17.0}).json()
    assert r2["country"] == "Libyen" and r2["source"] == "Nominatim", r2
    # Strait of Dover: Nominatim names the state ("England") but nothing below it
    w4 = World()
    w4.on("http://photon", lambda u: Resp(200, {"features": []}))
    w4.on("http://nominatim", lambda u: Resp(200, {"address": {"state": "England", "country": "Vereinigtes Königreich", "country_code": "gb"}}))
    w4.on("http://maritime", lambda u: Resp(200, {"name": "Straße von Dover"}))
    r4 = client(w4, maritime="http://maritime").get("/reverse", params={"lat": 51.0, "lon": 1.4}).json()
    assert r4["label"] == "Straße von Dover, Vereinigtes Königreich" and r4["state"] == "", r4
    # a state-level answer on land with an unknown water body stays as it was
    w5 = World()
    w5.on("http://photon", lambda u: Resp(200, {"features": []}))
    w5.on("http://nominatim", lambda u: Resp(200, {"address": {"state": "England", "country": "Vereinigtes Königreich", "country_code": "gb"}}))
    w5.on("http://maritime", lambda u: Resp(200, {"name": "Unbekannt"}))
    assert client(w5, maritime="http://maritime").get("/reverse", params={"lat": 52.0, "lon": -1.0}).json()["state"] == "England"
    # a real address is never touched by the maritime server
    w3 = World()
    w3.on("http://photon", photon_graz)
    w3.on("http://maritime", lambda u: Resp(200, {"name": "SOLLTE NICHT KOMMEN"}))
    r3 = client(w3, maritime="http://maritime").get("/reverse", params={"lat": 47.0, "lon": 15.4}).json()
    assert r3["source"] == "Photon" and "maritime" not in [t["backend"] for t in r3["tried"]], r3


def test_maritime_placeholder_name_is_no_hit():
    """The maritime server answers {"name": "Unbekannt"} when it has no water body: that must not become the address."""
    w = World(); _all_empty(w)
    w.on("http://maritime", lambda u: Resp(200, {"name": "Unbekannt"}))
    r = client(w, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5, "nocache": "true"}).json()
    assert r["found"] is False and r["tried"][-1] == {"backend": "maritime", "status": "empty"}, r
    w2 = World()
    w2.on("http://photon", lambda u: Resp(200, {"features": []}))
    w2.on("http://nominatim", lambda u: Resp(200, {"address": {"country": "Italien", "country_code": "it"}}))
    w2.on("http://maritime", lambda u: Resp(200, {"name": "Unbekannt"}))
    r2 = client(w2, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5}).json()
    assert r2["country"] == "Italien" and r2["source"] == "Nominatim" and r2["label"] == "Italien", r2


def test_maritime_sea_goes_to_country_and_the_near_feature_to_locality():
    """Real answer shape of the marine geocoder: nearest feature + contained_in layers (IHO English, Natural Earth German)."""
    adria = {"name": "Uvala Vrulje", "distance_km": 63.0, "contained_in": [
        {"layer": "iho", "lang": "en", "name": "Adriatic Sea"}, {"layer": "seavox", "lang": "en", "name": "MEDITERRANEAN SEA, EASTERN BASIN"},
        {"layer": "ne_marine", "lang": "de", "name": "Adriatisches Meer"}]}
    w = World(); _all_empty(w)
    w.on("http://maritime", lambda u: Resp(200, adria))
    r = client(w, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5}).json()
    assert r["country"] == "Adriatisches Meer" and r["locality"] == "Adriatisches Meer" and r["label"] == "Adriatisches Meer", r   # the bay is 63 km away
    near = dict(adria, distance_km=2.0)
    w2 = World(); _all_empty(w2)
    w2.on("http://maritime", lambda u: Resp(200, near))
    r2 = client(w2, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5}).json()
    assert r2["country"] == "Adriatisches Meer" and r2["locality"] == "Uvala Vrulje" and r2["state"] == "", r2
    assert r2["label"] == "Uvala Vrulje, Adriatisches Meer", r2
    # no feature at all (name null) but a sea polygon: still a hit
    w3 = World(); _all_empty(w3)
    w3.on("http://maritime", lambda u: Resp(200, {"name": None, "contained_in": adria["contained_in"]}))
    assert client(w3, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5}).json()["label"] == "Adriatisches Meer"
    # English asked: the IHO name wins; the zone / SeaVoX layers never do
    w4 = World(); _all_empty(w4)
    w4.on("http://maritime", lambda u: Resp(200, {"contained_in": [{"layer": "eez", "lang": "en", "name": "Italian Exclusive Economic Zone"}] + adria["contained_in"]}))
    assert client(w4, maritime="http://maritime").get("/reverse", params={"lat": 44.0, "lon": 13.5, "lang": "en"}).json()["label"] == "Adriatic Sea"
    # land country from Nominatim + water: Strait of Gibraltar keeps Spain, the sea is added as state
    w5 = World()
    w5.on("http://photon", lambda u: Resp(200, {"features": []}))
    w5.on("http://nominatim", lambda u: Resp(200, {"address": {"country": "Spanien", "country_code": "es"}}))
    w5.on("http://maritime", lambda u: Resp(200, {"name": "Straße von Gibraltar", "distance_km": 1.0,
                                                  "contained_in": [{"layer": "ne_marine", "lang": "de", "name": "Mittelmeer"}]}))
    r5 = client(w5, maritime="http://maritime").get("/reverse", params={"lat": 35.96, "lon": -5.6}).json()
    assert r5["country"] == "Spanien" and r5["locality"] == "Straße von Gibraltar" and r5["state"] == "Mittelmeer", r5
    assert r5["label"] == "Straße von Gibraltar, Mittelmeer, Spanien", r5


def test_tile_proxy_serves_tiles_and_rewrites_the_map_url():
    w = World()
    w.on("http://tiles.lan/styles/x/", lambda u: Resp(200, content=b"PNGDATA") if u.endswith("/5/17/11.png") else Resp(404))
    c = client(w, tile_url="http://tiles.lan/styles/x/{z}/{x}/{y}.png", tile_proxy=True)
    assert '"tileUrl": "/tiles/{z}/{x}/{y}"' in c.get("/ui").text and '"tileUrl": "/tiles/{z}/{x}/{y}"' in c.get("/nearby").text
    t = c.get("/tiles/5/17/11")
    assert t.status_code == 200 and t.content == b"PNGDATA" and "max-age" in t.headers["cache-control"], (t.status_code, t.content)
    assert c.get("/tiles/5/1/1").status_code == 404 and c.get("/tiles/3/9/0").status_code == 404, "unknown / out-of-range tile"
    off = client(World(), tile_url="http://tiles.lan/styles/x/{z}/{x}/{y}.png")          # proxy off: the URL is passed on as written
    assert '"tileUrl": "http://tiles.lan/styles/x/{z}/{x}/{y}.png"' in off.get("/nearby").text and off.get("/tiles/5/17/11").status_code == 404


def test_batch():
    w = World(); w.on("http://photon", photon_graz)
    c = client(w)
    r = c.post("/reverse-batch", json={"points": [{"lat": 47.0, "lon": 15.4}, {"lat": 47.1, "lon": 15.5}]}).json()
    assert [x["found"] for x in r["results"]] == [True, True]
    assert c.post("/reverse-batch", json={"points": [{"lat": 99, "lon": 15.4}]}).json()["results"][0]["found"] is False


def _traccar(points):
    return lambda u: Resp(200, content=gpx(points))


def test_track_at_interpolates_and_caches():
    w = World()
    w.on("http://traccar/api/positions/gpx", _traccar([(BASE, 47.0, 15.0), (BASE + 60, 47.001, 15.002)]))
    c = client(w)
    r = c.get("/track-at", params={"time": BASE + 30}).json()
    assert r["found"] and r["method"] == "interpolated" and r["gap_s"] == 30, r
    assert abs(r["lat"] - 47.0005) < 1e-6 and abs(r["lon"] - 15.001) < 1e-6
    assert 0.5 < r["confidence"] <= 1.0 and r["tier"] == 1 and r["sources"] == {"traccar": True}
    iso = datetime.fromtimestamp(BASE + 30, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c.get("/track-at", params={"time": iso}).json()["lat"] == r["lat"]            # ISO input
    assert w.count("positions/gpx") == 1, "a finished day must be fetched once, then served from cache"


def test_track_at_gaps_and_outage():
    w = World()
    w.on("http://traccar/api/positions/gpx", _traccar([(BASE, 47.0, 15.0)]))
    c = client(w)
    near = c.get("/track-at", params={"time": BASE + 3000}).json()                       # 50 min later: tier 2, nearest
    assert near["found"] and near["method"] == "nearest" and near["tier"] == 2 and near["confidence"] < 0.1
    assert c.get("/track-at", params={"time": BASE + 30000}).json()["found"] is False   # > 6 h from any point

    w2 = World(); w2.on("http://traccar/api/positions/gpx", lambda u: Resp(500))
    c2 = client(w2)
    assert c2.get("/track-at", params={"time": BASE}).status_code == 503
    w2.on("http://traccar/api/positions/gpx", _traccar([(BASE, 47.0, 15.0)]))             # back up: failure wasn't cached
    assert c2.get("/track-at", params={"time": BASE}).json()["found"] is True


def test_track_range_and_step():
    pts = [(BASE + i * 10, 47.0 + i * 1e-5, 15.0) for i in range(30)]
    w = World(); w.on("http://traccar/api/positions/gpx", _traccar(pts))
    c = client(w)
    r = c.get("/track-range", params={"from": BASE, "to": BASE + 400}).json()
    assert r["count"] == 30 and r["points"][0][3] == "T"
    assert c.get("/track-range", params={"from": BASE, "to": BASE + 400, "step": 60}).json()["count"] == 5
    assert c.get("/track-range", params={"from": BASE, "to": BASE + 9 * 86400}).status_code == 422


def test_context_with_weather():
    w = World(); w.on("http://photon", photon_graz)
    day = "2025-10-09"
    hourly = {"time": [f"{day}T{h:02d}:00" for h in range(24)], "weather_code": [63] * 24,
              "temperature_2m": [11.4] * 24, "precipitation": [1.2] * 24, "cloud_cover": [100] * 24}
    w.on("archive-api.open-meteo.com", lambda u: Resp(200, {"hourly": hourly}))
    c = client(w, weather_provider="open-meteo")
    r = c.get("/context", params={"lat": 47.0707, "lon": 15.4395, "time": BASE}).json()
    assert r["daypart_de"] == "Nachmittag" and r["month_de"] == "Oktober" and r["tz"] == "Europe/Vienna", r
    assert r["weather"]["summary_de"] == "Regen" and r["weather"]["temp_c"] == 11
    assert r["text_de"] == "Aufgenommen in Graz (Steiermark, Österreich), nachmittags im Oktober 2025; Regen bei 11 °C.", r["text_de"]
    assert r["extra_de"] == "nachmittags im Oktober 2025; Regen bei 11 °C", r["extra_de"]
    lat_sent = [u for u in w.calls if "archive-api.open-meteo" in u][0]
    assert "latitude=47.1&longitude=15.4" in lat_sent, "coordinates must be rounded before leaving the house"
    r2 = c.get("/context", params={"lat": 47.0707, "lon": 15.4395, "time": BASE + 600}).json()
    assert r2["weather"] and w.count("archive-api.open-meteo") == 1, "weather is cached per day and place"
    off = client(World(), weather_provider="none").get("/context", params={"lat": 1, "lon": 2, "time": BASE}).json()
    assert off["weather"] is None and off["place"] is None and off["text_de"].startswith("Aufgenommen, ")


def test_context_weather_status():
    """/context says WHY there is no weather: off | limited | failed | empty | no-hour | ok."""
    q = {"lat": 47.0707, "lon": 15.4395, "time": BASE}
    assert client(World(), weather_provider="none").get("/context", params=q).json()["weather_status"] == "off"
    for code, want in ((429, "limited"), (500, "failed")):
        w = World(); w.on("http://photon", photon_graz); w.on("archive-api.open-meteo.com", lambda u, c=code: Resp(c, {}))
        r = client(w, weather_provider="open-meteo").get("/context", params=q).json()
        assert r["weather"] is None and r["weather_status"] == want, (code, r["weather_status"])
    w = World(); w.on("http://photon", photon_graz)
    w.on("archive-api.open-meteo.com", lambda u: Resp(200, {"hourly": {"time": []}}))
    assert client(w, weather_provider="open-meteo").get("/context", params=q).json()["weather_status"] == "empty"
    w = World(); w.on("http://photon", photon_graz)
    w.on("archive-api.open-meteo.com", lambda u: Resp(200, {"hourly": {"time": ["2025-10-09T00:00"], "weather_code": [3],
                                                                         "temperature_2m": [9], "precipitation": [0], "cloud_cover": [1]}}))
    assert client(w, weather_provider="open-meteo").get("/context", params=q).json()["weather_status"] == "no-hour"
    # a day full of nulls is "empty" and is stored only for an hour (not for good), so the day is asked again later
    from app.weather import WeatherService
    from fakes import make_settings
    class _Cache:
        def __init__(self): self.rows = {}
        def get(self, t, k): return self.rows.get(k)
        def put(self, t, k, v, ttl): self.rows[k] = v; self.ttl = ttl
    w = World()
    w.on("archive-api.open-meteo.com", lambda u: Resp(200, {"hourly": {"time": [f"2025-10-09T{h:02d}:00" for h in range(24)],
                                                                        "weather_code": [None] * 24, "temperature_2m": [None] * 24}}))
    from fakes import install
    install(w)
    cache = _Cache()
    svc = WeatherService(make_settings(weather_provider="open-meteo"), cache)
    assert svc.at_status(47.0, 15.4, BASE) == (None, "empty") and cache.ttl == 3600, cache.ttl
    w = World(); w.on("http://photon", photon_graz)
    day = "2025-10-09"
    w.on("archive-api.open-meteo.com", lambda u: Resp(200, {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(24)], "weather_code": [3] * 24,
                                                                         "temperature_2m": [9] * 24, "precipitation": [0] * 24, "cloud_cover": [1] * 24}}))
    assert client(w, weather_provider="open-meteo").get("/context", params=q).json()["weather_status"] == "ok"


def test_auth():
    w = World(); w.on("http://photon", photon_graz)
    c = client(w, token="secret")
    assert c.get("/health").status_code == 200
    assert c.get("/reverse", params={"lat": 47, "lon": 15}).status_code == 401
    assert c.get("/reverse", params={"lat": 47, "lon": 15}, headers={"X-API-Key": "nope"}).status_code == 401
    assert c.get("/reverse", params={"lat": 47, "lon": 15}, headers={"X-API-Key": "secret"}).status_code == 200
    assert c.get("/reverse", params={"lat": 47, "lon": 15}, headers={"Authorization": "Bearer secret"}).status_code == 200
    assert "tok" not in c.get("/health").text, "health must never expose secrets"


def test_bad_input():
    c = client(World())
    assert c.get("/reverse", params={"lat": 91, "lon": 0}).status_code == 422
    assert c.get("/track-at", params={"time": "yesterday-ish"}).status_code == 422


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except Exception as e:                       # noqa: BLE001
                fails += 1; print("FAIL", name, "->", repr(e))
    sys.exit(1 if fails else 0)
