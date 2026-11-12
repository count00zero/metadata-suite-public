"""Clock plausibility checks.   python tests/test_clockcheck.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.clockcheck import check  # noqa: E402
from app.trackmath import Pt, Track  # noqa: E402

NOW = 1_790_000_000
T0 = 1_780_000_000


def check_(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if not cond else ""))
    if not cond:
        sys.exit(1)


# a walk: 10 m/s northwards for 6 hours, a fix every 60 s
track = Track([Pt(T0 + i * 60, 47.0 + i * 60 * 10 / 111195.0, 15.0, "T") for i in range(-300, 420)])
at = lambda t: (47.0 + (t - T0) * 10 / 111195.0, 15.0)

ok = check(T0, *at(T0), T0 + 5, track, NOW)
check_("everything fine", ok["status"] == "ok" and ok["text_de"] == "ok", ok)
r = check(T0, None, None, T0 - 3720, track, NOW)
check_("GPS time says the camera is 62 min ahead", r["status"] == "warn" and r["issues"][0]["type"] == "gps-time"
       and r["issues"][0]["shift_s"] == -3720 and "1 h 02 min" in r["text_de"] and "geht vor" in r["text_de"], r)
r = check(T0 + 1800, *at(T0 + 1800 - 3600), None, track, NOW)       # camera shows +1 h: true time is T0+1800-3600
check_("track says: 1 h off (time-zone mistake)", r["status"] == "warn" and r["issues"][0]["type"] == "track"
       and r["issues"][0]["shift_s"] == -3600 and "Zeitzone" in r["text_de"], r)
r = check(T0, 10.0, 10.0, None, track, NOW)
check_("position far from any plausible shift is reported as plain mismatch", r["status"] == "warn" and "km" in r["text_de"], r)
r = check(946684800, None, None, None, None, NOW)
check_("year 2000 = clock never set", r["issues"][0]["type"] == "sanity" and "vor 2001" in r["text_de"], r)
r = check(NOW + 10 * 86400, None, None, None, None, NOW)
check_("future date", "Zukunft" in r["text_de"], r)
check_("no data -> ok", check(T0, None, None, None, None, NOW)["status"] == "ok")
check_("no track at that time -> no judgement", check(T0 + 10 * 86400, 47.0, 15.0, None, track, NOW)["status"] == "ok")
print("\nall clock tests passed")
