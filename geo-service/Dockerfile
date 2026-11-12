# geo-service -- reverse geocoding + GPS-track lookup + caption context.
#   docker compose build && docker compose up -d
#   curl "http://localhost:8430/reverse?lat=47.0707&lon=15.4395"
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GEO_CACHE_DB=/data/geo-cache.sqlite

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# The compose file runs the container as PUID:PGID (default 99:100 = unRAID's nobody:users) so the
# cache in the mounted /data volume is writable without chmod games on the host.
RUN mkdir /data && chmod 777 /data
VOLUME /data
EXPOSE 8000 8001 8002

# Liveness only: /health answers without touching Geocoder/Traccar/Dawarich, so a down Pelias can
# never make autoheal restart this container in a loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# serve.py = the main API on 8000 plus the Nominatim (8001) and Photon (8002) facades, one process, one cache
CMD ["python", "-m", "app.serve"]
