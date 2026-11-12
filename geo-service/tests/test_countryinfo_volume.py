"""countryInfo.txt comes from the GeoNames volume; nothing is bundled.   python tests/test_countryinfo_volume.py"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakes import World, client  # noqa: E402

here = os.path.dirname(os.path.abspath(__file__))
assert not os.path.exists(os.path.join(os.path.dirname(here), "app", "data", "countryInfo.txt")), "file must not be in the image"
d = tempfile.mkdtemp()
shutil.copy(os.path.join(here, "data", "countryInfo.txt"), os.path.join(d, "countryInfo.txt"))
ok = client(World(), gn_country_file=os.path.join(d, "countryInfo.txt")).get("/countryInfoJSON?country=AT").json()
assert ok["geonames"][0]["countryName"] == "Austria", ok
missing = client(World(), gn_country_file="/geonames/countryInfo.txt")
r = missing.get("/countryInfoJSON?country=AT").json()
assert "status" in r and "GEONAMES_HOST_DIR" in r["status"]["message"], r
assert missing.get("/health").status_code == 200 and missing.get("/timezoneJSON?lat=47&lng=15").status_code == 200
print("ok   countryInfo from the volume, friendly error without it, service keeps running")
