#!/usr/bin/env bash
# Makes sure the image metadata-suite-media:ffmpeg<v>-mediainfo<v> (ffmpeg + ffprobe + mediainfo, see Dockerfile.media) exists in Docker:
#   1. already in Docker                -> nothing to do (a tar copy is made if none exists yet)
#   2. missing, tar file on disk        -> docker load (seconds)   <- e.g. after `docker image prune -a`
#   3. missing, no tar file             -> compile once (~6 min), then save it as tar file
# The tar files live in MEDIA_IMAGE_DIR (.env, default ./media-images) -- put that on the NVMe, outside anything you prune.
# Called by build_start.sh; run it by hand before a plain `docker compose build`.
cd "$(dirname "$0")" || exit 1
IMG="$(docker compose --profile tools config --images 2>/dev/null | grep '^metadata-suite-media:' | head -1)"
[ -n "$IMG" ] || { echo "ensure_media_image: Image-Name nicht ermittelbar (docker compose config)"; exit 1; }

DIR="${MEDIA_IMAGE_DIR:-}"
[ -z "$DIR" ] && [ -f .env ] && DIR="$(grep -E '^MEDIA_IMAGE_DIR=' .env | tail -1 | cut -d= -f2- | sed -e 's/[[:space:]]*#.*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
DIR="${DIR:-./media-images}"
mkdir -p "$DIR" || exit 1
TAR="$DIR/$(echo "$IMG" | tr ':/' '__').tar"

save_tar() {
  docker save -o "$TAR.part" "$IMG" && mv -f "$TAR.part" "$TAR" && echo "ffmpeg/mediainfo-Image gesichert: $TAR ($(du -h "$TAR" | cut -f1))" \
    || { rm -f "$TAR.part"; echo "WARNUNG: Image konnte nicht nach $TAR gesichert werden"; }
}

if docker image inspect "$IMG" >/dev/null 2>&1; then
  [ -f "$TAR" ] || save_tar
  exit 0
fi
if [ -f "$TAR" ]; then
  echo "ffmpeg/mediainfo-Image $IMG fehlt in Docker -- lade $TAR ..."
  if docker load -i "$TAR" >/dev/null && docker image inspect "$IMG" >/dev/null 2>&1; then exit 0; fi
  echo "WARNUNG: $TAR war nicht brauchbar -- baue das Image neu"
  rm -f "$TAR"
fi
echo "ffmpeg/mediainfo-Image $IMG wird einmalig gebaut (ca. 6 Minuten) ..."
docker compose --profile tools build media-tools || exit 1
save_tar
