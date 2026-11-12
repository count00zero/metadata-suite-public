# Compose-Profile: welche es gibt, wann, wo und wie man sie benutzt

`docker compose up -d` startet nur die zwei Dauerdienste **`metadata-suite`** (Port 8420) und **`geo-service`** (Ports 8430 bis 8432).
Alles Weitere sind **Hilfsdienste mit einem Profil**. Sie laufen nie von allein mit `up` und werden von `docker compose build`
ohne Profil-Angabe auch nicht gebaut. Das ist Absicht: Manche sind schwer (Stunden Laufzeit, 150 GB Platz) und sollen nur starten, wenn du es willst.

Alle Befehle gelten im Ordner der Compose-Datei, also bei dir `/opt/metadata-suite` (die Datei im Hauptordner enthält alle Dienste;
`geo-service/docker-compose.yml` enthält dieselben Hilfsdienste für den Betrieb des geo-service allein).

## Überblick

| Profil | Dienst | Art | Zweck | Dauer / Platz |
|---|---|---|---|---|
| `import` | `sights-import` | einmalig, auf Befehl (`run --rm`) | Sehenswürdigkeiten-Datenbank bauen und anreichern (OSM, Wikidata, Wichtigkeit, Wikipedia-Texte, Übersetzung) | Minuten bis Stunden, einige GB |
| `refresh` | `sights-refresh` | Dauerdienst (`up -d`), läuft nach Zeitplan | alles aus `import` automatisch alle N Tage: Downloads, Neuaufbau, Anreicherung | läuft im Hintergrund, Last nur zur eingestellten Zeit |
| `wikimedia` | `wikimedia-build` | einmalig oder Dauerdienst mit Monatslauf | `wikimedia_importance.tsv.gz` selbst erzeugen (Wichtigkeit der Wikipedia-Artikel) | 9 bis 12 Stunden, etwa 150 GB (nach dem Lauf wieder frei) |

`sights-refresh` verwendet dasselbe Image wie `sights-import`. Gebaut wird es nur einmal.

## Bauen

```
docker compose build                                                  # nur metadata-suite und geo-service
docker compose --profile '*' build                                    # alles, auch die drei Hilfsdienste
docker compose --profile import --profile refresh --profile wikimedia build    # dasselbe, falls '*' nicht geht (älteres Compose)
docker compose --profile import build sights-import                   # nur ein Dienst
```

`--profile '*'` beim Bauen startet nichts. **Nicht** `COMPOSE_PROFILES=…` in die `.env` schreiben: Das gilt auch für
`docker compose up -d` und würde `sights-refresh` und `wikimedia-build` (inklusive 150-GB-Lauf) sofort mitstarten.

Nach einem `git pull` mit Änderungen an `geo-service/app` müssen `sights-import`/`sights-refresh` neu gebaut werden, sonst läuft die alte Version (ein altes Image kennt zum Beispiel `wikitext` nicht und zeigt dann nur die Hilfe).

## Profil `import` : `sights-import` (auf Befehl)

**Wann:** Erstaufbau der Sehenswürdigkeiten-Datenbank; gezielt einzelne Schritte nachholen; Probeläufe.
**Wo:** im Hauptordner. Die Datenbank `sights.sqlite` liegt in `GEO_DATA_HOST_DIR` (Standard `./geo-data`, im Ordner `geo-service` allein `./data`), die OSM-Planetdatei wird nur lesend aus `GEO_OSM_PBF_HOST_DIR` (`./osm`) gelesen.

```
docker compose --profile import run --rm sights-import <befehl>
```

| Befehl | Was er tut | Hinweis |
|---|---|---|
| `build` | Datenbank aus der Planetdatei bauen (osmium-Filter, Export, R-Tree) | etwa 1 Stunde, ersetzt die Datenbank am Ende atomar, die bisherige bleibt als `.prev` |
| `wikidata` | Sitelinks, Kurzbeschreibung, Wikipedia-Titel von Wikidata | fortsetzbar |
| `importance` | Wikimedia-Wichtigkeit (0 bis 1) eintragen | nimmt die neueste Datei (heruntergeladen, konfiguriert oder selbst gebaut) |
| `wikitext` | erste Sätze des Wikipedia-Artikels (de/en) als `summary` | braucht vorher `wikidata` (Titel) und bei `GEO_SIGHTS_WP_SCOPE=notable` auch `importance`; fortsetzbar |
| `translate` | fehlende deutsche Beschreibungen per LLM (Ollama/Google) | braucht `AI_PROVIDER`; fortsetzbar |
| `download` | Quelldateien laden (Wichtigkeit, optional Planet-PBF) | behält die neueste und die zuletzt erfolgreich genutzte Datei |
| `refresh` | ein kompletter automatischer Lauf von Hand (Download, Build bei Änderung, Wikidata, Wichtigkeit, Wikitext, Übersetzung) | jeder Schritt einzeln abgesichert |
| `schedule` | Dauerlauf nach Zeitplan (das macht `sights-refresh`) | |
| `status` | Zahlen zur Datenbank als JSON | |
| `all` | `build`, `wikidata`, `importance` | Standard des Dienstes |

Typischer Erstaufbau:

```
docker compose --profile import run --rm sights-import all
docker compose --profile import run --rm sights-import wikitext
docker compose --profile import run --rm sights-import translate
docker compose --profile import run --rm sights-import status
```

Einstellungen stehen in der `.env` (Gruppen `GEO_SIGHTS_*`, am Ende von `.env.example`). Jeder Schritt lässt sich mit gesetzter Variable steuern, etwa `GEO_SIGHTS_TR_MAX=20` für einen Probelauf der Übersetzung.

