"""Fake upstream world for the tests: replaces requests.get in the service modules."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Resp:
    def __init__(self, status=200, json_data=None, content=b"", headers=None):
        self.status_code, self._json, self.content, self.headers = status, json_data, content, headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class World:
    def __init__(self):
        self.calls: list[str] = []
        self.routes: list[tuple[str, callable]] = []

    def on(self, substring, handler):
        self.routes.insert(0, (substring, handler))      # newest route wins

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for sub, fn in self.routes:
            if sub in url:
                r = fn(url)
                if isinstance(r, Exception):
                    raise r
                return r
        raise requests.ConnectionError(f"no route for {url}")

    def count(self, substring) -> int:
        return sum(1 for c in self.calls if substring in c)


def install(world: World):
    from app import backends, tracks, weather
    for m in (backends, tracks, weather):
        m.requests = type("R", (), {"get": staticmethod(world.get), "RequestException": requests.RequestException,
                                    "ConnectionError": requests.ConnectionError})


def photon_graz(url):
    return Resp(200, {"features": [{"properties": {"country": "Österreich", "countrycode": "AT", "city": "Graz",
                                                  "state": "Steiermark", "name": "Hauptplatz", "street": "Hauptplatz"}}]})


def nominatim_graz(url):
    return Resp(200, {"name": "Hauptplatz", "address": {"country": "Österreich", "country_code": "at", "city": "Graz",
                                                       "state": "Steiermark", "road": "Hauptplatz"}})


def pelias_graz(url):
    return Resp(200, {"features": [{"properties": {"country": "Österreich", "country_a": "AUT", "locality": "Graz",
                                                  "region": "Steiermark", "name": "Hauptplatz"}}]})


EMPTY_FEATURES = lambda url: Resp(200, {"features": []})
SERVER_ERROR = lambda url: Resp(500)


def gpx(points) -> bytes:
    rows = "".join(
        f'<trkpt lat="{la}" lon="{lo}"><time>{datetime.fromtimestamp(e, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
        for e, la, lo in points)
    return f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{rows}</trkseg></trk></gpx>'.encode()


def make_settings(**kw):
    from app.config import Settings, parse_devices
    d = tempfile.mkdtemp()
    base = dict(photon="http://photon", nominatim="http://nominatim", pelias="http://pelias", maritime="",
                cache_db=os.path.join(d, "c.sqlite"), traccar_url="http://traccar", traccar_token="tok",
                traccar_devices=parse_devices("7:Phone"), dawarich_server="", http_timeout=1.0)
    base.setdefault("gn_country_file", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "countryInfo.txt"))
    base.update(kw)
    return Settings(**base)


def client(world: World, **kw):
    from fastapi.testclient import TestClient
    from app.main import create_app
    install(world)
    return TestClient(create_app(make_settings(**kw)))


def serve(world: World, **kw):
    """Starts the real FastAPI app on a free local port (urllib clients need real HTTP).
    Returns (server, base_url); stop with server.should_exit = True."""
    import socket
    import threading
    import time
    import uvicorn
    from app.main import create_app
    install(world)
    app = create_app(make_settings(**kw))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "service did not start"
    return srv, f"http://127.0.0.1:{port}"
