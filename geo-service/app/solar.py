"""solar.py -- sun height and moon phase for a place and moment (offline, no dependencies).

Sun: NOAA's low-precision algorithm (about 0.01 degrees), enough to tell golden hour from night.
Moon: elongation from the low-precision lunar/solar longitudes (about 1 degree), i.e. phase names and
illumination are right to within a few percent.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

rad, deg = math.radians, math.degrees


def _jd(epoch: float) -> float:
    return epoch / 86400.0 + 2440587.5


def sun_position(lat: float, lon: float, epoch: float) -> tuple:
    """(elevation in degrees, hour angle in degrees: < 0 before solar noon, > 0 after)."""
    jc = (_jd(epoch) - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = (math.sin(rad(m)) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(rad(2 * m)) * (0.019993 - 0.000101 * jc) + math.sin(rad(3 * m)) * 0.000289)
    true_long = l0 + c
    app_long = true_long - 0.00569 - 0.00478 * math.sin(rad(125.04 - 1934.136 * jc))
    obliq = 23 + (26 + ((21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813)))) / 60) / 60
    obliq += 0.00256 * math.cos(rad(125.04 - 1934.136 * jc))
    decl = deg(math.asin(math.sin(rad(obliq)) * math.sin(rad(app_long))))
    y = math.tan(rad(obliq / 2)) ** 2
    eqt = 4 * deg(y * math.sin(2 * rad(l0)) - 2 * ecc * math.sin(rad(m)) + 4 * ecc * y * math.sin(rad(m)) * math.cos(2 * rad(l0))
                  - 0.5 * y * y * math.sin(4 * rad(l0)) - 1.25 * ecc * ecc * math.sin(2 * rad(m)))
    utc_min = (epoch % 86400) / 60.0
    true_solar_min = (utc_min + eqt + 4 * lon) % 1440
    ha = true_solar_min / 4 - 180
    cos_zen = (math.sin(rad(lat)) * math.sin(rad(decl)) + math.cos(rad(lat)) * math.cos(rad(decl)) * math.cos(rad(ha)))
    elev = 90 - deg(math.acos(max(-1.0, min(1.0, cos_zen))))
    return elev, ha


def daylight(lat: float, lon: float, epoch: float) -> dict:
    """{'elevation': 3.2, 'phase': 'goldene Stunde', 'side': 'abends'|'morgens'|'', 'label': 'goldene Stunde (abends)'}.
    Photographers' bands: > 6 deg Tageslicht, -4..6 goldene Stunde, -8..-4 blaue Stunde, -18..-8 Dämmerung, below Nacht."""
    e, ha = sun_position(lat, lon, epoch)
    side = "abends" if ha > 0 else "morgens"
    if e > 6:
        phase, side = "Tageslicht", ""
    elif e > -4:
        phase = "goldene Stunde"
    elif e > -8:
        phase = "blaue Stunde"
    elif e > -18:
        phase = "Dämmerung"
    else:
        phase, side = "Nacht", ""
    return {"elevation": round(e, 1), "phase": phase, "side": side, "label": f"{phase} ({side})" if side else phase}


MOON_NAMES = ((22.5, "Neumond"), (67.5, "zunehmende Sichel"), (112.5, "erstes Viertel"), (157.5, "zunehmender Mond"),
              (202.5, "Vollmond"), (247.5, "abnehmender Mond"), (292.5, "letztes Viertel"), (337.5, "abnehmende Sichel"))


def moon(epoch: float) -> dict:
    """{'phase': 'Vollmond', 'illumination': 99, 'age_days': 14.8, 'label': 'Vollmond (99 %)', 'angle': 181.2}."""
    d = _jd(epoch) - 2451545.0
    lm = (218.316 + 13.176396 * d) % 360
    mm = rad((134.963 + 13.064993 * d) % 360)
    moon_lon = lm + 6.289 * math.sin(mm)
    ls = (280.460 + 0.9856474 * d) % 360
    g = rad((357.528 + 0.9856003 * d) % 360)
    sun_lon = ls + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)
    angle = (moon_lon - sun_lon) % 360
    illum = round((1 - math.cos(rad(angle))) / 2 * 100)
    name = "Neumond"
    for limit, n in MOON_NAMES:
        if angle < limit:
            name = n
            break
    return {"phase": name, "illumination": illum, "age_days": round(angle / 360 * 29.530589, 1),
            "label": f"{name} ({illum} %)", "angle": round(angle, 1)}


def notable_moon_de(m: dict) -> str:
    """'bei Vollmond' / 'bei Neumond' / '' -- only the phases a caption would mention."""
    if m["illumination"] >= 97:
        return "bei Vollmond"
    if m["illumination"] <= 2:
        return "bei Neumond"
    return ""
