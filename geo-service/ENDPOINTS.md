# geo-service – Endpoint-Übersicht

> Dieselbe Liste gibt es klickbar in der Suite (🔗 links neben dem Info-Icon, `/endpoints/`), mit Koordinaten/PLZ/Ort aus deinen Overrides.

Basis: **http://192.168.1.33:8430** (Port/Host in `docker-compose.yml` anpassen; die Links unten funktionieren direkt im Browser).

**Token:** Ist `GEO_SERVICE_TOKEN` gesetzt, sendest du ihn als Header `X-Api-Key: …` oder `Authorization: Bearer …`. Bei den **GeoNames-kompatiblen** Endpunkten (Abschnitt 2, inkl. Höhe) geht zusätzlich `&token=DEIN_TOKEN` in der URL – bei den eigenen Endpunkten (Abschnitt 1) nicht. `/health` ist immer offen.

Beispielkoordinaten: Volksschule Musterdorf, `48.1454285 / 16.3257166`.

---

## 1. Eigene Endpunkte (JSON)

| Zweck | Link |
|---|---|
| Health (schnell) | http://192.168.1.33:8430/health |
| Health mit echtem Backend-Ping | http://192.168.1.33:8430/health?probe=true |
| Cache-/Backend-Statistik | http://192.168.1.33:8430/stats |
| Wetter-Archiv-Statistik | http://192.168.1.33:8430/weather/stats |

### Reverse Geocoding
Kette: Cache → Photon → Nominatim → Pelias (→ Maritime für offenes Wasser), mit Circuit Breaker.

- Standard: http://192.168.1.33:8430/reverse?lat=48.1454285&lon=16.3257166
- Andere Sprache: http://192.168.1.33:8430/reverse?lat=48.1454285&lon=16.3257166&lang=en
- Cache umgehen: http://192.168.1.33:8430/reverse?lat=48.1454285&lon=16.3257166&nocache=true
- Viele Punkte (POST):
  ```
  curl -X POST http://192.168.1.33:8430/reverse-batch -H 'Content-Type: application/json' \
       -d '{"points":[{"lat":48.1454,"lon":16.3257},{"lat":48.2082,"lon":16.3738}]}'
  ```

### GPS-Track (Traccar / Dawarich)
- Position zu einem Zeitpunkt (max. Lücke 600 s): http://192.168.1.33:8430/track-at?time=2026-10-04T12:24:39%2B02:00&max_gap=600
- Bestimmte Geräte: http://192.168.1.33:8430/track-at?time=2026-10-04T12:24:39%2B02:00&devices=default
- Ganzer Tag, ein Punkt pro Minute: http://192.168.1.33:8430/track-range?from=2026-10-04T00:00:00%2B02:00&to=2026-10-04T23:59:59%2B02:00&step=60

> `+` in Zeitzonen immer als `%2B` schreiben.

### Kontext für KI-Beschreibung / XMP
Ort, Tageszeit, Jahreszeit, Wetter, Reisename (je nach Konfiguration):
- http://192.168.1.33:8430/context?lat=48.1454285&lon=16.3257166&time=2026-10-04T12:24:39%2B02:00
  (enthält zusätzlich Tageslicht, Mond, Feiertag, Wind/Niederschlag, Länderinfo im Ausland, Sehenswürdigkeiten, erkannte Reise; `&poi=false` / `&trip=false` schalten die teuren Teile ab)

### Alles zu einer Koordinate (/around)
Ort (Straße, Hausnummer, PLZ), Wasser (Meer, Bucht, See, Flüsse), beste Sehenswürdigkeiten, Höhe und Zeitzone in **einer** Antwort. `include=place,water,sights,elevation,timezone` wählt Teile; ein Teil, der ausfällt, steht unter `errors`, der Rest kommt trotzdem. `radius` bis 20000 m, `limit` bis 30, `lang`. Das Wasser braucht `MARITIME_SERVER`.
- Alles (2 km): http://192.168.1.33:8430/around?lat=48.1454285&lon=16.3257166
- Großer Umkreis (10 km): http://192.168.1.33:8430/around?lat=48.1454285&lon=16.3257166&radius=10000&limit=20
- Nur Ort und Wasser: http://192.168.1.33:8430/around?lat=48.1454285&lon=16.3257166&include=place,water
- Andere Sprache: http://192.168.1.33:8430/around?lat=48.1454285&lon=16.3257166&lang=en

