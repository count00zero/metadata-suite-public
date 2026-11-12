"""caption_context: trip name from the folder, place/time/weather from the service, offline fallback.
    PATH needs exiftool + ffmpeg.      python clients/tests/test_caption_context.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
for p in (os.path.join(ROOT, "tests"), os.path.join(ROOT, "clients"), ROOT):
    sys.path.insert(0, p)

if not (shutil.which("exiftool") and shutil.which("ffmpeg")):
    print("SKIP: exiftool/ffmpeg not on PATH"); sys.exit(0)

from fakes import Resp, World, photon_graz, serve           # noqa: E402
import caption_context as cc                                # noqa: E402
from geoclient import GeoClient                             # noqa: E402

T0 = int(datetime(2026, 10, 3, 8, 0, 0, tzinfo=timezone.utc).timestamp())     # 10:00 Vienna


def test_trip_name():
    f = cc.trip_name
    assert f("/mnt/NAS/photo/Urlaube/2026/2026.08.05-2026.08.22 Sri Lanka & Malediven/a.jpg") == "Sri Lanka & Malediven"
    assert f("/mnt/NAS/photo/Urlaube/2026/2026.08.05-2026.08.22 Sri Lanka & Malediven/Tag 3/x/a.jpg") == "Sri Lanka & Malediven"
    assert f("/mnt/NAS/photo/2026.10.03 Wandern Hochschwab/a.jpg") == "Wandern Hochschwab"
    assert f("/mnt/NAS/photo/2026/2026.07/a.jpg") == ""                 # month folder: no trip
    assert f("/mnt/NAS/photo/2026.07.29/a.jpg") == ""                   # day folder without a name
    assert f("/a.jpg") == ""


def test_extra_de():
    ctx = {"place": {"label": "Graz, Steiermark, Österreich"}, "daypart_hour": 10, "month_de": "Oktober", "year": 2026,
           "weather": {"summary_de": "Regen", "temp_c": 11}, "trip": "Testreise"}
    assert cc.extra_de(ctx) == "vormittags im Oktober 2026; Regen bei 11 °C; Wo wars: „Testreise“"
    assert cc.extra_de(ctx, with_place=True).startswith("Graz, Steiermark, Österreich; vormittags")
    assert cc.extra_de({"trip": ""}) == ""


def make(folder, name, *exif_args):
    path = os.path.join(folder, name)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=48x48",
                    "-frames:v", "1", path], check=True)
    subprocess.run(["exiftool", "-overwrite_original", "-q", *exif_args, path], check=True, capture_output=True)
    return path


def main():
    test_trip_name(); test_extra_de(); print("PASS pure functions")
    w = World(); w.on("http://photon", photon_graz)
    hourly = {"time": [f"2026-10-03T{h:02d}:00" for h in range(24)], "weather_code": [63] * 24,
              "temperature_2m": [11.4] * 24, "precipitation": [1.0] * 24, "cloud_cover": [90] * 24}
    w.on("open-meteo.com", lambda u: Resp(200, {"hourly": hourly}))
    srv, url = serve(w, weather_provider="open-meteo")
    base = tempfile.mkdtemp(prefix="capctx-")
    folder = os.path.join(base, "2026.10.03-2026.10.05 Testreise")
    os.makedirs(folder)
    local = "2026:10:03 10:00:00"
    with_gps = make(folder, "a.jpg", f"-DateTimeOriginal={local}", "-OffsetTimeOriginal=+02:00",
                    "-GPSLatitude=47.07", "-GPSLatitudeRef=N", "-GPSLongitude=15.44", "-GPSLongitudeRef=E")
    gc = GeoClient(url)

    ctx = cc.build(with_gps, gc)
    assert ctx["source"] == "service" and ctx["place"]["city"] == "Graz", ctx
    assert ctx["text_de"] == "Aufgenommen in Graz (Steiermark, Österreich), vormittags im Oktober 2026; Regen bei 11 °C. Reise: Testreise.", ctx["text_de"]
    assert ctx["extra_de"] == "vormittags im Oktober 2026; Regen bei 11 °C; Wo wars: „Testreise“", ctx["extra_de"]
    assert "Graz" not in ctx["extra_de"], "the prompt already carries the place -- extra must not repeat it"
    assert cc.build(with_gps, gc, weather=False)["weather"] is None

    sidecar_only = make(folder, "b.jpg", f"-DateTimeOriginal={local}", "-OffsetTimeOriginal=+02:00")
    subprocess.run(["exiftool", "-q", "-o", sidecar_only + ".xmp", sidecar_only], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-XMP-exif:GPSLatitude=47.07", "-XMP-exif:GPSLongitude=15.44",
                    sidecar_only + ".xmp"], check=True)
    assert cc.build(sidecar_only, gc)["place"]["city"] == "Graz", "GPS from the .xmp sidecar must be used"

    dead = GeoClient("http://127.0.0.1:9", timeout=2)               # service down: caption must not fail
    off = cc.build(with_gps, dead)
    assert off["source"] == "local" and off["place"] is None, off
    assert off["text_de"] == "Aufgenommen, vormittags im Oktober 2026. Reise: Testreise.", off["text_de"]
    assert off["extra_de"] == "vormittags im Oktober 2026; Wo wars: „Testreise“"

    no_gps = make(folder, "c.jpg", f"-DateTimeOriginal={local}", "-OffsetTimeOriginal=+02:00")
    n = cc.build(no_gps, gc)
    assert n["place"] is None and n["extra_de"].startswith("vormittags im Oktober 2026"), n
    no_time = make(folder, "d.jpg")
    nt = cc.build(no_time, gc)
    assert nt["epoch"] is None and nt["text_de"] == "Reise: Testreise.", nt
    srv.should_exit = True
    shutil.rmtree(base, ignore_errors=True)
    print("PASS caption_context")


if __name__ == "__main__":
    main()
