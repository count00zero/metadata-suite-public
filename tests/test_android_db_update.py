"""Android device list lives outside the image: update (UTF-16 CSV via file:// URL), reload on change, sanity guard."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ["ANDROID_DEVICES_FILE"] = os.path.join(tmp, "sub", "android_devices.json")
from app.core import devicenames as dn

assert dn.ANDROID_DB_PATH == os.environ["ANDROID_DEVICES_FILE"]
assert not os.path.exists(dn.ANDROID_DB_PATH)
assert dn.translate("samsung", "SM-S921B")["source"] != "google"          # no file -> raw/NFO rule, no crash

rows = ["Retail Branding,Marketing Name,Device,Model"] + [f"Brand{i},Name {i},dev{i},CODE-{i}" for i in range(10050)]
rows.append("Samsung,Galaxy Test,gt,SM-TEST1")
csv_path = os.path.join(tmp, "s.csv")
open(csv_path, "wb").write(b"\xff\xfe" + "\n".join(rows).encode("utf-16-le"))
n = dn.update_android_db(url="file://" + csv_path)
assert n >= 10051 and os.path.exists(dn.ANDROID_DB_PATH), n
assert dn.translate("samsung", "SM-TEST1")["label"].endswith("Galaxy Test"), dn.translate("samsung", "SM-TEST1")

# file changes -> picked up without restart
time.sleep(0.05)
d = json.load(open(dn.ANDROID_DB_PATH)); d["SM-TEST1"] = ["Samsung", "Galaxy Neu"]
json.dump(d, open(dn.ANDROID_DB_PATH, "w")); os.utime(dn.ANDROID_DB_PATH, (time.time() + 5, time.time() + 5))
assert dn.translate("samsung", "SM-TEST1")["label"].endswith("Galaxy Neu")

# a broken/small download keeps the existing file
small = os.path.join(tmp, "small.csv"); open(small, "w").write("a,b,c,d\nx,y,z,CODE\n")
try:
    dn.update_android_db(url="file://" + small); raise SystemExit("FAIL: sanity guard")
except RuntimeError:
    pass
assert json.load(open(dn.ANDROID_DB_PATH))["SM-TEST1"][1] == "Galaxy Neu"
print("PASS")
