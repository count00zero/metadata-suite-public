"""AI-Beschreibung preview lists every context passed to the prompt, not only the place."""
import os, sys, tempfile, html
from fastapi.testclient import TestClient
d = tempfile.mkdtemp(); os.environ["MEDIA_ROOT"] = d
from app.main import app
from app.modules.ai_description import router as r
from app.core import geoservice

items = r.context_items({"daypart": "mittags", "local_time": "12:24", "temp_c": 11, "weather": "Regen", "trip": "Test", "year": 2026})
assert ("Tageszeit", "mittags") in items and ("Temperatur", "11 °C") in items and ("Wo wars", "Test") in items, items
assert r.context_items({}) == []

t = r.templates.get_template("_scan_list.html")
out = t.render(rows=[{"path": "/x/a.jpg", "filename": "a.jpg", "geo_context": "Pottenstein", "existing_description": "alt",
                      "context_items": items, "extra_context": "x"}], request=None)
assert "Weitere Kontexte" in out and "Tageszeit: mittags" in out and "Wetter: Regen" in out, out
f = r.templates.get_template("_file_list.html")
o2 = f.render(rows=[{"path": "/x/a.jpg", "filename": "a.jpg", "geo_context": "P", "new_description": "n",
                     "context_items": items}], request=None)
assert "Wochentag" not in o2 and "Temperatur: 11" in o2
print("PASS")