### Sehenswürdigkeiten (/sights)
Bewertung nach Kategorie und Entfernung, Dubletten zusammengefasst, mit Richtung (`bearing`, `direction`) und Details (Beschreibung, Öffnungszeiten, Website, Wikidata/Wikipedia). Position per `lat`+`lon`, Ortsname `q` oder `address`; `radius` bis 20000 m, `limit` bis 50, `sort=score|distance`, `lang`, `categories` (castle, museum, attraction, zoo, theme_park, waterfall, viewpoint, religious, ruins, nature_reserve, monument, peak, historic, own, water).
- Um eine Koordinate (10 km): http://192.168.1.33:8430/sights?lat=48.1454285&lon=16.3257166&radius=10000&limit=10
- Großer Umkreis, nur Burgen und Museen: http://192.168.1.33:8430/sights?lat=48.1454285&lon=16.3257166&radius=20000&categories=castle,museum&limit=20
- Nach Entfernung sortiert: http://192.168.1.33:8430/sights?lat=48.1454285&lon=16.3257166&radius=10000&sort=distance
- Flüsse und Seen (Kategorie `water`, braucht MARITIME_SERVER mit Fluss-Schicht): http://192.168.1.33:8430/sights?lat=48.2201&lon=16.4172&radius=10000&categories=water
- Um einen Ortsnamen: http://192.168.1.33:8430/sights?q=Musterdorf&radius=10000
- Um eine Adresse: http://192.168.1.33:8430/sights?address=Musterdorf&radius=10000
- Handy-Seite „In der Nähe“ (PWA: Standort-Button, Ortseingabe, Karte): http://192.168.1.33:8430/nearby
- Regenradar (eigene Karte + Radar der letzten 2 Stunden, Animation): http://192.168.1.33:8430/radar
- Stand des Sehenswürdigkeiten-Index (Anzahl, Datum, Übersetzungen, Datenstand, letzte Aktualisierung): http://192.168.1.33:8430/sights/status
- Wien, Stephansplatz (10 km): http://192.168.1.33:8430/sights?lat=48.2086&lon=16.3725&radius=10000&limit=10
- Rom, Kolosseum (10 km, nach Entfernung): http://192.168.1.33:8430/sights?lat=41.8902&lon=12.4922&radius=10000&sort=distance&limit=10
- Hallstatt per Ortsname (10 km): http://192.168.1.33:8430/sights?q=Hallstatt&radius=10000&limit=10
- Großglockner: nur Aussichtspunkte und Gipfel (10 km): http://192.168.1.33:8430/sights?lat=47.0745&lon=12.6947&radius=10000&categories=viewpoint,peak&limit=10
- Paris, Eiffelturm auf Englisch: http://192.168.1.33:8430/sights?lat=48.8584&lon=2.2945&radius=10000&lang=en&limit=10
- Nur Wasserfälle im Umkreis von 20 km (Krimml): http://192.168.1.33:8430/sights?lat=47.2231&lon=12.1822&radius=20000&categories=waterfall
- Wikipedia-Artikel in der Nähe, Graz (GeoNames-Format): http://192.168.1.33:8430/findNearbyWikipediaJSON?lat=47.0707&lng=15.4395&radius=10&maxRows=10&lang=de

