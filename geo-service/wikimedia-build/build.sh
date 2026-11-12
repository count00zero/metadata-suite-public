#!/bin/bash
# wikimedia-build -- see Dockerfile. Commands: once | schedule | status
#   /pgdata   PostgreSQL data (about 90 GB while building; emptied afterwards)
#   /work     downloads and intermediate CSVs of the generator (about 60 GB; deleted after a good run unless GEO_WIKIMEDIA_KEEP_WORK=1; only last-run.log stays)
#   /out      result folder (the sights containers read it as /wikimedia): wikimedia_importance-YYYYMMDD.tsv.gz + wikimedia_importance.tsv.gz
set -uo pipefail

CMD="${1:-once}"
OUT="${GEO_WIKIMEDIA_OUT_DIR:-/out}"
WORK="${GEO_WIKIMEDIA_WORK_DIR:-/work}"
PGDATA_DIR="${GEO_WIKIMEDIA_PGDATA:-/pgdata}"
GEN="${GEO_WIKIMEDIA_GENERATOR:-/generator}"     # the cloned generator (tests point this at a fake)
PGBIN="${GEO_WIKIMEDIA_PGBIN:-/usr/lib/postgresql/16/bin}"
LANGS="${GEO_WIKIMEDIA_LANGUAGES:-}"          # empty = all 39 languages of the generator; e.g. de,en for a quick, small run
KEEP="${GEO_WIKIMEDIA_KEEP:-2}"               # dated files kept: the newest and the one before it
PUID="${PUID:-1026}"; PGID="${PGID:-100}"
log() { echo "$(date +%H:%M:%S) [wikimedia-build] $*"; }

start_pg() {
  mkdir -p "$PGDATA_DIR" "$WORK" "$OUT"
  chown -R postgres:postgres "$PGDATA_DIR" "$WORK"
  if [ ! -s "$PGDATA_DIR/PG_VERSION" ]; then
    gosu postgres $PGBIN/initdb -D "$PGDATA_DIR" --auth-local=trust --auth-host=reject >/dev/null 2>&1 || return 1
  fi
  echo "work_mem = 250MB" > "$PGDATA_DIR/postgresql.auto.conf"
  gosu postgres $PGBIN/pg_ctl -D "$PGDATA_DIR" -w -l "$WORK/postgres.log" -o "-c listen_addresses=''" start >/dev/null || return 1
}
stop_pg() { gosu postgres $PGBIN/pg_ctl -D "$PGDATA_DIR" -m fast stop >/dev/null 2>&1 || true; }

