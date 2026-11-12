"""Elevation (local SRTM3 tiles -> cache -> Open-Meteo), overrides over the cache, GeoNames height services,
tile downloader.   python tests/test_elevation.py"""
import gzip
import io
import json
import os
import sys
import tempfile
from array import array

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import Resp, World, client, photon_graz  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def write_tile(d, name, n=1201, fn=lambda r, c: 100 + c):
    a = array("h", (fn(r, c) for r in range(n) for c in range(n)))
    if sys.byteorder == "little":
        a.byteswap()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name + ".hgt"), "wb") as f:
        f.write(a.tobytes())


def nominatim_hall(url):
    return Resp(200, {"name": "Veranstaltungshalle", "lat": "48.14543", "lon": "16.32572", "osm_type": "way", "osm_id": 1,
                      "class": "amenity", "type": "community_centre",
                      "address": {"road": "Beispielgasse", "house_number": "20", "postcode": "2552", "country_code": "at",
                                  "country": "Österreich", "state": "Niederösterreich"}})


def main():
    d = tempfile.mkdtemp()
    srtm = os.path.join(d, "srtm")
    write_tile(srtm, "N47E016")                                  # value = 100 + column: east-west gradient
    w = World()
    w.on("api.open-meteo.com/v1/elevation", lambda u: Resp(200, {"elevation": [432.0]}))
    c = client(w, srtm_dir=srtm)

    # -- local tile, bilinear
    r = c.get("/elevation", params={"lat": 47.5, "lon": 16.5}).json()
    check("local tile value", r["elevation"] == 700.0 and r["source"] == "srtm", r)
    r = c.get("/elevation", params={"lat": 47.5, "lon": 16.0005}).json()
    check("bilinear interpolation", abs(r["elevation"] - 100.6) < 0.2, r)
    r2 = c.get("/elevation", params={"lat": 47.5, "lon": 16.0005}).json()
    check("second lookup comes from the cache", r2["cached"] is True and r2["elevation"] == r["elevation"], r2)

    # -- outside the tile: Open-Meteo, cached for good
    r = c.get("/elevation", params={"lat": 10.5, "lon": 20.5}).json()
    check("open-meteo fallback", r["elevation"] == 432.0 and r["source"] == "open-meteo", r)
    c.get("/elevation", params={"lat": 10.5, "lon": 20.5})
    check("open-meteo asked only once", w.count("open-meteo.com/v1/elevation") == 1, w.calls)

    # -- no tile, no upstream
    w2 = World()
    c2 = client(w2, srtm_dir=srtm)
    r = c2.get("/elevation", params={"lat": 10.5, "lon": 20.5}).json()
    check("unreachable -> none", r["elevation"] is None and r["source"] == "none", r)
    c3 = client(w, srtm_dir=srtm, elevation_providers=["local"])
    n0 = w.count("open-meteo.com/v1/elevation")
    r = c3.get("/elevation", params={"lat": 10.7, "lon": 20.7}).json()
    check("provider list respected (local only)", r["elevation"] is None and w.count("open-meteo.com/v1/elevation") == n0, r)

    # -- void corners
    write_tile(srtm, "N10E020", fn=lambda r_, c_: -32768 if c_ < 600 else 50)
    r = c3.get("/elevation", params={"lat": 10.5, "lon": 20.2}).json()
    check("all-void -> none", r["elevation"] is None, r)
    r = c3.get("/elevation", params={"lat": 10.5, "lon": 20.8}).json()
    check("tile with partial voids still answers", r["elevation"] == 50.0, r)

    # -- GeoNames height services
    j = c.get("/srtm3JSON", params={"lat": 47.5, "lng": 16.5}).json()
    check("srtm3JSON", j == {"srtm3": 700, "lng": 16.5, "lat": 47.5}, j)
    check("astergdemJSON + gtopo30JSON same value",
          c.get("/astergdemJSON", params={"lat": 47.5, "lng": 16.5}).json()["astergdem"] == 700
          and c.get("/gtopo30JSON", params={"lat": 47.5, "lng": 16.5}).json()["gtopo30"] == 700)
    t = c.get("/srtm3", params={"lat": 47.5, "lng": 16.5})
    check("srtm3 plain text", t.text == "700" and t.headers["content-type"].startswith("text/plain"), t.text)
    off = client(World(), srtm_dir=srtm, elevation_providers=[])
    check("no height -> -32768 / -9999",
          off.get("/srtm3JSON", params={"lat": 1, "lng": 1}).json()["srtm3"] == -32768
          and off.get("/astergdemJSON", params={"lat": 1, "lng": 1}).json()["astergdem"] == -9999)

    m = c.get("/srtm3JSON", params={"lats": "47.5,47.5", "lngs": "16.5,16.0005"}).json()
    check("multi-point JSON", [x["srtm3"] for x in m["geonames"]] == [700, 101], m)
    check("multi-point plain text", c.get("/srtm3", params={"lats": "47.5,47.5", "lngs": "16.5,16.5"}).text == "700\n700")
    check("bad point list -> status error", "status" in c.get("/srtm3JSON", params={"lats": "47.5,1", "lngs": "16.5"}).json()
          and "status" in c.get("/srtm3JSON", params={"lat": 95, "lng": 1}).json())

    # -- context carries elevation
    ctx = c.get("/context", params={"lat": 47.5, "lon": 16.5, "time": "2026-10-04T12:24:39+02:00"}).json()
    check("/context has elevation_m", ctx.get("elevation_m") == 700, ctx.get("elevation_m"))
    ctx = c.get("/context", params={"lat": 47.5, "lon": 16.5, "time": 1790000000, "elevation": "false"}).json()
    check("/context elevation=false", "elevation_m" not in ctx)

    # -- overrides over the cache
    w3 = World()
    w3.on("nominatim", nominatim_hall)
    w3.on("photon", photon_graz)
    c4 = client(w3, srtm_dir=srtm)
    q = {"lat": 48.14543, "lng": 16.32572}
    a = c4.get("/addressJSON", params=q).json()
    check("address before override", a["address"]["placename"] == "Veranstaltungshalle", a)
    item = {"id": "vs", "lat": 48.14543, "lng": 16.32572, "radius_m": 40,
            "address": {"placename": "Volksschule"}, "reverse": {"locality": "Volksschule Musterdorf"}, "elevation": 215}
    check("POST /overrides", c4.post("/overrides", json=item).status_code == 200)
    a = c4.get("/addressJSON", params=q).json()
    check("address override wins over the cache", a["address"]["placename"] == "Volksschule"
          and a["address"]["street"] == "Beispielgasse" and list(a) == ["address"], a)
    a = c4.get("/address", params={"lat": 48.14550, "lng": 16.32580}).text
    check("override radius (XML, 10 m off)", "<placename>Volksschule</placename>" in a, a)
    a = c4.get("/addressJSON", params={"lat": 48.1460, "lng": 16.3257}).json()
    check("outside radius unchanged", a["address"]["placename"] == "Veranstaltungshalle", a)
    r = c4.get("/reverse", params={"lat": 48.14543, "lon": 16.32572}).json()
    check("reverse override", r["locality"] == "Volksschule Musterdorf" and r["override"] == "vs" and r["found"]
          and r["sublocation"].startswith("Volksschule Musterdorf"), r)
    r = c4.get("/elevation", params={"lat": 48.14543, "lon": 16.32572}).json()
    check("elevation override", r["elevation"] == 215.0 and r["source"] == "override", r)
    check("override survives in the file", json.load(open(c4.get("/overrides").json()["file"]))[0]["id"] == "vs")
    check("stats count overrides", c4.get("/stats").json()["overrides"] == 1)
    check("bad override -> 422", c4.post("/overrides", json={"id": "x", "lat": 1}).status_code == 422)
    check("DELETE /overrides", c4.delete("/overrides/vs").status_code == 200 and c4.delete("/overrides/vs").status_code == 404)
    a = c4.get("/addressJSON", params=q).json()
    check("original answer is back after delete (cache was never changed)", a["address"]["placename"] == "Veranstaltungshalle", a)
    # hand-edited file is picked up
    path = c4.get("/overrides").json()["file"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"id": "h", "lat": 48.14543, "lng": 16.32572, "address": {"placename": "Handgeschrieben"}}], f)
    a = c4.get("/addressJSON", params=q).json()
    check("hand-edited overrides.json reloads", a["address"]["placename"] == "Handgeschrieben", a)
    with open(path, "w") as f:
        f.write("{not json")
    a = c4.get("/addressJSON", params=q).json()
    check("broken file keeps last good list", a["address"]["placename"] == "Handgeschrieben" and c4.get("/overrides").json()["error"])

    # override alone answers when no geocoder is reachable
    c5 = client(World(), srtm_dir=srtm)
    c5.post("/overrides", json={"id": "o", "lat": 47.0, "lng": 15.0, "address": {"placename": "Hütte", "countryCode": "AT"}})
    a = c5.get("/addressJSON", params={"lat": 47.0, "lng": 15.0}).json()
    check("override stands in without upstream", a.get("address", {}).get("placename") == "Hütte", a)

    # -- auth
    cs = client(w, srtm_dir=srtm, token="sekrit")
    check("/elevation needs the token", cs.get("/elevation", params={"lat": 47.5, "lon": 16.5}).status_code == 401
          and cs.get("/elevation", params={"lat": 47.5, "lon": 16.5}, headers={"X-Api-Key": "sekrit"}).status_code == 200)
    check("srtm3JSON accepts ?token=", cs.get("/srtm3JSON", params={"lat": 47.5, "lng": 16.5, "token": "sekrit"}).status_code == 200)

    # -- downloader
    from app import srtmfetch
    big = array("h", ((r * 7 + c * 3) % 30000 for r in range(3601) for c in range(3601)))
    if sys.byteorder == "little":
        big.byteswap()
    raw_gz = gzip.compress(big.tobytes())
    import urllib.error
    import urllib.request
    seen = []

    def fake_open(req, timeout=None):
        seen.append(req.full_url)
        if "N46E009" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return io.BytesIO(raw_gz)
    real = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        out = os.path.join(d, "dl")
        check("fetch new tile", srtmfetch.fetch_tile("http://x/skadi", 47, 16, out) == "new")
        check("fetch skips existing", srtmfetch.fetch_tile("http://x/skadi", 47, 16, out) == "have" and len(seen) == 1)
        check("ocean tile (404) -> missing", srtmfetch.fetch_tile("http://x/skadi", 46, 9, out) == "missing")
        check("url layout", seen[0] == "http://x/skadi/N47/N47E016.hgt.gz", seen)
    finally:
        urllib.request.urlopen = real
    p = os.path.join(out, "N47E016.hgt")
    check("thinned to 1201x1201", os.path.getsize(p) == 1201 * 1201 * 2)
    from app.elevation import Tile
    t = Tile.open(p)
    check("thinned values = every 3rd sample", t._v(2, 5) == (6 * 7 + 15 * 3) % 30000 and t._v(1200, 1200) == (3600 * 7 + 3600 * 3) % 30000)
    check("austria region = 27 tiles", len(srtmfetch.REGIONS["austria"]) == 27)
    check("tile names", srtmfetch.parse_tiles("N47E016,S01W002") == [(47, 16), (-1, -2)])

    print("\n%s" % ("ALL OK" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
