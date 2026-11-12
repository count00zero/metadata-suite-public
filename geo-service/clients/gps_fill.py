#!/usr/bin/env python3
"""gps_fill.py -- give photos/videos without GPS a position from your Traccar/Dawarich track.

    gps_fill.py /mnt/NAS/photo/Urlaube/2026/"2026.08.05-2026.08.22 Sri Lanka & Malediven"     # preview only
    gps_fill.py <folder> -camera "Luna Ultra" -apply                                          # write sidecars
    gps_fill.py <folder> -drift off -min-confidence=0.5 -report /tmp/fill.csv

What it does, per run:
  1. scans the folders (exiftool, batched) and resolves every file's capture time to UTC
     (sidecar date > EXIF+offset > file name; see mediameta.py);
  2. downloads the GPS track for those days once, through geo-service (cached there);
  3. DRIFT: for each camera, files that already carry GPS reveal the clock error --
       gps-time     camera clock minus the EXIF GPS time (satellite time, exact), or
       track-match  the time shift that makes the track pass through the photo's own GPS position
     (per camera and per day, robust median; only used when the samples agree);
  4. for files without GPS: position = track interpolated between the two surrounding points
     at the drift-corrected time, plus a confidence value and an error radius;
  5. -apply: writes <file>.xmp (XMP-exif GPS, place names, geo keywords, provenance in XMP-mon:*).
     The media files themselves are never touched. Without -apply nothing is written except
     the CSV report.

Flags accept both  -flag value  and  -flag=value  (also --flag), like the other scripts.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mediameta as mm                                          # noqa: E402
from geoclient import GeoClient, GeoServiceError                # noqa: E402
from trackmath import Pt, Track, best_shift, locate             # noqa: E402

VERSION = "1.0"
DAY = 86400


# ---------------------------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------------------------
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Fill missing GPS from the GPS track (preview unless -apply).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
                                 allow_abbrev=False)          # -dr must never silently mean -drift or -drift-min
    ap.add_argument("paths", nargs="+", help="folders and/or files")
    ap.add_argument("-d", "-depth", "--depth", type=int, default=7, dest="depth", help="folder depth (default 7)")
    ap.add_argument("-t", "-type", "--type", choices=["all", "image", "video"], default="all", dest="types")
    ap.add_argument("-camera", "--camera", default="", help="only files whose 'Make Model' contains this (case-insensitive)")
    ap.add_argument("-apply", "--apply", action="store_true", help="write the .xmp sidecars (default: preview only)")
    ap.add_argument("-force", "--force", action="store_true", help="also replace GPS that is already in a sidecar")
    ap.add_argument("-retry", "--retry", action="store_true", help="also retry files marked 'no-gps-found' by the suite")
    ap.add_argument("-mark-nogps", "--mark-nogps", action="store_true", dest="mark_nogps",
                    help="with -apply: mark files without any usable track point, like the suite's GPS Sidecar does")
    ap.add_argument("-min-confidence", "--min-confidence", type=float, default=0.3, dest="min_conf",
                    help="don't fill below this confidence (default 0.3)")
    ap.add_argument("-gap", "--gap", type=int, default=None, help="tier-1 gap seconds (default: service GEO_MAX_GAP_SECS=600)")
    ap.add_argument("-gap2", "--gap2", type=int, default=None, help="max gap seconds (default 21600 = 6 h)")
    ap.add_argument("-drift", "--drift", default="auto",
                    help="auto | off | <seconds>: camera clock minus true time, applied to all selected files")
    ap.add_argument("-max-drift", "--max-drift", type=int, default=14 * 3600, dest="max_drift",
                    help="ignore drift samples larger than this (default 14 h)")
    ap.add_argument("-max-shift", "--max-shift", type=int, default=900, dest="max_shift",
                    help="track-match search window in seconds each way (default 900)")
    ap.add_argument("-drift-min", "--drift-min", type=int, default=3, dest="drift_min",
                    help="smaller estimated drift counts as 'clock is fine' (default 3 s)")
    ap.add_argument("-tz", "--tz", default=os.environ.get("MEDIA_TIMEZONE", "Europe/Vienna"),
                    help="zone for files with no offset anywhere (default Europe/Vienna)")
    ap.add_argument("-sources", "--sources", default="traccar,dawarich")
    ap.add_argument("-devices", "--devices", default="default", help="default | all | 7,19 (Traccar device ids)")
    ap.add_argument("-include-import", "--include-import", action="store_true", dest="include_import",
                    help="also use the Dawarich import account (photo-derived points: circular for drift!)")
    ap.add_argument("-service", "--service", default=None, help="geo-service URL (default $GEO_SERVICE_URL)")
    ap.add_argument("-token", "--token", default=None, help="geo-service token (default $GEO_SERVICE_TOKEN)")
    ap.add_argument("-no-reverse", "--no-reverse", action="store_true", dest="no_reverse", help="skip place-name lookup")
    ap.add_argument("-limit", "--limit", type=int, default=0, help="stop after N written files")
    ap.add_argument("-report", "--report", default=None, help="CSV report path (default ./gps_fill_report_<time>.csv)")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------------------------
@dataclass
class Sample:
    camera: str
    day: int
    drift: int                 # camera clock minus true time, seconds
    method: str                # gps-time | track-match
    path: str = ""


@dataclass
class Drift:
    seconds: int = 0
    method: str = "none"
    n: int = 0
    scope: str = ""            # day | camera | manual | ""
    spread: float = 0.0


def _wmedian(samples: list[Sample]) -> tuple[float, float, int]:
    """(median, MAD, weight): satellite-time samples count double -- they are exact, a
    track match is a (good) guess."""
    vals: list[float] = []
    for s in samples:
        vals += [s.drift] * (2 if s.method == "gps-time" else 1)
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    return med, mad, len(vals)


class DriftModel:
    def __init__(self, samples: list[Sample], drift_min: int):
        self.by_cam: dict[str, list[Sample]] = defaultdict(list)
        for s in samples:
            self.by_cam[s.camera].append(s)
        self.drift_min = drift_min

    @staticmethod
    def _tol(med: float) -> float:
        return max(5.0, 0.25 * abs(med))

    def for_file(self, camera: str, epoch: int) -> Drift:
        samples = self.by_cam.get(camera, [])
        day = epoch - epoch % DAY
        same_day = [s for s in samples if s.day == day]
        for scope, group, need in (("day", same_day, 2), ("camera", samples, 3)):
            if not group:
                continue
            med, mad, w = _wmedian(group)
            if w >= need and mad <= self._tol(med):
                sec = int(round(med))
                methods = {s.method for s in group}
                method = "gps-time" if methods == {"gps-time"} else ("track-match" if methods == {"track-match"} else "gps-time+track-match")
                if abs(sec) < self.drift_min:
                    return Drift(0, method, len(group), scope, mad)
                return Drift(sec, method, len(group), scope, mad)
        return Drift()

    def table(self) -> list[dict]:
        rows = []
        for cam, ss in sorted(self.by_cam.items()):
            med, mad, w = _wmedian(ss)
            ok = w >= 3 and mad <= self._tol(med)
            rows.append({"camera": cam, "samples": len(ss), "gps_time": sum(s.method == "gps-time" for s in ss),
                         "track_match": sum(s.method == "track-match" for s in ss), "median_s": int(round(med)),
                         "mad_s": round(mad, 1), "stable": ok, "days": len({s.day for s in ss})})
        return rows


def estimate_drift(media: list[mm.Media], track: Track, args) -> DriftModel:
    samples: list[Sample] = []
    for m in media:
        if not (m.has_exif_gps() and m.epoch is not None):
            continue
        day = m.epoch - m.epoch % DAY
        if m.gps_epoch is not None:
            d = m.epoch - m.gps_epoch
            if abs(d) <= args.max_drift:
                samples.append(Sample(m.camera, day, d, "gps-time", m.path))
            continue
        if len(track):
            r = best_shift(track, m.lat, m.lon, m.epoch, max_shift=args.max_shift)
            if r and r["ok"]:
                samples.append(Sample(m.camera, day, -r["shift"], "track-match", m.path))
    return DriftModel(samples, args.drift_min)


# ---------------------------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------------------------
@dataclass
class Plan:
    media: mm.Media
    status: str
    reason: str = ""
    drift: Drift = field(default_factory=Drift)
    t_true: Optional[int] = None
    loc: Optional[dict] = None
    geo: Optional[dict] = None


def track_windows(epochs: list[int], pad: int) -> list[tuple[int, int]]:
    """Merged [e - pad, e + pad] intervals, so a trip photographed on 14 days of a year costs
    14 small fetches and not 365 days of track."""
    out: list[list[int]] = []
    for e in sorted(set(epochs)):
        lo, hi = e - pad, e + pad
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def plan_files(media: list[mm.Media], track: Track, model: Optional[DriftModel], args, gap1: int, gap2: int) -> list[Plan]:
    manual = None
    if args.drift not in ("auto", "off"):
        try:
            manual = int(float(args.drift))
        except ValueError:
            raise SystemExit(f"-drift: expected auto, off or a number of seconds, got {args.drift!r}")
    plans: list[Plan] = []
    for m in media:
        sc = m.sidecar
        if m.has_exif_gps():
            plans.append(Plan(m, "has-gps")); continue
        if sc and mm.real_gps(sc.lat, sc.lon) and not args.force:
            plans.append(Plan(m, "sidecar-has-gps")); continue
        if sc and sc.marker == mm.NOGPS_MARKER and not args.retry:
            plans.append(Plan(m, "marked-no-gps")); continue
        if m.epoch is None:
            plans.append(Plan(m, "no-time", "no usable capture time")); continue
        if manual is not None:
            drift = Drift(manual, "manual", 0, "manual") if abs(manual) >= args.drift_min else Drift()
        elif model is not None:
            drift = model.for_file(m.camera, m.epoch)
        else:
            drift = Drift()
        t_true = m.epoch - drift.seconds
        loc = locate(track, t_true, gap1, gap2) if len(track) else None
        p = Plan(m, "fill", drift=drift, t_true=t_true, loc=loc)
        if loc is None:
            p.status, p.reason = "no-track-point", f"no track point within {gap2 // 60} min"
        elif loc["confidence"] < args.min_conf:
            p.status, p.reason = "low-confidence", f"confidence {loc['confidence']} < {args.min_conf}"
        plans.append(p)
    return plans


# ---------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------
def sidecar_args(p: Plan, existing_subject: list) -> list[str]:
    """Same tag mapping as the suite's geocore._sidecar_location_args / _geo_keyword_args, plus provenance."""
    loc, g, d = p.loc, p.geo or {}, p.drift
    lat, lon = loc["lat"], loc["lon"]
    a = [f"-XMP-exif:GPSLatitude={lat}", f"-XMP-exif:GPSLongitude={lon}", "-XMP-photoshop:TransmissionReference="]
    for tag, key in (("XMP-photoshop:Country", "country"), ("XMP-iptcCore:CountryCode", "country_a"),
                     ("XMP-photoshop:State", "state"), ("XMP-iptcExt:LocationCreatedProvinceState", "county"),
                     ("XMP-photoshop:City", "city"), ("XMP-iptcCore:Location", "locality"),
                     ("XMP-iptcExt:LocationCreatedSublocation", "street")):
        if g.get(key):
            a.append(f"-{tag}={g[key]}")
    wanted = [f"geo:lat={lat}", f"geo:lon={lon}", "geotagged"]
    for k in ("country_a", "country", "county", "city", "locality", "street"):
        if g.get(k) and g[k] not in wanted:
            wanted.append(g[k])
    for old in existing_subject:                       # a re-fill must not leave the old coordinates behind
        if isinstance(old, str) and old.startswith(("geo:lat=", "geo:lon=")) and old not in wanted:
            a.append(f"-XMP-dc:Subject-={old}")
    for v in wanted:                                   # exiftool's += would duplicate, so only add what's missing
        if v not in existing_subject:
            a.append(f"-XMP-dc:Subject+={v}")
    srcs = sorted({x["source"] for x in (loc["before"], loc["after"]) if x})
    a += [f"-XMP-mon:GpsSource=track-{loc['method']}", f"-XMP-mon:GpsConfidence={loc['confidence']}",
          f"-XMP-mon:GpsGapSeconds={loc['gap_s']}", f"-XMP-mon:GpsUncertaintyM={loc['uncertainty_m']}",
          f"-XMP-mon:TimeBasis={p.media.basis}", f"-XMP-mon:ClockDriftSeconds={d.seconds}",
          f"-XMP-mon:ClockDriftMethod={d.method}", f"-XMP-mon:GpsFilledBy=gps_fill.py/{VERSION}"]
    if srcs:
        a.append(f"-XMP-mon:GpsTrackSources={','.join(srcs)}")
    return a