### Meer und Gewässer (Test des Maritime-Servers)
Feste Punkte im offenen Wasser: Reverse fragt Photon → Nominatim → Gazetteer → Maritime; findNearbyPlaceName nennt das Gewässer bzw. den nächsten Ort (`distance` in km). Bei Binnenseen antwortet meist schon der normale Geocoder.
- Point Nemo (Südpazifik, weiter weg von Land geht nicht) – Reverse: http://192.168.1.33:8430/reverse?lat=-48.8767&lon=-123.3933&nocache=true
- Mitte Nordatlantik – Reverse: http://192.168.1.33:8430/reverse?lat=40.0&lon=-40.0&nocache=true
- Straße von Gibraltar – Reverse: http://192.168.1.33:8430/reverse?lat=35.96&lon=-5.6&nocache=true
- Straße von Dover (Ärmelkanal) – Reverse: http://192.168.1.33:8430/reverse?lat=51.0&lon=1.4&nocache=true
- Straße von Messina – Reverse: http://192.168.1.33:8430/reverse?lat=38.25&lon=15.62&nocache=true
- Offene Adria vor Rimini – Reverse: http://192.168.1.33:8430/reverse?lat=44.0&lon=13.5&nocache=true
- Ostsee bei Bornholm – Reverse: http://192.168.1.33:8430/reverse?lat=55.5&lon=15.0&nocache=true
- Nordsee, Doggerbank – Reverse: http://192.168.1.33:8430/reverse?lat=54.8&lon=2.5&nocache=true
- Straße von Hormus – Reverse: http://192.168.1.33:8430/reverse?lat=26.57&lon=56.25&nocache=true
- Marianengraben – Reverse: http://192.168.1.33:8430/reverse?lat=11.35&lon=142.2&nocache=true
- Bodensee (Seemitte) – Reverse: http://192.168.1.33:8430/reverse?lat=47.58&lon=9.4&nocache=true
- Neusiedler See (Seemitte) – Reverse: http://192.168.1.33:8430/reverse?lat=47.85&lon=16.75&nocache=true
- Point Nemo (Südpazifik, weiter weg von Land geht nicht) – nächster Ort: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=-48.8767&lng=-123.3933&maxRows=3&style=MEDIUM
- Mitte Nordatlantik – nächster Ort: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=40.0&lng=-40.0&maxRows=3&style=MEDIUM
- Straße von Gibraltar – nächster Ort: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=35.96&lng=-5.6&maxRows=3&style=MEDIUM
- Straße von Dover (Ärmelkanal) – nächster Ort: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=51.0&lng=1.4&maxRows=3&style=MEDIUM

### Meine Orte, Sehenswürdigkeiten, Reisen, Uhr-Check
- Meine Orte (aus dem Override-Editor): http://192.168.1.33:8430/places
- Sehenswürdigkeiten in der Nähe (Kurzform, nach Entfernung): http://192.168.1.33:8430/pois?lat=48.1454285&lon=16.3257166&radius=10000
- Erkannte Reisen eines Zeitraums: http://192.168.1.33:8430/trips?from=2026-01-01&to=2026-12-31
- Reise zu einem Zeitpunkt: http://192.168.1.33:8430/trip-at?time=2026-08-10T12:00:00%2B02:00
- Kamera-Uhr prüfen (GPS-Zeit): http://192.168.1.33:8430/clock-check?time=1780000000&gps_time=1779996400
- Kamera-Uhr prüfen (eigene Foto-GPS-Position gegen die Spur): http://192.168.1.33:8430/clock-check?time=1780000000&lat=48.1454&lon=16.3257

### Wetter-Archiv (nur mit `WEATHER_PROVIDER=open-meteo`)
- Fortschritt: http://192.168.1.33:8430/weather/backfill
- Starten (POST): `curl -X POST http://192.168.1.33:8430/weather/backfill`
- Abbrechen (POST): `curl -X POST http://192.168.1.33:8430/weather/backfill/cancel`

### Höhe über Meer
Reihenfolge: Override → Cache → lokale SRTM3-Kacheln → Open-Meteo (`ELEVATION_PROVIDER`). Gefundene Werte bleiben im Cache.
- http://192.168.1.33:8430/elevation?lat=48.1454285&lon=16.3257166 → `{"elevation": 215.3, "source": "srtm|open-meteo|override|none", "cached": …}`
- Teil von `/context` (`elevation_m`); mit `&elevation=false` weglassen.

### Overrides (Korrekturen über Cache und Geocoder)
**Editor (GUI):** http://192.168.1.33:8430/ui – Adresse/Koordinaten eingeben, Felder überschreiben, speichern.

