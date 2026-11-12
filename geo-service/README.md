# geo-service

One small container that joins your self-hosted location data, plus three command-line tools that use it.

| Part | What it does |
|---|---|
| **geo-service** (container) | `/reverse` place names via Photon → Nominatim → Pelias (→ maritime) with a local cache and a circuit breaker; `/track-at` and `/track-range` positions from Traccar + Dawarich; `/context` place + time of day + season + (opt-in) weather |
| **`clients/gps_fill.py`** | gives photos/videos **without GPS** a position from your track — interpolated, with a confidence value, per-camera **clock-drift correction**, written to the `.xmp` sidecar |
| **`clients/caption_context.py`** | the facts an AI caption should know that aren't in the picture (time of day, weather, trip name) |
| **metadata-suite hook** (separate zip) | the suite asks the service for place names and can add that context to AI-Beschreibung |

Everything is optional and additive: with the service not running, the suite and your scripts behave exactly as before.

---

## 1. Run the service

```bash
cp .env.example .env        # adjust URLs/tokens; the variable names are the suite's own
docker compose up -d --build
curl "http://localhost:8430/health"
curl "http://localhost:8430/reverse?lat=47.0707&lon=15.4395"
```

`./data/geo-cache.sqlite` is the only state (reverse results, per-day track chunks, weather). Delete it any time.
The compose file runs as `PUID:PGID` (default `1026:100`; set `PUID`/`PGID` in `.env`) so `./data` is writable without chmod.
The healthcheck only tests the service itself, so a dead Pelias can't make autoheal restart it in a loop.

### Endpoints

| Endpoint | Returns |
|---|---|
| `GET /reverse?lat&lon[&lang][&nocache]` | `{found, country, country_a, state, county, city, locality, street, sublocation, label, source, cached, tried[]}`, zusätzlich `house_number`, `postcode`, `neighbourhood`, wenn das Backend sie kennt (sonst fehlen die Felder). `found:false` = every reachable backend had nothing (cached 1 h). **503** = no backend reachable (never cached). |
| `POST /reverse-batch` `{points:[{lat,lon}…]}` | up to 500 results in one call |
| `GET /track-at?time[&max_gap][&devices][&sources][&include_import]` | position at that moment: `{lat, lon, method: exact\|interpolated\|nearest, gap_s, span_s, confidence, uncertainty_m, tier, before, after}` |
| `GET /track-range?from&to[&step]` | raw points `[[epoch,lat,lon,"T"\|"D"\|"I"],…]` (max 7 days per call; `step` thins out) |
| `GET /context?lat&lon&time[&weather][&poi][&trip]` | `{place, local_time, tz, month_de, season_de, daypart_de, weather, text_de, extra_de}` **plus** `daylight`, `moon`, `holiday`, `weekend`, `country_info` (nur im Ausland), `pois`, `trip` und fertige Satzteile in `parts_de` – siehe „Kontext-Extras“ |
| `GET /around?lat&lon[&radius][&limit][&lang][&include]` | Alles zu einer Koordinate in einer Antwort: `place` (mit Hausnummer, PLZ), `water` (Meer, Bucht, See, Flüsse), `sights`, `elevation`, `timezone`. `include=place,water,…` wählt Teile; ausgefallene stehen unter `errors`. |
| `GET /sights?lat&lon \| q \| address [&radius][&limit][&categories][&sort][&lang]` | Sehenswürdigkeiten bewertet (Kategorie + Abstand), Dubletten zusammengefasst, mit Richtung und Details – siehe „Sehenswürdigkeiten“ |
| `GET /findNearbyWikipedia[JSON]?lat&lng[&radius][&maxRows][&lang]` | GeoNames-kompatibel: Sehenswürdigkeiten mit Wikipedia-Artikel |
| `GET /places` · `GET /pois?lat&lon[&radius][&limit]` · `GET /trips?from&to` · `GET /trip-at?time` · `GET /clock-check?time[&lat&lon][&gps_time]` | Meine Orte · Sehenswürdigkeiten · erkannte Reisen · Reise zu einem Zeitpunkt · Kamera-Uhr-Plausibilität |
| `GET /health` · `GET /stats` | liveness without secrets · cache and lookup counters |

`time` is epoch seconds or ISO 8601 (no zone = UTC). With `GEO_SERVICE_TOKEN` set, everything except `/health` needs `X-API-Key: <token>`
(or `Authorization: Bearer`) — worth doing, `/track-*` is your location history.

From bash (e.g. inside `Set-GPS-Data-via-traccar-and-dawarich.sh`; errors go to stderr, so command substitution stays clean):

```bash
CITY=$(python3 clients/geoclient.py reverse "$LAT" "$LON" --field city)      # or:
curl -fsS -H "X-API-Key: $GEO_SERVICE_TOKEN" "$GEO_SERVICE_URL/reverse?lat=$LAT&lon=$LON" | jq -r '.label'
```

### How the reverse chain behaves

* Same parsing and fields as the suite's `geocore.py` (alpha-3 country codes included), so every component names a place the same way.
* "Backend unreachable / HTTP 5xx" and "backend answered, nothing here" are different: only the first counts against the breaker (3 failures in a row → skipped for 60 s) and keeps the answer out of the cache.
* Maritime is only asked when everything else found nothing, like in the suite.
* Cache key is lat/lon rounded to `GEO_CACHE_PRECISION` decimals (4 ≈ 11 m).

### Tracks

Traccar (`/api/positions/gpx` per device, Bearer token) and Dawarich (`/api/v1/points`, paged) are fetched **per UTC day and cached** (`GEO_CACHE_TTL_DAYS` for finished days with points, 7 days for finished days without points, 5 min for the last two). A 14-day trip costs 14 upstream fetches once, however many photos ask.
`devices=default` = `TRACCAR_DEVICES`, `all` = plus `TRACCAR_DEVICES_ADDITIONAL`, or `7,19`. The Dawarich *import* account (points derived from photos) is only used with `include_import=true` — it would be circular for drift estimation.

**Confidence** (heuristic, not a probability; `trackmath.confidence()`):

| situation | confidence |
|---|---|
| point exactly on that second | 1.0 |
| between two points < 150 m apart (was standing still) | `0.9·exp(−span/12 h)` — hours hardly matter |
| between two points, moving | `exp(−gap/15 min) / (1 + (distance/2)/1 km)` — worst-case error is half the way |
| only one side known (start/end of track) | `0.8·exp(−gap/15 min)` |

`uncertainty_m` is the matching rough error radius. Nearest point must be within `GEO_MAX_GAP_SECS_TIER2` (6 h) or there's no answer.

---

## 2. `gps_fill.py` — smarter GPS fill-in

```bash
cd clients
export GEO_SERVICE_URL=http://192.168.1.33:8430  GEO_SERVICE_TOKEN=...
python3 gps_fill.py "/mnt/NAS/photo/Urlaube/2026/2026.08.05-2026.08.22 Sri Lanka & Malediven"          # preview + CSV report
python3 gps_fill.py <folder> -camera "Luna Ultra" -apply                                              # write sidecars
python3 gps_fill.py <folder> -drift off -min-confidence=0.5 -report /tmp/fill.csv -v
```

Flags take `-flag value` and `-flag=value` (also `--flag`), like your other scripts. **Without `-apply` nothing is written except the CSV.** Media files are never touched.

What happens per run:

