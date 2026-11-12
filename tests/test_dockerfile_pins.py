"""The tool pins must agree everywhere (Dockerfile stage 1, stage 2, compose, README) and the build must assert them."""
import os, re
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
df = open(os.path.join(root, "Dockerfile")).read()
dm = open(os.path.join(root, "Dockerfile.media")).read()      # ffmpeg + mediainfo are compiled there, once, and kept as an image
bs = open(os.path.join(root, "build_start.sh")).read()
dc = open(os.path.join(root, "docker-compose.yml")).read()
rd = open(os.path.join(root, "README.md")).read()
for name in ("FFMPEG_VERSION", "MEDIAINFO_VERSION", "EXIFTOOL_VERSION"):
    d_defaults = set(re.findall(rf"^ARG {name}=([\w.]+)", df + dm, re.M))
    assert len(d_defaults) == 1, (name, d_defaults)            # same default in every stage and in Dockerfile.media
    (v,) = d_defaults
    m = re.search(rf"{name}: \$\{{{name}:-([\w.]+)\}}", dc)
    assert m and m.group(1) == v, (name, v, m and m.group(1))   # compose default == Dockerfile default
    assert v in rd, (name, v)                                   # documented
    assert f"${{{name}}}" in (dm if name != "EXIFTOOL_VERSION" else df)    # used (download URL / assertion)
# Dockerfile.media asserts ffmpeg, ffprobe and mediainfo; stage 2 of the app Dockerfile re-checks after the copy
assert dm.count('= "${FFMPEG_VERSION}"') >= 2 and dm.count('grep -F "v${MEDIAINFO_VERSION}"') >= 1
assert df.count('= "${FFMPEG_VERSION}"') >= 1 and df.count('grep -F "v${MEDIAINFO_VERSION}"') >= 1
assert "FROM scratch" in dm and "COPY --from=build /opt/media/bin/ /opt/media/bin/" in dm
assert df.count("FROM python:3.13-slim") == 1 and dm.count("FROM python:3.13-slim") == 1
assert "FROM ${MEDIA_IMAGE} AS media-build" in df and "make install" not in df.split("FROM python:3.13-slim")[0]
tag = f"metadata-suite-media:ffmpeg${{FFMPEG_VERSION:-{re.search(r'^ARG FFMPEG_VERSION=([\w.]+)', dm, re.M).group(1)}}}-mediainfo"
assert tag in dc and dc.count("MEDIA_IMAGE: " + tag) == 1 and "image: " + tag in dc     # app build arg == tag of the media-tools image
assert re.search(r"^ARG MEDIA_IMAGE=metadata-suite-media:ffmpeg%s-mediainfo%s$" % tuple(re.findall(r"^ARG (?:FFMPEG|MEDIAINFO)_VERSION=([\w.]+)", dm, re.M)), df, re.M)
em = open(os.path.join(root, "ensure_media_image.sh")).read()
assert "ensure_media_image.sh" in bs and "docker load -i" in em and "docker save" in em and "docker image inspect" in em and "build media-tools" in em   # tag missing -> load tar -> else build once + save
assert "ffmpeg mediainfo" not in df                              # no distro packages any more
# modern ffmpeg: no deprecated -vframes left in the code
for sub in ("core/nfocore.py", "core/aicore.py"):
    assert "-vframes" not in open(os.path.join(root, "app", sub)).read(), sub
print("PASS")
