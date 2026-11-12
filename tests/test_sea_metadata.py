"""Sea + nearest water feature reach XMP, NFO line, keywords and the AI-description context.   python tests/test_sea_metadata.py"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.core import aicore, geocore, nfocore  # noqa: E402

ADRIA = {"name": "Uvala Vrulje", "distance_km": 2.0, "contained_in": [
    {"layer": "iho", "lang": "en", "name": "Adriatic Sea"}, {"layer": "seavox", "lang": "en", "name": "MEDITERRANEAN SEA, EASTERN BASIN"},
    {"layer": "ne_marine", "lang": "de", "name": "Adriatisches Meer"}]}

# 1. the suite's own maritime fallback maps like the geo-service
real = geocore._get_json
try:
    geocore._get_json = lambda url, headers=None: ADRIA
    g = geocore._try_maritime(44.0, 13.5, "http://m", "de")
    assert (g.country, g.locality, g.state) == ("Adriatisches Meer", "Uvala Vrulje", ""), g
    geocore._get_json = lambda url, headers=None: dict(ADRIA, distance_km=63.0)
    g2 = geocore._try_maritime(44.0, 13.5, "http://m", "de")
    assert (g2.country, g2.locality) == ("Adriatisches Meer", "Adriatisches Meer"), "a bay 63 km away is not the place"
    geocore._get_json = lambda url, headers=None: {"name": "Unbekannt"}
    assert geocore._try_maritime(44.0, 13.5, "http://m", "de") is None
finally:
    geocore._get_json = real

# 2. keywords: sea and bay; state (= the sea next to a land country) only without a city
kw = geocore._geo_keyword_args(44.0, 13.5, g)
assert "-XMP-dc:Subject+=Adriatisches Meer" in kw and "-XMP-dc:Subject+=Uvala Vrulje" in kw, kw
gib = geocore.GeoResult(country="Spanien", state="Mittelmeer", locality="Straße von Gibraltar", source="x")
assert "-XMP-dc:Subject+=Mittelmeer" in geocore._geo_keyword_args(35.9, -5.6, gib)
land = geocore.GeoResult(country="Italien", state="Sizilien", city="Messina", source="x")
assert "-XMP-dc:Subject+=Sizilien" not in geocore._geo_keyword_args(38.2, 15.6, land), "land keywords unchanged"

# 3. NFO "Ort:" line
line = nfocore.build_geo_plot_line(nfocore.GeoSidecar(exists=True, country="Adriatisches Meer", locality="Uvala Vrulje"), "yellow")
assert "Adriatisches Meer, Uvala Vrulje" in line, line
line2 = nfocore.build_geo_plot_line(nfocore.GeoSidecar(exists=True, country="Adriatisches Meer", locality="Adriatisches Meer"), "yellow")
assert line2.count("Adriatisches Meer") == 1, line2
line3 = nfocore.build_geo_plot_line(nfocore.GeoSidecar(exists=True, country="Spanien", state="Mittelmeer", locality="Straße von Gibraltar"), "yellow")
assert "Spanien, Mittelmeer, Straße von Gibraltar" in line3, line3
assert "Messina" in nfocore.build_geo_plot_line(nfocore.GeoSidecar(exists=True, country="Italien", state="Sizilien", city="Messina", locality="Ganzirri"), "y")

# 4. real sidecar round trip: XMP written, NFO reader and AI context read the same values back
if shutil.which("exiftool"):
    d = tempfile.mkdtemp()
    f = os.path.join(d, "IMG_1.jpg")
    open(f, "wb").write(b"\xff\xd8\xff\xd9")        # placeholder: the values are read from the .xmp sidecar
    xmp = f + ".xmp"
    ok, msg = geocore.write_location_to_xmp(xmp, 44.0, 13.5, g)
    assert ok, msg
    sc = nfocore.read_geo_sidecar(xmp)
    assert (sc.country, sc.locality) == ("Adriatisches Meer", "Uvala Vrulje"), sc
    assert aicore.build_ai_geo_context_from_existing(f) == "Adriatisches Meer, Uvala Vrulje"
    ok, msg = geocore.write_location_to_xmp(xmp, 35.9, -5.6, gib)
    assert ok, msg
    assert aicore.build_ai_geo_context_from_existing(f) == "Spanien, Mittelmeer, Straße von Gibraltar"
    ok, msg = geocore.write_location_to_xmp(xmp, 38.2, 15.6, land)
    assert aicore.build_ai_geo_context_from_existing(f) == "Italien, Sizilien, Messina"
else:
    print("SKIP: exiftool missing (round trip)")
# a sight the photo is most likely of joins the "Ort:" line (once)
g = nfocore.GeoSidecar(exists=True, country="Österreich", city="Wien", sight="Stephansdom")
assert "Wien, Stephansdom" in nfocore.build_geo_plot_line(g, "yellow")
g2 = nfocore.GeoSidecar(exists=True, country="Österreich", city="Wien", street="Stephansplatz", sight="Stephansplatz")
assert nfocore.build_geo_plot_line(g2, "yellow").count("Stephansplatz") == 1
print("all passed")