Datei `./geo-data/overrides.json` (Beispiel: `overrides.example.json`), wird bei Änderung automatisch neu gelesen.
- Liste: http://192.168.1.33:8430/overrides
- Originalwerte für einen Punkt (ohne Overrides): http://192.168.1.33:8430/overrides/lookup?lat=48.1454285&lng=16.3257166
- Setzen/ersetzen (POST, gleiche `id` = ersetzen):
  ```
  curl -X POST http://192.168.1.33:8430/overrides -H 'Content-Type: application/json' -d '{"id":"volksschule-musterdorf","lat":48.14543,"lng":16.32572,"radius_m":40,"address":{"placename":"Volksschule"},"reverse":{"locality":"Volksschule Musterdorf"},"elevation":215}'
  ```
- Löschen: `curl -X DELETE http://192.168.1.33:8430/overrides/volksschule-musterdorf`
- Wirkt auf `/addressJSON` (Abschnitt `address`), `/reverse` und `/context` (Abschnitt `reverse`) sowie `/elevation`, `/srtm3` … (Abschnitt `elevation`).

---

## 2. GeoNames-kompatible Webservices

Ersetzen den alten `geonames-wrapper`. Jeder Dienst existiert als **XML** (ohne Suffix) und als **JSON** (Suffix `JSON`).
Fehler kommen wie beim Original mit HTTP 200 und `{"status":{"message":"…","value":N}}`.

> **geonameIds sind synthetisch** (Registry `geonames-ids.sqlite`) und stimmen nicht mit geonames.org überein.

### findNearbyPlaceName – nächster bewohnter Ort
- JSON: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=48.1454285&lng=16.3257166&maxRows=5&style=MEDIUM
- XML: http://192.168.1.33:8430/findNearbyPlaceName?lat=48.1454285&lng=16.3257166&maxRows=5
- Mit Sprache: http://192.168.1.33:8430/findNearbyPlaceNameJSON?lat=48.1454285&lng=16.3257166&lang=de

### findNearby – nächste Objekte jeder Art
Nächste Toponyme (Orte, Straßen, Gebäude …) im Radius (km, Standard 1), nächstes zuerst. Optional `featureClass=` (A/P/R/S/T/H/L/V, mehrfach) und `featureCode=`; `fcl`/`fcode` sind aus den Quelldaten abgeleitet.
- JSON: http://192.168.1.33:8430/findNearbyJSON?lat=48.1454285&lng=16.3257166&radius=10&maxRows=10
- Nur Straßen: http://192.168.1.33:8430/findNearbyJSON?lat=48.1454285&lng=16.3257166&featureClass=R&maxRows=5
- XML: http://192.168.1.33:8430/findNearby?lat=48.1454285&lng=16.3257166

### extendedFindNearby – Objekt + Orts-/Verwaltungskette
Nächstes Objekt, dann Ort › Bezirk › Bundesland › Land (spezifisch zuerst).
- JSON: http://192.168.1.33:8430/extendedFindNearbyJSON?lat=48.1454285&lng=16.3257166
- XML: http://192.168.1.33:8430/extendedFindNearby?lat=48.1454285&lng=16.3257166

### findNearbyWikipedia – Sehenswürdigkeiten mit Wikipedia-Artikel
GeoNames-kompatibel, gleiche Daten wie `/sights`. `radius` in km (max. 20).
- JSON (Radius 10 km): http://192.168.1.33:8430/findNearbyWikipediaJSON?lat=48.1454285&lng=16.3257166&radius=10&maxRows=10&lang=de
- XML: http://192.168.1.33:8430/findNearbyWikipedia?lat=48.1454285&lng=16.3257166&radius=10&maxRows=10

### findNearbyPostalCodes – nächste Postleitzahlen
- Nach Koordinate: http://192.168.1.33:8430/findNearbyPostalCodesJSON?lat=48.1454285&lng=16.3257166&radius=10&maxRows=5
- Nach PLZ: http://192.168.1.33:8430/findNearbyPostalCodesJSON?postalcode=2552&country=AT
- Nach Ortsname: http://192.168.1.33:8430/findNearbyPostalCodesJSON?placename=Musterdorf
- XML: http://192.168.1.33:8430/findNearbyPostalCodes?lat=48.1454285&lng=16.3257166

