"""Documentation page (/docs/): README rendered at request time, tabs, TOC, table, internal links, nav icon on every page."""
import os
import re
import sys

os.environ.setdefault("MEDIA_ROOT", "/tmp")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)
r = c.get("/docs/")
assert r.status_code == 200, r.status_code
h = r.text
assert "<h2" in h and 'class="doc-toc"' in h and "<table" in h, "README not rendered (headings / toc / tables)"
assert 'class="doc-tab active"' in h and "/docs/?doc=geo" in h
for doc in ("geo", "endpoints"):
    t = c.get("/docs/", params={"doc": doc}).text
    assert 'class="doc-body"' in t and "<h2" in t, doc
# links between the documents stay in the page; unknown doc falls back to the suite README
assert "/docs/?doc=endpoints" in c.get("/docs/", params={"doc": "geo"}).text
assert c.get("/docs/", params={"doc": "nope"}).status_code == 200
# the icon is on every page, left of Info (Info stays last)
for path in ("/", "/timestamp/", "/gps/", "/ai/", "/nfo/", "/collage/", "/gpsfill/", "/ctxfill/", "/mtime/", "/shortvideos/",
             "/year/", "/geooverrides/", "/endpoints/", "/info/", "/docs/"):
    t = c.get(path).text
    i_docs = t.find('title="Dokumentation (README)"')
    i_info = t.find('title="Info')
    assert i_docs != -1 and i_info != -1 and i_docs < i_info, path
print("all ok")
