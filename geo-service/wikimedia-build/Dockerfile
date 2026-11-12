# wikimedia-build -- builds wikimedia_importance.tsv.gz yourself (the generator of Nominatim's official file,
# https://github.com/osm-search/wikipedia-wikidata) with a PostgreSQL inside the container. Heavy: ~150 GB disk (90 GB database),
# 9-12 h for all 39 languages on 4 cores/NVMe. Run on demand or monthly:
#   docker compose --profile wikimedia build wikimedia-build
#   docker compose --profile wikimedia run --rm wikimedia-build once         (one build now)
#   docker compose --profile wikimedia up -d wikimedia-build                 (every GEO_WIKIMEDIA_BUILD_DAYS days)
FROM postgres:16

ARG GENERATOR_REF=master
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates wget curl coreutils jq moreutils pigz nodejs npm python3 gosu \
    && npm install -g wikidata-taxonomy \
    && git clone --depth 1 --branch ${GENERATOR_REF} https://github.com/osm-search/wikipedia-wikidata.git /generator \
    && rm -rf /var/lib/apt/lists/*

COPY build.sh /usr/local/bin/wikimedia-build.sh
RUN chmod +x /usr/local/bin/wikimedia-build.sh
ENTRYPOINT ["/usr/local/bin/wikimedia-build.sh"]
CMD ["once"]
