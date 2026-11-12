"""endpointscatalog.py -- the geo-service's endpoints as data (the page /endpoints/ renders it; geo-service/ENDPOINTS.md is the
same list as a document, tests/test_endpoints_page.py checks that both stay in step).

Item = (label, path_template) for a link, or (label, path_template, body) for a POST/DELETE shown as a curl line.
`port` says which service port the item lives on: api (8430), nom (8431 Nominatim facade), photon (8432 Photon facade).
Placeholders, filled in the browser from the chosen sample point (your overrides): {lat} {lng} {postal} {city} {cc}
and {time} {day_from} {day_to} {year_from} {year_to} {epoch} {epoch_gps}.
"""
from __future__ import annotations

# (title, note, port, [items])  --  items: dict(label, path, method="GET", body=None)
def _l(label, path, **kw):
    return {"label": label, "path": path, **kw}


SECTIONS = [
    {"id": "own", "title": "Eigene Endpunkte (JSON)", "groups": [
        {"title": "Status", "port": "api", "items": [
            _l("Health (schnell)", "/health"),
            _l("Health mit echtem Backend-Ping", "/health?probe=true"),
            _l("Cache-/Backend-Statistik", "/stats"),
            _l("Wetter-Archiv-Statistik", "/weather/stats"),
        ]},
        {"title": "Reverse Geocoding", "note": "Kette: Cache → Photon → Nominatim → Pelias (→ Maritime für offenes Wasser), mit Circuit Breaker. Die Antwort nennt Herkunft (`upstream_source`) und Cache-Zeit (`cached_at`).",
         "port": "api", "items": [
            _l("Standard", "/reverse?lat={lat}&lon={lng}"),
            _l("Andere Sprache", "/reverse?lat={lat}&lon={lng}&lang=en"),
            _l("Cache umgehen", "/reverse?lat={lat}&lon={lng}&nocache=true"),
            _l("Viele Punkte (POST)", "/reverse-batch", method="POST",
               body='{"points":[{"lat":{lat},"lon":{lng}},{"lat":48.2082,"lon":16.3738}]}'),
        ]},
        {"title": "GPS-Track (Traccar / Dawarich)", "note": "`+` in Zeitzonen immer als `%2B` schreiben.", "port": "api", "items": [
            _l("Position zu einem Zeitpunkt (max. Lücke 600 s)", "/track-at?time={time}&max_gap=600"),
            _l("Bestimmte Geräte", "/track-at?time={time}&devices=default"),
            _l("Ganzer Tag, ein Punkt pro Minute", "/track-range?from={day_from}&to={day_to}&step=60"),
        ]},
        {"title": "Kontext für KI-Beschreibung / XMP",
         "note": "Ort, Tageszeit, Jahreszeit, Wetter, Reisename, Tageslicht, Mond, Feiertag, Wind/Niederschlag, Länderinfo im Ausland, Sehenswürdigkeiten, erkannte Reise. `&poi=false` / `&trip=false` schalten die teuren Teile ab.",
         "port": "api", "items": [
            _l("Kontext", "/context?lat={lat}&lon={lng}&time={time}"),
            _l("Kontext ohne Sehenswürdigkeiten und Reise", "/context?lat={lat}&lon={lng}&time={time}&poi=false&trip=false"),
        ]},
        {"title": "Alles zu einer Koordinate (/around)",
         "note": "Ort (Straße, Hausnummer, PLZ), Wasser (Meer, Bucht, See, Flüsse), beste Sehenswürdigkeiten, Höhe und Zeitzone in EINER Antwort. `include=place,water,sights,elevation,timezone` wählt Teile; ein Teil, der ausfällt, steht unter `errors`, der Rest kommt trotzdem. `radius` bis 20000 m, `limit` bis 30.",
         "port": "api", "items": [
            _l("Alles (2 km)", "/around?lat={lat}&lon={lng}"),
            _l("Großer Umkreis (10 km)", "/around?lat={lat}&lon={lng}&radius=10000&limit=20"),
            _l("Nur Ort und Wasser", "/around?lat={lat}&lon={lng}&include=place,water"),
            _l("Andere Sprache", "/around?lat={lat}&lon={lng}&lang=en"),
        ]},
        {"title": "Sehenswürdigkeiten (/sights)",
         "note": "Bewertung nach Kategorie und Entfernung, Dubletten zusammengefasst, mit Richtung und Details (Beschreibung, Öffnungszeiten, Website, Wikidata/Wikipedia). Position per `lat`+`lon`, Ortsname `q` oder `address`; `radius` bis 20000 m, `limit` bis 50, `sort=score|distance`, `categories` (castle, museum, attraction, zoo, theme_park, waterfall, viewpoint, religious, ruins, nature_reserve, monument, peak, historic, own, water).",
         "port": "api", "items": [
            _l("Um eine Koordinate (10 km)", "/sights?lat={lat}&lon={lng}&radius=10000&limit=10"),
            _l("Großer Umkreis, nur Burgen und Museen", "/sights?lat={lat}&lon={lng}&radius=20000&categories=castle,museum&limit=20"),
            _l("Nach Entfernung sortiert", "/sights?lat={lat}&lon={lng}&radius=10000&sort=distance"),
            _l("Um einen Ortsnamen", "/sights?q={city}&radius=10000"),
            _l("Um eine Adresse", "/sights?address={city}&radius=10000"),
            _l("Handy-Seite „In der Nähe“ (PWA: Standort-Button, Ortseingabe, Karte)", "/nearby"),
            _l("Regenradar (eigene Karte + Radar der letzten 2 Stunden, Animation)", "/radar"),
            _l("Stand des Sehenswürdigkeiten-Index (Anzahl, Datum, Übersetzungen, Datenstand, letzte Aktualisierung)", "/sights/status"),
            _l("Wien, Stephansplatz (10 km)", "/sights?lat=48.2086&lon=16.3725&radius=10000&limit=10"),
            _l("Rom, Kolosseum (10 km, nach Entfernung)", "/sights?lat=41.8902&lon=12.4922&radius=10000&sort=distance&limit=10"),
            _l("Hallstatt per Ortsname (10 km)", "/sights?q=Hallstatt&radius=10000&limit=10"),
            _l("Großglockner: nur Aussichtspunkte und Gipfel (10 km)", "/sights?lat=47.0745&lon=12.6947&radius=10000&categories=viewpoint,peak&limit=10"),
            _l("Paris, Eiffelturm auf Englisch", "/sights?lat=48.8584&lon=2.2945&radius=10000&lang=en&limit=10"),
            _l("Nur Wasserfälle im Umkreis von 20 km (Krimml)", "/sights?lat=47.2231&lon=12.1822&radius=20000&categories=waterfall"),
            _l("Wikipedia-Artikel in der Nähe, Graz (GeoNames-Format)", "/findNearbyWikipediaJSON?lat=47.0707&lng=15.4395&radius=10&maxRows=10&lang=de"),
        ]},
        {"title": "Meer und Gewässer (Test des Maritime-Servers)",
         "note": "Feste Punkte im offenen Wasser: Reverse fragt Photon → Nominatim → Gazetteer → Maritime; findNearbyPlaceName nennt das Gewässer bzw. den nächsten Ort (`distance` in km). Bei Binnenseen antwortet meist schon der normale Geocoder.",
         "port": "api", "items": [
            _l("Point Nemo (Südpazifik, weiter weg von Land geht nicht) – Reverse", "/reverse?lat=-48.8767&lon=-123.3933&nocache=true"),
            _l("Mitte Nordatlantik – Reverse", "/reverse?lat=40.0&lon=-40.0&nocache=true"),
            _l("Straße von Gibraltar – Reverse", "/reverse?lat=35.96&lon=-5.6&nocache=true"),
            _l("Straße von Dover (Ärmelkanal) – Reverse", "/reverse?lat=51.0&lon=1.4&nocache=true"),
            _l("Straße von Messina – Reverse", "/reverse?lat=38.25&lon=15.62&nocache=true"),
            _l("Offene Adria vor Rimini – Reverse", "/reverse?lat=44.0&lon=13.5&nocache=true"),
            _l("Ostsee bei Bornholm – Reverse", "/reverse?lat=55.5&lon=15.0&nocache=true"),
            _l("Nordsee, Doggerbank – Reverse", "/reverse?lat=54.8&lon=2.5&nocache=true"),
            _l("Straße von Hormus – Reverse", "/reverse?lat=26.57&lon=56.25&nocache=true"),
            _l("Marianengraben – Reverse", "/reverse?lat=11.35&lon=142.2&nocache=true"),
            _l("Bodensee (Seemitte) – Reverse", "/reverse?lat=47.58&lon=9.4&nocache=true"),
            _l("Neusiedler See (Seemitte) – Reverse", "/reverse?lat=47.85&lon=16.75&nocache=true"),
            _l("Point Nemo (Südpazifik, weiter weg von Land geht nicht) – nächster Ort", "/findNearbyPlaceNameJSON?lat=-48.8767&lng=-123.3933&maxRows=3&style=MEDIUM"),
            _l("Mitte Nordatlantik – nächster Ort", "/findNearbyPlaceNameJSON?lat=40.0&lng=-40.0&maxRows=3&style=MEDIUM"),
            _l("Straße von Gibraltar – nächster Ort", "/findNearbyPlaceNameJSON?lat=35.96&lng=-5.6&maxRows=3&style=MEDIUM"),
            _l("Straße von Dover (Ärmelkanal) – nächster Ort", "/findNearbyPlaceNameJSON?lat=51.0&lng=1.4&maxRows=3&style=MEDIUM"),
        ]},
        {"title": "Meine Orte, Sehenswürdigkeiten, Reisen, Uhr-Check", "port": "api", "items": [
            _l("Meine Orte (aus dem Override-Editor)", "/places"),
            _l("Sehenswürdigkeiten in der Nähe (Kurzform, nach Entfernung)", "/pois?lat={lat}&lon={lng}&radius=10000"),
            _l("Erkannte Reisen eines Zeitraums", "/trips?from={year_from}&to={year_to}"),
            _l("Reise zu einem Zeitpunkt", "/trip-at?time={time}"),
            _l("Kamera-Uhr prüfen (GPS-Zeit)", "/clock-check?time={epoch}&gps_time={epoch_gps}"),
            _l("Kamera-Uhr prüfen (Foto-GPS-Position gegen die Spur)", "/clock-check?time={epoch}&lat={lat}&lon={lng}"),
        ]},
        {"title": "Wetter-Archiv", "note": "Nur mit `WEATHER_PROVIDER=open-meteo`.", "port": "api", "items": [
            _l("Fortschritt", "/weather/backfill"),
            _l("Starten (POST)", "/weather/backfill", method="POST"),
            _l("Abbrechen (POST)", "/weather/backfill/cancel", method="POST"),
        ]},
        {"title": "Höhe über Meer", "note": "Reihenfolge: Override → Cache → lokale SRTM3-Kacheln → Open-Meteo (`ELEVATION_PROVIDER`). Auch Teil von `/context` (`elevation_m`).",
         "port": "api", "items": [_l("Höhe", "/elevation?lat={lat}&lon={lng}")]},
        {"title": "Overrides (Korrekturen über Cache und Geocoder)",
         "note": "Datei `./geo-data/overrides.json`, wird bei Änderung automatisch neu gelesen. Wirkt auf `/addressJSON`, `/reverse`, `/context`, `/elevation`, `/srtm3` …",
         "port": "api", "items": [
            _l("Editor (GUI)", "/ui"),
            _l("Liste", "/overrides"),
            _l("Originalwerte für einen Punkt (ohne Overrides)", "/overrides/lookup?lat={lat}&lng={lng}"),
            _l("Setzen/ersetzen (POST, gleiche id = ersetzen)", "/overrides", method="POST",
               body='{"id":"mein-ort","lat":{lat},"lng":{lng},"radius_m":40,"address":{"placename":"Mein Ort"},"reverse":{"locality":"Mein Ort"},"elevation":215}'),
            _l("Löschen", "/overrides/mein-ort", method="DELETE"),
        ]},
    ]},
    {"id": "geonames", "title": "GeoNames-kompatible Webservices",
     "note": "Ersetzen den alten geonames-wrapper. Jeder Dienst existiert als XML (ohne Suffix) und als JSON (Suffix JSON). Fehler kommen wie beim Original mit HTTP 200. geonameIds sind synthetisch.",
     "groups": [
        {"title": "findNearbyPlaceName – nächster bewohnter Ort", "port": "api", "items": [
            _l("JSON", "/findNearbyPlaceNameJSON?lat={lat}&lng={lng}&maxRows=5&style=MEDIUM"),
            _l("XML", "/findNearbyPlaceName?lat={lat}&lng={lng}&maxRows=5"),
            _l("Mit Sprache", "/findNearbyPlaceNameJSON?lat={lat}&lng={lng}&lang=de"),
        ]},
        {"title": "findNearby – nächste Objekte jeder Art", "port": "api", "items": [
            _l("JSON (Radius 10 km)", "/findNearbyJSON?lat={lat}&lng={lng}&radius=10&maxRows=10"),
            _l("Nur Straßen", "/findNearbyJSON?lat={lat}&lng={lng}&featureClass=R&maxRows=5"),
            _l("XML", "/findNearby?lat={lat}&lng={lng}"),
        ]},
        {"title": "extendedFindNearby – Objekt + Orts-/Verwaltungskette", "port": "api", "items": [
            _l("JSON", "/extendedFindNearbyJSON?lat={lat}&lng={lng}"),
            _l("XML", "/extendedFindNearby?lat={lat}&lng={lng}"),
        ]},
        {"title": "findNearbyWikipedia – Sehenswürdigkeiten mit Wikipedia-Artikel", "note": "GeoNames-kompatibel, gleiche Daten wie `/sights`. `radius` in km (max. 20).", "port": "api", "items": [
            _l("JSON (Radius 10 km)", "/findNearbyWikipediaJSON?lat={lat}&lng={lng}&radius=10&maxRows=10&lang=de"),
            _l("XML", "/findNearbyWikipedia?lat={lat}&lng={lng}&radius=10&maxRows=10"),
        ]},
        {"title": "findNearbyPostalCodes – nächste Postleitzahlen", "port": "api", "items": [
            _l("Nach Koordinate", "/findNearbyPostalCodesJSON?lat={lat}&lng={lng}&radius=10&maxRows=5"),
            _l("Nach PLZ", "/findNearbyPostalCodesJSON?postalcode={postal}&country={cc}"),
            _l("Nach Ortsname", "/findNearbyPostalCodesJSON?placename={city}"),
            _l("XML", "/findNearbyPostalCodes?lat={lat}&lng={lng}"),
        ]},
        {"title": "search – Textsuche", "port": "api", "items": [
            _l("Suche", "/searchJSON?q={city}&country={cc}&maxRows=5"),
            _l("Nur Namen", "/searchJSON?name={city}&country={cc}&lang=de"),
            _l("Blättern", "/searchJSON?q={city}&maxRows=10&startRow=10"),
            _l("Beginnt mit … (Prefix)", "/searchJSON?name_startsWith={city}&country={cc}&maxRows=5"),
            _l("Nur Orte (featureClass P)", "/searchJSON?q={city}&featureClass=P&maxRows=5"),
            _l("Nach Einwohnern sortiert", "/searchJSON?q={city}&orderby=population&maxRows=5"),
            _l("XML", "/search?q={city}&country={cc}"),
        ]},
        {"title": "get / hierarchy – Details und Verwaltungshierarchie",
         "note": "Die Links nutzen die geonameId des nächsten Orts am Beispielort (und seines Bundeslands/Bezirks); sie wird beim Laden und bei jedem Wechsel des Beispielorts automatisch ermittelt. Bei anderen Orten eine ID aus einem search-/findNearby…-Ergebnis nehmen.", "port": "api", "items": [
            _l("get (JSON)", "/getJSON?geonameId={id}&style=FULL", needs_id=True),
            _l("get (XML)", "/get?geonameId={id}", needs_id=True),
            _l("hierarchy (JSON)", "/hierarchyJSON?geonameId={id}", needs_id=True),
            _l("hierarchy (XML)", "/hierarchy?geonameId={id}", needs_id=True),
            _l("children (JSON) – Bezirke des Bundeslands", "/childrenJSON?geonameId={state_id}", needs_id=True),
            _l("children (JSON) – Gemeinden des Bezirks", "/childrenJSON?geonameId={county_id}", needs_id=True),
            _l("children (XML) – Bezirke des Bundeslands", "/children?geonameId={state_id}", needs_id=True),
        ]},
        {"title": "countryInfo – Länderdaten", "note": "Quelle ist countryInfo.txt in deinem GeoNames-Volume.", "port": "api", "items": [
            _l("Dein Land", "/countryInfoJSON?country={cc}"),
            _l("Alle Länder", "/countryInfoJSON"),
            _l("XML", "/countryInfo?country={cc}"),
        ]},
        {"title": "timezone – Zeitzone, Offsets, lokale Zeit, Sonnenauf-/untergang", "port": "api", "items": [
            _l("JSON", "/timezoneJSON?lat={lat}&lng={lng}"),
            _l("Mit Datum", "/timezoneJSON?lat={lat}&lng={lng}&date={date}"),
            _l("XML", "/timezone?lat={lat}&lng={lng}"),
        ]},
        {"title": "address – Straße/Hausnummer/PLZ/Ort zu Koordinaten", "port": "api", "items": [
            _l("JSON", "/addressJSON?lat={lat}&lng={lng}"),
            _l("XML", "/address?lat={lat}&lng={lng}"),
        ]},
        {"title": "countryCode / countrySubdivision – Land und Bundesland zu Koordinaten", "port": "api", "items": [
            _l("countryCode (Klartext: AT)", "/countryCode?lat={lat}&lng={lng}"),
            _l("countryCode (JSON)", "/countryCodeJSON?lat={lat}&lng={lng}"),
            _l("countryCode (XML)", "/countryCode?lat={lat}&lng={lng}&type=xml"),
            _l("countrySubdivision (JSON)", "/countrySubdivisionJSON?lat={lat}&lng={lng}"),
            _l("countrySubdivision (XML)", "/countrySubdivision?lat={lat}&lng={lng}"),
        ]},
        {"title": "geoCodeAddress / streetNameLookup / findNearestAddress / findNearbyStreetsOSM",
         "note": "findNearbyStreetsOSM ist eine Näherung (Straßen aus Photon, `line` ist die Diagonale des Straßen-Rechtecks, keine echte Geometrie).",
         "port": "api", "items": [
            _l("geoCodeAddress – Adresstext → Adresse (JSON)", "/geoCodeAddressJSON?q={city}"),
            _l("geoCodeAddress (XML)", "/geoCodeAddress?q={city}"),
            _l("streetNameLookup – Straßen nach Name (JSON)", "/streetNameLookupJSON?q=Hauptstra%C3%9Fe&country={cc}&maxRows=10"),
            _l("streetNameLookup (XML)", "/streetNameLookup?q=Hauptstra%C3%9Fe&country={cc}"),
            _l("findNearestAddress (JSON)", "/findNearestAddressJSON?lat={lat}&lng={lng}"),
            _l("findNearestAddress (XML)", "/findNearestAddress?lat={lat}&lng={lng}"),
            _l("findNearbyStreetsOSM (JSON)", "/findNearbyStreetsOSMJSON?lat={lat}&lng={lng}&radius=10&maxRows=10"),
            _l("findNearbyStreetsOSM (XML)", "/findNearbyStreetsOSM?lat={lat}&lng={lng}"),
        ]},
        {"title": "postalCodeSearch / postalCodeLookup", "port": "api", "items": [
            _l("Nach PLZ", "/postalCodeSearchJSON?postalcode={postal}&country={cc}&maxRows=10"),
            _l("Nach Ortsname", "/postalCodeSearchJSON?placename={city}&country={cc}"),
            _l("Lookup (PLZ → Orte)", "/postalCodeLookupJSON?postalcode={postal}&country={cc}"),
            _l("XML", "/postalCodeSearch?postalcode={postal}&country={cc}"),
        ]},
        {"title": "srtm3 / astergdem / gtopo30 – Höhe",
         "note": "Alle drei liefern denselben Wert. Keine Höhe bekannt: -32768 (srtm3) bzw. -9999.", "port": "api", "items": [
            _l("srtm3 (JSON)", "/srtm3JSON?lat={lat}&lng={lng}"),
            _l("astergdem (JSON)", "/astergdemJSON?lat={lat}&lng={lng}"),
            _l("gtopo30 (JSON)", "/gtopo30JSON?lat={lat}&lng={lng}"),
            _l("Klartext", "/srtm3?lat={lat}&lng={lng}"),
            _l("Mehrere Punkte in einem Aufruf (max. 20)", "/srtm3JSON?lats={lat},48.2082&lngs={lng},16.3738"),
            _l("Mehrere Punkte, Klartext (eine Zahl pro Zeile)", "/srtm3?lats={lat},48.2082&lngs={lng},16.3738"),
        ]},
    ]},
    {"id": "compat", "title": "Nominatim- und Photon-kompatibel (für Traccar und Dawarich)",
     "note": "Eigene Ports: 8431 = Nominatim-API, 8432 = Photon-API. Antworten kommen aus Cache → Geocoder, danach werden deine Overrides darübergelegt.",
     "groups": [
        {"title": "Nominatim (Port 8431)", "port": "nom", "items": [
            _l("Status", "/status"),
            _l("reverse (jsonv2)", "/reverse?format=jsonv2&lat={lat}&lon={lng}"),
            _l("reverse (json, wie Traccar)", "/reverse?format=json&lat={lat}&lon={lng}&zoom=18&addressdetails=1"),
            _l("reverse (geojson)", "/reverse?format=geojson&lat={lat}&lon={lng}"),
            _l("search", "/search?q={city}&format=jsonv2&limit=3"),
        ]},
        {"title": "Photon (Port 8432)", "port": "photon", "items": [
            _l("Status", "/status"),
            _l("reverse", "/reverse?lon={lng}&lat={lat}"),
            _l("Suche", "/api?q={city}&limit=3"),
        ]},
    ]},
]


SECTIONS.append({"id": "curl", "title": "Schnelltest per curl",
                 "note": "Ein Klick auf die ganze Befehlszeile kopiert sie in die Zwischenablage – dann im Terminal einfügen und ausführen. Mit dem gewählten Beispielort.",
                 "groups": [{"title": "Einzelne Abfragen", "port": "api", "items": [
                     _l("Health mit echtem Backend-Ping", "/health?probe=true", curl=True),
                     _l("Reverse Geocoding", "/reverse?lat={lat}&lon={lng}", curl=True),
                     _l("Nächster Ort (GeoNames)", "/findNearbyPlaceNameJSON?lat={lat}&lng={lng}&maxRows=3", curl=True),
                     _l("Länderdaten", "/countryInfoJSON?country={cc}", curl=True),
                     _l("Alles auf einmal (4 Befehle mit BASE=…)", "", curl=True, script=True),
                 ]}]})


def all_paths() -> set:
    """Path part (before '?') of every item -- used by the test that compares with ENDPOINTS.md."""
    out = set()
    for sec in SECTIONS:
        for g in sec["groups"]:
            for it in g["items"]:
                if it["path"]:
                    out.add((g["port"], it["path"].split("?")[0]))
    return out