1. **Scan** (exiftool, batched, names via stdin) and resolve every file's capture time to UTC, most trustworthy first: offset-qualified `.xmp` date (what *Timestamp & Rename* writes) → EXIF `DateTimeOriginal` + `OffsetTimeOriginal`/`OffsetTime`/`TimeZoneOffset` → file name `YYYY.MM.DD-HH.MM.SS` + the folder's most common offset (`dir-offset`) → … + `-tz` (`tz-assumed`, least trustworthy; the summary counts those). Run it **after** Timestamp & Rename.
2. **Track** for just the days around those photos, once, through the service.
3. **Drift** per camera (`Make Model`, falling back to the sidecar's) from files that already have GPS:
   * `gps-time` — camera clock minus EXIF GPS time (satellite time, exact);
   * `track-match` — the time shift (±`-max-shift`, default 15 min, 10 s steps) at which the track passes through the photo's own GPS position. Only trusted when the minimum is unambiguous (< 60 m and clearly better than anything ≥ 2 min away): a track where you stood still gives a flat curve and is rejected.
   * Estimates are per camera **and per day**; samples must agree (median absolute deviation ≤ 25 % of the drift or 5 s), satellite-time samples count double. A day with too few samples falls back to the camera-wide estimate, else **no correction** (shown as "no"/"not stable" in the table). `-drift off`, or `-drift=-3600` to force a known error (camera clock minus true time).
4. **Fill**: files with no EXIF GPS (and no GPS in a sidecar, unless `-force`; and not marked `no-gps-found` by the suite, unless `-retry`) get the track position at the drift-corrected time. Below `-min-confidence` (0.3) they're reported but not written.
5. **Write** (`-apply`): `<file>.xmp` — `XMP-exif:GPSLatitude/Longitude`, place fields exactly as the suite writes them (`Country`, `CountryCode` alpha-3, `State`, `LocationCreatedProvinceState`, `City`, `Location`, `LocationCreatedSublocation`), geo keywords (`geo:lat=…`, `geotagged`, place names — added only if missing; exiftool's `+=` would duplicate), and **provenance** in a small `XMP-mon:*` namespace: `GpsSource`, `GpsConfidence`, `GpsGapSeconds`, `GpsUncertaintyM`, `GpsTrackSources`, `TimeBasis`, `ClockDriftSeconds`, `ClockDriftMethod`, `GpsFilledBy`. Immich/Kodi ignore it; you can query it:
   `exiftool -config clients/exiftool_mon.config -XMP-mon:all -r /mnt/NAS/photo`

Re-running is safe: files that got GPS are skipped next time (`sidecar-has-gps`); `-force` replaces cleanly (old `geo:lat=` keywords are removed).
`-camera` filters once, at scan time, so scan, drift model and fill can't disagree about which files are in scope.

Limits worth knowing: interpolation assumes you moved in a straight line between two track points; for a long gap while driving the confidence says so and the default threshold skips it. Tracks that *only* come from the phone you also took the photo with can't reveal that phone's clock error (that's what `gps-time` is for).

## 3. `caption_context.py` — context for AI captions

```bash
python3 caption_context.py ".../2026.08.05-2026.08.22 Sri Lanka & Malediven/2026.08.09-17.31.02.jpg"
# Aufgenommen in Kandy (Zentralprovinz, Sri Lanka), nachmittags im August 2026; Regen bei 24 °C. Reise: Sri Lanka & Malediven.
python3 caption_context.py <file> -extra       # only the part for the prompt (the place is passed separately)
python3 caption_context.py <file> -offline -json
```

Place, local time of day (timezone from the coordinates), season and weather come from `/context`; the trip name from the folder (`<start>-<end> <name>`, date-only folders have none). With the service down you still get time of day, month and trip name — a caption is never blocked by it.
Weather is **off by default** (`WEATHER_PROVIDER=none`). With `open-meteo` the service sends coordinates rounded to 0.1° (~11 km) to open-meteo.com, one request per place and day, cached — the only third party it ever contacts.

### Weather archive ("shadow database")

Every requested day up to and including today is stored **permanently** in the `weather` table of `geo-cache.sqlite` (days up to 7 days old come from the forecast API, older ones from the archive API; the last days' values may be revised slightly upstream later, the archive keeps what it got first). `/context` reads them without any network access. Only future days are not stored.

Fill it ahead of time instead of one photo at a time:

```bash
# which cell/days are missing for October 2025? (nothing is fetched)
curl -XPOST $URL/weather/backfill -H 'Content-Type: application/json' \
     -d '{"from":"2025-10-01","to":"2025-10-31","tracks":true,"dry_run":true}'
curl $URL/weather/backfill          # progress: phase, needed_cell_days, done, counts{fetched,cached,failed,recent,missing}
curl $URL/weather/stats             # what the archive holds (days, cells, first/last day)
curl -XPOST $URL/weather/backfill/cancel
```

Body: `points` (`[[epoch, lat, lon], ...]`, e.g. photos/videos with GPS — the suite's *GPS füllen → Wetter-Archiv* box sends them), `from`/`to` (epoch or ISO) with `tracks: true` to take every cell/day from the Traccar/Dawarich tracks, `sources`, `devices`, `include_import`, `dry_run`. The range is clamped to 1940-01-01 .. now; track ranges over 90 days are probed block by block (one request per source/device) and empty blocks are skipped (`blocks_skipped` in the progress). One request to Open-Meteo per distinct (0.1° cell, UTC day); already archived pairs are skipped, so re-running is cheap and a stopped run simply continues. Limits: `WEATHER_BACKFILL_DELAY` (default 0.4 s between requests) and `WEATHER_BACKFILL_MAX` (default 5000 requests per run, then status `capped`); an HTTP 429 from Open-Meteo stops the run with `rate_limited`. Privacy: a backfill sends Open-Meteo your visited cells and days (rounded to ~11 km) — the same information individual lookups would, just all at once.

## 4. metadata-suite hook (separate zip)

Set `GEO_SERVICE_URL` (and optionally `GEO_SERVICE_TOKEN`) in the suite's `.env`:

* `geocore.reverse_geocode()` asks the service first and falls back to the suite's own Photon/Nominatim/Pelias chain **only while the service is down** (after 2 failures it's skipped for 60 s so a batch never crawls). "Nothing found" from the service is final.
* `AI_CONTEXT=true`: AI-Beschreibung adds *"vormittags im Oktober 2026; Regen bei 11 °C; Wo wars: „…“"* to the prompt, with instructions to use at most one item, only if it fits the scene (no weather for indoor shots) and never to invent. The list view shows what was passed.
* Info page: new settings listed (token masked), service in the reachability check.

## Tests

```bash
pip install -r requirements-dev.txt
PATH=<dir with exiftool and ffmpeg>:$PATH python3 tests/run_all.py
```

`tests/test_service.py` (fake upstreams: chain, breaker, cache, outage-not-cached, tracks, weather, auth), `tests/test_trackmath.py` (interpolation, confidence, antimeridian, drift search), `clients/tests/test_e2e_gps_fill.py` (real exiftool + real files + the service over HTTP: drift +120 s via GPS time and −60 s via track match recovered, positions land on the track at the *true* time, media bytes unchanged, idempotent re-run, `-force`, camera filter, manual drift, dead service), `clients/tests/test_caption_context.py`, and a guard that the two copies of `trackmath.py`/`textde.py` stay byte-identical.

## Not done / honest gaps

* The Docker image was **not built** here (no Docker daemon in the sandbox); the entrypoint was smoke-tested with plain uvicorn. Same base image and pins as the suite.
* GPS Sidecar in the suite still fetches tracks itself; interpolation/confidence/drift live in `gps_fill.py` for now. Porting them as a suite module is the natural next step.
* Dawarich is queried with `?api_key=` like the suite does; the service never logs URLs.
* `trackmath.py` and `textde.py` exist in two places on purpose (each side stays self-contained); the guard test catches drift between them.

### Photon (u. a.) „unreachable"? – Live-Check
Die Geocoder werden **nicht nur beim Start** geprüft: jede nicht gecachte Abfrage versucht sie neu. Nur nach `GEO_BREAKER_FAILURES` (3) Fehlern in Folge pausiert ein Backend `GEO_BREAKER_COOLDOWN` Sekunden („skipped (circuit open)"). `GET /health?probe=true` fragt jeden konfigurierten Geocoder **jetzt** live ab, nennt den echten Grund (`ConnectionError…`, `HTTP 500`, Antwortzeit) und schließt die Pause sofort, sobald er antwortet. Zwischengespeicherte Antworten (`"cached": true`) wiederholen den alten `tried`-Status nicht mehr – der stand für den Moment, in dem der Cache-Eintrag entstand (z. B. als Photon noch aus war).


## GeoNames-kompatible Endpunkte (ersetzt den `geonames-wrapper`-Container)

Der geo-service beantwortet jetzt **`findNearbyPlaceName`** und **`findNearbyPostalCodes`** (je als `…JSON` und als XML ohne Suffix) mit denselben Pfaden, Parametern, Feldnamen und Feldreihenfolgen wie `api.geonames.org` – im Client nur die Basis-URL tauschen (`http://<host>:8430`). `username` wird ignoriert.

```
/findNearbyPlaceNameJSON?lat=48.15&lng=16.33[&maxRows=5][&style=SHORT|MEDIUM|LONG|FULL][&lang=de]
/findNearbyPostalCodesJSON?lat=48.15&lng=16.33[&radius=10][&maxRows=5]
/findNearbyPostalCodesJSON?postalcode=2552&country=AT      (oder placename=…, statt lat/lng)
```

- **findNearbyPlaceName:** nächster *bewohnter Ort* – Pelias-Reverse nur auf `locality,localadmin` (echte Nächster-Nachbar-Suche, liefert „Musterdorf“ statt einer Straße/Siedlung); ohne Pelias-Treffer Nominatim (Stadtebene), danach Photon. `lang` macht einen zweiten, lokalisierten Nominatim-Aufruf, damit `name` und `toponymName` auseinanderfallen können wie bei GeoNames.
- **findNearbyPostalCodes:** Pelias-Layer `postalcode`; hat Pelias dort nichts, ein Ring aus Nominatim-Abfragen um den Mittelpunkt (`GN_POSTAL_PROBE_*`, gleichzeitig). Eine Zeile je (PLZ, Ort); `adminCode1/adminName1-3` einmal am Mittelpunkt aufgelöst.
- **Cache:** Antworten stehen in `geo-cache.sqlite` (Tabelle `geonames`, wie lange: `GEO_CACHE_TTL_DAYS`); Header `X-Geo-Cache: hit` bei Treffern. Nichts wird gecacht, wenn nichts gefunden wurde.
- **Fehler** wie bei GeoNames: HTTP 200 mit `{"status":{"message":…,"value":…}}` (20 = Parameter fehlt, 13 = kein Backend erreichbar).
- **Token:** Ist `GEO_SERVICE_TOKEN` gesetzt, geht der Header wie sonst – oder `?token=…` in der URL (GeoNames-Clients können oft keine Header setzen).
- **Geblieben wie im Wrapper (Näherungen):** `geonameId` ist **synthetisch** (stabil, aber nie die echte GeoNames-ID), `fcl`/`fcode` werden aus OSM-Tags geraten, `population` ist 0 solange die Quelle keine liefert, `adminCode2/3` der PLZ-Zeilen fehlen (nicht aus OSM ableitbar).
- **Außerdem:** `search`, `get`, `hierarchy`, `countryInfo`, `address` (siehe unten).
- **Nicht übernommen:** `children`, `contains`, `siblings`, `neighbours` (brauchen direkten Postgres-Zugriff auf Nominatim), `geoCodeAddress`, `streetNameLookup`, `findNearby`, `findNearbyStreetsOSM`, `countryCode`, `countrySubdivision`.

### Umzug vom geonames-wrapper
1. In der `.env` des geo-service die Backends setzen, die der Wrapper hatte: `NOMINATIM_SERVER=` (= alt `NOMINATIM_URL`), `PELIAS_SERVER=` (= `PELIAS_URL`), `PHOTON_SERVER=` (= `PHOTON_URL`). Die alten Namen `MAX_ROWS_LIMIT`, `POSTAL_PROBE_RINGS`, `POSTAL_PROBE_POINTS_PER_RING`, `POSTAL_PROBE_CONCURRENCY`, `HTTP_TIMEOUT_SECONDS` werden als Fallback gelesen. `NOMINATIM_DB_DSN` wird **nicht** mehr gebraucht.
2. Optional, damit schon vergebene IDs gleich bleiben: die alte `id_registry.sqlite3` nach `<geo-data>/geonames-ids.sqlite` kopieren (gleiches Tabellenlayout; Pfad abweichend per `GN_ID_DB`).
3. `docker compose up -d --build geo-service`, Clients von Port 7475 auf 8430 umstellen, prüfen, dann den alten Container entfernen.

### Eigene GeoNames-Datenbank (Gazetteer) – Pelias nicht mehr nötig

Aus den GeoNames-Dumps baut der Service eine SQLite-Datei (`gazetteer.sqlite` neben dem Cache) und beantwortet damit die GeoNames-Endpunkte selbst – mit **echten geonames.org-IDs**, echter Feature-Klasse/-Code und echten Einwohnerzahlen statt der Schätzungen aus OSM-/Pelias-Daten.

**Dateien** (Ordner `GEONAMES_HOST_DIR` → `/geonames`, nur lesen; `.txt` oder `.zip`, nichts auspacken nötig):

| Datei | wofür | Pflicht |
|---|---|---|
| `allCountries.txt` / `.zip` | alle Orte weltweit | ja |
| `alternateNamesV2.txt` / `.zip` | Namen in anderen Sprachen (Wien = Vienna), Suche mit `lang=` | empfohlen |
| `admin1CodesASCII.txt`, `admin2Codes.txt` | Namen von Bundesland / Bezirk | empfohlen |
| `hierarchy.txt` / `.zip` | exaktes `children` / `hierarchy` | empfohlen |
| `postal_allCountries.zip` (aus `export/zip/`, so umbenennen) | Postleitzahlen | empfohlen |
| `featureCodes_en.txt` | `fcodeName` | optional |
| `countryInfo.txt` | Ländernamen (wie bisher) | wie bisher |

**Bauen / erneuern** (dauert für „alles weltweit“ einige Minuten; die alte Datenbank bleibt bis zum Schluss in Betrieb):
```bash
docker compose exec geo-service python -m app.gazetteer          # bauen
docker compose exec geo-service python -m app.gazetteer --status # was ist drin, welche Dateien wurden gefunden
```
Variablen: `GN_GAZETTEER_DB`, `GN_GAZETTEER_LANGS` (Standard `de,en`), `GN_GAZETTEER_FTS_CLASSES` (Suchindex einschränken), `GN_GAZETTEER_REVERSE_KM`, `GN_DUMP_DIR`.

**Was damit aus der eigenen Datenbank kommt** (sobald die Datei existiert; sonst wie bisher aus Pelias/Nominatim/Photon):
- `findNearbyPlaceName`, `findNearby`, `extendedFindNearby`: Orte/Objekte nach Entfernung (Koordinaten-Index), Kreis wächst 2 → 10 → 50 → 250 → 1000 km, bis etwas gefunden ist.
- `findNearbyPostalCodes`, `postalCodeSearch`, `postalCodeLookup`: aus der GeoNames-PLZ-Tabelle.
- `search`: Volltext mit Prefix, `name_startsWith`, `name_equals`, `featureClass/Code`, Rechteck, `cities`, `orderby=population`, mehrere Länder; echte Gesamtzahl; `lang=` liefert den Namen in dieser Sprache.
- `get`, `hierarchy`, `children`: aus `hierarchy.txt` bzw. den Admin-Codes – vollständig und sofort, ohne Raster.
- `/reverse`: als letztes Glied der Kette (`GEO_CHAIN=photon,nominatim,pelias,gazetteer`) der nächste bewohnte Ort mit Bundesland und Land, wenn kein Server etwas weiß (Quelle `GeoNames`).

**IDs:** echte GeoNames-IDs liegen unter 1 Mrd. Synthetische IDs (OSM-Orte, Verwaltungsstufen aus Nominatim) werden jetzt ab 1 000 000 000 vergeben; bereits vergebene kleine IDs bleiben gültig, ein `get` mit so einer ID antwortet aber zuerst aus GeoNames.
**Grenzen:** keine Straßen/Hausnummern (dafür bleiben Photon/Nominatim); keine fehlertolerante Suche bei Tippfehlern; Bezirk eines Punkts bei Grenzlage aus dem nächsten Ort abgeleitet. Daten: GeoNames, CC-BY 4.0 (https://www.geonames.org).

### search · get · hierarchy · countryInfo · address

```
/searchJSON?q=Musterdorf[&name=][&country=AT[,DE]][&maxRows=10][&startRow=0][&style=][&lang=][&name_startsWith=][&name_equals=][&featureClass=][&featureCode=][&east=&west=&north=&south=][&cities=][&orderby=population][&isNameRequired=true]   → totalResultsCount + geonames[]
/getJSON?geonameId=…[&style=]
/hierarchyJSON?geonameId=…[&style=]
/findNearbyJSON?lat=…&lng=…[&radius=1][&maxRows=10][&featureClass=][&featureCode=][&style=][&lang=]
/extendedFindNearbyJSON?lat=…&lng=…
/childrenJSON?geonameId=…[&maxRows=200][&style=]
/countryInfoJSON[?country=AT]
/addressJSON?lat=…&lng=…
```

- **search:** erster Treffer-liefernde Geocoder aus `GN_SEARCH_ORDER` (Standard `pelias,nominatim,photon`; alt: `SEARCH_BACKEND_ORDER`) – kein Mischen. `country` filtert, `startRow`/`maxRows` blättern, `lang` geht direkt an den Aufruf (dadurch sind `name` und `toponymName` gleich).
- **search-Filter:** `name_startsWith` nutzt bei Pelias `/v1/autocomplete` (echte Prefix-Suche), `east/west/north/south` und `country` gehen als Grenzen an den Geocoder; alles genaue (Prefix/Gleichheit, `featureClass`/`featureCode`, Rechteck, `cities`, `orderby=population`) wird danach auf den normalisierten Treffern angewendet – bei Filtern wird deshalb mehr geholt (bis 100).
- **findNearby:** nächste Objekte jeder Art im Radius (Pelias-Reverse, sonst Nominatim, sonst Photon), nächstes zuerst. **extendedFindNearby:** nächstes Objekt, dann Ort › Bezirk › Bundesland › Land. `fcl`/`fcode` sind aus den Quelldaten abgeleitet, daher filtert `featureClass`/`featureCode` auf diese Ableitung.
- **children:** Unterebene eines Landes (Bundesländer), Bundeslands (Bezirke) oder Bezirks (Gemeinden). Kein Umkreis, sondern die Grenze: Nominatim liefert den Umriss des Elternteils, darüber liegt ein Raster, jeder noch nicht abgedeckte Punkt wird mit passendem Zoom abgefragt und nur akzeptiert, wenn Land, Bundesland und Bezirk in der Adresse stimmen. Der Umriss jedes Treffers deckt weitere Rasterpunkte ab, daher reichen meist wenige Dutzend Abfragen. Die IDs sind dieselben wie in `hierarchy`. Grenzen: ein Kind, das kleiner als der Rasterabstand ist und ganz in einem größeren Nachbarn liegt, kann fehlen; nach `GN_CHILDREN_MAX_PROBES` (400) endet der Lauf mit `"complete": false` und ohne Cache. Gemeinden direkt unter dem Bundesland, wenn das Land keine Bezirke hat.
- **get:** liefert einen Ort, den der Service **schon einmal ausgegeben hat** (Schnappschuss in `geonames-ids.sqlite`, wie beim Wrapper) – unbekannte IDs → Status 15. Photon kennt keine Suche nach ID, deshalb dieser Weg.
- **hierarchy:** Land › Bundesland › Bezirk › Ort + der Ort selbst, aus **einem** Nominatim-Reverse am Ort; die Zwischenstufen bekommen eigene, stabile synthetische IDs. Der Landesname kommt aus `countryInfo.txt` (also „Austria“).
- **countryInfo:** GeoNames' `countryInfo.txt` kommt aus deinem GeoNames-Volume (`GEONAMES_HOST_DIR` → `/geonames/countryInfo.txt`, nicht im Image). Mit `country=` kommt die Bounding Box (south/north/east/west) aus Nominatim dazu und wird dauerhaft gecacht; die Gesamtliste hat keine Boxen.
- **address:** Straße, Hausnummer, PLZ, Ort, `adminCode1` am Punkt (Nominatim, Zoom 18); gecacht.

### postalCodeSearch · postalCodeLookup

```
/postalCodeSearchJSON?postalcode=2552[&country=AT][&placename=][&maxRows=10]    → {"postalCodes":[{postalCode, placeName, countryCode, lat, lng, adminCode1, adminName1, adminCode2, adminName2}]}
/postalCodeSearch?…                                                              → XML, <code> je Zeile
/postalCodeLookupJSON?postalcode=2552[&country=AT][&maxRows=20]                  → {"postalcodes":[{postalcode, placeName, …}]}   (nur JSON, wie bei GeoNames)
```

- Quelle: Nominatim (`postalcode=` bzw. bei reinem `placename` Freitext-Suche), `country` wird als `countrycodes` übergeben. **Die Qualität hängt davon ab, welche Postleitzahlen dein Nominatim-Import kennt** (OSM `addr:postcode` / `postal_code`-Grenzen).
- `postalcode` gilt als Präfix (`25` findet 2552 und 2544). Mit `postalcode` **und** `placename` wird zusätzlich nach dem Ortsnamen gefiltert.
- `placeName` ist der Ort (Stadt/Gemeinde/Dorf) aus der Adresse – nicht die PLZ selbst, die Nominatim bei PLZ-Objekten sonst als „Namen“ liefert. Gleiche (PLZ, Ort, Position) wird nur einmal ausgegeben; die Feldschreibweise (`postalCode` vs. `postalcode`) folgt der echten GeoNames-API.
- Gecacht wie die anderen Endpunkte; leere Ergebnisse nicht.

**Alle Endpunkte mit klickbaren Beispiel-Links:** siehe [ENDPOINTS.md](ENDPOINTS.md).

## Höhe und Overrides

**Höhe über Meer** (`/elevation`, `/srtm3JSON`, `/astergdemJSON`, `/gtopo30JSON`, `elevation_m` in `/context`):
Override → Cache → lokale SRTM3-Kacheln (`./geo-data/srtm/N47E016.hgt`, 1201×1201 oder 3601×3601, auch `.hgt.gz`) →
Open-Meteo (`ELEVATION_PROVIDER=local,open-meteo`). Jeder gefundene Wert bleibt im Cache. An Open-Meteo gehen nur auf
3 Nachkommastellen (~110 m) gerundete Koordinaten. Beim Start lädt der Dienst im Hintergrund einmalig die 27 Kacheln für
Österreich (~78 MB, `GEO_SRTM_PRELOAD=austria`, leer = nichts laden). Mehrere Gebiete per Komma:
`GEO_SRTM_PRELOAD=austria,germany,italy,croatia,slovenia,sri_lanka,maldives` (weitere: `switzerland czechia slovakia hungary poland france spain portugal greece bosnia serbia romania bulgaria netherlands belgium uk ireland denmark sweden norway finland iceland turkey arabia uae oman saudi_arabia qatar bahrain kuwait jordan egypt north_africa usa usa_mainland alaska hawaii`, auch deutsche Namen wie `deutschland`/`malediven`; `europe` = mehrere GB und lange Ladezeit). Unbekannte Namen werden im Log gemeldet und ignoriert. Andere Gebiete von Hand:
`sh tools/fetch_srtm.sh --region germany,italy`, `--bbox 45,5,48,15` oder `--tiles N47E016`. Quelle: AWS Terrain Tiles (SRTM-basiert), auf das
SRTM3-Raster ausgedünnt. Alles außerhalb der Kacheln kommt bei Bedarf von Open-Meteo (und danach aus dem Cache).
`astergdem` und `gtopo30` liefern denselben Wert wie `srtm3`.

**Overrides** (`./geo-data/overrides.json`, Vorlage `overrides.example.json`) liegen *über* Cache und Geocodern: der Cache
bleibt unverändert, Löschen des Overrides bringt die Originalantwort zurück. Pro Eintrag `lat`, `lng`, `radius_m`
(Standard 25 m) und mindestens einen Abschnitt: `address` (Felder von `/addressJSON`), `reverse` (`country`, `country_a`,
`state`, `county`, `city`, `locality`, `street`, optional `sublocation`/`label`) oder `elevation` (Meter). Der nächste
Eintrag innerhalb des Radius gewinnt. Verwaltung per Datei oder `GET/POST /overrides`, `DELETE /overrides/{id}`.
Das Sidecar/NFO-Ergebnis der Suite bekommt den korrigierten Ortsnamen, weil die Suite `/reverse` fragt.

## timezone und Mehrfach-Höhen

`/timezoneJSON?lat&lng[&date]` (und XML `/timezone`) liefert wie GeoNames `timezoneId`, `countryCode/Name`, `gmtOffset`
(1. Jänner), `dstOffset` (1. Juli), `rawOffset`, aktuelle Ortszeit sowie `sunrise`/`sunset` (Sonnengleichung, ±ca. 2 min;
`null` bei Polartag/-nacht). Alles offline (timezonefinder + tz-Datenbank). `srtm3`/`astergdem`/`gtopo30` nehmen auch
`lats=a,b&lngs=c,d` (max. 20 Punkte; Klartext = eine Zahl je Zeile, JSON = `{"geonames":[…]}`).

## Override-Editor (GUI)

`http://<host>:8430/ui` (auch `/`): Adresse **oder** Koordinaten eingeben → der Editor holt über dieselben Wege wie im Normalbetrieb
(Cache → Geocoder, Höhe, Zeitzone) alle Werte und zeigt sie als **Original**. Einzelne Felder überschreiben (gelb markiert; ↺ holt das Original
zurück), Radius/ID/Notiz setzen, **speichern** → geschrieben wird nur, was vom Original abweicht, in die Overrides-Datei
(`./geo-data/overrides.json`, gilt sofort). Ein schon vorhandener Override an der Stelle wird mitgeladen; unten Liste zum Bearbeiten/Löschen.
Ist `GEO_SERVICE_TOKEN` gesetzt, fragt die Seite nach dem Token (oder `/ui?token=…`).
Der Editor nutzt `GET /overrides/lookup?lat&lng` (Originalwerte ohne Overrides + passender Override).

## Nominatim-/Photon-Ports für Traccar und Dawarich

Traccar und Dawarich erwarten das Antwortformat von **Nominatim** bzw. **Photon**, nicht das eigene `/reverse`-JSON. Dafür gibt es zwei zusätzliche Ports im selben Container (derselbe Prozess, derselbe Cache, dieselben Overrides):

| Host-Port | Container | API | Pfade |
|---|---|---|---|
| 8431 | 8001 (`GEO_NOMINATIM_PORT`) | Nominatim | `/reverse` (`format=json\|jsonv2\|geojson\|geocodejson`, ohne `format` XML), `/search`, `/status` |
| 8432 | 8002 (`GEO_PHOTON_PORT`) | Photon | `/reverse`, `/api`, `/status` |

- Reihenfolge: Cache → Backends nach `GEO_CHAIN` (Nominatim liefert Hausnummer/PLZ, ebenso Photon/Pelias) → `MARITIME_SERVER` als Notanker → **Overrides darüber** (Adresse und Reverse-Abschnitt; die „Volksschule statt Veranstaltungshalle“ erscheint also auch in Traccar/Dawarich). Ein Override allein genügt, auch wenn kein Backend den Ort kennt.
- Der Cache enthält die rohe Backend-Antwort; Overrides wirken erst beim Ausliefern, wie überall sonst.
- **Kein Token nötig** (die Clients können ihn meist nicht senden; es gibt hier nur Ortsabfragen, keine Tracks). `GEO_COMPAT_AUTH=true` + `GEO_SERVICE_TOKEN` schaltet ihn ein (`?token=`, `?key=`, `X-Api-Key`, Bearer). Port nicht ins Internet veröffentlichen.
- `GEO_NOMINATIM_PORT=0` / `GEO_PHOTON_PORT=0` schaltet einen Port ab; ohne `ports:`-Eintrag in compose ist er von außen nicht erreichbar.
- Einstellen: **Traccar** `geocoder.type=nominatim`, `geocoder.url=http://<host>:8431/reverse` · **Dawarich** `NOMINATIM_API_HOST=<host>:8431` oder `PHOTON_API_HOST=<host>:8432`.
- Ehrlich: Format und Felder sind gegen die Nominatim-/Photon-Spezifikation und typische Antworten getestet, **nicht** gegen echte Traccar-/Dawarich-Instanzen. Bitte nach dem Umstellen kurz prüfen, dass Adressen ankommen (Traccar: Position → Adresse; Dawarich: Besuche/Places).

## Volumes: alle Host-Pfade in der `.env`

Die linke Seite jedes Volume-Mappings steht in der `.env` (neben der `docker-compose.yml`); die Container-Pfade sind fest:

| Variable | Standard | Container-Pfad | Inhalt |
|---|---|---|---|
| `MEDIA_HOST_DIR` | `/mnt/NAS` | `MEDIA_ROOT` (Suite) | Foto-/Video-Share |
| `CONFIG_HOST_DIR` | `./config` | `/config` (Suite) | `presets.json`, `camera_names.json`, Orte |
| `GEO_DATA_HOST_DIR` | `./geo-data` (standalone: `./data`) | `/data` | Cache, `overrides.json`, `srtm/` |
| `GEONAMES_HOST_DIR` | `./geonames` | `/geonames` (nur lesen) | Ordner mit deinen GeoNames-Dateien; daraus wird `countryInfo.txt` gelesen |

## GeoNames-Volume (statt Datei im Image)

Im Image liegt **keine** GeoNames-Datei mehr. `countryInfo.txt` wird aus dem Volume gelesen: Ordner mit der Datei in `GEONAMES_HOST_DIR` eintragen (→ `/geonames`, nur lesen; anderer Name/Pfad: `GN_COUNTRY_INFO_FILE`). Fehlt die Datei, läuft der Service normal weiter; nur `/countryInfo` und Ländernamen in `/hierarchy`/`/timezone` bleiben leer bzw. melden den Fehler. Der Maritime-Resolver bleibt extern (`MARITIME_SERVER`); der Service baut dafür nichts selbst.

## Karte im Override-Editor (dein tileserver-GL) – Radius und Fläche

`GEO_TILE_URL` auf die **Raster-Kacheln** deines tileserver-GL setzen, z. B.
`GEO_TILE_URL=http://192.168.1.33:8777/styles/protomaps-light/{z}/{x}/{y}.png` (optional `GEO_TILE_ATTRIBUTION`, `GEO_TILE_MAXZOOM`).
Die URL muss vom **Browser** aus erreichbar sein (nicht nur vom Container). Ohne `GEO_TILE_URL` bleibt der Editor wie bisher ohne Karte. Die Karte öffnet auf **Zuhause** (Zoom 15): `GEO_HOME_LAT`/`GEO_HOME_LON`, sonst der Ort der Art „Zuhause“ in *Meine Orte*; ohne beides passt sie sich an alle Overrides an.
Die Kartenbibliothek (Leaflet) liegt im Image; es werden keine externen Skripte geladen. Auch in der Suite (iframe) verfügbar.

- **Punkt wählen:** auf die Karte klicken (oder Adresse suchen / Koordinaten eingeben); der Marker ist verschiebbar. Klick in ein vorhandenes Gebiet lädt dessen Override.
- **Radius:** Feld „Radius (m)“ oder „Radius auf Karte setzen“ + Klick (Abstand zum Marker). Der Kreis wird live gezeigt.
- **Fläche:** „Fläche zeichnen“ → Eckpunkte anklicken → „Fläche fertig“; Punkte sind verschiebbar, Rechtsklick löscht einen Punkt. Alles **innerhalb** der Fläche nimmt den Override, egal wie weit vom Ankerpunkt; der Radius gilt dann nicht. Gespeichert als `"polygon": [[lat, lng], …]` in `overrides.json` (siehe `overrides.example.json`).
- Vorhandene Overrides erscheinen grau gestrichelt auf der Karte (abschaltbar).
- Ehrlich: Die Karte nutzt Raster-Kacheln (Vektor-Stil rendert tileserver-GL serverseitig); Polygone über den Antimeridian (±180°) werden nicht unterstützt.



## Kontext-Extras: Licht, Mond, Feiertage, Reisen, Sehenswürdigkeiten, Uhr-Check

`/context` liefert zusätzlich (alles ohne Netz außer Sehenswürdigkeiten, Reisen und Wetter):

* **Tageslicht** (`daylight`): Phase `Nacht` / `Dämmerung` / `blaue Stunde` / `goldene Stunde` / `Tag` aus der Sonnenhöhe (Aufnahmezeit + Ort), dazu `sunrise`/`sunset` (Ortszeit). Genauigkeit der Sonnenzeiten ca. ±2 Minuten.
* **Mondphase** (`moon`) und **Feiertag** (`holiday`, `weekend`): Feiertage über die Bibliothek `holidays` (gepinnt in `requirements.txt`), deutsche Namen; regional (Bundesland) nur für AT und DE, sonst landesweit.
* **Länderinfo** (`country_info`): Hauptstadt, Währung, Sprachen aus `countryInfo.txt` deines GeoNames-Volumes – **nur wenn das Land nicht `GEO_HOME_COUNTRY` ist**.
* **Wetter erweitert**: Wind (km/h + Wort), Niederschlag (mm), Bewölkung, Luftfeuchte. Alte Archivzeilen werden beim nächsten Zugriff einmalig nachgeladen.
* **Sehenswürdigkeiten** (`/sights`, `/pois`, `pois`; ein gemeinsamer Code, `sights.py`; Details unten): über **deinen Photon** (`PHOTON_SERVER`, Filter nach OSM-Tags: Attraktion, Museum, historisch, Aussichtspunkt, Gipfel, Wasserfall), nach Entfernung sortiert, ~100 m-Zellen im Cache. Dazu deine **Meine Orte** mit Häkchen „auch als Sehenswürdigkeit“. Ehrlich: ältere Photon-Versionen ignorieren den Tag-Filter – dann wird clientseitig gefiltert, es kommen mehr Anfragen-Rohdaten, aber dieselbe Auswahl; ob dein Photon das kann, ist in der Sandbox nicht prüfbar.
* **Reisen automatisch** (`/trips`, `/trip-at`, `trip`): aus dem GPS-Verlauf (Traccar/Dawarich). Zuhause = `GEO_HOME_LAT/LON`, sonst ein Ort der Art **Zuhause** in „Meine Orte“, sonst die häufigste Nachtposition. Ein Tag ist „unterwegs“, wenn seine Mittelposition > `GEO_TRIP_AWAY_KM` entfernt ist; ≥ `GEO_TRIP_MIN_DAYS` zusammenhängende Tage = Reise (kurze Zuhause-Pausen bis `GEO_TRIP_MAX_GAP_DAYS` und bis zu 5 Tage ohne Messpunkte werden überbrückt). Name: besuchte Länder („Italien & Slowenien“), im Inland die meistbesuchte Region. Eine Berechnung gilt für ein 30-Tage-Fenster und wird gecacht. Ein Ordnername der Suite (`YYYY.MM.DD Name`) hat Vorrang.
* **Orte vereinheitlichen**: Präfixe wie „Marktgemeinde“ / „Politischer Bezirk“ werden entfernt (`GEO_NORMALIZE_NAMES`), eigene Schreibweisen stehen in `place-names.json` im Datenordner (`{"city": {"Wien, Innere Stadt": "Wien"}, "*": {...}}`, wird bei Änderung neu geladen). Gilt für `/reverse`, `/context` und die Nominatim-/Photon-Ports; Overrides haben das letzte Wort. **Meine Orte**: im Override-Editor unter „Mein Ort“ Name/Art/Häkchen setzen – Liste unter `/places`; die Suite zeigt sie im GPS Sidecar zusammen mit der alten `predefined_locations` (Import-Knopf auf der Geo-Overrides-Seite).
* **Uhr-Plausibilität** (`/clock-check`): GPS-Satellitenzeit der Datei gegen Aufnahmezeit; bei Dateien mit **eigener** Kamera-GPS-Position wird die Spur zur Aufnahmezeit verglichen und bei Abweichung die Zeitverschiebung vorgeschlagen (ganze Stunden = Zeitzone/Sommerzeit?); Jahr < 2001 / Zukunft = Kamera-Uhr nie gestellt. Eine aus der Spur *gefüllte* Position beweist nichts und wird von der Suite nicht geschickt.

**`/sights` im Detail.** Position per `lat`+`lon`, Ortsname `q` (eigene GeoNames-Datenbank, dann Nominatim/Photon/Pelias) oder `address`. `radius` bis 20000 m (Standard 2000), `limit` bis 50 (Standard 10), `lang`, `sort=score` (Standard) oder `distance`, `categories` als Liste aus `castle, museum, attraction, zoo, theme_park, waterfall, viewpoint, religious, ruins, nature_reserve, monument, peak, historic, own, water` (Flüsse und Seen, braucht `MARITIME_SERVER`). Die Antwort enthält je Treffer `name, kind, category, score, distance_m, bearing, direction (N/NO/O …), lat, lon, osm` und, wenn Photon sie liefert, `description, opening_hours, website, phone, wikidata, wikidata_url, wikipedia, wikipedia_url`.
Reisetitel ohne Track: Liegt ein Foto in keinem Reiseordner und auf keiner aufgezeichneten Reise, aber weiter als `GEO_PLACE_TRIP_KM` (Standard 100) von Zuhause, liefert `/context` als `trip` einen Ortstitel wie „Triest und Umgebung“ (`source: place`; XMP `CtxTripSource` = `place`). Zuhause kommt aus `GEO_HOME_LAT/LON` oder einem „Meine Orte“-Eintrag der Art Zuhause; 0 schaltet es ab.

Burgen und Schlösser werden präziser benannt: Aus OSM `castle_type` wird „Schloss“, „Palast“, „Herrenhaus“, „Festung“, „Burg“, „Kreml“, „Zitadelle“ oder „Römerkastell“ statt pauschal „Burg“. Die neue Spalte `castle_type` entsteht beim Neuaufbau des Index (`build`, ca. 1 h); bis dahin bleibt es bei „Burg“.

Wertung: Kategoriegewicht (Burg 1,0 · Museum 0,95 · Attraktion 0,9 … historisch 0,5) × Abstandsabfall `1/(1+(d/d0)²)` mit `d0 = max(120 m, 20 % des Radius)`, dazu ein Bonus für Wikidata/Wikipedia (+0,30), Website (+0,05), Öffnungszeiten (+0,03), Beschreibung (+0,02); Meine Orte zählen wie 0,95 + 0,10. Dubletten (gleiche Wikidata-ID, gleiche OSM-Id oder gleicher Name innerhalb 250 m, zum Beispiel Punkt und Gebäudeumriss) werden zusammengefasst. `/pois` und `/context` rufen denselben Code auf, bleiben aber nach Entfernung sortiert. Der Rohdaten-Cache heißt jetzt `v3` (Zellen um 100 m, Radius-Stufen 0,5/1/2,5/5/10/20 km); alte `v2`-Listen werden nicht mehr gelesen.
Grenze von Photon: jede Anfrage liefert nur die nächsten Objekte (bis 50 je Tag). In einer dichten Stadt kann ein berühmtes, weiter entferntes Ziel fehlen – das behebt der geplante eigene Sehenswürdigkeiten-Index aus der Planet-Datei.
`details` (Öffnungszeiten, Website, Wikidata …) erscheinen nur, wenn dein Photon-Index die OSM-Zusatzfelder (`extra`) mitführt.

Sehenswürdigkeiten sind nur echte Ziele: Gedenktafeln/Mahnmale (`historic=memorial`), Gräber, Wegkreuze, Grenzsteine und „irgendein altes Haus“ (`historic=building`) werden ausgefiltert; weitere Werte per `GEO_POI_SKIP`. Cache-Dauer von Sehenswürdigkeiten, Reisen und abgeschlossenen Track-Tagen = `GEO_CACHE_TTL_DAYS` (Reisen der letzten 14 Tage 1 h, laufende Track-Tage 5 min, leere Track-Tage 7 Tage).

Neue Einstellungen (`.env`): `GEO_HOME_COUNTRY`, `GEO_HOME_LAT/LON`, `GEO_TRIP_AWAY_KM`, `GEO_TRIP_MIN_DAYS`, `GEO_TRIP_MAX_GAP_DAYS`, `GEO_TRIP_LOOKBACK_DAYS`, `GEO_POI`, `GEO_POI_RADIUS_M`, `GEO_POI_MAX`, `GEO_NORMALIZE_NAMES`, `GEO_NAME_ALIASES_FILE`.

### Straße mit Hausnummer im Override

Steht im `reverse`-Abschnitt eines Overrides `"street": "Beispielgasse 84"`, trennen die Nominatim-/Photon-Schnittstellen die Hausnummer ab (Straße `Beispielgasse`, Hausnummer `84`) und verwerfen die Nummer, die der Geocoder für die rohen Koordinaten gefunden hat. Traccar zeigt dann `84 Beispielgasse, …` statt `86 … 84`.

### Herkunft und Alter im `/reverse`-Ergebnis

`GET /reverse` nennt zusätzlich zu `cached` (true/false) die **Herkunft des Roh-Werts** und **wann er in den Cache kam**: `upstream_source` (z. B. `Photon`, `Nominatim`, `Pelias`, `Maritime`), `cached_at` (ISO-Zeit in deiner Zeitzone) und `cached_age_s` (Alter in Sekunden). Ersetzt ein Override die Antwort, steht `source` auf `override` (und `override` nennt die ID), `upstream_source` und `cached_at` beschreiben aber weiterhin den darunterliegenden Geocoder-Treffer.

* **Warum kein Wetter?** `/context` liefert `weather_status`: `ok`, `off` (`WEATHER_PROVIDER=none`), `limited` (Open-Meteo HTTP 429), `failed` (HTTP-Fehler / vom Container aus nicht erreichbar), `empty` (keine Stunden für Tag/Zelle), `no-hour` (Stunde oder Wettercode fehlt). Die Suite zeigt den Grund beim „Kontext nachfüllen“.

* `POST /weather/backfill` nimmt zusätzlich `"refresh": "none|bad|all"`: `bad` holt gespeicherte Tage ohne Stunden/Wettercode oder aus alter Zeilenversion neu, `all` jeden angefragten Tag (mit `dry_run` vorab zählen).

## Sehenswürdigkeiten-Index (`/sights` ohne Photon)

> Die Hilfsdienste dieses Abschnitts (Profile `import`, `refresh`, `wikimedia`) sind im Hauptordner in [README-PROFILES.md](../README-PROFILES.md) vollständig beschrieben: wann, wo und wie, einschließlich `docker compose --profile '*' build`.

Standardmäßig fragt `/sights` Photon pro OSM-Tag. Mit einem eigenen Index geht es lokal und in Millisekunden: ein Import-Container liest deine vorhandene **Planet-PBF (Nominatim) nur lesend** und baut `sights.sqlite` (SQLite + R-Tree) neben dem Cache.

```bash
# .env: GEO_OSM_PBF_HOST_DIR=/mnt/ssd/nominatim-planet/data     (Ordner mit planet-latest.osm.pbf)
docker compose --profile import build sights-import
docker compose --profile import run --rm sights-import all       # osmium-Filter + Export + Wikidata
docker compose --profile import run --rm sights-import status
docker compose up -d geo-service                                  # GEO_SIGHTS_DB leer = <data>/sights.sqlite
```

* **Auswahl:** `tourism` = attraction/museum/viewpoint/zoo/theme_park, `historic` ohne den Skip-Katalog (`GEO_POI_SKIP`), `natural` = peak/waterfall, `leisure` = nature_reserve. Behalten wird nur, was einen Namen hat und (wikidata/wikipedia hat **oder** zur Kernkategorie gehört).
* **Schritte einzeln:** `build` (nur OSM), `wikidata` (Sitelinks, Kurzbeschreibung de/en; 50 IDs je Abfrage, **fortsetzbar** – einfach nochmal starten), `all`, `status`.
* **Neuaufbau** ersetzt die Datei atomar (`.building` → Umbenennung); bereits geholte Wikidata-Daten werden übernommen.
* **Platz/Zeit:** Arbeitsordner einige GB während des Laufs; die Planet-Datei bleibt unangetastet. Die PBF muss nicht aktuell sein.
* **Ehrlich:** Pipeline und Format sind mit einer kleinen Test-PBF und simuliertem Wikidata geprüft, **nicht** gegen die echte 85-GB-Planet-Datei und das echte Wikidata. Bei Problemen die Ausgabe von `status` und das Log schicken.

### Automatische Aktualisierung (`refresh`, `schedule`)

Der Index kann sich selbst auf dem Laufenden halten. Ein Dienst `sights-refresh` (Profil `refresh`) führt im eingestellten Abstand die ganze Kette aus: **Downloads** → **Neuaufbau** (nur wenn die Planet-PBF neuer ist) → **Wikidata** (nur neue Objekte) → **Importance** (nur wenn die Datei neu ist oder neu gebaut wurde) → **Wikipedia-Kurztexte** → **Übersetzung**. Jeder Schritt läuft für sich: schlägt einer fehl, laufen die nächsten trotzdem, und das Ergebnis steht in `/sights/status` (und auf der Info-Seite der Suite).

```bash
# .env
GEO_SIGHTS_REFRESH_DAYS=30            # alle 30 Tage (0 = aus)
GEO_SIGHTS_REFRESH_AT=03:30           # um diese Uhrzeit
GEO_DOWNLOAD_HOST_DIR=/mnt/ssd/sights-downloads
# optional: auch die Planet-PBF laden (ca. 85 GB) statt die von Nominatim zu verwenden
#GEO_SIGHTS_PBF_URL=https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf

docker compose --profile import build sights-import       # einmalig (das Image wird geteilt)
docker compose --profile refresh up -d sights-refresh
docker compose --profile import run --rm sights-import refresh    # oder einmal von Hand
```

* **Downloads:** `GEO_SIGHTS_IMPORTANCE_URL` (Standard: zwei Quellen, `downloads.opencagedata.com/public/wikimedia_importance/%YYYYMM%/wikimedia_importance.tsv.gz` und `nominatim.org/data/wikimedia-importance.csv.gz`; geladen wird die mit dem neueren Änderungsdatum, eine nicht erreichbare wird übersprungen; mehrere Adressen mit Komma getrennt, `off` = nicht laden). `%YYYYMM%` in einer Adresse steht für den Monatsordner: OpenCage legt die Dateien in `…/public/202610/` ab und lässt manche Monate aus, deshalb wird der aktuelle Monat und dann rückwärts (bis 24 Monate) mit je einer HEAD-Anfrage probiert, ohne Verzeichnisliste und `GEO_SIGHTS_PBF_URL` (Standard leer: die gemountete `GEO_OSM_PBF_FILE`, die dein Nominatim pflegt, wird genutzt). Geladen wird nur, wenn der Server eine neuere Datei hat (ETag / Last-Modified); ein abgebrochener Download wird per Range-Anfrage fortgesetzt; erst die vollständige und geprüfte Datei (gzip bzw. OSM-Header) wird übernommen.
* **Aufbewahrung:** je Quelle bleiben die **neueste** Datei und die **letzte davor, die erfolgreich benutzt wurde** (Neuaufbau bzw. Import mit ihr lief durch); alles Ältere wird gelöscht. Ist die neueste noch nie erfolgreich benutzt worden, bleibt zusätzlich die letzte funktionierende. Dazu bleibt die bisherige `sights.sqlite` als `sights.sqlite.prev` (Hardlink, kostet keinen Platz; `GEO_SIGHTS_KEEP_PREV=0` schaltet es ab). Dateinamen: `wikimedia-importance.2026-10-24.csv.gz`, Liste in `manifest.json`.
* **Schritte abschalten:** `GEO_SIGHTS_REFRESH_WIKITEXT=0`, `GEO_SIGHTS_REFRESH_TRANSLATE=0`; `GEO_SIGHTS_REFRESH_FORCE_BUILD=1` baut in jedem Lauf neu.
* **Ehrlich:** Ablauf, Fortsetzen, Aufbewahrung und Zeitplan sind mit simulierten Servern getestet; die echten Downloads (und ein Neuaufbau aus der echten Planet-Datei im Dienst) hier nicht.

### Wikimedia-Wichtigkeit selbst erzeugen (`wikimedia-build`)

Die fertige Datei von nominatim.org ist oft veraltet. `wikimedia-build` baut `wikimedia_importance.tsv.gz` mit dem Generator, den auch das Nominatim-Team nutzt ([osm-search/wikipedia-wikidata](https://github.com/osm-search/wikipedia-wikidata)), in einem eigenen Container mit PostgreSQL:

```
docker compose --profile wikimedia build wikimedia-build
docker compose --profile wikimedia run --rm wikimedia-build once         # jetzt einmal bauen
docker compose --profile wikimedia up -d wikimedia-build                 # danach alle GEO_WIKIMEDIA_BUILD_DAYS Tage (30) um GEO_WIKIMEDIA_BUILD_AT (03:00)
docker compose --profile wikimedia run --rm wikimedia-build status
```

Aufwand: etwa 150 GB Platz (90 GB PostgreSQL in `GEO_WIKIMEDIA_PGDATA_HOST_DIR`, 60 GB Downloads in `GEO_WIKIMEDIA_WORK_HOST_DIR`, beides nach einem guten Lauf wieder geleert) und 9 bis 12 Stunden bei 39 Sprachen. Für einen kleinen, schnellen Lauf `GEO_WIKIMEDIA_LANGUAGES=de,en` setzen. Der Generator nimmt selbst den neuesten vollständigen Wikimedia-Dump; ist noch keiner komplett, tut der Lauf nichts und versucht es später wieder. Ein abgebrochener Lauf setzt mit den schon geladenen Dateien fort.

Ergebnis im Ordner `GEO_WIKIMEDIA_HOST_DIR` (dort liegt auch die bisherige Datei): `wikimedia_importance-JJJJMMTT.tsv.gz` (neueste und die davor bleiben, `GEO_WIKIMEDIA_KEEP`) und `wikimedia_importance.tsv.gz` als aktuelle Kopie. Der Schritt `importance` (auch in der automatischen Aktualisierung) nimmt von heruntergeladener, konfigurierter und selbst gebauter Datei **die neueste** und läuft neu, sobald sich Name oder Zeitstempel gegenüber dem letzten Lauf ändern. Den Download der alten Datei schaltest du mit `GEO_SIGHTS_IMPORTANCE_URL=off` ab.

### Wikipedia-Kurztext (`wikitext`)

Wikidata liefert nur Zweiwort-Beschreibungen („Kirche in Wien“). `wikitext` holt zusätzlich den **Anfang des Wikipedia-Artikels** (die ersten Sätze, Deutsch und Englisch) und speichert ihn in der Datenbank; `/sights`, `/context`-Texte und die Handy-Seite `/nearby` zeigen ihn als `summary` („Mehr“ aufklappen).

```bash
docker compose --profile import run --rm sights-import wikitext     # fortsetzbar, 20 Artikel je Abfrage
docker compose --profile import run --rm sights-import status       # "wikipedia_extracts"
```

Den Artikeltitel liefert der Schritt `wikidata` (Sitelinks) oder, falls der fehlt, der OSM-Tag `wikipedia=de:Titel`. Mit `GEO_SIGHTS_WP_SCOPE=notable` (Standard) braucht es zusätzlich die Wichtigkeit (`importance`). Gibt es nichts zu holen, nennt der Lauf die Zahlen (Sehenswürdigkeiten, mit Titel, mit Wichtigkeit, schon gelesen) und den fehlenden Schritt.

* **Wer:** Sehenswürdigkeiten mit Artikeltitel (aus dem Wikidata-Lauf) – standardmäßig nur die mit Wikimedia-Wichtigkeit (`GEO_SIGHTS_WP_SCOPE=notable`), `all` nimmt alle. Danach zuerst `wikidata` und `importance`, weil die Titel von dort kommen.
* **Maße:** `GEO_SIGHTS_WP_SENTENCES` (3 Sätze), `GEO_SIGHTS_WP_MAXCHARS` (400 Zeichen, Satzende bevorzugt), `GEO_SIGHTS_WP_LANGS` (`de,en`), `GEO_SIGHTS_WP_DELAY`/`_WORKERS` (höflicher Takt wie beim Wikidata-Import), `GEO_SIGHTS_WP_MAX` (Probelauf).
* Ein Neuaufbau des Index übernimmt die Texte. Ehrlich: mit simulierter Wikipedia getestet, nicht gegen die echte.

### Deutsche Beschreibungen per LLM (`translate`)

Viele Wikidata-Einträge haben keine deutsche Kurzbeschreibung; dann blieb bisher ein englischer Text. `translate` übersetzt diese Lücken mit **Ollama und/oder Google** (gleiche Einstellungen wie die Suite: `AI_PROVIDER`, `OLLAMA_URL`, `OLLAMA_MODEL`, `GOOGLE_AI_API_KEY`, `GOOGLE_AI_MODEL`) und schreibt sie in die Datenbank.

```bash
docker compose --profile import build sights-import
GEO_SIGHTS_TR_MAX=20 docker compose --profile import run --rm sights-import translate   # Probelauf
docker compose --profile import run --rm sights-import translate                        # alle, fortsetzbar
docker compose --profile import run --rm sights-import status                           # "descriptions_translated"
```

* **Modell:** `OLLAMA_MODEL_TEXT` (z. B. `qwen2.5:7b-instruct`) bzw. `GOOGLE_AI_MODEL_TEXT` hat Vorrang vor `OLLAMA_MODEL`/`GOOGLE_AI_MODEL`, damit nicht das Bildmodell übersetzt. Leer = die normalen Modelle.
* **Was:** nur Sehenswürdigkeiten ohne deutsche Wikidata-Beschreibung, mit englischem Text (Wikidata, sonst OSM-`description`). Standard `GEO_SIGHTS_TR_SCOPE=notable`: nur solche mit Wikimedia-Wichtigkeit oder Sitelinks, wichtigste zuerst; `all` nimmt alle. **Namen werden nicht übersetzt** (Eigennamen).
* **Schutz:** Das Modell soll `UNKNOWN` antworten statt zu raten. Echo, Erklärtext, noch englische Antworten und absurde Längen werden verworfen; der Eintrag gilt dann als gefragt (`GEO_SIGHTS_TR_RETRY=1` fragt ihn erneut, zusammen mit den noch nie gefragten; `GEO_SIGHTS_TR_RETRY=only` fragt nur die abgelehnten bzw. unbekannten). Ein Anbieter, der ausfällt (Timeout, Verbindung, HTTP-Fehler), pausiert kurz und wird später wieder versucht; 429/5xx führt zu Wartezeit. Der Lauf bricht erst ab, wenn 10 Minuten lang kein Anbieter antwortet.
* **Tempo:** `GEO_SIGHTS_TR_BATCH` (Standard 10 Beschreibungen je Anfrage) und `GEO_SIGHTS_TR_WORKERS` (Standard 2 parallel). Fehlende oder abgelehnte Zeilen einer Sammelanfrage werden einzeln (mit Wechsel zum nächsten Anbieter) erneut gefragt. Bei Ollama greifen parallele Anfragen nur mit `OLLAMA_NUM_PARALLEL` ≥ Workers.
* **Sprachen:** Englisch und jede andere Sprache außer Deutsch wird übersetzt; bereits deutsche OSM-Texte bleiben. Lehnt ein Anbieter eine Antwort ab (Echo, noch Englisch, Erklärtext), fragt der nächste in `AI_PROVIDER`.
* **Log:** Je Eintrag eine Zeile mit Anbieter, Original und Übersetzung bzw. Ablehnungsgrund (UNKNOWN, Echo, Erklärtext, noch Englisch, Länge); mit `GEO_SIGHTS_TR_LOG=0` nur der Fortschritt.
* **Nachvollziehbar:** Der Text steht in `desc_de_ai`, der Anbieter in `ai_src`. Wikidata-Texte haben Vorrang. Zurücksetzen: `UPDATE sight SET desc_de_ai='', ai_state=0, ai_src='';`. Ein Neuaufbau übernimmt die Übersetzungen.
* **Ehrlich:** Mit simulierten Anbietern getestet, nicht gegen dein Ollama/Google. Erst mit `GEO_SIGHTS_TR_MAX=20` ansehen, ob die Qualität passt.

## Regenradar (`/radar`)

`http://<host>:8430/radar` zeigt deine eigene Karte (Vektorstil des tileserver-GL) mit dem Radar der **letzten 2 Stunden** (10-Minuten-Schritte) als Animation: Play/Pause, Zeitregler, „Jetzt“, Standortknopf, Legende; die Karte öffnet bei deinem Zuhause (`GEO_HOME_LAT/LON` oder Ort der Art „Zuhause“). MapLibre GL ist im Image enthalten (`/ui-assets/maplibre-gl/`), die Seite braucht kein CDN. Standard-Radarquelle ist RainViewer (kostenlos, nur private Nutzung, **keine Vorhersage**, höchste Zoomstufe 7); die Quelle „RainViewer“ steht in der Kartenquellenangabe.

| Variable | Bedeutung |
|---|---|
| `GEO_RADAR_STYLE_URL` | MapLibre-Stil, z. B. `http://192.168.1.33:8777/styles/streets/style.json`; leer = aus `GEO_TILE_URL` abgeleitet (`…/{z}/{x}/{y}.png` → `…/style.json`) |
| `GEO_RADAR_API` | RainViewer-kompatible Quelle; für einen eigenen LibreWXR-Server `http://<host>:8080/public/weather-maps.json` |
| `GEO_RADAR_COLOR`, `GEO_RADAR_OPTIONS`, `GEO_RADAR_MAXZOOM` | Farbschema-ID (2), `smooth_snow` (`1_0`), höchste Radar-Zoomstufe (7) |

Der Browser lädt Stil und Radar direkt; läuft die Seite über HTTPS, müssen auch Stil-URL und Radar-API HTTPS sein (sonst „Mixed Content“). Nicht geprüft: die Farb-ID `2` bei echten RainViewer-Daten und die Abdeckung für Österreich. Möglicher Ausbau: eigener Radarserver (LibreWXR) und der GeoSphere-Nowcast (3 h Vorhersage, 1 km) mit einem kleinen Umwandler in Kacheln.

## PWA „In der Nähe“ (`/nearby`)

`http://<host>:8430/nearby` ist eine eigene Handy-Seite über `/sights`: Knopf **Mein Standort** (Browser-Standortfreigabe) oder Ortseingabe, Umkreis 500 m – 20 km, Filter nach Art, Sortierung nach Wichtigkeit oder Entfernung. Jeder Treffer zeigt Entfernung, Richtungspfeil, Öffnungszeiten, Beschreibung und Links (Wikipedia, Website, Karten-App). Die Karte nutzt dein tileserver-GL (`GEO_TILE_URL`, wie der Override-Editor); ohne Kachel-URL bleibt nur die Liste. **Layout:** Im Querformat auf großen Bildschirmen steht die Liste links und die Karte füllt den Rest in voller Höhe; im Hochformat und auf dem Handy liegt die Karte über die volle Breite (etwa die halbe Bildschirmhöhe) über der Liste, die sich auf breiten Bildschirmen in Spalten teilt. **Punkt auf der Karte setzen:** Rechtsklick, langes Drücken (Handy), Umschalt+Klick oder die Taste **P** (an der Mausposition, sonst Kartenmitte) startet sofort die Suche an dieser Stelle; der Punkt lässt sich danach ziehen. Koordinaten wie `47.0707, 15.4395` im Suchfeld gelten ebenfalls als Position. Umkreis, Art und Sortierung wirken weiter auf den gewählten Punkt.

* **Installierbar:** Manifest + Service Worker (Seitenrahmen offline, die Daten kommen immer live). „Zum Startbildschirm hinzufügen“ im Browser.
* **Standortfreigabe braucht HTTPS** (außer `localhost`). Im LAN per `http://192.168.1.33:8430` verweigern Chrome/Safari auf dem Handy die Position – dann Ortseingabe nutzen oder die Seite hinter deinem HTTPS-Reverse-Proxy öffnen.
* Mit `GEO_SERVICE_TOKEN` fragt die Seite nach dem Token (einmal pro Browser-Sitzung) oder nimmt `?token=`.

## Wasser im Reverse (Maritime-Server)

Antwortet der Maritime-Server (`MARITIME_SERVER`), landen die Angaben so im Ergebnis:

* **Offenes Wasser:** `country` = das Meer, in dem der Punkt liegt (aus den Polygon-Ebenen `contained_in`; in der gewünschten Sprache, bevorzugt IHO, dann Natural Earth – SeaVoX- und Wirtschaftszonen-Namen werden nie genommen). `locality` = das nächste benannte Objekt (Bucht, Meerenge …), aber nur wenn es höchstens 10 km entfernt ist; sonst trägt `locality` ebenfalls das Meer. Beispiel: „Uvala Vrulje, Adriatisches Meer“.
* **Land-Treffer, der eigentlich Wasser ist** (nur Land/Bundesland oder Objekt > 500 m entfernt): `country` bleibt das Land, `locality` = Gewässer, `state` = das Meer. Beispiel: „Straße von Gibraltar, Mittelmeer, Spanien“.
* Weiß der Maritime-Server nichts, nimmt der Dienst als letzten Ausweg das nächste Gewässer (Klasse H) der eigenen GeoNames-Daten im Umkreis von 300 km.

## Karte hinter HTTPS (`GEO_TILE_PROXY`)

Läuft die Seite über HTTPS (z. B. `https://geocoding.example.org/nearby`) und `GEO_TILE_URL` ist `http://192.168.1.33:8777/…`, blockiert der Browser die Kacheln („Mixed Content“) – die Karte bleibt grau. Auch ein Handy außerhalb des LANs erreicht den Tileserver nicht. Mit `GEO_TILE_PROXY=true` lädt der Browser die Kacheln von `/tiles/{z}/{x}/{y}` desselben Hosts; der geo-service holt sie intern von `GEO_TILE_URL` (die URL muss vom Container aus erreichbar sein) und liefert sie mit 24 h Browser-Cache aus. Kacheln sind reine Kartendaten und ohne Token abrufbar.

## Wikimedia-Importance für die Wertung

Nominatim liefert mit `wikimedia-importance.sql.gz` für jeden Wikipedia-Artikel einen Wert (0 – 1), wie viel Beachtung er bekommt. Der Import ordnet ihn jeder Sehenswürdigkeit zu (über die Wikidata-ID oder Sprache + Artikeltitel) und die Wertung nutzt ihn mit bis zu +0,25 (ab ca. 0,25 = gewöhnlicher Artikel, ca. 0,7 = weltberühmt). Dadurch schlägt der Stephansdom die Kapelle ums Eck auch dann, wenn beide eine Wikipedia-Seite haben. Das Sekundär-Raster (`wikimedia-secondary-importance`) ist ein Ortsraster für Nominatim und wird hier nicht gebraucht.

```bash
# .env: GEO_WIKIMEDIA_HOST_DIR=/mnt/ssd/nominatim-planet     (Ordner mit wikimedia-importance.sql.gz)
docker compose --profile import build sights-import
docker compose --profile import run --rm sights-import importance
docker compose --profile import run --rm sights-import status     # "with_importance": ...
```

* Erst **nach** dem Wikidata-Lauf starten (der liefert die Artikeltitel zum Zuordnen) – und nie gleichzeitig mit ihm: beide schreiben in `sights.sqlite`. `all` macht es automatisch am Ende, wenn die Datei da ist.
* Dauer: die Datei wird einmal gelesen (einige Minuten, je nach Platte); der Rest ist Sekunden.
* Die Spalte wird bei Bedarf selbst angelegt; ein Neuaufbau (`build`) übernimmt die Werte.
* Ehrlich: getestet mit einem selbst gebauten Dump im `COPY`- und im CSV-Format, nicht mit deiner echten Datei. Passt das Format nicht, bricht der Lauf mit „no importance rows found“ ab – dann die ersten 5 Zeilen von `zcat wikimedia-importance.sql.gz | head -20` schicken.
