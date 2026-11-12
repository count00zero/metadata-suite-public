#!/usr/bin/env bash
# Rebuild + restart. Passes the git commit/branch into the image so the Info page shows them.
cd "$(dirname "$0")" || exit 1
export GIT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null)"
export GIT_BRANCH="$(git branch --show-current 2>/dev/null)"
export GIT_MESSAGE="$(git log -1 --pretty=%s 2>/dev/null)"
echo "Build: commit=${GIT_COMMIT:-?} branch=${GIT_BRANCH:-?} message=${GIT_MESSAGE:-?}"
# ffmpeg/mediainfo are compiled once into the image metadata-suite-media:ffmpeg<v>-mediainfo<v> (Dockerfile.media); a tar copy
# lives in MEDIA_IMAGE_DIR, so after a `docker image prune -a` it is loaded again instead of recompiled (see ensure_media_image.sh).
./ensure_media_image.sh || exit 1
#docker compose down && docker compose build --no-cache && docker compose up --force-recreate
docker compose down --rmi local && docker compose build && docker compose up --force-recreate
