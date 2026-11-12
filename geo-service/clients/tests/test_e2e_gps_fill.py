"""End to end: real exiftool + real files + the real FastAPI service over HTTP (only the upstream
Traccar/Photon servers are faked).   PATH must contain exiftool and ffmpeg.

    python clients/tests/test_e2e_gps_fill.py
"""
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, os.path.join(ROOT, "clients"))
sys.path.insert(0, ROOT)

if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg not on PATH"); sys.exit(0)

import uvicorn                                              # noqa: E402
from fakes import Resp, World, gpx, install, make_settings, photon_graz  # noqa: E402
from app.main import create_app                             # noqa: E402
from app.trackmath import haversine_m                       # noqa: E402
import gps_fill                                             # noqa: E402
import mediameta as mm                                      # noqa: E402

T0 = int(datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc).timestamp())      # 10:00 in Vienna
M_PER_DEG_LON = 111_320 * math.cos(math.radians(47.0))


def true_pos(t):                                            # walking east at 5 m/s, track point every 10 s
    return 47.0, round(15.0 + 5.0 * (t - T0) / M_PER_DEG_LON, 7)


def utc(t, fmt="%Y:%m:%d %H:%M:%S"):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime(fmt)


def local(t):
    return utc(t + 7200)                                    # CEST


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def exif(path, *args):
    return subprocess.run(["exiftool", "-config", mm.CONFIG, "-charset", "filename=utf8", *args, path],
                          capture_output=True, text=True, encoding="utf-8").stdout.strip()


def make_photo(folder, name, make, model, cam_clock_epoch, *, offset="+02:00", gps_true_epoch=None, gps_at=None):
    """cam_clock_epoch = what the camera clock showed (as UTC epoch); gps_* adds EXIF GPS."""
    path = os.path.join(folder, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=blue:s=48x48:d=1,format=yuvj420p",
                    "-frames:v", "1", path], check=True)
    args = [f"-Make={make}", f"-Model={model}"]
    if cam_clock_epoch is not None:
        args.append(f"-DateTimeOriginal={local(cam_clock_epoch)}")
        if offset:
            args.append(f"-OffsetTimeOriginal={offset}")
    if gps_at is not None:
        lat, lon = true_pos(gps_at)
        args += [f"-GPSLatitude={lat}", "-GPSLatitudeRef=N", f"-GPSLongitude={lon}", "-GPSLongitudeRef=E"]
        if gps_true_epoch is not None:
            args += [f"-GPSDateStamp={utc(gps_true_epoch, '%Y:%m:%d')}", f"-GPSTimeStamp={utc(gps_true_epoch, '%H:%M:%S')}"]
    subprocess.run(["exiftool", "-overwrite_original", "-q", *args, path], check=True, capture_output=True)
    return path


