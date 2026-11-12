# backup
docker export metadata-suite > /mnt/FINN/DockerImage_BACKUP/metadata-suite_container_backup.tar
docker save metadata-suite:latest | gzip > /mnt/FINN/DockerImage_BACKUP/metadata-suite_image_$(date +%F).tar.gz
docker export geo-service > /mnt/FINN/DockerImage_BACKUP/geo-service_container_backup.tar
docker save geo-service:latest | gzip > /mnt/FINN/DockerImage_BACKUP/geo-service_image_$(date +%F).tar.gz

# media images: ffmpeg + mediainfo, compiled once from Dockerfile.media (see ensure_media_image.sh)
#  1. every metadata-suite-media:<tag> that Docker has now  -> media_<tag>_<date>.tar.gz
#  2. the tar copies kept on disk in MEDIA_IMAGE_DIR (.env, default ./media-images) -> $BACKUP_DIR/media-images/
cd "$(dirname "$0")" || exit 1
BACKUP_DIR="${BACKUP_DIR:-/mnt/FINN/DockerImage_BACKUP}"
MEDIA_DIR="${MEDIA_IMAGE_DIR:-}"
[ -z "$MEDIA_DIR" ] && [ -f .env ] && MEDIA_DIR="$(grep -E '^MEDIA_IMAGE_DIR=' .env | tail -1 | cut -d= -f2- | sed -e 's/[[:space:]]*#.*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
MEDIA_DIR="${MEDIA_DIR:-./media-images}"
for img in $(docker images --format '{{.Repository}}:{{.Tag}}' metadata-suite-media); do
  docker save "$img" | gzip > "$BACKUP_DIR/media_$(echo "$img" | tr ':/' '__')_$(date +%F).tar.gz"
done
if ls "$MEDIA_DIR"/*.tar >/dev/null 2>&1; then
  mkdir -p "$BACKUP_DIR/media-images" && cp -pu "$MEDIA_DIR"/*.tar "$BACKUP_DIR/media-images/"
else
  echo "Hinweis: keine tar-Kopien in $MEDIA_DIR (ensure_media_image.sh legt sie an)"
fi

# restore
#docker import /mnt/FINN/DockerImage_BACKUP/metadata-suite_container_backup.tar metadata-suite:latest
#gunzip -c metadata-suite_image_2026-10-21.tar.gz | docker load
#docker import /mnt/FINN/DockerImage_BACKUP/geo-service_container_backup.tar geo-service:latest
#gunzip -c geo-service_image_2026-10-21.tar.gz | docker load
# media image (either way): put the .tar from $BACKUP_DIR/media-images/ back into MEDIA_IMAGE_DIR and run ./ensure_media_image.sh (docker load),
#   or: gunzip -c media_metadata-suite-media_ffmpeg9.0.2-mediainfo26.10_<date>.tar.gz | docker load