### search – Textsuche
- http://192.168.1.33:8430/searchJSON?q=Musterdorf&country=AT&maxRows=5
- Nur Namen: http://192.168.1.33:8430/searchJSON?name=Baden&country=AT&lang=de
- Blättern: http://192.168.1.33:8430/searchJSON?q=Wien&maxRows=10&startRow=10
- Beginnt mit (Prefix): http://192.168.1.33:8430/searchJSON?name_startsWith=Muster&country=AT&maxRows=5
- Nur Orte: http://192.168.1.33:8430/searchJSON?q=Baden&featureClass=P&maxRows=5
- Nach Einwohnern: http://192.168.1.33:8430/searchJSON?q=Baden&orderby=population&maxRows=5
- Weitere Filter: `name_equals`, `featureCode`, `east/west/north/south` (Rechteck), `cities=cities1000|5000|15000`, `isNameRequired=true`, mehrere `country` (`AT,DE` oder wiederholt). `population` ist nur gefüllt, wenn die Quelle (z. B. Pelias mit GeoNames-Daten) sie liefert.
- XML: http://192.168.1.33:8430/search?q=Musterdorf&country=AT

### get – Details zu einer geonameId
Die ID aus einem `search`-/`findNearby…`-Ergebnis nehmen und statt `ID` einsetzen:
- `http://192.168.1.33:8430/getJSON?geonameId=ID&style=FULL`
- `http://192.168.1.33:8430/get?geonameId=ID` (XML)

### hierarchy – Verwaltungshierarchie (Land → Bundesland → Bezirk → Gemeinde → Ort)
- `http://192.168.1.33:8430/hierarchyJSON?geonameId=ID`
- `http://192.168.1.33:8430/hierarchy?geonameId=ID` (XML)

### children – Unterebene (Land → Bundesländer → Bezirke → Gemeinden)
Für die geonameId eines Landes, Bundeslands oder Bezirks (zum Beispiel aus `hierarchy`). Der Service legt ein Raster über die echte Grenze des Elternteils, fragt Nominatim an den Punkten und nimmt nur Treffer, deren Adresskette wirklich im Elternteil liegt. Das dauert beim ersten Mal einige Sekunden, danach kommt die Antwort aus dem Cache. Ist die Abfragegrenze (`GN_CHILDREN_MAX_PROBES`) erreicht, steht `"complete": false` in der Antwort und sie wird nicht gecacht.
- `http://192.168.1.33:8430/childrenJSON?geonameId=ID&maxRows=200`
- `http://192.168.1.33:8430/children?geonameId=ID` (XML)

### countryInfo – Länderdaten (Name, Hauptstadt, Währung, Sprachen, Bounding Box)
- Österreich: http://192.168.1.33:8430/countryInfoJSON?country=AT
- Alle Länder: http://192.168.1.33:8430/countryInfoJSON
- XML: http://192.168.1.33:8430/countryInfo?country=AT

Quelle ist `countryInfo.txt` in deinem GeoNames-Volume (`GEONAMES_HOST_DIR` → `/geonames`; Pfad änderbar per `GN_COUNTRY_INFO_FILE`).

### timezone – Zeitzone, Offsets, lokale Zeit, Sonnenauf-/untergang
Offline berechnet (Zeitzonen-Polygone + tz-Datenbank), kein Upstream nötig. `date=yyyy-MM-dd` optional (für Sonnenzeiten).
`gmtOffset` = Offset am 1. Jänner, `dstOffset` = am 1. Juli, `rawOffset` = Standardzeit, `time` = aktuelle Ortszeit.
- JSON: http://192.168.1.33:8430/timezoneJSON?lat=6.02499420&lng=80.21890920
- Mit Datum: http://192.168.1.33:8430/timezoneJSON?lat=6.02499420&lng=80.21890920&date=2026-10-10
- XML: http://192.168.1.33:8430/timezone?lat=6.02499420&lng=80.21890920