def start_service():
    world = World()
    points = [(t, *true_pos(t)) for t in range(T0, T0 + 7201, 10)]

    def traccar(url):                                       # honour from/to like the real one
        q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
        lo = int(datetime.strptime(q["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        hi = int(datetime.strptime(q["to"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
        return Resp(200, content=gpx([p for p in points if lo <= p[0] <= hi]))

    world.on("http://traccar/api/positions/gpx", traccar)
    world.on("http://photon", photon_graz)
    install(world)
    app = create_app(make_settings())
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "service did not start"
    return srv, port, world


def build_library():
    base = tempfile.mkdtemp(prefix="gpsfill-")
    folder = os.path.join(base, "2026.10.03-2026.10.05 Testreise")
    os.makedirs(folder)
    files = {}
    # Alpha: clock 120 s AHEAD of satellite time; 3 reference photos carry GPS + GPSDateTime, 3 need a fill
    for i, t in enumerate((T0 + 600, T0 + 1200, T0 + 1800)):
        files[f"alpha_ref{i}"] = make_photo(folder, f"A_ref{i}.jpg", "Alpha", "One", t + 120, gps_true_epoch=t, gps_at=t)
    for i, t in enumerate((T0 + 2400, T0 + 2405, T0 + 3000)):
        files[f"alpha_fill{i}"] = make_photo(folder, f"A_fill{i}.jpg", "Alpha", "One", t + 120)
    # Beta: clock 60 s BEHIND; reference photos have GPS but NO GPS time -> drift only visible via the track
    for i, t in enumerate((T0 + 700, T0 + 1300, T0 + 1900, T0 + 2500)):
        files[f"beta_ref{i}"] = make_photo(folder, f"B_ref{i}.jpg", "Beta", "Two", t - 60, gps_at=t)
    for i, t in enumerate((T0 + 3300, T0 + 3600, T0 + 3900)):
        files[f"beta_fill{i}"] = make_photo(folder, f"B_fill{i}.jpg", "Beta", "Two", t - 60)
    # Gamma: no reference files; one file with only the file name (local time, no offset anywhere)
    files["gamma_fill0"] = make_photo(folder, "G_fill0.jpg", "Gamma", "Three", T0 + 4200)
    files["gamma_name"] = make_photo(folder, f"{local(T0 + 4500).replace(':', '.').replace(' ', '-')}.jpg", "Gamma", "Three", None)
    # too late for any track point (6 h gap limit)
    files["alpha_late"] = make_photo(folder, "A_late.jpg", "Alpha", "One", T0 + 7200 + 40000)
    # already has GPS in its sidecar
    files["has_sidecar"] = make_photo(folder, "S_has.jpg", "Gamma", "Three", T0 + 5000)
    subprocess.run(["exiftool", "-q", "-o", files["has_sidecar"] + ".xmp", files["has_sidecar"]], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSLatitude=10.5", "-XMP-exif:GPSLongitude=20.5",
                    files["has_sidecar"] + ".xmp"], check=True)
    return base, folder, files


def run(args):
    return gps_fill.run(args)


def main():
    srv, port, world = start_service()
    base, folder, files = build_library()
    url = f"http://127.0.0.1:{port}"
    report = os.path.join(base, "report.csv")
    before = {k: sha(v) for k, v in files.items()}
    common = ["-service", url, "-report", report]

    # ---- 1. preview writes nothing but the report ------------------------------------------------
    out = run([folder, *common])
    assert out["files"] == len(files), out["files"]
    assert not any(os.path.exists(p + ".xmp") for k, p in files.items() if k != "has_sidecar"), "preview must not write sidecars"
    assert os.path.exists(report)
    drift = {r["camera"]: r for r in out["drift"]}
    assert drift["Alpha One"]["gps_time"] == 3 and abs(drift["Alpha One"]["median_s"] - 120) <= 1, drift
    assert drift["Beta Two"]["track_match"] == 4 and abs(drift["Beta Two"]["median_s"] + 60) <= 10 and drift["Beta Two"]["stable"], drift
    assert "Gamma Three" not in drift, "no reference files -> no drift row"
    st = out["counts"]
    assert st["fill"] == 8, st                       # 3 alpha + 3 beta + 2 gamma
    assert st["no-track-point"] == 1 and st["sidecar-has-gps"] == 1 and st["has-gps"] == 7, st

    # ---- 2. apply ---------------------------------------------------------------------------------
    out = run([folder, *common, "-apply"])
    assert out["errors"] == 0 and sum(v == "ok" for v in out["written"].values()) == 8, out["written"]
    for k, p in files.items():
        assert sha(p) == before[k], f"media file {k} was modified!"
    # Alpha fill photos: true time = clock - 120 s -> must land exactly on the track at the TRUE time
    for k, t in (("alpha_fill0", T0 + 2400), ("alpha_fill1", T0 + 2405), ("alpha_fill2", T0 + 3000)):
        x = json.loads(exif(files[k] + ".xmp", "-json", "-n", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude"))[0]
        d = haversine_m(x["GPSLatitude"], x["GPSLongitude"], *true_pos(t))
        wrong = haversine_m(x["GPSLatitude"], x["GPSLongitude"], *true_pos(t + 120))
        assert d < 2.0, (k, d)
        assert wrong > 500, "uncorrected clock would be ~600 m off -- the correction did not happen"
    for k, t in (("beta_fill0", T0 + 3300), ("beta_fill1", T0 + 3600)):       # clock 60 s behind -> true = clock + 60
        x = json.loads(exif(files[k] + ".xmp", "-json", "-n", "-XMP-exif:GPSLatitude", "-XMP-exif:GPSLongitude"))[0]
        assert haversine_m(x["GPSLatitude"], x["GPSLongitude"], *true_pos(t)) < 15.0, k      # grid step 10 s = 50 m max; median is exact

    x = json.loads(exif(files["alpha_fill1"] + ".xmp", "-json", "-XMP-mon:all", "-XMP-photoshop:City",
                        "-XMP-iptcCore:CountryCode", "-XMP-dc:Subject", "-XMP-photoshop:TransmissionReference"))[0]
    assert x["GpsSource"] == "track-interpolated" and x["ClockDriftSeconds"] == 120 and x["ClockDriftMethod"] == "gps-time", x
    assert x["GpsTrackSources"] == "Traccar" and x["TimeBasis"] == "exif+offset" and x["GpsGapSeconds"] == 5, x
    assert 0.5 < x["GpsConfidence"] <= 1.0 and x["GpsFilledBy"].startswith("gps_fill.py/"), x
    assert x["City"] == "Graz" and x["CountryCode"] == "AUT", x
    assert x["Subject"].count("geotagged") == 1 and sum(s.startswith("geo:lat=") for s in x["Subject"]) == 1, x
    assert "TransmissionReference" not in x
    assert exif(files["alpha_fill0"] + ".xmp", "-s3", "-XMP-mon:GpsSource") == "track-exact"      # sits exactly on a track point
    gamma = exif(files["gamma_name"] + ".xmp", "-s", "-XMP-mon:TimeBasis", "-XMP-mon:ClockDriftMethod")
    assert "filename+dir-offset" in gamma and "none" in gamma, gamma                    # folder-wide offset borrowed, no drift data
    assert not os.path.exists(files["alpha_late"] + ".xmp")
    assert exif(files["has_sidecar"] + ".xmp", "-n", "-s3", "-XMP-exif:GPSLatitude") == "10.5", "existing sidecar GPS was overwritten"

    # ---- 3. idempotent: a second run fills nothing, duplicates nothing -------------------------------
    out2 = run([folder, *common, "-apply"])
    assert out2["counts"].get("fill", 0) == 0 and out2["counts"]["sidecar-has-gps"] == 9, out2["counts"]

    # ---- 4. -force replaces cleanly (one geo:lat keyword, not two) ----------------------------------------
    run([folder, *common, "-apply", "-force", "-camera", "Alpha"])
    subj = exif(files["alpha_fill0"] + ".xmp", "-s3", "-XMP-dc:Subject")
    assert subj.count("geo:lat=") == 1 and subj.count("geotagged") == 1, subj

    # ---- 5. camera filter is applied everywhere (scan, drift, fill) ---------------------------------
    o3 = run([folder, *common, "-camera=Gamma"])
    assert {p.media.camera for p in o3["plans"]} == {"Gamma Three"} and o3["files"] == 3, (o3["files"],)
    o4 = run([folder, *common, "-drift", "off", "-camera", "alpha"])
    assert {p.media.camera for p in o4["plans"]} == {"Alpha One"} and not o4["drift"], "case-insensitive filter, no drift model with -drift off"

    # ---- 6. manual drift ---------------------------------------------------------------------------------------
    o5 = run([folder, *common, "-camera", "Beta", "-drift=-60", "-force"])
    f5 = [p for p in o5["plans"] if p.status == "fill"]
    assert f5 and all(p.drift.seconds == -60 and p.drift.method == "manual" for p in f5)

    # ---- 7. a dead service is a clean error, not a traceback ------------------------------------------------
    try:
        run([folder, "-service", "http://127.0.0.1:9", "-report", report])
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert e.code == 2

    srv.should_exit = True
    shutil.rmtree(base, ignore_errors=True)
    print("PASS e2e gps_fill (", len(files), "files )")


if __name__ == "__main__":
    main()
