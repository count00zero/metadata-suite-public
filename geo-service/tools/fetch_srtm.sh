#!/bin/sh
# Download SRTM3 tiles into ./geo-data/srtm (the folder the geo-service container sees as /data/srtm).
#   sh tools/fetch_srtm.sh --austria
#   sh tools/fetch_srtm.sh --tiles N47E016,N46E014
#   sh tools/fetch_srtm.sh --bbox 45,5,48,15
# Needs only python3 (standard library). Run it on a machine with internet access.
cd "$(dirname "$0")/.." || exit 1
exec python3 -m app.srtmfetch --out "${GEO_SRTM_OUT:-../geo-data/srtm}" "$@"