### Mehrere Höhenpunkte in einem Aufruf (max. 20)
- http://192.168.1.33:8430/srtm3JSON?lats=48.1454,48.2082&lngs=16.3257,16.3738 → `{"geonames":[…]}`
- Klartext, eine Zahl pro Zeile: http://192.168.1.33:8430/srtm3?lats=48.1454,48.2082&lngs=16.3257,16.3738

### address – Straße/Hausnummer/PLZ/Ort zu Koordinaten
- http://192.168.1.33:8430/addressJSON?lat=48.1454285&lng=16.3257166
- XML: http://192.168.1.33:8430/address?lat=48.1454285&lng=16.3257166

### countryCode / countrySubdivision – Land und Bundesland zu Koordinaten
`countryCode` liefert wie das Original Klartext (`AT`), mit `type=json|xml` oder dem Suffix `JSON` die Felder `countryCode`, `countryName`, `languages`, `distance`. `radius=km` sucht auf offenem Wasser den nächsten Ort. `countrySubdivision` kennt nur Ebene 1 (Bundesland, ISO-3166-2-Code).
- countryCode (Klartext): http://192.168.1.33:8430/countryCode?lat=48.1454285&lng=16.3257166
- countryCode (JSON): http://192.168.1.33:8430/countryCodeJSON?lat=48.1454285&lng=16.3257166
- countryCode (XML): http://192.168.1.33:8430/countryCode?lat=48.1454285&lng=16.3257166&type=xml
- countrySubdivision (JSON): http://192.168.1.33:8430/countrySubdivisionJSON?lat=48.1454285&lng=16.3257166
- countrySubdivision (XML): http://192.168.1.33:8430/countrySubdivision?lat=48.1454285&lng=16.3257166

### geoCodeAddress / streetNameLookup / findNearestAddress / findNearbyStreetsOSM
Quelle: dein Nominatim (Adressen, Straßennamen) und dein Photon (Straßen in der Nähe). `findNearbyStreetsOSM` ist eine **Näherung**: Photon nennt die Straßen, `line` ist die Diagonale des Straßen-Rechtecks (oder nur der Punkt), keine echte Geometrie; `maxspeed`/`oneway` fehlen. `streetNameLookup` antwortet in der Form von `address`: `{"address":[{street, postalcode, placename, adminCode1 …}]}`.
- geoCodeAddress (JSON): http://192.168.1.33:8430/geoCodeAddressJSON?q=Musterdorf
- geoCodeAddress (XML): http://192.168.1.33:8430/geoCodeAddress?q=Musterdorf
- streetNameLookup (JSON): http://192.168.1.33:8430/streetNameLookupJSON?q=Hauptstra%C3%9Fe&country=AT&maxRows=10
- streetNameLookup (XML): http://192.168.1.33:8430/streetNameLookup?q=Hauptstra%C3%9Fe&country=AT
- findNearestAddress (JSON): http://192.168.1.33:8430/findNearestAddressJSON?lat=48.1454285&lng=16.3257166
- findNearestAddress (XML): http://192.168.1.33:8430/findNearestAddress?lat=48.1454285&lng=16.3257166
- findNearbyStreetsOSM (JSON): http://192.168.1.33:8430/findNearbyStreetsOSMJSON?lat=48.1454285&lng=16.3257166&radius=10&maxRows=10
- findNearbyStreetsOSM (XML): http://192.168.1.33:8430/findNearbyStreetsOSM?lat=48.1454285&lng=16.3257166

### postalCodeSearch / postalCodeLookup
- Nach PLZ: http://192.168.1.33:8430/postalCodeSearchJSON?postalcode=2552&country=AT&maxRows=10
- Nach Ortsname: http://192.168.1.33:8430/postalCodeSearchJSON?placename=Musterdorf&country=AT
- Lookup (PLZ → Orte): http://192.168.1.33:8430/postalCodeLookupJSON?postalcode=2552&country=AT
- XML: http://192.168.1.33:8430/postalCodeSearch?postalcode=2552&country=AT