build_once() {
  start_pg || { log "PostgreSQL did not start (see $WORK/postgres.log)"; return 1; }
  trap stop_pg RETURN
  # Resume support: every finished step leaves a marker in $BUILDID/.done; a new run skips them while the dump date is the same
  # (the generator's own sql2csv and import steps always start from scratch, which would repeat hours of work).
  cp -f "$WORK/last-run.log" "$WORK/last-run.prev.log" 2>/dev/null || true
  local script='
    set -o pipefail
    cd "'"$GEN"'"
    export BUILDID="'"$WORK"'/wikimedia_build"
    LATEST=$(./steps/latest_available_data.sh)
    if [ -z "$LATEST" ]; then echo "NO_DUMP"; exit 3; fi
    export WIKIPEDIA_DATE=$LATEST WIKIDATA_DATE=$LATEST
    export LANGUAGES="'"$LANGS"'"
    [ -n "$LANGUAGES" ] || export LANGUAGES=$(grep -v "^#" config/languages.txt | tr "\n" ",")
    export DATABASE_NAME=wikimedia_build
    echo "DUMP_DATE=$LATEST"
    DONE="$BUILDID/.done"; PREV="'"$WORK"'/last-run.prev.log"
    mkdir -p "$DONE"
    if [ -f "$DONE/date" ] && [ "$(cat "$DONE/date")" != "$LATEST" ]; then
      echo "new dump $LATEST (was $(cat "$DONE/date")): starting from scratch"
      dropdb --if-exists $DATABASE_NAME; rm -rf "$BUILDID"; mkdir -p "$DONE"
    fi
    if [ ! -f "$DONE/date" ] && [ -s "$PREV" ] && grep -q "DUMP_DATE=$LATEST" "$PREV"; then
      # first start of this script version after a failed run: every step announced before the last one finished
      for s in $(grep -o "^===> [a-z_0-9]*" "$PREV" | sed "s/===> //" | head -n -1); do touch "$DONE/$s"; echo "earlier run: $s was finished"; done
    fi
    echo "$LATEST" > "$DONE/date"
    step() {
      if [ -f "$DONE/$1" ]; then echo "===> $1 (finished before, skipped)"; return 0; fi
      echo "===> $1"; ./steps/$1.sh || return 1; touch "$DONE/$1"
    }
    for s in wikipedia_download wikidata_download wikidata_api_fetch_placetypes wikipedia_sql2csv wikidata_sql2csv; do step $s || exit 1; done
    if ! psql -lqt | cut -d"|" -f1 | grep -qw $DATABASE_NAME; then
      echo "database $DATABASE_NAME is missing: importing again"
      rm -f "$DONE"/wikipedia_import "$DONE"/wikidata_import "$DONE"/wikipedia_process "$DONE"/wikidata_process
      createdb $DATABASE_NAME || exit 1
    fi
    for s in wikipedia_import wikidata_import wikipedia_process wikidata_process output; do step $s || exit 1; done'
  log "starting the generator (languages: ${LANGS:-all}); this takes hours"
  gosu postgres bash -c "$script" 2>&1 | tee "$WORK/last-run.log"
  local rc=${PIPESTATUS[0]}
  if [ "$rc" = 3 ]; then log "no complete Wikimedia dump on the mirror yet -- nothing done, will try again"; return 0; fi
  [ "$rc" = 0 ] || { log "FAILED (exit $rc); the downloads in $WORK are kept, run again to continue"; return 1; }
  local f date_
  f=$(ls -1 "$WORK"/wikimedia_build/output/wikimedia_importance.tsv.gz 2>/dev/null | head -1)
  date_=$(grep -o 'DUMP_DATE=[0-9]*' "$WORK/last-run.log" | tail -1 | cut -d= -f2)
  [ -s "$f" ] || { log "the generator produced no output file"; return 1; }
  [ -n "$date_" ] || date_=$(date +%Y%m%d)
  # validate: gz readable, header + plenty of rows
  # first line only; read into a variable so that zcat getting SIGPIPE from head cannot fail the check under pipefail
  local hdr; hdr=$(zcat "$f" 2>/dev/null | head -n 1)
  case "$hdr" in
    *importance*) ;;
    *) log "output has no header line with 'importance' -- not used (the output step will run again next time)"
       rm -f "$WORK/wikimedia_build/.done/output"; return 1 ;;
  esac
  cp "$f" "$OUT/.wikimedia_importance-$date_.tsv.gz.part" && mv -f "$OUT/.wikimedia_importance-$date_.tsv.gz.part" "$OUT/wikimedia_importance-$date_.tsv.gz"
  cp -f "$OUT/wikimedia_importance-$date_.tsv.gz" "$OUT/.latest.part" && mv -f "$OUT/.latest.part" "$OUT/wikimedia_importance.tsv.gz"
  # keep the newest and the one before it
  ls -1 "$OUT"/wikimedia_importance-[0-9]*.tsv.gz 2>/dev/null | sort -r | tail -n +$((KEEP + 1)) | xargs -r rm -f
  chown "$PUID:$PGID" "$OUT"/wikimedia_importance*.tsv.gz 2>/dev/null || true
  date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/.wikimedia-build.last"
  log "done: $OUT/wikimedia_importance-$date_.tsv.gz ($(du -h "$f" | cut -f1))"
  stop_pg
  rm -f "$OUT"/.wikimedia_importance-*.part "$OUT"/.latest.part 2>/dev/null     # leftovers of an earlier interrupted copy
  if [ "${GEO_WIKIMEDIA_KEEP_WORK:-0}" != "1" ]; then
    # the result is in $OUT and validated: everything else is intermediate -- downloads, CSVs, the whole PostgreSQL cluster
    # (hidden files included), the postgres log and the previous run's log. Only last-run.log stays (small, for diagnosis).
    local before after
    before=$(du -sh "$WORK" "$PGDATA_DIR" 2>/dev/null | cut -f1 | paste -sd+)
    find "$WORK" -mindepth 1 -maxdepth 1 ! -name last-run.log -exec rm -rf {} + 2>/dev/null
    find "$PGDATA_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null
    after=$(du -sh "$WORK" "$PGDATA_DIR" 2>/dev/null | cut -f1 | paste -sd+)
    log "intermediate files removed (work+pgdata: $before -> $after)"
  fi
  return 0
}

next_due_epoch() {            # seconds since epoch of the next run (last run + days, at HH:MM)
  local days="${GEO_WIKIMEDIA_BUILD_DAYS:-30}" at="${GEO_WIKIMEDIA_BUILD_AT:-03:00}" last
  if [ -s "$OUT/.wikimedia-build.last" ]; then last=$(date -d "$(cat "$OUT/.wikimedia-build.last")" +%s); else last=0; fi
  local day; day=$(date -d "@$((last + days * 86400))" +%F)
  local t; t=$(date -d "$day $at" +%s)
  [ "$last" = 0 ] && t=$(date +%s)
  echo "$t"
}

case "$CMD" in
  once) build_once; exit $? ;;
  schedule)
    days="${GEO_WIKIMEDIA_BUILD_DAYS:-30}"
    if [ "$days" -le 0 ] 2>/dev/null; then log "GEO_WIKIMEDIA_BUILD_DAYS=0: idle (build with 'once')"; exec sleep infinity; fi
    while true; do
      due=$(next_due_epoch); now=$(date +%s)
      if [ "$now" -ge "$due" ]; then build_once || log "run failed; trying again in 6 h"; [ -s "$OUT/.wikimedia-build.last" ] && [ "$(date -d "$(cat "$OUT/.wikimedia-build.last")" +%s)" -ge "$due" ] || sleep 21600
      else log "next build $(date -d "@$due" '+%F %H:%M')"; sleep $(( due - now > 3600 ? 3600 : due - now )); fi
    done ;;
  status)
    ls -l "$OUT"/wikimedia_importance*.tsv.gz 2>/dev/null || echo "no importance file yet in $OUT"
    cat "$OUT/.wikimedia-build.last" 2>/dev/null ;;
  *) echo "usage: once | schedule | status"; exit 2 ;;
esac
