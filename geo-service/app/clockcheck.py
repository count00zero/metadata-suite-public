"""clockcheck.py -- is the camera clock plausible?  (pure, no I/O)

Three independent checks, each only when its data is there:
  gps-time   the file carries the GPS satellite time (UTC): capture time minus GPS time should be ~0
  track      the file carries its OWN position (camera GPS, not one filled in from tracks): the recorded track must have
             been at that place at the capture time; if not, the time shift (up to 12 h, 5-minute steps) that makes the
             track match is suggested -- whole hours point to a time-zone / summer-time mistake
  sanity     years before 2001, or a time in the future, are camera defaults / unset clocks
"""
from __future__ import annotations

from typing import Optional

from .trackmath import Track, best_shift, haversine_m, position_at

GPS_TOLERANCE_S = 120
TRACK_MISMATCH_M = 2000.0
SANE_FROM = 978307200          # 2001-01-01


def _dur_de(seconds: int) -> str:
    s = abs(int(seconds))
    h, m = s // 3600, (s % 3600) // 60
    return (f"{h} h {m:02d} min" if h else f"{m} min") if s >= 60 else f"{s} s"


def check(epoch: int, lat: Optional[float], lon: Optional[float], gps_epoch: Optional[int], track: Optional[Track],
          now_epoch: int) -> dict:
    issues = []
    if epoch < SANE_FROM:
        issues.append({"type": "sanity", "text_de": "Aufnahmedatum vor 2001 – die Kamera-Uhr war vermutlich nie gestellt"})
    elif epoch > now_epoch + 86400:
        issues.append({"type": "sanity", "text_de": "Aufnahmedatum liegt in der Zukunft – Kamera-Uhr falsch"})
    if gps_epoch is not None:
        delta = int(epoch - gps_epoch)
        if abs(delta) > GPS_TOLERANCE_S:
            issues.append({"type": "gps-time", "shift_s": -delta,
                           "text_de": f"Kamera-Uhr {'geht vor' if delta > 0 else 'geht nach'} um {_dur_de(delta)} gegenüber der GPS-Zeit"})
    if track is not None and len(track) and lat is not None and lon is not None:
        pos = position_at(track, epoch, 900)
        if pos is not None:
            d = haversine_m(lat, lon, pos[0], pos[1])
            if d > TRACK_MISMATCH_M:
                bs = best_shift(track, lat, lon, epoch, max_shift=43200, step=300, max_span=900, near_window=1800)
                if bs and bs["ok"]:
                    shift = int(bs["shift"])
                    hint = " (ganze Stunden: Zeitzone/Sommerzeit?)" if shift % 3600 == 0 else ""
                    issues.append({"type": "track", "shift_s": shift,
                                   "text_de": f"Spur passt zum Foto erst mit {'+' if shift > 0 else '−'}{_dur_de(shift)}{hint}"})
                else:
                    issues.append({"type": "track", "text_de": f"Foto-GPS liegt {d / 1000:.0f} km von der Spur zur Aufnahmezeit entfernt"})
    return {"status": "warn" if issues else "ok", "issues": issues,
            "text_de": "; ".join(i["text_de"] for i in issues) if issues else "ok"}