### srtm3 / astergdem / gtopo30 – Höhe
Alle drei liefern denselben Wert (eine Quelle). `JSON`: `{"srtm3": 215, "lng": …, "lat": …}`, ohne Suffix: Klartext-Zahl wie beim Original. Keine Höhe bekannt: `-32768` (srtm3) bzw. `-9999`.
- http://192.168.1.33:8430/srtm3JSON?lat=48.1454285&lng=16.3257166
- http://192.168.1.33:8430/astergdemJSON?lat=48.1454285&lng=16.3257166
- http://192.168.1.33:8430/gtopo30JSON?lat=48.1454285&lng=16.3257166
- Klartext: http://192.168.1.33:8430/srtm3?lat=48.1454285&lng=16.3257166

---

## 2b. Nominatim- und Photon-kompatibel (für Traccar und Dawarich)

Eigene Ports, damit das bestehende API auf 8430 unverändert bleibt: **8431 = Nominatim-API**, **8432 = Photon-API**.
Antworten kommen aus Cache → deinem Nominatim/Photon/Pelias (Reihenfolge `GEO_CHAIN`) → danach werden deine **Overrides** darübergelegt.
Die Ports sind standardmäßig **ohne Token** offen (es werden nur Ortsabfragen beantwortet, keine Tracks); `GEO_COMPAT_AUTH=true` verlangt `?token=`/`?key=`/`X-Api-Key`.

| Zweck | Link |
|---|---|
| Nominatim: Status | http://192.168.1.33:8431/status |
| Nominatim: reverse (jsonv2) | http://192.168.1.33:8431/reverse?format=jsonv2&lat=48.1454285&lon=16.3257166 |
| Nominatim: reverse (json, wie Traccar) | http://192.168.1.33:8431/reverse?format=json&lat=48.1454285&lon=16.3257166&zoom=18&addressdetails=1 |
| Nominatim: reverse (geojson) | http://192.168.1.33:8431/reverse?format=geojson&lat=48.1454285&lon=16.3257166 |
| Nominatim: search | http://192.168.1.33:8431/search?q=Musterdorf&format=jsonv2&limit=3 |
| Photon: Status | http://192.168.1.33:8432/status |
| Photon: reverse | http://192.168.1.33:8432/reverse?lon=16.3257166&lat=48.1454285 |
| Photon: Suche | http://192.168.1.33:8432/api?q=Musterdorf&limit=3 |

Einrichtung: **Traccar** `geocoder.type=nominatim`, `geocoder.url=http://192.168.1.33:8431/reverse`; **Dawarich** `NOMINATIM_API_HOST=192.168.1.33:8431` bzw. `PHOTON_API_HOST=192.168.1.33:8432` (ggf. `*_API_USE_HTTPS=false`).
Nicht gefunden: Nominatim antwortet `{"error":"Unable to geocode"}` (HTTP 200, wie das Original), Photon eine leere `features`-Liste.

---

## 3. Schnelltest per curl

```bash
BASE=http://192.168.1.33:8430
curl -s "$BASE/health?probe=true"
curl -s "$BASE/reverse?lat=48.1454285&lon=16.3257166"
curl -s "$BASE/findNearbyPlaceNameJSON?lat=48.1454285&lng=16.3257166&maxRows=3"
curl -s "$BASE/countryInfoJSON?country=AT"
```

## 4. Passende Einstellungen (`.env`)
`ELEVATION_PROVIDER`, `GEO_SRTM_PRELOAD`, `GEO_OVERRIDES_FILE`, `GEO_SERVICE_TOKEN`, `GEO_CHAIN`, `GEO_LANG`, `WEATHER_PROVIDER`, `GN_SEARCH_ORDER`, `GN_MAX_ROWS_LIMIT`, `GN_COUNTRY_INFO_FILE` … siehe `.env.example`.
- Reverse mit Herkunft: http://192.168.1.33:8430/reverse?lat=48.15908&lon=16.33280&lang=de → enthält `source`, `upstream_source` (z. B. Photon), `cached`, `cached_at`, `cached_age_s`