## Profil `refresh` : `sights-refresh` (Dauerdienst)

**Wann:** Wenn die Sehenswürdigkeiten automatisch aktuell bleiben sollen (empfohlen, sobald der Erstaufbau steht).
**Wo:** startet im Hauptordner, läuft mit `restart: unless-stopped` weiter.
**Zeitplan:** `GEO_SIGHTS_REFRESH_DAYS` (Standard `0` = aus, der Dienst wartet dann nur) und `GEO_SIGHTS_REFRESH_AT` (`03:30`).

```
docker compose --profile refresh up -d sights-refresh       # starten
docker compose logs -f sights-refresh                       # zusehen
docker compose --profile refresh stop sights-refresh        # anhalten
```

Was passiert: Download der Quelldateien (nur wenn sich etwas geändert hat), Neuaufbau der Datenbank bei neuer Planetdatei, danach Wikidata, Wichtigkeit, Wikipedia-Kurztexte und Übersetzung (einzeln abschaltbar mit `GEO_SIGHTS_REFRESH_WIKITEXT=0` und `GEO_SIGHTS_REFRESH_TRANSLATE=0`).
Den Stand zeigt die Info-Seite der Suite (Abschnitt Sehenswürdigkeiten-Index) und `/sights/status`.
Downloads liegen in `GEO_DOWNLOAD_HOST_DIR` (`./downloads`).

## Profil `wikimedia` : `wikimedia-build`

**Wann:** Wenn die fertige Datei von nominatim.org zu alt ist und du die Wichtigkeit selbst erzeugen willst, einmalig und danach etwa monatlich.
**Wo:** im Hauptordner. Läuft als root, weil der Container eine eigene PostgreSQL startet; das Ergebnis gehört danach `PUID:PGID`.
**Ordner:** Ergebnis in `GEO_WIKIMEDIA_HOST_DIR` (`./wikimedia`, dort liegt auch die bisherige Datei), Downloads in `GEO_WIKIMEDIA_WORK_HOST_DIR` (60 GB), PostgreSQL in `GEO_WIKIMEDIA_PGDATA_HOST_DIR` (90 GB, am besten NVMe). Arbeits- und Datenbankordner werden nach einem guten Lauf geleert (`GEO_WIKIMEDIA_KEEP_WORK=1` behält sie).

```
docker compose --profile wikimedia build wikimedia-build
docker compose --profile wikimedia run --rm wikimedia-build once       # jetzt einmal bauen (9 bis 12 h bei allen 39 Sprachen)
docker compose --profile wikimedia up -d wikimedia-build               # danach alle GEO_WIKIMEDIA_BUILD_DAYS Tage (30) um GEO_WIKIMEDIA_BUILD_AT (03:00)
docker compose --profile wikimedia run --rm wikimedia-build status     # vorhandene Dateien und letzter Lauf
```

Bricht der Lauf mit „could not resize shared memory segment … No space left on device“ ab, war der gemeinsame Speicher des Containers zu klein (Docker gibt standardmäßig nur 64 MB). Der Dienst setzt `shm_size: 4gb`; nach `git pull` den Dienst mit `docker compose --profile wikimedia up -d --force-recreate wikimedia-build` bzw. beim nächsten `run` neu starten. Ein neuer Lauf macht dort weiter, wo der alte aufgehört hat: Jeder fertige Schritt wird vermerkt und beim nächsten Start übersprungen (auch die vierstündige Umwandlung und der Import), solange der Wikimedia-Dump derselbe ist. Bei einem neueren Dump beginnt er von vorn. Ein Lauf, der noch vor dieser Funktion abgebrochen ist, wird aus seinem Protokoll erkannt (`last-run.log` im Arbeitsordner).

Ergebnis: `wikimedia_importance-JJJJMMTT.tsv.gz` (neueste und die davor bleiben, `GEO_WIKIMEDIA_KEEP`) plus `wikimedia_importance.tsv.gz` als aktuelle Kopie.
Für einen kleinen schnellen Test `GEO_WIKIMEDIA_LANGUAGES=de,en` setzen. Ein abgebrochener Lauf setzt mit den geladenen Dateien fort; ist noch kein vollständiger Wikimedia-Dump verfügbar, tut er nichts und versucht es später wieder.
Ohne eigenen Bau bezieht der automatische Download die Datei standardmäßig von zwei Stellen (OpenCage und nominatim.org) und nimmt die mit dem neueren Datum; `wikimedia-build` lohnt sich also vor allem, wenn beide veraltet sind oder du andere Sprachen willst.
Den nächsten Schritt `importance` übernehmen `sights-import` und `sights-refresh` selbst: Sie nehmen von heruntergeladener, konfigurierter und selbst gebauter Datei die neueste. Mit `GEO_SIGHTS_IMPORTANCE_URL=off` unterdrückst du den Download der alten Datei.

## Empfohlene Reihenfolge

1. `docker compose --profile '*' build`
2. Einmalig: `sights-import all`, dann `wikitext` und `translate`.
3. Optional einmalig: `wikimedia-build once` (über Nacht), danach `sights-import importance`.
4. Dauerbetrieb: `GEO_SIGHTS_REFRESH_DAYS=30` setzen und `docker compose --profile refresh up -d sights-refresh`; falls du die Wichtigkeit selbst erzeugst, zusätzlich `docker compose --profile wikimedia up -d wikimedia-build`.

## Aufräumen

```
docker compose --profile refresh stop sights-refresh && docker compose --profile refresh rm -f sights-refresh
docker compose --profile wikimedia stop wikimedia-build && docker compose --profile wikimedia rm -f wikimedia-build
```

Die Daten in den Host-Ordnern bleiben dabei erhalten.
