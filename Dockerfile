# Metadata Suite — Python 3.13 to match the rest of the maintainer's GEOCODING toolchain
# (PYTHON_BIN="python3.13" in env_set_GPS), plus exiftool for metadata read/write,
# ffmpeg for the AI Description module (video frame extraction + image resize before
# sending to Ollama/Gemini -- same two-tier resize the CLI's generate_ai_description.py
# used) and for NFO Maker's own poster/fanart/thumb/folder.jpg extraction, and
# mediainfo for NFO Maker's video stream metadata (width/height/codec/duration/bitrate/
# framerate/audio format -- same tool HomeVideo_NFO_Maker_new.sh itself shells out to).
# ---- stage 1: ffmpeg + ffprobe + mediainfo -- NOT compiled here ------------------------------------
# They come from the separate image built from Dockerfile.media (tag = the two versions), which build_start.sh
# builds once when it is missing. Cache prunes / image cleanups therefore never trigger the long compile.
# The tag follows FFMPEG_VERSION / MEDIAINFO_VERSION (compose passes MEDIA_IMAGE); stage 2 below still verifies the versions.
#   missing image ("pull access denied") -> docker compose --profile tools build media-tools   (or ./build_start.sh)
ARG MEDIA_IMAGE=metadata-suite-media:ffmpeg9.0.2-mediainfo26.10
FROM ${MEDIA_IMAGE} AS media-build

# ---- stage 2: the app image -------------------------------------------------------------------
FROM python:3.13-slim
# exiftool: NOT from apt -- Debian's libimage-exiftool-perl lags far behind the current release
# (12.x vs 13.x). Built from the official
# source tag instead, pinned by EXIFTOOL_VERSION; the build FAILS if `exiftool -ver` reports
# anything else, so the image can never silently contain a different version.
#   upgrade:  docker compose build --build-arg EXIFTOOL_VERSION=13.60   (or edit the ARG)
#   check:    docker compose exec metadata-suite exiftool -ver
# Optional: EXIFTOOL_SHA256=<sha256 of the GitHub tag tarball> pins the download content too.
ARG EXIFTOOL_VERSION=13.59
ARG EXIFTOOL_SHA256=""

ARG FFMPEG_VERSION=9.0.2
ARG MEDIAINFO_VERSION=26.10

# exiftran (apt): lossless JPEG rotation for the Datei-Zeitstempel module (core/rotatecore.py).
# ffmpeg/ffprobe/mediainfo come from stage 1 (static except libdav1d/zlib/libstdc++, installed below;
# the glob 'libdav1d[0-9]*' picks the runtime package whatever its soname number is on this Debian).
COPY --from=media-build /opt/media/bin/ /usr/local/bin/

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        perl make curl ca-certificates libarchive-zip-perl libio-compress-perl zlib1g libstdc++6 'libdav1d[0-9]*' exiftran tzdata \
 && curl -fsSL -o /tmp/exiftool.tar.gz \
        "https://github.com/exiftool/exiftool/archive/refs/tags/${EXIFTOOL_VERSION}.tar.gz" \
 && if [ -n "${EXIFTOOL_SHA256}" ]; then echo "${EXIFTOOL_SHA256}  /tmp/exiftool.tar.gz" | sha256sum -c -; fi \
 && mkdir /tmp/exiftool-src && tar -xzf /tmp/exiftool.tar.gz -C /tmp/exiftool-src --strip-components=1 \
 && cd /tmp/exiftool-src && perl Makefile.PL && make install \
 && cd / && rm -rf /tmp/exiftool.tar.gz /tmp/exiftool-src \
 && apt-get purge -y --auto-remove make curl \
 && rm -rf /var/lib/apt/lists/* \
 && test "$(exiftool -ver)" = "${EXIFTOOL_VERSION}" \
 && exiftool -ver \
 && test "$(ffmpeg -version | sed -n '1s/^ffmpeg version \([^ ]*\).*/\1/p')" = "${FFMPEG_VERSION}" \
 && ffprobe -version | head -1 \
 && mediainfo --Version | grep -F "v${MEDIAINFO_VERSION}"

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# the READMEs, rendered at request time by the documentation page (/docs/)
COPY README.md ./docs/README.md
COPY geo-service/README.md ./docs/geo-service-README.md
COPY geo-service/ENDPOINTS.md ./docs/geo-service-ENDPOINTS.md

# --- build identity, shown on the Info page (ⓘ, far right of the nav) --------------------
# Build date: stamped here, AFTER `COPY app`, so it is the time the app code last changed the
# image (a fully cached rebuild keeps the old stamp). Optional: pass BUILD_DATE / GIT_COMMIT /
# GIT_BRANCH / APP_VERSION as build args, e.g.
#   GIT_COMMIT=$(git rev-parse --short HEAD) GIT_BRANCH=$(git branch --show-current) docker compose build
ARG APP_VERSION=""
ARG BUILD_DATE=""
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ARG GIT_MESSAGE=""
ARG EXIFTOOL_VERSION
ARG FFMPEG_VERSION
ARG MEDIAINFO_VERSION
RUN if [ -n "${BUILD_DATE}" ]; then echo "${BUILD_DATE}" > /srv/.build_date; else date -u +%Y-%m-%dT%H:%M:%SZ > /srv/.build_date; fi
ENV APP_VERSION=${APP_VERSION} \
    GIT_COMMIT=${GIT_COMMIT} \
    GIT_BRANCH=${GIT_BRANCH} \
    GIT_MESSAGE="${GIT_MESSAGE}" \
    EXIFTOOL_VERSION=${EXIFTOOL_VERSION} \
    FFMPEG_VERSION=${FFMPEG_VERSION} \
    MEDIAINFO_VERSION=${MEDIAINFO_VERSION}

ENV PYTHONUNBUFFERED=1
# Optional safety fence: restrict browsing/execution to this path (e.g. /mnt/NAS).
# Leave unset to allow any path visible inside the container.
ENV MEDIA_ROOT=""
# Where reusable presets are stored (mount a volume here to persist them).
ENV TIMESTAMP_PRESETS_FILE="/config/presets.json" \
    CAMERA_NAMES_FILE="/config/camera_names.json"
# Named root-folder shortcuts for the browse dropdown (NAME=path,NAME2=path2).
# Leave unset to use the built-in VIDEOS/PHOTOS defaults.
ENV ROOT_FOLDERS=""

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "/srv/app/log_config.json"]
