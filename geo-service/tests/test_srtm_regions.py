import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
"""GEO_SRTM_PRELOAD region names: comma list, aliases, unknown names reported, tile names right."""
from app import srtmfetch as f

t, unk = f.resolve_regions(["austria"])
assert len(t) == 27 and unk == [], (len(t), unk)
t, unk = f.resolve_regions(["Austria", " Germany ", "Malediven", "sri lanka", "narnia"])
assert unk == ["narnia"], unk
names = {f.name(la, lo) for la, lo in t}
assert {"N47E016", "N52E013", "N04E073", "N07E080", "S01E073"} <= names, sorted(names)[:5]   # Wien, Berlin, Malé, Sri Lanka, south Maldives
assert len(f.REGIONS["sri_lanka"]) < 20 and len(f.REGIONS["maldives"]) < 30
assert len(f.REGIONS["europe"]) > 1000
assert set(f.REGIONS["austria"]) <= set(f.REGIONS["europe"])
assert f.resolve_regions([]) == ([], [])
print("PASS")