def write_plan(p: Plan) -> tuple[bool, str]:
    m = p.media
    xmp = mm.sidecar_target(m.path)
    if not os.path.exists(xmp):                        # create like the suite's ensure_xmp_sidecar()
        cmd = mm.exiftool_base() + (["-api", "QuickTimeUTC=0", "-ee"] if m.is_video else []) + ["-o", xmp, m.path]
        subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300)
        if not os.path.exists(xmp):
            return False, "could not create the .xmp sidecar"
    return mm.exiftool_write(sidecar_args(p, m.sidecar.subject if m.sidecar else []), xmp)


# ---------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------
def fmt_dur(s: float) -> str:
    s = int(round(s)); sign = "-" if s < 0 else "+"; s = abs(s)
    return f"{sign}{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{sign}{s // 60}m{s % 60:02d}s" if s >= 60 else f"{sign}{s}s"


def write_report(path: str, plans: list[Plan], written: dict[str, str]) -> None:
    cols = ["path", "status", "written", "camera", "time_basis", "camera_utc", "drift_s", "drift_method", "drift_scope",
            "true_utc", "lat", "lon", "method", "gap_s", "span_s", "confidence", "uncertainty_m", "track_sources", "place", "reason"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for p in plans:
            m, loc, g = p.media, p.loc or {}, p.geo or {}
            srcs = ",".join(sorted({x["source"] for x in (loc.get("before"), loc.get("after")) if x}))
            w.writerow([m.path, p.status, written.get(m.path, ""), m.camera, m.basis, m.epoch or "", p.drift.seconds if p.drift.method != "none" else "",
                        p.drift.method, p.drift.scope, p.t_true or "", loc.get("lat", ""), loc.get("lon", ""), loc.get("method", ""),
                        loc.get("gap_s", ""), loc.get("span_s", ""), loc.get("confidence", ""), loc.get("uncertainty_m", ""),
                        srcs, g.get("label", ""), p.reason])


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
def run(argv=None) -> dict:
    args = parse_args(argv)
    t_start = time.time()
    gc = GeoClient(args.service, args.token)
    try:
        health = gc.health()
    except GeoServiceError as e:
        print(f"geo-service not reachable: {e}", file=sys.stderr)
        raise SystemExit(2)
    gap1, gap2 = args.gap or 600, args.gap2 or 21600

    print(f"Scanning {', '.join(args.paths)} ...", file=sys.stderr)
    media = mm.collect(args.paths, args.depth, args.camera, args.types)
    mm.resolve_times(media, args.tz)
    if not media:
        print("No matching photos/videos found.", file=sys.stderr)
        return {"files": 0}
    timed = [m for m in media if m.epoch is not None]
    print(f"{len(media)} files ({len(timed)} with a usable capture time)"
          + (f", camera filter {args.camera!r}" if args.camera else ""), file=sys.stderr)

    needs_track = [m for m in timed if not m.has_exif_gps() or args.drift == "auto"]
    track = Track([])
    if needs_track:
        pad = gap2 + (args.max_shift if args.drift == "auto" else 0) + 3600
        pts: list[Pt] = []
        try:
            for lo, hi in track_windows([m.epoch for m in needs_track], pad):
                pts += [Pt(e, la, lo_, s) for e, la, lo_, s in gc.track_range(lo, hi, args.sources, args.devices, args.include_import)]
        except GeoServiceError as e:
            print(f"track download failed: {e}", file=sys.stderr)
            raise SystemExit(2)
        track = Track(pts)
        print(f"track: {len(track)} points", file=sys.stderr)

    model = estimate_drift(media, track, args) if args.drift == "auto" else None
    plans = plan_files(media, track, model, args, gap1, gap2)

    # place names for everything that would be written (deduplicated; the service caches too)
    fills = [p for p in plans if p.status == "fill"]
    if not args.no_reverse:
        seen: dict[tuple, Optional[dict]] = {}
        for p in fills:
            key = (round(p.loc["lat"], 4), round(p.loc["lon"], 4))
            if key not in seen:
                try:
                    r = gc.reverse(*key)
                    seen[key] = r if r.get("found") else None
                except GeoServiceError as e:
                    seen[key] = None
                    if args.verbose:
                        print(f"  reverse failed {key}: {e}", file=sys.stderr)
            p.geo = seen[key]

    written: dict[str, str] = {}
    errors = 0
    if args.apply:
        n = 0
        for p in fills:
            if args.limit and n >= args.limit:
                break
            ok, msg = write_plan(p)
            written[p.media.path] = "ok" if ok else f"ERROR {msg}"
            errors += not ok
            n += ok
        if args.mark_nogps:
            for p in plans:
                if p.status == "no-track-point":
                    xmp = mm.sidecar_target(p.media.path)
                    if os.path.exists(xmp):
                        mm.exiftool_write([f"-XMP-photoshop:TransmissionReference={mm.NOGPS_MARKER}"], xmp)

    report = args.report or f"gps_fill_report_{time.strftime('%Y%m%d-%H%M%S')}.csv"
    write_report(report, plans, written)

    # ---- console summary
    print()
    rows = model.table() if model else []
    if rows:
        print("Clock drift per camera (camera clock minus true time):")
        print(f"  {'camera':<34} {'samples':>7} {'gps-time':>8} {'track':>6} {'median':>11} {'spread':>7}  usable")
        for r in rows:
            print(f"  {r['camera'][:34]:<34} {r['samples']:>7} {r['gps_time']:>8} {r['track_match']:>6} "
                  f"{fmt_dur(r['median_s']):>11} {r['mad_s']:>6}s  {'yes' if r['stable'] else 'no (too few / inconsistent)'}")
        print()
    elif args.drift == "auto":
        print("Clock drift: no reference files with GPS for the selected cameras -> assuming clocks are right.\n")
    counts = defaultdict(int)
    for p in plans:
        counts[p.status] += 1
    drifted = [p for p in fills if p.drift.seconds]
    print("Result" + ("" if args.apply else "  (preview -- nothing written, add -apply)") + ":")
    for k in ("fill", "low-confidence", "no-track-point", "no-time", "has-gps", "sidecar-has-gps", "marked-no-gps"):
        if counts.get(k):
            label = {"fill": "would be filled" if not args.apply else "filled", "low-confidence": "skipped, confidence too low",
                     "no-track-point": "no track point in range", "no-time": "no usable capture time", "has-gps": "already have EXIF GPS",
                     "sidecar-has-gps": "sidecar already has GPS (-force to replace)",
                     "marked-no-gps": "marked no-gps (-retry to retry)"}[k]
            print(f"  {counts[k]:>6}  {label}")
    if drifted:
        print(f"  {len(drifted):>6}  of the filled files used a drift correction")
    if args.apply:
        print(f"  {sum(v == 'ok' for v in written.values()):>6}  sidecars written, {errors} errors")
    assumed = sum(1 for p in fills if "tz-assumed" in p.media.basis)
    if assumed:
        print(f"  note: {assumed} filled file(s) rely on the assumed zone {args.tz} (no offset in the files) -- check abroad trips")
    print(f"Report: {report}   ({time.time() - t_start:.1f}s)")
    if args.verbose:
        for p in fills[:50]:
            l = p.loc
            print(f"  {os.path.basename(p.media.path)}  {l['lat']:.5f},{l['lon']:.5f}  {l['method']} gap={l['gap_s']}s conf={l['confidence']} "
                  f"drift={p.drift.seconds}s  {(p.geo or {}).get('label', '')}")
    return {"files": len(media), "counts": dict(counts), "written": written, "errors": errors, "report": report,
            "drift": rows, "plans": plans, "service": health.get("version")}


if __name__ == "__main__":
    out = run()
    sys.exit(1 if out.get("errors") else 0)
