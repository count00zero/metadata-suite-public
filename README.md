# Metadata Suite

A small modular web app for NAS media maintenance.

- **Module #1 — Timestamp & Rename** (ports `Timestamp-Bilder-Videos.sh` +
  `resolve_times.py`): browse a folder, see a live preview of the resolved local
  timestamp / target filename for every photo or video, pick which files to apply it
  to, and execute.
- **Module #2 — GPS Sidecar** (ports `Set-GPS-Data-via-traccar-and-dawarich.sh`'s
  coordinate/location pipeline): for every photo/video in a folder, resolve a complete
  GPS position (its own EXIF GPS, or the nearest Traccar/Dawarich track point in time)
  and a full reverse-geocoded location (country/state/county/city/locality/street + ISO
  alpha-3 country code), preview it, and write it to a `.xmp` sidecar (and, for images,
  optionally directly into the file too).
- **Module #3 — AI Description** (ports `Set-GPS-Data-via-traccar-and-dawarich.sh`'s
  `-AIM`/`-AIF` passes + the `generate_ai_description.py` helper it shells out to): for
  every JPEG/video in a folder, generate a short German scene description with a
  self-hosted Ollama vision model and/or Google Gemini (same provider priority-list and
  fallback-on-failure behavior as the CLI), preview it, select which to apply, and write
  it to a `.xmp` sidecar (and, for images, optionally directly into the file too).
- **Module #4 — Datei-Zeitstempel** (ports `restore_file_timestamp.py`): for every
  photo/video (and `.xmp` sidecar) in a folder, compares its current filesystem mtime
  against the capture time encoded in its filename (falling back to its own EXIF/
  QuickTime capture date), shows which ones have drifted, select which to fix, and
  resets their mtime — independent of *why* it drifted (a backup/restore, a copy to
  another filesystem, an exiftool write from outside this app, ...), not tied to any
  single other module's writes.
- **Module #5 — Collage** (ports `photo_pile_collage.py`/`.sh`): point it at a year/
  Urlaube folder full of trip folders (or straight at one trip folder), pick which
  trip(s) to (re)generate, and build one scattered-photo-pile or mosaic collage per
  trip — six layouts, metadata copied from a randomly chosen source photo, mirrored to
  the Home-Video library, with a "neu erzeugen" do-over per trip if a result wasn't to
  your liking. See its own section below.
- **Module #6 — NFO Maker** (ports `HomeVideo_NFO_Maker_new.sh`): generates Kodi `.nfo`
  files plus `folder.jpg`/`poster.jpg`/`fanart.jpg`/`thumb.jpg` for videos — camera/
  recording-software detection, mediainfo/exiftool stream metadata, a plot built from
  that plus whatever GPS/location/AI-description data is already sitting in the
  video's `.xmp` sidecar. See its own section below for the one deliberate scope
  decision (read-only for geo/AI data) and everything else that was ported.

A **standard flow** chains five of the modules via "Weiter zu ..." handoff buttons:
**Timestamp & Rename → GPS Sidecar → Kontext nachfüllen → AI-Beschreibung → NFO Maker** (the last hop only
when the just-processed batch actually contained videos — generating `.nfo` files for a
folder of photos makes no sense). A "Weiter zu GPS Sidecar →" button appears after
executing a Timestamp & Rename batch, "Weiter zu Kontext nachfüllen →" after a GPS Sidecar
batch (GPS Sidecar writes position, place and camera name only; height, time zone, weather, light, trip and IPTC are
the Kontext step's job), "Weiter zu AI-Beschreibung →" after the Kontext step (it carries ALL files that came in, also
those that needed nothing), and "Weiter zu NFO Maker →" after an AI-Beschreibung batch that included at least
one video. The order is a reasonable one to work in, not a requirement: a file the GPS
Sidecar module geotags from scratch (no EXIF GPS of its own) then has real GPS for the
*next* Timestamp & Rename pass to use for its own, more accurate time-zone resolution;
the AI Description module's prompt includes whatever location the GPS Sidecar module
already wrote, giving the model real geographic context instead of guessing from the
image alone; and NFO Maker's plot can then include both the location and the AI
description, since it only ever *reads* whatever the two previous steps already wrote to
the `.xmp` sidecar. All modules can otherwise be used independently, in any order.
Landing on a target module via its "Weiter zu ..." button loads that same folder's
preview/scan immediately — "semi-automatic": one click gets you there and scanning
starts right away, but nothing is written until you review the list and click that
module's own execute button yourself.

Each handoff carries the **exact files** the previous module's execute step just
touched, not just the folder it was pointed at — a short-lived id (`core/handoff.py`,
expires after 30 minutes) travels in the "Weiter zu ..." link's URL, and the target
module resolves it server-side into that precise file list, bypassing its usual folder
walk entirely. So if a folder has 22 files and only 1 of them actually got renamed by
Timestamp & Rename, "Weiter zu GPS Sidecar" loads exactly that 1 file — the other 21
never get scanned, counted, or reported as "bereits abgeschlossen". The GPS Sidecar →
AI-Beschreibung hop carries the exact image/video files GPS Sidecar actually wrote a
`.xmp` for; the AI-Beschreibung → NFO Maker hop carries only the exact *video* files AI-
Beschreibung actually wrote a description for (never the photos in the same batch). If
a handoff link is followed after its 30-minute window (or the server restarted since),
it falls back to the old plain folder handoff — same as before this existed.

**A handoff list is never split into steps:** the files were already chosen in the previous module, so Kontext, AI-Beschreibung,
NFO Maker and GPS Sidecar load all of them at once when they arrive via "Weiter zu ..." (no BATCH_SIZE, no "Nächste laden").
The stepped loading only applies to lists found by a folder scan. In the Jahreslauf the Kontext step now always runs right
after GPS and before AI/NFO.

Datei-Zeitstempel and Collage sit outside this chain (independent, standalone tools).
**Kurzvideos löschen** is also standalone — it finds videos shorter than
`NFO_SHORT_VIDEO_SECS` (together with their `.nfo`/poster/fanart/thumb/`.xmp` companion
files) and deletes them directly, without routing through NFO Maker's own scan (which
would also generate thumbnails for every other video in the folder — unnecessary work
this module skips entirely). All three are listed last in the module navigation for
that reason, with Kurzvideos löschen positioned last of all.

Across every module, the **"Ausführen"**-style button (whatever it's labeled — "Ausführen",
"NFOs erstellen", "Collagen erstellen", "Zeitstempel korrigieren") greys itself out and
becomes unclickable (an htmx out-of-band swap, `id="exec-btn"`, from the results that
come back) once that exact batch has actually been processed, so it can't be clicked a
second time by accident and silently reprocess the same selection. It goes back to normal
the next time the scan/preview step ("Vorschau laden"/"Scannen"/"Collagen suchen") is run,
which is also the only way to run the same files again on purpose.

Designed as **one container, one FastAPI app, multiple route-groups** — a future tool gets
added the same way: a new `app/modules/<name>/` package with its own router and templates,
plus one more `app.include_router(...)` line in `app/main.py`. The GPS Sidecar module's
own GPS-track and reverse-geocoding backends
(Traccar, Dawarich, Photon, Nominatim, Pelias, a maritime-backup service) are **not**
part of this container — they're the maintainer's own already-running self-hosted services,
reached purely by URL, same as any other external dependency.

> **Hilfsdienste (Compose-Profile):** `import`, `refresh` und `wikimedia` laufen nie mit `up` und werden von `docker compose build` ohne Angabe nicht gebaut. Alles dazu, auch `docker compose --profile '*' build`: [README-PROFILES.md](README-PROFILES.md).

## Layout

```
app/
  main.py                     # host app, mounts /static, includes module routers
  core/
    timecore.py                # timestamp resolution logic (ported from resolve_times.py)
    geocore.py                   # GPS-track + reverse-geocoding logic (ported from
                                  # Set-GPS-Data-via-traccar-and-dawarich.sh)
    aicore.py                     # AI-description generation logic (ported from the same
                                    # script's -AIM/-AIF passes + generate_ai_description.py)
    mtimecore.py                   # mtime restore logic (ported from
                                     # restore_file_timestamp.py; used by GPS Sidecar's +
                                     # AI Description's own buttons AND module #4)
    executor.py                 # rename / exiftool write / touch / .AAC+.WAV companion
    settings.py                  # JSON-backed reusable presets + env-var config (all modules)
    pathsafety.py                 # shared MEDIA_ROOT/Basis-Ordner path checks (all modules)
    jobs.py                        # in-memory background-job tracking for "Vorschau laden"
                                     # progress bar + Cancel button (all modules)
    nfocore.py                      # NFO/thumbnail generation logic + the shared
                                     # delete_short_video_with_companions() helper
    xmpsync.py                      # reads back everything currently in a .xmp sidecar and
                                     # writes it all into the image in one go (GPS Sidecar's
                                     # and AI-Beschreibung's "ins Bild einbetten" checkboxes)
    handoff.py                      # short-lived store for "Weiter zu ..." handoffs --
                                     # carries the EXACT files a module's execute step just
                                     # touched to the next module, not just the folder
  modules/
    timestamp_tool/
      router.py                  # /timestamp/* routes
      templates/                  # Jinja2 + HTMX fragments
    gps_sidecar/
      router.py                  # /gps/* routes
      templates/                  # Jinja2 + HTMX fragments
    ai_description/
      router.py                  # /ai/* routes
      templates/                  # Jinja2 + HTMX fragments
    mtime_restore/
      router.py                  # /mtime/* routes
      templates/                  # Jinja2 + HTMX fragments
    collage/
      router.py                  # /collage/* routes
      templates/                  # Jinja2 + HTMX fragments
    nfo_maker/
      router.py                  # /nfo/* routes
      templates/                  # Jinja2 + HTMX fragments
    short_video_cleanup/
      router.py                  # /shortvideos/* routes -- standalone short-video finder
                                   # + deleter, reusing nfocore's shared delete helper
      templates/                  # Jinja2 + HTMX fragments
  static/style.css
  static/favicon.ico, favicon.png, favicon-192.png, apple-touch-icon.png
                                # browser-tab/bookmark icon, served both via a <link
                                # rel="icon"> in every module's own <head> and via
                                # /favicon.ico (app/main.py) for tools that only ever
                                # check the bare root path
config/                        # presets.json lives here (mount as a volume)
Dockerfile
docker-compose.yml
.env.example                   # copy to .env and adjust — see Configuration below
requirements.txt
```

## Resolution logic (unchanged from the CLI tools)

Same priority as `Timestamp-Bilder-Videos.sh` / `resolve_times.py`:

1. GPS coordinates present → IANA zone looked up offline via `timezonefinder`;
   `GPSDateTime` (real UTC) converted into it. If there's no `GPSDateTime` (only a GPS
   position), the wall-clock reading itself still has to come from filename → metadata →
   folder → mtime (in that order) — the preview's "Quelle" names exactly which one actually
   supplied it (e.g. "EXIF-Metadaten"), not just "no GPS timestamp".
2. No GPS, but filename digits *and* raw `CreateDate`/`DateTimeOriginal` both parse:
   many action cams (DJI Osmo Action, Insta360, ...) write true UTC into metadata while
   the filename already carries the correct local wall clock — the difference between the
   two *is* the real offset (rounded to 15 min), no GPS needed. A best-effort zone name is
   shown alongside the raw offset for readability (e.g. `Asia/Colombo, +05:30`).
3. Otherwise: filename → folder digits → metadata → file mtime — but **before** falling
   back to the configured Fallback-Zone, the same folder is searched for the
   nearest-in-time *other* file that resolved confidently (real GPS, or the UTC-diff trick
   above) and its timezone is borrowed instead. Only if no such file exists anywhere in the
   same folder does this file actually fall back to the Fallback-Zone. This mirrors the CLI
   script's `find_nearest_sibling_tz_offset()`, scoped to one folder (not recursive): a
   folder usually holds one trip/event, so a sibling shot minutes or hours earlier/later in
   the same place is a much better zone guess than a static global default. The preview's
   "Quelle" says so explicitly when this happens, e.g. `Quelle: Dateiname, Zeitzone von
   Nachbardatei „IMG_1234.jpg“ übernommen (zeitlicher Abstand 1h32m) · Zeitzone:
   Europe/Vienna` — naming both which wallclock reading this file itself used and which
   neighboring file's zone got borrowed. This borrowing only ever happens across files that
   are actually being resolved in the same batch (one `Vorschau laden` /
   `GPS Sidecar: Vorschau laden` call, or one "Umbenannte einblenden" scan) — a file that
   was itself skipped as already-correctly-named isn't read at all, so it can't act as a
   donor either.

The "Quelle" shown for each file in the preview is a human-readable rendering of which of
these it used (e.g. `Quelle: GPS-Zeitstempel (UTC) · Zeitzone: Europe/Vienna`), so it's
visible which data actually drove the rename, not just the result.

**Fixed:** the UTC-diff trick (point 2 above) silently never fired for PHOTOS whose only
populated capture-time EXIF tag is `CreateDate` rather than `DateTimeOriginal` — an
Insta360 Luna Ultra photo with no `DateTimeOriginal` at all. `get_exif()` always reads
`CreateDate` for every file, but `_resolve_core()` discarded it unless the file was a
video (`... if ext in VIDEO_EXT else None`), so that file's `meta_dt` silently ended up
`None` for a photo. With the UTC-diff trick unable to fire, the file wasn't `is_confident`
either, so point 3's same-folder sibling-borrow had nothing to receive from it and nothing
to lend it when every photo from the same batch hit the identical gap — the file (and
every sibling like it) fell all the way through to the plain Fallback-Zone with no real
offset at all. Fixed by reading `CreateDate` for every file type, same priority as before
(`CreateDate` wins over `DateTimeOriginal` when both are present) — this was already how
videos worked, now photos get the same treatment the module docstring always described.

**Camera detection fallback for a camera that writes NO Make/Model metadata at all:**
confirmed against the maintainer's real files — an Insta360 Luna Ultra *video* carries no
EXIF/QuickTime `Make`/`Model` tag whatsoever (unlike its photos, which do), so the
camera-model filter below matched 0 files despite "nearly all videos are from Luna
Ultra". The old bash toolchain's own `Set-GPS` script already had a working answer for
this — `{ head -c 20M "$Video"; tail -c 5M "$Video"; } | strings | grep -i -m1 -E
"insta360|luna ultra"` — so `timecore.sniff_camera_from_bytes()` ports that exact
approach: when a file's EXIF Make/Model are both empty, it reads only the first 20MB and
last 5MB of the file (never the whole thing — these can be multi-GB action-cam videos)
and searches that binary data, case-insensitively, for a known camera/app signature
string, falling back to that as `camera_make`/`camera_model` instead of leaving them
empty. Verified against a synthetic file with no Make/Model tag at all, with the
signature string buried in the file's own binary data, same as a real Luna Ultra video.
Add further confirmed signatures to `CAMERA_SIGNATURES` in `timecore.py` as they come up
(e.g. for DJI Osmo Action 6, once/if it turns out to need the same treatment).

**Per-file "is the local time really stored?" indicator + Make/Model write-back.**
While a "Kamera-Modell enthält" filter is set, every row additionally shows
(`timecore.date_diagnostics()`, one small exiftool call per kept file): the date stored in
the video/photo file itself (video: raw QuickTime `CreateDate`, shown without any UTC
conversion; photo: EXIF `DateTimeOriginal`, plus a "kein UTC-Offset gespeichert" note when
no offset info exists at all), a video's embedded XMP date, and the `.xmp` sidecar's
`xmp:CreateDate` — each marked ✓/⚠ against the timestamp this scan resolved (a value with
an offset is compared as an instant, one without as local wall clock, 2s tolerance; a
camera's raw UTC `CreateDate` therefore shows up as ⚠). The row headline says "⚠ Lokalzeit
fehlt in der Video-Datei – EXIF-Backwrite nötig", "⚠ Datei ok, aber .xmp weicht ab", or "✅
stimmen überein", and a **"Nur Abweichende auswählen"** button ticks exactly the rows that
need a backwrite/sidecar refresh. Without a camera filter nothing extra is read.
Make/Model are now also written where viewers read them: whenever `ensure_xmp_sidecar()`
creates or refreshes a `.xmp`, `XMP-tiff:Make/Model` is added if the sidecar lacks them
(from the file's tags, or — for Luna Ultra videos — the byte-signature sniff); and an
"EXIF zurückschreiben" run adds `Make`/`Model` to the file itself, but only when the file
has neither already (a camera's own tags are never overwritten). After that, later scans
find a Luna Ultra video by its real tags, no sniffing needed.

**EXIF write failures and the "only files that need a backwrite" checkbox.** Some real
files make exiftool's QuickTime writer fail outright (`Not a HASH reference at
WriteQuickTime.pl line 1732`, seen on Insta360 videos; not reproducible with synthetic
files, so the cause is unconfirmed — the optional Make/Model write was the newest
addition to that command). A failed write leaves the file untouched, so
`executor.apply_resolution()` now retries with less: full command (`exiftool -m -q -api LargeFileSupport=1 … -P -overwrite_original`) → without Make/Model →
also without `-quicktime:time:all`. The first error is always kept and shown as an
"ℹ Erster Versuch fehlgeschlagen (…) – geschrieben ohne …" note; only if every attempt
fails is it reported as an error. Independently, whenever "EXIF zurückschreiben" runs and a
`.xmp` exists, its `xmp:CreateDate`/`ModifyDate`/`DateTimeOriginal` are pinned directly to
the resolved local time + offset (`geocore.write_dates_to_xmp()`) — even if the video itself
couldn't be rewritten — so Immich is right regardless. New form checkbox **"Nur Dateien ohne
Lokalzeit in der Datei (EXIF-Backwrite nötig)"** (`only_needs_backwrite`): runs the per-file
date check for every scanned file (not only under a camera filter), lists only those where
the local time is missing in the file itself, and implies "Umbenannte einblenden". It
combines with the camera filter.

**Readable device names (`core/devicenames.py`).** A raw EXIF Make/Model like
`Samsung SM-S921B` is translated for display and filtering in three stages, first match
wins: (1) your own `config/camera_names.json` (`CAMERA_NAMES_FILE`; see
`config/camera_names.example.json` — case-insensitive fnmatch patterns against the Model and
"Make Model", re-read whenever the file changes), (2) NFO Maker's `CAMERA_SOURCE_RULES`
table, so both modules call a device the same thing ("Samsung Galaxy S24"; its
software/encoder-only labels such as "Google Movie" are skipped), (3) Google's Play
"Supported Devices" list, kept **outside the image** in your config volume as `/config/android_devices.json` (`ANDROID_DEVICES_FILE`; ~43k Android
models, e.g. `SM-S921B` → Galaxy S24, `22101316UG` → Redmi Note 12 Pro+ 5G). Fetch it once and refresh it whenever you like with
`docker compose exec metadata-suite python -m app.core.devicenames update` (the container downloads Google's public
[supported_devices.csv](https://storage.googleapis.com/play_public/supported_devices.csv); the app itself never goes online and picks up a
changed file without restart; without the file the raw name is shown). Otherwise the
raw name is shown. Timestamp & Rename shows `📷 <name> (<raw> · <source>)` per row, and
"Kamera-Modell enthält" matches the raw Make/Model *and* the translated name ("SM-S921B",
"S24", "Galaxy" all find the same files). Display/filter only: EXIF/XMP still get the raw
Make/Model, never the translated name. Android only — iPhones/cameras come from your own
or NFO Maker's rules.

**Re-checking files already mis-named by the bug above (camera-model filter):** the fix
only changes resolution behavior going forward — files a Luna Ultra (or any camera hit by
a similar gap, e.g. a DJI Osmo Action 6) already produced *before* the fix keep whatever
wrong offset they were given, and since their filename already matches the target pattern
(`is_already_named()`), a normal re-scan hides them again. The Timestamp & Rename form has
a "Kamera-Modell enthält" field (matched case-insensitively against the file's own EXIF
`Make`+`Model`, e.g. "Luna Ultra" or "Osmo Action 6") for exactly this: setting it
auto-enables "Umbenannte einblenden" (no need to click it separately), so a scan surfaces
*every* file from that camera in the folder/library, already-correctly-named ones
included, each tagged with a 📷 camera badge. Important: because the UTC-diff trick (point
2 above) always trusts the filename's own wall-clock digits and only recomputes the
*offset*, re-resolving one of these files essentially never changes the filename itself —
only the offset/zone embedded in the result changes. So for this specific repair, what
actually needs re-running is **"EXIF zurückschreiben"**, which rewrites the correct offset
into the file's EXIF and `.xmp` sidecar; "Umbenennen" can stay checked too (it'll just be a
no-op for these rows) but isn't what fixes anything here. The filter only narrows what
Timestamp & Rename resolves/shows — no other module filters by camera.

**This matters for videos specifically, not just photos — and not only because of the
Luna Ultra photo bug above.** the maintainer's own convention has been to skip "EXIF
zurückschreiben" for videos entirely, relying on just the `.xmp` sidecar (which, per
[Immich's own XMP sidecar docs](https://docs.immich.app/features/xmp-sidecars/), Immich
does read for videos too, including `xmp:CreateDate`, for its "taken at" date and
timeline sort order). That's fine for a camera whose own `DateTimeOriginal`/`CreateDate`
is already local wall-clock — but action cams like the Insta360 Luna Ultra (and
reportedly the DJI Osmo Action 6) write their video `CreateDate` as **true UTC**, so
without an actual EXIF backwrite to the video file, the already-correctly-named file's
`.xmp` sidecar — built by `geocore.ensure_xmp_sidecar()`'s `-tagsFromFile` copy, which
copies tags literally (`-api QuickTimeUTC=0`, no UTC→local conversion) — permanently
carries the camera's raw UTC value rather than the resolved local time: a 2-hour-ish
offset in Immich (or any other `.xmp`-reading viewer) that persists even after the file
was renamed correctly, because renaming alone never touches the embedded metadata.
Confirmed end-to-end with a synthetic Luna Ultra video.

**Fixed (same change as above):** executing with "EXIF zurückschreiben" now also
re-syncs an *already-existing* `.xmp` sidecar right after a successful write
(`geocore.ensure_xmp_sidecar()`, same function GPS Sidecar/AI-Beschreibung's own
"Ausführen" already called) — so the corrected timestamp lands in the sidecar
immediately, in the same step, instead of silently staying stale until GPS
Sidecar/AI-Beschreibung happens to run again on that file. Only refreshes a sidecar that
already exists; never creates one (that's still GPS Sidecar's/AI-Beschreibung's job, the
first time a file gets any geo/AI data at all).

**Consistency cross-check (web UI only):** when the filename itself encodes a date/time,
the preview also compares it against the timestamp actually resolved. For the filename-
based paths above (plain filename, folder, and the UTC-diff trick) they agree by
construction, since the resolved wall clock *is* the filename's. But when GPS supplies its
own real timestamp independently of the filename, the two can legitimately disagree — a
wrong camera clock, a copied/re-encoded file, or a bad GPS fix. If they differ by 2 minutes
or more, the row is flagged with a warning naming both timestamps, instead of silently
producing a rename the original filename would contradict. This check only adds a warning
for review — it never changes which timestamp gets used for the rename/EXIF write, so it
doesn't affect `Timestamp-Bilder-Videos.sh`/`resolve_times.py` parity.

Companion files (a `.xmp` sidecar, a companion audio track, a generated thumbnail, or any
other same-stem sibling) are found and shown as their own selectable rows, not renamed
automatically — see "Companion files" below.

**Every row in the preview is checked by default**, same as every other module — "Alle
auswählen"/"Keine" are there for adjusting the selection, not for turning it on in the
first place.

**Already-correctly-named files are skipped by default.** Before resolving anything, a
cheap filename-only check (no exiftool/GPS call) looks for files whose name already
*starts with* the target format (`YYYY.MM.DD-HH.MM.SS`) — anything appended after that
(a disambiguation suffix like `-1`, a camera/app tag like `_05` or `_HDR`, `(1)`, ...) is
ignored, so `2026.09.04-14.58.25_05.jpg` still counts as already named. These are left
out of the preview entirely by default — they can't be selected or renamed, and nothing
is read from their metadata. If the scan actually found any, a **"Umbenannte einblenden"
(N)** button appears next to "Vorschau laden"; clicking it reloads the same folder with
those files included too, so the whole folder's contents can still be reviewed on demand.
The button only shows up when there's something to reveal, and disappears again once it's
been clicked (since at that point nothing is being skipped anymore).

**"Vorschau laden" shows real per-file progress and a working Cancel button**, in both
modules. Clicking it starts the scan as a background job and immediately shows a progress
bar ("N / M Dateien verarbeitet — aktuell: `<filename>`"), self-updating a few times a
second, instead of a single opaque "lädt…" spinner blocking the whole request until every
file is done. Clicking **Abbrechen** actually stops the scan close to immediately — a
slow/stuck exiftool call on a flaky NAS mount is killed mid-flight rather than left to run
out its own timeout, and no further files are touched. GPS Sidecar and AI Description
each split their work into **two separately cancelable background jobs** instead of one
(see each module's own section below for why) — the progress bar labels which phase is
currently running, and a cancelled scan or cancelled expensive-step both leave you with
something useful to act on immediately rather than a dead end. This uses a small in-memory job tracker
(`core/jobs.py`) scoped to this one process/container — a job doesn't survive a server
restart, which only matters if that happens mid-scan (the UI simply reports the job as not
found and you re-run it).

**Fallback-Zone suggestion.** Once a scan finishes, the folder's own confidently-resolved
files (real GPS-derived timezone, the filename/UTC-diff trick, or a borrowed sibling
offset — anything `tz_confident`, see "Resolution logic" above) are tallied by IANA zone
(`timecore.suggest_fallback_zone()`). If the most common one differs from the Fallback-Zone
currently set in the form, and more than one file backs it up, a banner appears above the
file list ("💡 Häufigste sicher erkannte Zeitzone in diesem Ordner: `Asia/Colombo` (6 von 6
sicher erkannten Datei(en))") with an **"Übernehmen & neu laden"** button that writes the
suggested zone into the Fallback-Zone field and re-submits the scan — handy when a whole
folder is from a trip and the configured default is still set to home. Available in both
modules.

**Companion files are found, shown, and individually selectable, right alongside the
file they belong to.** Renaming a video or photo used to either silently drag along only
its companion audio track (`.AAC`/`.WAV`/`.MP3`), or — for a `.xmp` sidecar, or a
`-thumb`/`-poster`/`-fanart` generated image — not touch it at all, leaving it behind
under the old filename, now mismatched with its renamed primary file. `timecore.
find_companions()` instead looks, per primary file, for every OTHER file in the same
folder that belongs to the same capture: its `.xmp` sidecar (`"<original filename>.xmp"`),
a `-thumb`/`-poster`/`-fanart` image, a companion audio track, or simply another file
sharing the exact same stem under a different extension (e.g. a `.JPG` still next to an
`.AVI` video). Each one shows up as its own row in the preview, indented and dashed-
bordered under its primary file, with its own computed target name (the new timestamp +
whatever came after the old stem, e.g. `2026.09.27-12.11.17.MP4.xmp` or
`2026.09.27-12.11.17-thumb.jpg`) and its own checkbox — so you see and choose exactly
what gets renamed, rather than it happening invisibly (or not at all). A companion row
is only ever renamed + mtime-touched (`executor.apply_companion()`), never exiftool-
written (`do_exifwrite` doesn't apply to it — a sidecar/thumbnail/audio track has no
`alldates`/GPS tags of its own worth writing the way a real media file does).

A `-thumb`/`-poster`/`-fanart` image is deliberately excluded from being its OWN
independent candidate (it technically has a recognized image extension, so it would
otherwise get its own, meaningless EXIF/filename-based resolution) whenever the video/
photo it belongs to is also present in the same folder — it only ever moves in lockstep
with that file. An *orphaned* thumbnail (its video/photo missing) is left as its own
independent candidate, same as before, so it's never silently dropped from the preview.

This matching is deliberately NOT a bare filename-prefix check: a disambiguation suffix
this app's own rename might produce (or a camera's own numbering, e.g. `IMG_0001.jpg`
vs. `IMG_00011.jpg`) is its own, independently-timestamped file, not a companion — only
an EXACT stem match (or stem + a known generated-image suffix) counts, so
`2026.08.09-17.17.49-1.MP4` next to `2026.08.09-17.17.49.MP4` is correctly left alone as
its own separate file instead of being swept up as a "companion".

**Fixed: a `.nfo` file could end up shown as its own independent candidate instead of
staying attached as a companion, and an unchanged companion flooded the preview.**
Two related issues, both surfacing through "Umbenannte einblenden" / the camera-model
filter (either one bypasses the normal `is_already_named()` skip):
1. `.nfo` is one of the extensions `scan_candidates()` matches for an `ALL`-type
   Basis-Ordner — fine normally (an already-named `.nfo` gets silently skipped, same as
   its video), but once `include_already_named` is set, it got resolved as its own
   (meaningless — it's a text file with no real capture-time EXIF) primary candidate,
   and because `find_companions()` only attaches a file as a companion when it ISN'T
   *also* independently a primary row, it showed up as its own standalone entry instead
   of staying attached under its video. Fixed the same way an orphaned-vs-attached
   `-thumb`/`-poster`/`-fanart` image already was: excluded from independent candidacy
   whenever a sibling video/photo with its exact stem exists; an orphaned `.nfo` (no
   matching video) is still left as its own candidate.
2. Separately — and this is what actually caused the maintainer's report with a **VIDEOS-only**
   Basis-Ordner, where `.nfo`/`.jpg`/`.xmp` could never have become independent
   candidates in the first place (`type_filter="VIDEO"` excludes them at `find_files()`
   level already) — revealing a previously-hidden already-named video via "Umbenannte
   einblenden" still pulled in *all* of its companions (`.xmp`, `.nfo`,
   poster/fanart/thumb) as their own rows, even though none of them were going to
   change (a companion's target name is built from the primary's new stem, so it only
   ever differs from the companion's current name when the primary itself is actually
   being renamed). Fixed by simply not emitting a companion row at all when its target
   name already matches its current name — nothing to act on, nothing shown. A
   companion of a file that IS being renamed still shows exactly as before.

## GPS Sidecar module

**Two separately cancelable steps, so a deselected file never pays for reverse-geocoding
at all.** "Vorschau laden" only runs the cheap/local step: metadata + any GPS the file
already carries (`timecore.resolve_batch`, same exiftool pass as module #1), one
folder-wide Traccar/Dawarich track fetch, and local nearest-track-point matching — no
network geocoding yet. The result is a selectable list (all files checked by default,
already-complete ones excluded per "Bereits abgeschlossene überspringen" as before);
files without any resolvable GPS position are shown with a warning and can't be selected.
Deselect whatever you don't want processed, then click **"Geocoding starten"** — only
*then* does the second step run, calling Photon/Nominatim/Pelias/maritime-backup once per
*still-selected* file. Its own resulting list (what "Ausführen" acts on) is likewise all
checked by default (anything with neither a resolved GPS position nor a geocoded location
is shown disabled instead), same as every other step in this app — so narrowing the
selection for "Ausführen" only ever means deselecting a few files, not having to
re-select everything from scratch. Both steps are their own background job (`core/jobs.py`) with a
real progress bar and a working **Abbrechen** button; cancelling the geocoding step
doesn't lose the scan — it drops you back on the same selectable list (nothing already
geocoded in that run is kept, but nothing needs re-scanning either) so you can adjust the
selection and click "Geocoding starten" again. This is the same job/progress/cancel
mechanism module #1 uses for its own "Vorschau laden", just split across two endpoints
(`/browse/start` → `/geocode/start`) instead of one.

**Only files matching the selected Basis-Ordner's type are ever looked at.** Pointing
"Vorschau laden" at a folder that mixes photos and videos while a VIDEOS-type
Basis-Ordner (or any explicit `type_filter=VIDEO`) is selected means only the videos
(and, implicitly, whatever `.xmp` sidecars/companions belong to them) are scanned at
all — photos in that same folder are filtered out of the file list itself
(`core/timecore.py`'s `find_files()`), not merely skipped later during resolution, so the
progress bar's total and the scan's candidate list both already reflect only the files
that were ever actually going to be processed. (The same applies to Timestamp & Rename's
own "Vorschau laden".)

For every file that reaches the geocoding step, the same priority chain as the CLI's
`reverse_geocode_lookup()`/`resolve_geo_fields()`:

1. **GPS already in the file** (reused from module #1's own `resolve_one()` call —
   see "Code reuse" below) → used as-is. Resolved during the scan step, not geocoding.
2. **No GPS in the file** → the nearest Traccar/Dawarich track point **in time** to the
   file's resolved capture time is used instead, tried within a tight window first
   (`GEO_MAX_GAP_SECS`, default 10 min), then a wider one (`GEO_MAX_GAP_SECS_TIER2`,
   default 6h) if nothing was found. Still nothing within that? The file is left without
   a GPS position (shown in the preview, not selectable). Also resolved during the scan
   step — this is all local computation against the already-fetched track, no network
   call of its own.
3. Whatever position was resolved (from either source) is then **reverse-geocoded**
   (the geocoding step, network calls): Photon first, then Nominatim, then Pelias, then
   (only if all three came back empty — the normal case out on open water) a self-hosted
   **maritime backup** for named bodies of water. An ISO **alpha-3** country code is
   attached either from Pelias directly, or converted from whichever alpha-2 code
   Photon/Nominatim gave, via a built-in static ISO 3166-1 table (replacing the CLI's
   external `alpha2_to_alpha3.py` script/process with a plain Python lookup — same
   result, one less moving part).

Writing (on "Ausführen"): a `.xmp` sidecar always gets the resolved GPS + location
(`XMP-exif:GPSLatitude/Longitude`, `XMP-photoshop:Country/State/City`,
`XMP-iptcCore:CountryCode/Location`). "Bei Bildern zusätzlich direkt einbetten" — now
**unchecked by default** — additionally embeds directly into the image file itself, but
not just this module's own GPS/location fields: it syncs the image with *everything
currently in the `.xmp` sidecar* (see "Simplified image embedding" below), in one single
write. Videos only ever get the sidecar — never direct file modification — same
convention as the rest of the CLI toolchain.
A file whose `.xmp` sidecar already has Country/City filled in is skipped by default
("Bereits abgeschlossene überspringen") so repeat browses of the same folder don't keep
re-querying the geocoding servers for files that are already done.

**Code reuse with module #1:** `timecore.Resolution` now also carries the raw `lat`/`lon`
GPS coordinates `resolve_one()` reads from the file (previously parsed internally and
discarded) and a `has_gps` property — the GPS Sidecar module reuses module #1's own
`resolve_many()`/`resolve_one()` call (in `EXIF` mode) to get both a file's existing GPS
*and* its fully resolved, timezone-aware capture time in one exiftool call, rather than
reading each file a second way. The MEDIA_ROOT/Basis-Ordner path-containment checks and
the Pfad field's sub-folder suggestions were factored out of `timestamp_tool/router.py`
into `core/pathsafety.py` for the same reason — both modules now share one
implementation instead of two that could drift apart. The Pfad field's "remember the
last path per Basis-Ordner" browser-local-storage key is shared between both modules
too, so picking "PHOTOS" in one module restores the same last-used path the other
module remembers for it.

**Deliberately not ported in this first pass** (CLI-only for now): AI-generated scene
descriptions, Kodi NFO generation, DJI `.srt` GPS extraction, predefined/interactive
location selection, and exiftool's own `-geotag`/GPX-matching engine for images (its
MakerNotes-rebuild retry and tiered `GeoMaxExtSecs` radius) — the track-nearest-point
matching here is a straightforward Python port of the same *intent* (nearest-in-time
match within a capped gap), not a call into exiftool's geotagging engine itself.
Sibling-photo time-zone-offset detection (`find_nearest_sibling_tz_offset()`) is no
longer on this list — it's implemented once in `timecore.resolve_many()` (see
"Resolution logic" above) and the GPS Sidecar module gets it for free through the same
code-reuse described below, with no changes needed on its own side.

## AI Description module

Ports the CLI script's `-AIM`/`-AIF` passes and the `generate_ai_description.py` helper
they shell out to: a short German scene description, written by a self-hosted Ollama
vision model and/or Google Gemini, per photo/video.

**Same two-phase, separately-cancelable split as GPS Sidecar, same reason.** Calling an
AI provider is the expensive step here — a model inference call instead of a network
geocoding lookup, but the same cost shape — so it's kept out of the cheap scan phase
entirely. **"Vorschau laden"** only reads what's already on disk: any existing AI
description (and whether it looks garbled — see below), plus whatever location fields
the GPS Sidecar module (or the original CLI) already wrote, formatted into the same
geo-context string the prompt uses. No AI provider is called yet. The result is a
selectable list (all files checked by default); deselect whatever you don't want
processed, then click **"Beschreibung generieren"** — only *then* does the second step
run, calling the configured AI provider(s) once per *still-selected* file. Both steps are
their own background job with a real progress bar and a working **Abbrechen** button;
cancelling generation doesn't lose the scan — it drops you back on the same selectable
list (nothing already generated in that run is kept, but nothing needs re-scanning) so
you can adjust the selection and click "Beschreibung generieren" again.

**The location context actually fed into the AI prompt is now shown explicitly, both
before and after generation** — a **"📍 Ortskontext für den Prompt"** line on the scan
list (showing the resolved "Country, State, City"/"City only for Austria" string, or an
explicit "— keiner gefunden" when no `Country`/`State`/`City` is set on the file's own
`.xmp` sidecar yet, e.g. because GPS Sidecar hasn't run on it), and the same line again
("📍 Ortskontext, der an die KI übergeben wurde") on the generation results, next to each
resulting description — so it's directly visible whether a given description's wording
(or lack of any location mention) reflects a real location context or the complete
absence of one, rather than having to trust the prompt-building code blindly.
`aicore.build_ai_geo_context_from_existing()` itself was verified byte-for-byte against
the original CLI script's own `build_ai_geo_context_from_existing()` bash function (same
`XMP-photoshop:Country/State/City` tags, same Austria-conditional "city only" formatting)
and against an end-to-end test that captures the actual prompt text handed to the
configured AI provider — the plumbing is correct; if a description still doesn't *mention*
a location that the UI confirms was fed into the prompt, that's the configured model
choosing not to follow that part of the instruction (seen more with a small local vision
model than with a larger one), not a bug in how this app builds or passes the context.

**Fill-missing vs. force-regenerate.** By default (`overwrite_existing` unchecked) a file
that already has a usable AI description is excluded from the scan entirely, same idea as
GPS Sidecar's "Bereits abgeschlossene überspringen" — repeat browses of a folder only
surface the files that still need one. A description is treated as **missing** if it's
actually absent *or* if it looks **garbled**: a historical corruption bug (in the CLI
toolchain, before this web port existed) could leak log output or error text into the
description field instead of real text, detected the same way the CLI's own repair tooling
does (long runs of a repeated character, an embedded `[YYYY-MM-DD HH:MM:SS]` timestamp, or
a handful of literal marker substrings like `"ERROR:"`) — such a file is offered for
regeneration automatically, no separate "repair" step needed. Checking "Alle vorhandenen
Beschreibungen überschreiben" switches to force-regenerate mode instead: every matching
file is listed, including ones with a perfectly good existing description (shown
read-only for reference, so you can judge whether it's worth replacing).

Each AI provider call: for an image, the file itself (resized in two tiers — 640px wide
first, then down to 400px if still over 200KB — via `ffmpeg`, same as the CLI) is sent
directly; for a video, a single frame is extracted via `ffmpeg` first (at the 1-second
mark, to avoid a black or fading-in opening frame; falling back to frame 0 for very short
clips) and that frame is sent instead.

**That extracted video frame is only ever a throwaway temp file for this one AI call —
it is never saved as `<stem>-thumb.jpg` next to the video.** Generating and persisting a
video's actual thumbnail (and `poster.jpg`/`fanart.jpg`) is the NFO Maker module's job
alone (see "NFO Maker module" below); AI Description used to also persist its own copy
here, but that meant every video got frame-extracted twice — once here, once again
(unconditionally) whenever NFO Maker ran — for no benefit, since NFO Maker's own ffmpeg
call overwrites it regardless. If a `<stem>-thumb.jpg` already sits on disk (from an
earlier NFO Maker run, or the old NAS CLI toolchain), it's still read and reused as-is
with no `ffmpeg` call at all — only the *writing* of a new one was removed, not that
reuse. Until NFO Maker has actually run on a video, its row here (and everywhere else)
shows the generic "▶" placeholder rather than a real frame — that's expected now.

`AI_PROVIDER` is a priority list (e.g.
`ollama,google`) — each configured provider is tried in order, falling through to the
next on failure, same semantics as the CLI's own `generate_ai_description.py`; the
provider and model that actually produced each description are shown next to it. The
same German system/user prompts and the same post-processing (stripping a handful of
known AI-boilerplate prefixes/suffixes, capitalizing the result, rejecting anything with
fewer than 10 distinct non-whitespace characters as unusable) were ported verbatim from
`generate_ai_description.py`.

Writing (on "Ausführen"): a `.xmp` sidecar always gets the generated description
(`XMP-dc:Description`). "Bei Bildern zusätzlich direkt einbetten" — **still checked by
default here** — additionally embeds directly into the image file, but (like GPS
Sidecar's own checkbox) syncs the *whole current `.xmp` snapshot*, not just the
description: if GPS Sidecar already ran on this photo, its GPS/location fields get
embedded alongside the description, all in one write (see "Simplified image embedding"
below). Videos only ever get the sidecar — same convention as GPS Sidecar and the rest
of the CLI toolchain.

**Two deliberate deviations from 1:1 CLI parity:**

1. The CLI's video pass required a video to already *have* a `.xmp` sidecar (created
   earlier by its GPS-tagging pass) before it would write a description into it. This
   module instead creates the sidecar on demand (`geocore.ensure_xmp_sidecar()`) if one
   doesn't exist yet, consistent with how GPS Sidecar itself already behaves — so AI
   Description works standalone on a video that was never run through GPS Sidecar first,
   not just as a follow-on step.
2. The CLI's own `exiftool_write_image()`-equivalent always reported success (`return 0`)
   regardless of what exiftool actually did. This module's version returns the real
   `(success, message)` outcome instead, so a write failure (e.g. a camera's MakerNotes
   block defeating exiftool's rebuild step — see the next paragraph) shows up as an
   actual error in the result screen rather than a silent false "✓".

**MakerNotes-retry, ported from the CLI.** A handful of cameras (Kuna Ultra, DJI Neo 2,
...) have non-standard MakerNotes blocks that make exiftool's automatic rebuild step fail
outright ("Error rebuilding maker notes" / "No writable tags set" / "Duplicate
MakerNote") — and when that happens, exiftool drops *every* requested tag write for that
call, not just MakerNotes. When that failure signature is detected, the same write is
retried once with `-MakerNotes=` (strip the block entirely) instead of giving up.

**Code reuse:** the Basis-Ordner/Pfad field, its sub-folder suggestions, the "remember
last path per Basis-Ordner" browser storage, and `core/pathsafety.py`'s path-containment
checks are all the same shared implementation the other two modules use. Only JPEG stills
get a direct AI call (`.jpg`/`.jpeg` — the CLI's own `-AIM` restriction; PNG/GIF/PSD are
skipped here even though Timestamp & Rename accepts them), same as the original script.

### Per-group fallback for broken QuickTime structures

If every full-write variant fails (e.g. `Not a HASH reference at WriteQuickTime.pl line 1732`, seen on Insta360 Luna Ultra videos even with exiftool 12.76), the executor writes each tag group on its own (alldates, MediaCreateDate, xmp:alldates, quicktime:time:all, TimeZoneOffset, GPS time) and reports in the result note which groups could be written and which not. The `.xmp` sidecar dates and the file mtime are set regardless -- that is what Immich uses for videos.

## Immich-first diagnostics (XMP decides, not the video file)

Immich takes the capture date **and timezone** from the `.xmp` sidecar as soon as it carries a date (embedded date tags are then ignored; a value without offset is read as UTC -- verified against Immich's XMP-sidecar docs and `metadata.service.ts`). So the per-file check in Timestamp & Rename (camera filter active, or one of the "Nur ..." checkboxes) now judges **the sidecar**:

- ✅ ".xmp stimmt mit dem Ziel überein – Immich sortiert korrekt" -- DateTimeOriginal/CreateDate in the `.xmp` are offset-qualified and the right instant.
- ⚠ keine .xmp (video) / ⚠ Datum weicht ab / ⚠ Datum ohne UTC-Offset -- needs a fix. The video file's own raw-UTC value is shown for information only when the sidecar is fine.
- Photos without a sidecar are still judged by the image file itself (local time + offset present).
- New checkbox **"Nur Dateien mit falschem/fehlendem Datum im XMP (Immich-Sortierung)"** (`only_needs_fix`); the older "ohne Lokalzeit in der Datei selbst" checkbox stays for the EXIF-backwrite view.
- New execute option **".xmp-Sidecar-Datum setzen"** (`do_xmpdate`, default on): pins `xmp:CreateDate/ModifyDate` + `exif:DateTimeOriginal` in an *existing* sidecar to local time + offset, independent of "EXIF zurückschreiben" -- so videos need no backwrite. Never creates a sidecar (GPS Sidecar does).

After fixing sidecars, re-run Immich's metadata extraction job.

## Unchecked-checkbox defaults (bug fix)

Browsers send nothing for an unchecked checkbox, and FastAPI then used the parameter's default -- so an endpoint declared `Form(True)` could never be switched off. This made "Bereits abgeschlossene überspringen" (GPS Sidecar) impossible to deactivate, and the same latent bug sat on "ins Bild einbetten" (AI-Beschreibung, 3 endpoints) and "AI-Beschreibung" (NFO Maker, 2 endpoints). All of these now default to `False` server-side; the pages render the checkbox pre-checked where the default should be on, so the visible behaviour is unchanged except that unchecking now works.

## Old "Ergebnis" is cleared on a new scan

`app/static/clear-results.js` (loaded by every module page) empties `#results-panel` the moment the scan form (`#browse-form`: "Vorschau laden"/"Scannen"/...) is submitted, so the previous run's result never stays next to a fresh preview. Verified in a real Chromium with htmx.

## Audit against the original scripts (2026-10-05) -- fixed

- **Offset tags in photos:** "EXIF zurückschreiben" now also writes `OffsetTimeOriginal/OffsetTimeDigitized/OffsetTime` (like the original Set-GPS) -- only when the zone is real information (GPS, the photo's own embedded offset, UTC-diff trick, borrowed sibling), never for a Fallback-Zone guess. The photo diagnostics now require `OffsetTimeOriginal`/`OffsetTime` (the old check accepted `TimeZoneOffset`), so already-processed photos show up under "Nur Dateien mit falschem/fehlendem Datum" and get the tags on the next backwrite run.
- **Embedded offset as a timezone source:** a photo carrying its own `OffsetTimeOriginal/OffsetTime/OffsetTimeDigitized` (and a DateTimeOriginal that agrees with the filename) resolves with exactly that offset (`EXIF-Offset(...)`, confident) instead of the default zone -- matters abroad. Videos unaffected.
- **GPS sign bug when embedding into photos:** exiftool drops the sign of `-GPSLatitude=-33.5` without Ref tags (southern/western hemisphere landed in N/E). Now written as absolute value + explicit `GPSLatitudeRef/GPSLongitudeRef` (xmpsync + geocore).
- **Sidecar location fields as in the original:** `State`, county -> `iptcExt:LocationCreatedProvinceState`, locality -> `iptcCore:Location`, street -> `iptcExt:LocationCreatedSublocation` (NFO Maker reads exactly these; the street used to get lost), plus `dc:Subject` geo keywords (`geo:lat=`, `geo:lon=`, `geotagged`, country code/country/county/city/locality/street). Embedding into a photo builds `IPTC:Sub-location` as "locality, street".
- **NFO Maker:** repair-ai flagged every NFO as garbled (the NFO's own indentation matched the repeat check) -- whitespace ignored now; repair-ai/reintegrate-ai/repair-italic modes now rewrite the NFO without also needing "überschreiben"; `.m4v` recognised.
- **AI-Beschreibung:** video frames get the original video prompt ("einzelner Moment (Frame) aus einem privaten Heimvideo").
- **Collage:** one failing trip no longer aborts the batch (recorded as an error row).
- **Timestamp & Rename:** the optional RESTORE_TIMESTAMP step only re-touches files whose direct touch failed (it uses one fixed MTIME_TIMEZONE and would otherwise overwrite correct per-file epochs of travel photos).

### Still open
Nothing from the audit of the original scripts is open any more (the remaining ideas -- large-video fallback, recursive sibling timezone detection, standalone description sync, touch-now, AI folder-context hint -- were discarded on purpose).

### GPS Sidecar "Ausführen" progress + auto-scroll (Oct 2026)
Writing the reverse-geocoding results now runs as a background job with a progress bar and **Abbrechen** (`POST /gps/execute/start`, `GET /gps/execute/progress/{id}`, `POST /gps/execute/cancel/{id}`; the old synchronous `POST /gps/execute` stays for scripts). In every module the page scrolls to the progress panel as soon as it appears and to the result when the run ends (`static/clear-results.js`).

### Filename add-ons are preserved (Oct 2026)
A file that already starts with a timestamp keeps whatever follows it: `2026.07.03-09.35.00_02.jpg` stays `…_02.jpg` (only the timestamp part is ever replaced), and its `.xmp`/companions follow with the same add-on. Two numbered shots can therefore no longer collapse onto one target. Companion rows are still only listed when the primary is really renamed.

### Companion files are never listed (Oct 2026)
`.xmp` sidecars, companion audio and generated thumbs/posters no longer appear as rows in Timestamp & Rename. They are still renamed/touched together with their media file (they run whenever it is selected), and the media file's row announces it: "📎 Begleitdateien werden mitgeändert: a.jpg.xmp → b.jpg.xmp". The "Datei(en) gefunden" count counts media files only.

### Same-timestamp numbering (Oct 2026)
When two files resolve to the same target name (same second, different camera/milliseconds) or the target already exists as another file, only those files get a running `_01`, `_02`, … after the timestamp (a file already holding exactly the plain name keeps it; files without a collision are never numbered). The number is part of the preview and is executed exactly as shown (`name_suffix` travels with the row). Existing add-ons are kept (`…_02` colliding becomes `…_02_01`).

### Progress coverage audit (Oct 2026)
Every step that touches many files now shows progress + Abbrechen: scans/previews (all modules), geocoding, **Ausführen** in Timestamp & Rename, GPS Sidecar and AI-Beschreibung (`/ai/execute/start`), NFO/collage generation, and **"Datei-Zeitstempel wiederherstellen"** in Timestamp/GPS/AI (`<module>/restore-mtime/start`, shared `core/mtime_routes.py`). Deliberately without progress (instant, no exiftool): Datei-Zeitstempel's own "Ausführen" (only `os.utime`), the delete buttons in NFO Maker and Kurzvideo-Bereinigung, and single-row "neu generieren".

### No EXIF write-back for videos (Oct 2026)
"EXIF zurückschreiben" now only touches **images**. Videos are never rewritten (a result note says so; the file mtime is still set), the date goes only into the `.xmp` sidecar, which is what Immich reads. The "Nur Bilder ohne Lokalzeit…" filter therefore lists no videos, and video diagnostics show the file's own value as info only. (Background: exiftool 12.76 fails on some Insta360 files with `Not a HASH reference at WriteQuickTime.pl line 1732`.)

### Track-matching time (GPS Sidecar)
- **Filename special cases** (as in the original): range names `A-B` / `A bis B` -> midpoint; date-only names -> 12:00 that day; year-only names -> no position lookup. The list shows "⏱ Track-Zeit: …".
- **Video `CreateDate` as UTC**: for videos whose time did *not* come from the filename, the raw QuickTime `CreateDate` is read as UTC for the track match. Files with a timestamp in the name are unchanged.
- Fix: a date-only/year-only name vs. a far-away metadata date no longer crashes the zone resolution (UTC-diff trick now only for gaps < 15h).
- **Timestamp & Rename**: when the `.xmp` date is written, the sidecar's own mtime is pinned to the capture time too and shown ("✓ .xmp-Zeitstempel gesetzt"); the sidecar is also part of the "Datei-Zeitstempel wiederherstellen" list.

### GPS Sidecar additions (Oct 2026)
- **Camera filter** ("Kamera-Modell enthält") in GPS Sidecar and AI-Beschreibung (EXIF Make/Model or translated device name; Insta360 byte sniff for videos without tags).
- **Override existing GPS** ("Vorhandenes GPS mit Track/.srt/festem Ort überschreiben"): files that already have GPS (image EXIF and/or `.xmp`) get the position from `.srt` / fixed place / Traccar-Dawarich track. If nothing is found the old GPS stays. Photos get the GPS written into the image too (explicit N/S/E/W Ref tags); videos only into the sidecar. Ignores "Bereits abgeschlossene überspringen". The list shows "↻ überschreibt vorhandenes GPS …".
- **Sidecar GPS reuse**: GPS already in the `.xmp` counts as existing GPS ("aus dem .xmp-Sidecar").
- **No-GPS memory**: when track sources were actually queried and nothing matched, the sidecar gets `XMP-photoshop:TransmissionReference=no-gps-found`; the next run skips these (counter shown) unless "erneut versuchen" is ticked. The marker is cleared when GPS is written. Marking happens at scan time (opt-out checkbox).
- **DJI `.srt`**: same-named `.srt` next to a video is parsed (`[latitude: x] [longitude: y]` and `GPS (lon, lat, n)` layouts) before the track lookup; optional "`.srt` nach erfolgreichem Schreiben löschen" (only after the GPS was read back from the sidecar).
- **Fixed places (`-GEO`)**: the geo-service's *Meine Orte* (Geo-Overrides editor with map) are offered as dropdown "Fester Ort"; without the service the list is empty. The old `predefined_locations` file (`GEO_PREDEFINED_LOCATIONS`) was removed – entries that were already imported stay normal *Meine Orte* (`legacy-…`). Applies to all files without GPS (or all with overwrite) and replaces track matching and reverse geocoding.
- **Keywords in images**: IPTC `Keywords` + `XMP-dc:Subject` (`geo:lat=…`, `geo:lon=…`, `geotagged`, ISO3, country, county, city, locality, street) are now embedded into photos too (existing non-geo keywords are kept).
- **TimeZoneOffset**: written only for whole-hour zones (EXIF stores integers); half-hour zones (+05:30, +05:45) are carried exactly by `OffsetTimeOriginal/Digitized/OffsetTime`.

## Progress & cancel for "Ausführen" (Timestamp & Rename)

The execute step (rename + EXIF write + sidecar resync) runs as a background job, like the scan.
The results panel shows "n / total Datei(en) geschrieben — aktuell: <file>" with a progress bar and
an **Abbrechen** button (stops cleanly *between* files; already processed files stay changed and are
listed in the result, with a "Abgebrochen nach n von m" note). Endpoints: `POST /timestamp/execute/start`,
`GET /timestamp/execute/progress/{id}`, `POST /timestamp/execute/cancel/{id}`. The old synchronous
`POST /timestamp/execute` still exists for scripts. The MEDIA_ROOT check runs for every selected file
before the job starts. Jobs are in-memory: after a server restart the progress poll says "nicht gefunden".

## Simplified image embedding (one write, not one per module)

For a **photo**, GPS Sidecar and AI Description can each run on it in the standard flow,
and each always writes its own data (`.xmp` sidecar, always) plus an optional direct
embed into the image file itself. Embedding into the image is **not** each module writing
only its own slice of data separately anymore — both modules' "Bei Bildern zusätzlich
direkt einbetten" checkboxes call the same shared `core/xmpsync.py::sync_xmp_to_image()`:
it reads back *everything currently in the `.xmp` sidecar* (GPS + resolved location, and
the AI description if one has been generated yet) and writes the whole snapshot into the
image in one single `exiftool` call, regardless of which module's checkbox triggered it.

This replaced each module writing only its own fields to the image independently
(`geocore.write_location_to_image()` for GPS Sidecar, `aicore.write_description_to_image()`
for AI Description) — harmless correctness-wise (exiftool writes are additive; one
module's fields were never erased by the other's write), but wasteful: a photo that went
through both modules got the actual image file rewritten by `exiftool -overwrite_original`
*twice*, each needing its own mtime bump + restore. Reading the current `.xmp` state and
writing it once accomplishes the same end result with one rewrite instead of two.

To make that one-write-per-photo the default without losing the option to embed earlier,
the checkboxes' **defaults changed**: GPS Sidecar's is now **unchecked** by default (for a
photo, it usually is **not** the last step of the standard flow — AI Description normally
follows), while AI Description's stays **checked** by default (it usually *is* the last
step for a photo). Ticking GPS Sidecar's checkbox is still fully supported for a batch that
will never go through AI Description — it still gets everything in the `.xmp` so far (just
GPS/location at that point, nothing missing), it just means the image gets rewritten again
later if AI Description also embeds afterward. This change is **not retroactive** — photos
already embedded under the old per-module behavior are untouched; only future runs differ.
Videos are unaffected either way — they never get direct file modification, only the `.xmp`
sidecar, in any module.

## Restoring file timestamps after a write

Every `exiftool -overwrite_original` write bumps the touched file's filesystem
modification time (mtime) to "now", even though nothing about when the photo/video was
actually *taken* has changed. **Timestamp & Rename mostly doesn't have this problem** —
its own execution step sets the mtime directly to the resolved capture time right after
writing, as an inherent part of applying a rename, not a separate step. **GPS Sidecar and
AI Description do**, since all they write via exiftool is GPS/location/description tags,
so every "Ausführen" on either of those two modules silently leaves every touched file
(and its `.xmp` sidecar, if one was written) mtime-stamped "now".

All three of those modules' results screens show a **"🕒 Datei-Zeitstempel
wiederherstellen"** button whenever anything was actually written, listing exactly the
files and `.xmp` sidecars that were just touched — a quick, targeted "undo the mtime bump
from what I just did" for that one batch. For Timestamp & Rename this is a safety net
rather than something it normally needs: its own mtime-setting step already runs
unconditionally after every rename/write, so the button only ever has real work to do if
that specific `os.utime()` call itself failed — this independent filename/EXIF-based
resolution can still fix the mtime in that one edge case.

**`RESTORE_TIMESTAMP=true`** (env var, default `false`) runs that same button's logic
automatically right after every "Ausführen" in all three modules, no click needed — the
results screen then shows the restore outcome inline instead of the button. Leave it
unset to keep the manual, opt-in button (the default).

**Module #4 — Datei-Zeitstempel** is the standalone, broader version of the same idea:
instead of only the files one specific "Ausführen" just touched, it scans a whole folder
(or NAS share) and compares *every* photo/video's (and `.xmp` sidecar's) current mtime
against what it *should* be — whatever actually caused the drift (a backup/restore, a
copy between filesystems, an exiftool write from some other tool entirely, not just this
app's own modules). It uses the exact same browse/preview-then-execute workflow as
Timestamp & Rename (a cancelable background scan job with a real progress bar, since
there's no separate expensive phase here to protect, unlike GPS Sidecar/AI Description's
two-phase split): "Vorschau laden" shows only the files whose mtime actually differs from
its resolved target, each row showing "Aktuell → Ziel" and which source resolved it, with
a running count of how many were already correct (left out of the list, just tallied) and
how many have no resolvable date at all (shown anyway, disabled, with a warning — never
silently dropped). Nothing is touched until "Zeitstempel korrigieren" is clicked, and it
applies exactly what was previewed (same "preview is what gets applied" convention as
Timestamp & Rename's own execute), not a fresh re-resolution.

Both the per-module buttons and the standalone module share one resolution rule —
filename-pattern first, EXIF/QuickTime capture-date fallback, same logic as the CLI
toolchain's `restore_file_timestamp.py` (which the original bash script calls as a manual
follow-up pass after its own GPS-tagging writes) — implemented once in `core/mtimecore.py`
(`resolve_target()`/`scan_one()` for previewing, `restore_one()`/`restore_many()` for the
two modules' own immediate-apply buttons) and never ported twice. A file is only ever left
untouched (and reported as such, never guessed at) if neither the filename nor EXIF/
QuickTime yields a usable date, or the mtime is already correct — same "never guess" rule
as the CLI script throughout.

**Timezone:** filenames/EXIF here encode a real-world local wall-clock time (e.g.
`2026.09.27-12.11.17.MP4` means 12:11:17 local time), but a file's actual mtime is stored
as a plain UTC epoch. `core/mtimecore.py` converts between the two using an explicit IANA
zone (`MTIME_TIMEZONE`, default `Europe/Vienna`) rather than a naive, container-local
conversion — the container this app runs in has no system timezone configured (it
defaults to UTC on the stock `python:3.13-slim` base image), so a naive conversion would
silently misreport/misapply every timestamp by the difference between UTC and your real
locale (exactly 2 hours for Vienna in CEST — this is what caused an already-correct file
to be reported and treated as "2 hours off"). This works correctly regardless of the
container's own system timezone because the `tzdata` PyPI package (`requirements.txt`)
ships the full IANA database for Python's `zoneinfo` to use even without OS-level tzdata.
Change `MTIME_TIMEZONE` only if your files' encoded timestamps are in a different
real-world locale than Vienna.

## Collage module

Ports `photo_pile_collage.py`/`.sh`'s "trip based" collage generator. Six layouts:
**polaroid** (default-ish, white border, scattered pile), **postcard** (postcard mount +
soft shadow, bigger cards, gentler rotation), **cascade** (heavier overlap + shadow, like
photos piled on top of each other), **magazine** (hand-torn paper edges, chaotic
rotation), **mosaic** (non-overlapping justified grid, no rotation), and **random** (a
different one of the above picked per trip). Every source image is used at most once, and
no photo is ever cropped at a canvas edge.

**Trip-folder auto-discovery, same as the CLI's `--recursive`:** point Pfad at a year/
Urlaube folder full of trip folders — each immediate subfolder with its own images (or, if
it only has further subfolders and no images of its own, *its* subfolders in turn, so this
also works pointed one level higher, e.g. at `Urlaube/` itself) becomes its own trip,
collage generated separately. Point Pfad straight at one trip folder instead, and that one
folder becomes the single trip — no separate flag or toggle needed, the module just tries
discovery first and falls back to treating Pfad as the one trip when discovery finds
nothing below it, exactly like the CLI does.

**Workflow — two phases, like GPS Sidecar/AI Description:**
1. **"Collagen suchen"** — cheap (no image decoding, just a filesystem walk): lists every
   trip folder discovered, how many images each has, and whether a collage for it already
   exists (unchecked by default in that case, so a second run doesn't silently overwrite
   anything unless "Vorhandene Collagen überschreiben" is ticked).
2. **"Collagen erstellen"** — the actual rendering, only for whichever trips are still
   selected. For each: builds the canvas, saves `<Trip-Name>.jpg` next to the trip folder,
   copies it in as the trip folder's own `folder.jpg`, mirrors it to the Home-Video
   library (see below), copies GPS/location/description/date metadata from one randomly
   chosen source photo via `exiftool -TagsFromFile` (no reverse-geocoding — a direct
   copy), and resets the collage file's own mtime to match that source's capture date
   (`core/mtimecore.py`, in-process — the CLI shells out to `restore_file_timestamp.py`
   for this same step).

Each finished result shows a thumbnail of the actual collage and a **"🔀 Neu erzeugen"**
button — a synchronous, single-trip do-over with a fresh random seed and an implicit
overwrite, for when a result wasn't to your liking, without re-running the whole batch or
touching the "überschreiben" setting for anything else.

All settings shown on the page (layout, canvas size, "Vorhandene Collagen überschreiben",
...) are read live at the moment "Collagen erstellen" is clicked, not snapshotted back when
"Collagen suchen" was clicked — so ticking "überschreiben" only after seeing which trips
already exist (the natural order: search, notice a trip is marked "existiert bereits",
*then* tick the box) takes effect, as does any other setting changed in between.

**Settings, standard via `.env`, overrideable per run:** layout, canvas width/height,
image count (use only N random images per trip instead of all), border width, max rotation
(scattered layouts only), cell size / tile height (override the automatic sizing),
jitter, size-variation, mosaic spacing, seed, and "sync existing collage into folder.jpg
without regenerating" are all plain fields on the Collage module page (canvas size/count/
border/rotation/layout always visible, the rest under "Erweiterte Einstellungen") — they
default from `COLLAGE_*` env vars (mirroring the maintainer's own standard invocation: `--width
4096 --height 2304 --count 139 --border 6 --layout random --rotation 25`) but a single run
can override any of them without touching the environment. See `.env.example` for the full
list.

**Home-Video mirroring:** `PHOTO_ROOT`/`VIDEO_ROOT` (same env vars and behavior as the CLI)
— a collage generated under `<PHOTO_ROOT>/Urlaube/2026/Foo` is copied to
`<VIDEO_ROOT>/Urlaube/2026/Foo.jpg`, but only when that folder already exists on the video
side (mirroring never creates folders there) and only when the destination doesn't already
exist unless "überschreiben" is active. Set `VIDEO_ROOT=` (empty) to disable mirroring
entirely.

**Safety note:** every other module only ever writes inside the folder it was pointed at
(or to a sibling file right next to one it's processing). This module is the one exception
— a trip's collage is written one level **above** the trip folder itself (next to it, so
it sits alongside sibling trip collages), which can be outside the Basis-Ordner that was
selected if Pfad was pointed directly at the Basis-Ordner's own root. The `MEDIA_ROOT`
safety fence is still always enforced either way; only the stricter "must stay inside the
selected Basis-Ordner" check is relaxed for this one output location, since writing next
to the trip folder is this module's entire point.

## NFO Maker module

Ports `HomeVideo_NFO_Maker_new.sh`'s Kodi `.nfo` + artwork generator. Point it at a
folder (depth 1–7) or straight at a single video file; for each candidate video it
extracts stream metadata (`mediainfo` + `exiftool`), detects the recording camera or
software (the same large curated pattern table as the CLI, including its exact
last-match-wins quirk when several fields match), extracts `folder.jpg`/`<Name>-poster
.jpg`/`<Name>-fanart.jpg`/`<Name>-thumb.jpg` via `ffmpeg`, and writes `<Name>.nfo`.

**Deliberate scope decision, confirmed up front:** this module is **read-only** for
GPS/location and AI-description data. The CLI script can fetch a missing `.xmp` geo
sidecar itself (shelling out to `Set-GPS-Data-via-traccar-and-dawarich.sh`) and can call
an AI provider itself for the plot description. Both of those are already separate
modules here — **#2 GPS Sidecar** and **#3 AI-Beschreibung** — that write exactly those
`.xmp` fields, so NFO Maker only ever *reads* whatever's already in a video's `.xmp`
sidecar. A video with no sidecar yet (or a sidecar with no AI description yet) simply
gets an `.nfo` without that piece — run GPS Sidecar / AI-Beschreibung on it first if you
want it included. (This also means the CLI's `--force-geo` flag has no equivalent here:
there's no fetch step left to force.)

**Workflow — two phases, like GPS Sidecar/AI-Beschreibung/Collage:**
1. **"Scannen"** — cheap (mediainfo/exiftool metadata + reading any existing `.xmp`, no
   `ffmpeg` thumbnail extraction yet): lists every candidate video with its camera,
   length, size, existing geo/AI-description summary (if any), and whether it already
   has an `.nfo` (unchecked by default in that case, so a second run doesn't silently
   overwrite anything unless **"Vorhandene .nfo überschreiben"** is ticked). In **Normal**
   mode this first pass only shows videos that **don't have an `.nfo` yet** — a hint
   ("N weitere Video(s) ... haben bereits eine .nfo") with a **"Weiter scannen, um
   existierende .nfo-Dateien zu finden"** button extends the same scan to include those
   too, without re-walking the folder from scratch. While a scan of a big/deep folder is
   still running, **"Fertig — bisherige Videos übernehmen"** stops it early and hands
   back whatever was already found instead of discarding it — handy for a depth-7 scan of
   a huge tree where you don't want to wait for the very last file just to start working.
2. **"NFOs erstellen"** — the actual thumbnail extraction + `.nfo` write, only for
   trips still selected. Every setting shown on the page (force/force-jpg/
   force-timestamp/AI-toggle/`-L`/`-O`) is read **live** at the moment this is clicked,
   not snapshotted back when "Scannen" was clicked — so ticking "überschreiben" only
   after seeing which videos already have an `.nfo` takes effect (same fix already
   applied to the Collage module after the maintainer ran into exactly that with its own
   "Vorhandene Collagen überschreiben" checkbox).

Each finished result shows the generated thumbnail and a **"🔀 Neu erzeugen"** button —
a synchronous, single-video do-over (always overwrites the `.nfo` and its thumbnails)
for when a result wasn't to your liking.

**Smart filter buttons** on the scan list, next to "Alle auswählen"/"Keine":
- **"⚠ Nur unvollständige (Ort/AI fehlt) auswählen"** — selects only videos whose `.xmp`
  sidecar is missing entirely, has no location fields at all, or (when the AI toggle is
  on) has no AI description or a garbled one. Each such row also shows which of those
  reasons applied. Meant to flag "run GPS Sidecar / AI-Beschreibung on these first" before
  (re)generating their `.nfo` here.
- **"⏱ Nur sehr kurze Videos (<N s) auswählen"** — selects only videos under the
  configured threshold (`NFO_SHORT_VIDEO_SECS`, default 10 seconds — a common
  accidental-recording length). A video mediainfo couldn't read is never counted as
  short, even though its duration also reads as 0 — an unreadable file must never be
  silently swept into a destructive filter.

**Deleting accidental short clips:** the **"🗑 Ausgewählte kurze Videos (<N s) löschen"**
button (next to "NFOs erstellen") deletes every *checked* row's video together with its
companion files (`<Name>.nfo`, `<Name>-poster.jpg`, `<Name>-fanart.jpg`,
`<Name>-thumb.jpg`, `<Name>.xmp`) — but **only** for videos actually under the configured
threshold. Safeguards: a confirmation prompt before anything happens; the threshold is
re-verified server-side with a fresh `mediainfo` read right before deleting (never trusts
what the browser last displayed, in case a file changed or a request got replayed); any
checked video that doesn't pass is skipped and reported, never deleted; the shared
`folder.jpg` in the same directory is never touched, since it belongs to the whole folder,
not to one clip. This is a real, unrecoverable delete — there is no undo.

**Modus** (mirrors the CLI's mutually-exclusive scan flags):
- **Normal** — every video found under Pfad.
- **repair-ai** — only videos whose *existing* `.nfo` plot has a garbled AI description
  (a model repetition-loop, or leaked internal log output from an old bug) — scans
  `.nfo` files directly, not videos, and finds each one's matching video next to it.
- **reintegrate-ai** — only videos whose `.xmp` sidecar *already* has a clean AI
  description that isn't in the `.nfo` yet (e.g. after backfilling descriptions via the
  AI-Beschreibung module) — no AI provider call needed, since this module never calls
  one anyway.
- **repair-italic** — only videos whose `.nfo` still has the old `[I]`/`[/I]` plot
  markup from before the formatting changed.

**`-O`/`--older`** protects an `.nfo` modified within the last *N* days (0 or negative)
from being regenerated, even with "überschreiben" ticked or in a repair-* mode —
exactly like the CLI, this check always wins.

**Settings, standard via `.env`, overrideable per run:** director/studio/genre/tag/
mpaa/certification/user- and critic-rating, the Kodi `[COLOR]` used for the location
line, the `<actor>` list (and the dynamic "CAMERA" actor's own thumbnail path), whether
an existing AI description gets embedded, whether to skip reading `.xmp` at all
(`-L`/`--nolocation`), and the `-O`/`--older` default — see `.env.example` for the full
list and the actor-list format (`Name|Role|Thumb` triples separated by `;`).

**Camera/software detection fidelity note:** ported as a literal pattern table
(`core/nfocore.py`'s `CAMERA_SOURCE_RULES`), including the CLI's own existing quirk of
scanning every candidate field with no early exit — so when more than one curated field
matches a pattern, the **last** one (in the CLI's fixed field order) wins, not the
first. Preserved faithfully rather than "fixed", since it's the CLI's actual existing
behavior and changing it would silently reclassify videos that already work correctly
today.

**Fixed:** "Samsung Galaxy S24" videos were being mislabeled "Altes Android
Smartphone". The rule only matched the literal string `"Galaxy S24"`, but Samsung's own
EXIF `Model` tag reports the model *number* (`SM-S921*` for the S24, `SM-S926*` for the
S24+, `SM-S928*` for the S24 Ultra, `SM-S721*` for the S24 FE), never the marketing name
— so it never actually matched, and the file fell through to a much more generic,
coincidence-prone rule further down. The rule now also matches the `SM-S92*`/`SM-S721*`
model-number family. If a video still comes out mislabeled, the most likely next culprit
is one of the other very short/generic patterns in that same table (e.g. a bare `"10"`
or `"6*"`) matching some unrelated field value by coincidence — if that happens, the
`curated_fields`/`all_strings` captured on `VideoMetadata` (visible by inspecting a
scanned row) show exactly which field matched and why.

## Kurzvideos löschen module

Standalone module (`/shortvideos`), positioned last in the module navigation. Finds
every video under Pfad shorter than the configured `NFO_SHORT_VIDEO_SECS` threshold
(default 10s — the maintainer's own NAS is configured to 7s) and deletes it together with its
companion files in one step: `.nfo`, `-poster.jpg`, `-fanart.jpg`, `-thumb.jpg`, and
`.xmp`. A shared `folder.jpg` in the same directory is **never** touched — it's one
cover image per folder, not a per-video companion.

This logic already existed *inside* NFO Maker's own scan results ("Ausgewählte kurze
Videos löschen"), tied to a full NFO-generation scan over the whole folder (which also
extracts a thumbnail for every OTHER video it finds along the way). This module exists
so finding and removing short clips doesn't require that detour: its own scan only ever
does one mediainfo duration read per video — no thumbnail extraction, no `.nfo`
writing, no GPS/AI reads — which is the whole point ("Will speed things up!").

The actual deletion (the companion-file list, and the mandatory fresh duration re-check
right before removing anything — never trusting what a scan preview last showed) is one
shared implementation, `core/nfocore.py`'s `delete_short_video_with_companions()`, used
by both this module's own `/shortvideos/delete` and NFO Maker's `/nfo/delete`, so the
two can never drift apart.

The "Schwellwert" field on this module's own scan form only narrows or widens *that one
preview* — deleting still always re-checks every candidate against the real, configured
`NFO_SHORT_VIDEO_SECS` value, so loosening the preview threshold just to look at more
candidates can never delete more than the configuration actually allows.

## Running locally (without Docker)

```bash
pip install -r requirements.txt
# exiftool must be on PATH (apt install libimage-exiftool-perl / similar)
# ffmpeg must be on PATH too, for the AI Description module (apt install ffmpeg / similar)
uvicorn app.main:app --reload --port 8420
```

Open http://localhost:8420/

## Running in Docker

```bash
docker build -t metadata-suite .
docker run -p 8420:8000 \
  -v /mnt/NAS:/mnt/NAS \
  -v $(pwd)/config:/config \
  -e MEDIA_ROOT=/mnt/NAS \
  metadata-suite
```

Or, with `docker-compose.yml` (recommended — this is what persists your settings across
updates):

```bash
cp .env.example .env    # then edit .env with your paths
docker compose up -d --build
```

`docker-compose.yml` reads `.env` automatically (same folder, built-in compose behavior)
for `${VAR}` substitution. Your real settings live only in `.env` — re-extracting a newer
version of this project (a new zip, a `git pull`, ...) overwrites `docker-compose.yml`
itself but never touches `.env`, so nothing gets clobbered. `.env` is gitignored for the
same reason; `.env.example` is the checked-in template.

### `check_env.py` — check your private `.env` locally

Since no private data lives in the repo any more (director, studio, credit, actors, hosts, tokens ...), `check_env.py`
verifies that **your own `.env`** contains everything that is needed. It uses only the Python standard library, needs no
Docker and runs on any machine; nothing is sent anywhere, and **secret values (TOKEN/API_KEY/...) are never printed** —
only "set (N characters)".

```bash
python check_env.py                      # ./.env against ./.env.example
python check_env.py --env /path/.env     # another file
python check_env.py --paths              # also check that the *_HOST_DIR folders exist (run this on the server)
```

| Level | Meaning | Examples |
|---|---|---|
| `FEHLER` (exit code 1) | something required is missing or malformed | `TRACCAR_URL` set but no `TRACCAR_TOKEN`; `DAWARICH_SERVER` without API key; `AI_PROVIDER=google` without `GOOGLE_AI_API_KEY`; `ollama` without `OLLAMA_URL`/`OLLAMA_MODEL`; invalid URL, number, `true/false`, `ROOT_FOLDERS`, `*_DEVICES` or `NFO_ACTORS` format; `.env` tracked by Git |
| `WARNUNG` | empty private value or an example value copied from `.env.example` | `NFO_DIRECTOR`, `NFO_STUDIO`, `IPTC_CREDIT`, `NFO_ACTORS` empty; `GEO_SERVICE_TOKEN` empty/short; `192.168.1.x`, `Your Name`; no geocoder; `TZ` missing; unknown variable name (typo hint, e.g. `NFO_DIRECTR` → `NFO_DIRECTOR`) |
| `HINWEIS` | minor | `.env` readable by others (`chmod 600 .env`), AI key set but provider unused |

One `.env` serves both containers (suite and geo-service), so there is nothing separate to check for `geo-service/`;
its variables (`GEO_*`, `PHOTON_*` ...) are recognised too.

Exit codes: `0` ok (warnings possible), `1` at least one error, `2` file unreadable. Update the rule lists inside the
script whenever a new mandatory/sensitive variable is added to `.env.example`.

## Configuration (environment variables)

Set these in `.env` (see `.env.example`) when using `docker-compose.yml`, or pass them
directly to `docker run -e ...` / `uvicorn`'s environment otherwise.

| Variable | Default | Meaning |
|---|---|---|
| `MEDIA_ROOT` | unset (no restriction) | Safety fence: browsing/execution is refused outside this path. Set it to your NAS mount. |
| `BATCH_SIZE` | `200` | Stepped loading: files per step in every list module (`0` = everything at once). Per-module overrides `BATCH_SIZE_TIMESTAMP`, `_GPS`, `_AI`, `_NFO`, `_MTIME`, `_SHORTVIDEOS` (empty = use `BATCH_SIZE`). See "Stepped loading". |
| `TIMESTAMP_PRESETS_FILE` | `./config/presets.json` | Where reusable presets are stored. |
| `ROOT_FOLDERS` | `VIDEOS=/mnt/NAS/video/Home-Video/,PHOTOS=/mnt/NAS/photo/` | Named shortcuts shown in the "Basis-Ordner" dropdown next to Pfad (every module) — pick a name/type instead of typing or remembering the NAS path. Format: `NAME=path,NAME2=path2` (comma-separated). Picking one fills the Pfad field with that folder and derives the file-type filter from it (see below) — Pfad can still be edited afterward, but only to go *deeper* into that folder, never above it. An optional `:TYPE` suffix on the path (`ALL`\|`IMAGE`\|`VIDEO`) sets the type explicitly, e.g. `RAW=/mnt/NAS/raw/:ALL`; without it, the type is guessed from the name (contains `VIDEO` → `VIDEO`, `PHOTO`/`IMAGE` → `IMAGE`, else `ALL`). **Don't wrap the value in quotes** — docker-compose's `environment:` list (and a plain `.env` file) takes everything after `=` literally, quotes included, unlike a shell. |
| `TRACCAR_URL` / `TRACCAR_TOKEN` | unset | GPS Sidecar: Traccar server URL + Bearer token, for devices with no GPS of their own. Leave unset to hide/disable Traccar as a source. |
| `TRACCAR_DEVICES` | unset | GPS Sidecar: comma-separated `deviceId[:Label]` pairs, e.g. `7:Phone A - Traccar,19:Phone A - gpslogger`, shown as individually selectable checkboxes, **checked by default**. |
| `TRACCAR_DEVICES_ADDITIONAL` | unset | GPS Sidecar: same format as `TRACCAR_DEVICES` — extra devices you only need occasionally (e.g. tagging from someone else's phone). Listed alongside `TRACCAR_DEVICES` in the UI, individually selectable, but **unchecked by default**. |
| `DAWARICH_SERVER` / `DAWARICH_API_KEY` | unset | GPS Sidecar: Dawarich server URL + API key, an alternative/additional GPS track source to Traccar. |
| `DAWARICH_IMPORT_API_KEY` | unset | GPS Sidecar: optional second "archive" Dawarich account (same server, different key) queried as an extra point source when its checkbox is ticked. |
| `PHOTON_SERVER` / `NOMINATIM_SERVER` / `PELIAS_SERVER` | unset | GPS Sidecar: reverse-geocoding chain, tried in this order until one answers. Leave any unset to skip it. |
| `MARITIME_SERVER` | unset | GPS Sidecar: last-resort reverse-geocoding backup for coordinates out at sea (named bodies of water), tried only once Photon/Nominatim/Pelias all came back empty. |
| `GEO_LANG` | `de` | GPS Sidecar: language requested from the reverse-geocoding backends. |
| `GEO_MAX_GAP_SECS` / `GEO_MAX_GAP_SECS_TIER2` | `600` / `21600` | GPS Sidecar: tiered search radius (seconds) for matching a file's capture time to the nearest Traccar/Dawarich track point — tight window tried first, wider one only if nothing was found within it. |
| `AI_PROVIDER` | unset | AI Description: priority list of providers to try, in order, e.g. `ollama` or `ollama,google`. Leave unset/empty to disable the module (browsing existing descriptions still works; "Beschreibung generieren" fails for every file). |
| `OLLAMA_URL` / `OLLAMA_MODEL` | unset | AI Description: self-hosted Ollama server URL + a vision-capable model name (e.g. `qwen2.5vl:3b`). |
| `GOOGLE_AI_API_KEY` / `GOOGLE_AI_MODEL` | unset | AI Description: Google Gemini API key + model name, as an alternative or fallback provider to Ollama. |
| `RESTORE_TIMESTAMP` | `false` | Timestamp & Rename, GPS Sidecar, AI Description: `true`/`1`/`yes`/`on` runs each module's own "🕒 Datei-Zeitstempel wiederherstellen" step automatically right after "Ausführen" instead of needing the button click. See "Restoring file timestamps after a write" above. |
| `MTIME_TIMEZONE` | `Europe/Vienna` | Datei-Zeitstempel (module #4 and the other three modules' mtime-restore step): IANA zone filenames/mtimes are interpreted in, independent of the container's own system timezone (which defaults to UTC — see "Restoring file timestamps after a write" above). |
| `COLLAGE_LAYOUT` | `random` | Collage: default layout (`polaroid`\|`postcard`\|`cascade`\|`magazine`\|`mosaic`\|`random`), overrideable per run on the module page. |
| `COLLAGE_WIDTH` / `COLLAGE_HEIGHT` | `4096` / `2304` | Collage: default canvas size in px. |
| `COLLAGE_COUNT` | `139` | Collage: default max number of randomly selected images per trip (unset/empty = all of them). |
| `COLLAGE_BORDER` | `6` | Collage: default border width in px around each tile. |
| `COLLAGE_ROTATION` | `25` | Collage: default max rotation angle in degrees for the scattered layouts (unset = each layout's own default: 25 polaroid, 8 postcard, 12 cascade, 20 magazine; always ignored for mosaic). |
| `COLLAGE_CELL` / `COLLAGE_TILE_SIZE` | unset (automatic) | Collage: fixed tile size (scattered layouts) / row height (mosaic) in px, overriding the automatic size-from-image-count calculation. |
| `COLLAGE_JITTER` | `0.55` | Collage: random placement jitter as a fraction of the tile size (scattered layouts). |
| `COLLAGE_SIZE_VARIATION` | `0.35` | Collage: random tile/row size variation (0.0–1.0) around the base size, for a mix of bigger/smaller photos. |
| `COLLAGE_SPACING` | `0` | Collage: gap in px between tiles, mosaic layout only. |
| `COLLAGE_SEED` | unset (random each run) | Collage: random seed for reproducible layouts. With layout `random`, a per-trip seed is derived from this so each trip still gets its own (but reproducible) layout choice. |
| `COLLAGE_SYNC_FOLDER_JPG` | `false` | Collage: when a trip's collage is skipped because it already exists, still copy that existing file in as the trip folder's own `folder.jpg` (and mirror it) without regenerating anything. |
| `PHOTO_ROOT` / `VIDEO_ROOT` | `/mnt/NAS/photo` / `/mnt/NAS/video/Home-Video` | Collage: base photo-library path a generated collage is mirrored from, and the corresponding Home-Video path it's mirrored to. Set `VIDEO_ROOT=` (empty) to disable mirroring entirely. |
| `NFO_DIRECTOR` / `NFO_STUDIO` / `NFO_SET` / `NFO_GENRE` / `NFO_TAG` | (empty) / (empty) / `Home Movie` / `Home-Video` / `Home-Video` | NFO Maker: the matching fixed fields in every generated `.nfo`. |
| `NFO_MPAA` / `NFO_CERTIFICATION` | `Rated R` / `Germany:6` | NFO Maker: the matching fixed fields in every generated `.nfo`. |
| `NFO_USERRATING` / `NFO_CRITICRATING` | `10` / `100` | NFO Maker: the matching fixed fields in every generated `.nfo`. |
| `NFO_GEO_COLOR` | `cyan` | NFO Maker: Kodi `[COLOR]` name used for the plot's "Ort: ..." location line. |
| `NFO_CAMERA_THUMB` | `/storage/videos/HomeVideo/.actors/camera.png` | NFO Maker: Kodi-side path to the dynamic "CAMERA" actor's thumbnail (role = detected camera/software per video). |
| `NFO_ACTORS` | (empty) | NFO Maker: the static `<actor>` list for every `.nfo` (besides "CAMERA"). Format: actors separated by `;`, each actor's `Name\|Role\|Thumb-path` separated by `\|`. See `.env.example` for the literal default spelled out. |
| `NFO_AI_DESCRIPTION` | `true` | NFO Maker: default for "AI-Beschreibung in den Plot übernehmen" (still overrideable per run) — whether an AI description already in a video's `.xmp` sidecar gets embedded. Never generates one itself either way. |
| `NFO_SKIP_LOCATION` | `false` | NFO Maker: default for `-L`/`--nolocation` (still overrideable per run) — skip reading `.xmp` for GPS/location entirely. |
| `NFO_OLDER` | unset (no protection) | NFO Maker: default for `-O`/`--older` (still overrideable per run) — protects an `.nfo` modified within the last *N* days (0 or negative) from regeneration, even with "überschreiben" ticked. |
| `NFO_SHORT_VIDEO_SECS` | `10` | NFO Maker: threshold (seconds) for the "sehr kurze Videos" filter and the short-clip delete button — a video counts as short when its duration is strictly below this. Always re-checked server-side right before a delete. |

Same variable names as the CLI toolchain's own `~/.GEOCODING/env_nfo_ai`, so values from
an existing setup can be copied in unchanged. The AI Description module also needs
`ffmpeg` on `PATH` (installed automatically in the Docker image; install it yourself —
e.g. `apt install ffmpeg` — when running locally without Docker) for video-frame
extraction and image resizing before a provider call.

There's no separate Typ-Filter control anymore — the file type for a browse is always
derived from the selected Basis-Ordner (and re-derived server-side from `ROOT_FOLDERS`,
not trusted from the browser), since it's always implied by which named root you picked.

The Pfad field offers sub-folder suggestions as you type (via a browser `<datalist>`,
backed by `GET /timestamp/subfolders`), always scoped to the selected Basis-Ordner — typing
or pasting something outside it falls back to listing the root's own children, and the
`/browse` endpoint itself rejects (400) any path that isn't the root or a descendant of it.
The suggestions also refresh on focus (clicking into the field), not just on typing — so
after going up a level (the "⬆️" button, or deleting the deeper part of the path by hand),
simply clicking back into the field shows that folder's own children right away, instead of
still showing the stale, deeper suggestion list from before.

**Last-used path per Basis-Ordner is remembered in the browser.** Whatever Pfad you last
edited for a given Basis-Ordner (e.g. `PHOTOS` → `/mnt/NAS/photo/2026/2026.10`) is saved in
the browser's local storage, keyed by that Basis-Ordner's name. Switching to a different
Basis-Ordner and back restores it, and it's also restored on a fresh page load — no server
round-trip, so it's per-browser, not shared between devices. Clearing the Pfad field
completely (select-all + cut/delete) restores that Basis-Ordner's configured standard path
and forgets the saved override, so you always have a quick way back to the default.

**Tiefe (depth) is shared across all four modules for the current browser session.**
Changing Tiefe in one module carries the same value over when you switch to another
(mirroring the cross-module "last Basis-Ordner" memory above), via the browser's
`sessionStorage` — unlike the per-Basis-Ordner Pfad memory, this intentionally does
*not* survive a browser/tab restart: a fresh session always starts back at the
default, Tiefe 0.

**Files found below the scanned folder (Tiefe > 0) show which subfolder they came
from.** When a scan with Tiefe > 0 actually finds something below the typed Pfad, each
such row displays its path *relative to that Pfad* (e.g. `📁 2026.10/urlaub`, never the
full absolute path) right under the row's other details, so it's clear which subfolder
a file came from. Files sitting directly in the scanned folder (the normal Tiefe = 0
case) show nothing extra.

## Not yet done / known gaps

- Video thumbnails: if a `<basename>-thumb.jpg` file already sits next to the video (e.g.
  `2026.08.09-17.17.49-thumb.jpg` next to `...17.49.mp4` — as produced by the existing NAS
  toolchain or by the NFO writer), it's used as the preview image; otherwise it still falls
  back to the static "▶" placeholder icon *until* the AI Description module generates a
  description for that video (see its own section below — that's the one place this app
  extracts and permanently saves a real frame itself). Timestamp & Rename and GPS Sidecar
  never extract one on their own.
- No authentication — this assumes it sits behind your existing reverse proxy / private
  network, same as the rest of the homelab stack.
- The `execute` endpoint trusts the `all_resolutions` hidden field exactly as previewed
  (no re-resolution at execute time), so if a file changes on disk between preview and
  execute, the preview you saw is what gets applied. Reload the preview if you're unsure.
- Presets currently cover the same knobs as the CLI script's flags (mode, timezone, GPS
  filter, type filter, depth, rename/exifwrite toggles) — no custom filename *pattern*
  templating yet (it always uses `YYYY.MM.DD-HH.MM.SS`), since that wasn't configurable
  in the original script either. The GPS Sidecar module has no presets yet either.
- GPS Sidecar: NFO generation, DJI `.srt` extraction, predefined/interactive location
  selection, and exiftool's own `-geotag` GPX-matching engine (tiered radius) remain
  CLI-only for now — see "Deliberately not ported in this first pass" above. (AI
  descriptions are now ported — see the "AI Description module" section above.)
- AI Description: no manual edit/retry of a single generated description before
  "Ausführen" (regenerate the whole selection again, or exclude a file and run it
  separately) — same one-shot-per-file flow as the CLI script.
- Same-folder sibling timezone-borrowing (both in Timestamp & Rename and, for free, in
  GPS Sidecar) only ever looks at files actually included in the current batch — an
  already-correctly-named file skipped by "Umbenannte einblenden" isn't read and can't
  act as a donor. Also, unlike the CLI's recursive folder search, this is intentionally
  scoped to one folder only (not subfolders).
- GPS Sidecar fetches one Traccar/Dawarich track per folder-scan (covering every file
  that needs a lookup, padded ±12h), not per file — fine for a normal trip/day folder,
  but a folder spanning many separate trips will fetch one wide track covering the gaps
  between them too.
- Datei-Zeitstempel's `execute` trusts each previewed row's already-resolved target
  exactly as scanned (no re-resolution at execute time) — same convention as Timestamp &
  Rename's own execute, and for the same reason: if a file changes on disk between
  preview and execute, the preview you saw is what gets applied. Reload the preview if
  you're unsure. It also has no presets yet, same as GPS Sidecar and AI Description.

## GPS timestamp vs. wall clock (phone clock set to the wrong zone)

Some phones (e.g. Xiaomi after a manually set system clock) write a GPS "UTC" stamp that is
really derived from the wrongly-zoned clock: wall clock 19:55 in Abu Dhabi, GPSTimeStamp 17:55Z
(= treated as Vienna). Trusting that stamp puts the file 2 h too late.

Rule in `timecore._resolve_core`: if a GPS UTC time exists and the filename (or metadata) wall-clock
time differs from the GPS-derived local time by **1–14 h and a multiple of 15 min**, the GPS time is
distrusted. The wall clock is kept, GPS only supplies the zone/offset (`GPS-Zone+Dateiname(Zone)`),
and a warning is shown. EXIF write-back then rewrites GPSTimeStamp correctly.

Set `GPS_UTC_TRUST=always` in `.env` to restore the old behaviour (GPS UTC always wins), e.g. for a
camera with a wrong clock but a correct GPS time.

Exception (corroborated GPS time): if the file carries its own `OffsetTime*` and
`DateTimeOriginal - offset` equals the GPS UTC (within 2 min), the GPS time is trusted even if the
wall clock differs. Typical case: phone in flight mode, clock still on the home zone
(16:07:44 +02:00 == 14:07:44Z) but the photo was taken 18:07:44 local in Abu Dhabi (+04:00).

## Filter "Nur Dateien, die umbenannt werden müssen" (Timestamp & Rename)

Checkbox `only_needs_rename`: after the full resolution (filename, GPS, EXIF offset, same-timestamp
numbering) only files whose computed target name differs from the current name are listed. It
automatically includes already pattern-named files (e.g. `16.11.24` that GPS Asia/Dubai says is
`18.11.24`) and hides all correctly named ones; the hint shows how many were hidden.

### Selection buttons instead of a list filter (replaces the checkbox above)

The three list-building checkboxes ("Nur Dateien, die umbenannt werden müssen", "Nur Dateien mit
falschem/fehlendem Datum im XMP", "Nur Bilder ohne Lokalzeit in der Datei") are gone from the form.
Instead the list toolbar next to "Alle auswählen / Keine" has selection buttons with counts:
**Nur umbenennen (N)**, **Nur .xmp-Datum falsch/fehlt (N)**, **Nur Bilder ohne Lokalzeit in der Datei (N)**.
"Nur …" replaces the selection, the **+** next to it adds to it (combine e.g. rename AND xmp).
The per-file diagnostics (one exiftool call each, 8 in parallel, with progress) now always run so
the flags exist for every row. Files already named correctly are only listed when
"Umbenannte einblenden" is used -- needed to select ".xmp only" cases among them.
(Backend params only_needs_* still exist for scripts/tests.)

The "GPS timestamp contradicts the capture time" warning is only shown when filename and metadata
clock do NOT agree with each other. If both agree and only the GPS stamp deviates (e.g. a DJI drone
without GPS fix, GPSStatus=V, writing its own clock as "UTC"), the result is the same but silent.

## Tests

`python tests/run_all.py` (optionally with name filters, e.g. `python tests/run_all.py nfo gps`) runs
every `tests/test_*.py` in its own process against a temporary media tree (needs `ffmpeg` and
`exiftool`). Audited 2026-10-06: 34 tests, all green. Changes made in that audit: stale expectations
fixed (NFO Maker two-stage scan hides videos that already have an `.nfo`; AI -> NFO handoff link now
carries a `handoff_id`; `MEDIA_ROOT` fence is read once at import), vacuous asserts (`... or True`,
`x != x`) replaced by real checks, misleading comments/messages updated to "videos are never
rewritten", dead fake-exiftool scaffolding removed, duplicate `test_exec_disable2` deleted.


## exiftool version in the Docker image

The image does **not** use Debian's outdated `libimage-exiftool-perl`; the `Dockerfile` builds exiftool from
the official source tag (`https://github.com/exiftool/exiftool/archive/refs/tags/<version>.tar.gz`) with
`EXIFTOOL_VERSION` (default **13.59**, also settable in `.env` / compose `build.args`). The build runs
`test "$(exiftool -ver)" = "$EXIFTOOL_VERSION"` and aborts on a mismatch, so the image always contains
exactly the pinned version. Optional `EXIFTOOL_SHA256` build arg additionally pins the tarball content.

- Check: `docker compose exec metadata-suite exiftool -ver`
- Upgrade: `EXIFTOOL_VERSION=13.60 docker compose build` (changing the arg invalidates the cached layer;
  use `--no-cache` only if you also want fresh apt packages), then `docker compose up -d`.

## Info page (ⓘ, far right of the nav)

`/info/` — read-only overview, reachable from the ⓘ icon that is the last pill of every module's nav:

- **Build:** version, build date (+ age), git commit/branch, start time/uptime, host, Python, container/mtime time zone.
  Taken from build args baked into the image (`APP_VERSION`, `BUILD_DATE`, `GIT_COMMIT`, `GIT_BRANCH`; compose passes them through,
  e.g. `GIT_COMMIT=$(git rev-parse --short HEAD) docker compose build`). Without `BUILD_DATE` the Dockerfile stamps
  `/srv/.build_date` after `COPY app` (= when the app code last changed the image).
  Without `APP_VERSION` the version label is filled automatically from the build day (+ short commit), e.g. `2026.10.13+ab12cd3`.
- **Versions:** exiftool (compared with the pinned `EXIFTOOL_VERSION` -> warning on mismatch), ffmpeg, ffprobe, mediainfo, perl, Python,
  pip packages (compared with `requirements.txt` pins), htmx / Alpine.js (CDN versions read from the template).
- **Paths:** presets, camera names, Android device list, fixed places, `MEDIA_ROOT` / `PHOTO_ROOT` / `VIDEO_ROOT`, and the root-folder dropdown --
  each with exists / readable / writable / size / mtime.
- **External services:** configured Traccar, Dawarich, Photon, Nominatim, Pelias, Maritime and Ollama URLs; "Erreichbarkeit prüfen" tests them
  in parallel (3 s timeout; any HTTP answer counts as reachable).
- **Environment:** every variable the app reads, grouped, with value or default; tokens/API keys are masked (never sent to the browser);
  unknown variables with an app prefix are flagged (typo detection).
- Filter box (variable / path / version), "nur gesetzte Werte", "Report kopieren" (`/info/report.txt`, secrets masked) and `/info/data.json`.

## Stepped loading ("Nächste N laden")

Large folders (1000+ files) no longer have to be resolved in one go. All six list modules (Timestamp & Rename, GPS Sidecar,
AI-Beschreibung, NFO Maker, Datei-Zeitstempel, Kurzvideos) work in **steps** of `BATCH_SIZE` files (default 200, `0` = off;
per-module override `BATCH_SIZE_TIMESTAMP|GPS|AI|NFO|MTIME|SHORTVIDEOS`).

- The cheap part of the scan (walking the folder, name checks) still covers everything and fixes the **sorted candidate list**;
  that list is snapshotted server-side (`core/stepper.py`, in memory, 6 h TTL, max 40 scans) under a `scan_id`.
- Only the first N files go through the expensive part (exiftool resolution, diagnostics, mediainfo, ...). On that step the normal flow
  applies unchanged: filters, selection buttons, execute (also geocoding / AI generation in GPS and AI).
- The bar above the list shows "Schritt n · Dateien a–b von T" and **"Nächste N laden (M übrig)"**. It works with or without having
  executed anything. The next step continues from the snapshot (no re-walk): files renamed or deleted in the meantime are skipped,
  nothing appears twice or gets lost. Unknown/expired `scan_id` (e.g. after a restart) -> a fresh scan from step 1.
- The bar is only shown when there is more than one step. Re-submitting the form ("Vorschau laden") always starts at step 1.
- Caveats: counters ("bereits abgeschlossen", "ausgeblendet" ...) and filters such as the camera filter apply **per step**, so a step can
  be empty while more remain; "Fallback-Zone"/sibling-zone borrowing in Timestamp only sees the files of the current step; the
  Track fetch in GPS Sidecar covers only the current step's time range.
- Tests: `tests/test_step_timestamp.py`, `tests/test_step_modules.py`.

## Load filters before "Vorschau laden" (Timestamp & Rename)

The form now has a group **„Nur laden, was …“** with three checkboxes: *Nur umbenennen*, *Nur .xmp-Datum falsch/fehlt*,
*Nur Bilder ohne Lokalzeit*. They decide **which files are loaded into the list at all**. Several ticks = files meeting **any** of the
conditions (OR); nothing ticked = everything. They work together with stepped loading (each step is filtered), and already-named
files are checked automatically. The list's selection buttons ("Nur umbenennen (n)" … with "+") stay for narrowing down afterwards.

The former action checkboxes in the form (*Umbenennen*, *.xmp-Sidecar-Datum setzen*, *EXIF zurückschreiben (nur Bilder)*) were removed there;
they exist only below the list (same texts and tooltips), defaults: rename on, .xmp date on, EXIF write off. The choice is kept when
"Nächste N laden" is used. Presets no longer store/restore these two options (old preset files still load). Test: `tests/test_load_filters.py`.

## exiftool pool (speed)

Starting exiftool (Perl) costs ~100 ms per call, reading a file only ~4 ms -- and a scan used to start it several times per file.
`core/exifpool.py` now keeps up to `EXIFTOOL_POOL_SIZE` (default 6, `0` = off) long-running exiftool processes (`-stay_open`) and every
module sends its commands there (drop-in `exifpool.run(cmd, timeout=, cancel_event=)`; the old `timeout N exiftool ...` shape is understood).

- **Same results:** it is still exiftool with the same arguments; the pool only removes the start-up cost.
- **Robust:** timeout / Cancel kill just the one worker that handled the file (replaced automatically); a worker that died while idle is
  retried once on a fresh one; arguments with a newline (e.g. multi-line AI descriptions) and `EXIFTOOL_POOL_SIZE=0` use a plain
  one-shot subprocess; idle workers are closed after `EXIFTOOL_POOL_IDLE_SECS` (300).
- **Measured** (synthetic files, this sandbox, no NAS latency): per call 107 ms -> 3 ms; Timestamp scan of 120 images 19.5 s -> 1.1 s,
  GPS scan 12.6 s -> 0.4 s, Kurzvideos (60 videos) 15.0 s -> 1.4 s, NFO scan (60 videos) 8.0 s -> 1.0 s. Real NAS folders are additionally
  bound by the network read latency, which the pool can't change.
- **Writes** (EXIF write-back, .xmp sidecars) use the same pool -- exiftool stays the only writer for images and sidecars.

### mediainfo / ffmpeg (checked)
- **mediainfo** (~35 ms/call) stays for the NFO fields (Android manufacturer, Encoded_Application, CodecConfigurationBox, ... exist only there).
  Changes: mediainfo and exiftool of one video are read in parallel; *Kurzvideos* first asks the pooled exiftool for the duration (~5 ms)
  and only calls mediainfo for videos that are not clearly longer than the threshold (threshold x1.1 + 1 s), so results are identical;
  the seek-time lookup for range/year-named videos uses the same quick duration.
- **ffmpeg** stays (thumbnails, AI frames). Added `-nostdin` (+ quiet log level, stdin=DEVNULL for the NFO thumbnails). `-noaccurate_seek` was
  tested and was *slower* (2.7 s vs 1.3 s on a long-GOP file), so it is not used. Pillow instead of ffmpeg for the AI resize would save ~50 ms
  per image against AI calls of seconds -- not worth the format risk.
- Tests: `tests/test_exifpool.py` (protocol, unicode, errors, timeout/cancel, crash recovery, concurrency, fallback, speed ratio).
- Shutdown: workers are closed politely at normal exit (uvicorn stop / container stop). Only if the Python process is killed with SIGKILL *outside* a
  container could `-stay_open` exiftool processes stay behind (exiftool doesn't exit on stdin EOF); inside Docker they die with the container.

## ffmpeg / ffprobe / mediainfo in the Docker image

Debian's packages are old (ffmpeg 7.1, mediainfo 24.x), so — like exiftool — they are **built from the official source tarballs** in a first
build stage and only the binaries are copied into the final image:

| Tool | Pinned version (build arg) | Source |
|---|---|---|
| ffmpeg + ffprobe | `FFMPEG_VERSION` = **9.0.2** (latest stable at the time of writing, released 2026-09-18) | `ffmpeg.org/releases/ffmpeg-<v>.tar.xz` |
| mediainfo (CLI) | `MEDIAINFO_VERSION` = **26.10** | `mediaarea.net/download/binary/mediainfo/<v>/MediaInfo_CLI_<v>_GNU_FromSource.tar.xz` |

- The build **fails** if the built `ffmpeg -version` / `mediainfo --Version` doesn't report the pinned version; optional `FFMPEG_SHA256` /
  `MEDIAINFO_SHA256` pin the tarball content. The Info page shows installed vs. pinned version (warning on mismatch).
- ffmpeg is built static (`--disable-shared`), without ffplay/docs, with libdav1d for AV1; everything else needed (h264/hevc/prores decode, scaling, MJPEG
  encode) is native.
- **Compiled once, kept as an image:** ffmpeg + mediainfo are built from `Dockerfile.media` into the small image
  `metadata-suite-media:ffmpeg<v>-mediainfo<v>` (service `media-tools`, profile `tools`). The app `Dockerfile` only copies the binaries from it, so a build-cache
  prune or an image cleanup can no longer trigger the ~6-minute compile. `build_start.sh` (via `ensure_media_image.sh`) makes sure it exists: already there -> nothing;
  missing but a **tar copy** exists in `MEDIA_IMAGE_DIR` (`.env`, default `./media-images`, best on the NVMe) -> `docker load` (seconds, e.g. after a
  `docker image prune -a`); neither -> compile once and save the tar copy. By hand: `./ensure_media_image.sh`. A plain `docker compose build` on a machine without that
  image stops with "pull access denied for metadata-suite-media" -> run `build_start.sh` (or the command above) once.
- Upgrade: set `FFMPEG_VERSION` / `MEDIAINFO_VERSION` in `.env` and run `./build_start.sh` (new tag -> compiled once, the old image stays until you
  `docker image rm` it).
- Code compatibility: the app uses only long-stable ffmpeg options; the deprecated `-vframes` alias was replaced by `-frames:v`.
- **Not verified here:** the sandbox has no access to ffmpeg.org / mediaarea.net, so this Docker build was not run and the app was tested against the local
  ffmpeg 6.1 / mediainfo 24.01. If the build fails, the error names the step (download, compile or version assertion).

## Config folder (fix: camera names path)

All user config files live in **one folder — the one holding `presets.json`** (`/config` in Docker, `./config` locally):
`presets.json`, `camera_names.json` (own camera names). Previously `camera_names.json`
defaulted to `<app>/config` (= `/srv/config` in the container) unless its own variable was set, so the Info page expected it in the wrong place.
Now the Dockerfile and `docker-compose.yml` set `CAMERA_NAMES_FILE=/config/camera_names.json`,
and without any variable the default follows the folder of `TIMESTAMP_PRESETS_FILE`. Example file: `config/camera_names.example.json`.
Test: `tests/test_config_paths.py`. (The former `predefined_locations` file was removed – fixed places are *Meine Orte* of the geo-service.)

## GPS track requests: one per trip/month, not per day

`geocore.plan_track_windows()` groups the capture times that need an external position into **as few Traccar/Dawarich requests as possible**:
days at most `GPS_TRACK_BRIDGE_DAYS` (default 3) apart share one window (a trip or month folder = one request per source and device set); a bigger
gap starts a new window (no weeks of unneeded points between two trips); a window longer than `GPS_TRACK_MAX_SPAN_DAYS` (default 31) is split
(keeps each response within the 60 s timeout); every window is padded by 12 h so photos near midnight/the window edge still find neighbours.
Overlapping/duplicate points are merged. The list header shows "(n Abrufe je Quelle)".

- No per-day cut at 00:00 (the bash batch mode's day windows `00:00:01–23:59:59` can't use the point at 00:05 for a photo at 23:58).
- `nearest_point()` bisects the sorted track (was a full scan per file): 2000 lookups on a 200 000-point month track < 1 s.
- Stepped loading: each step fetches only the window(s) of its own files.
- The Bash script `Set-GPS-Data-via-traccar-and-dawarich.sh` still fetches per day in its batch/recursive mode -- same idea applies there (not changed).
- Test: `tests/test_track_windows.py`.

## Geo-Service (optional)

A second service in the **same `docker-compose.yml`** (code in `geo-service/`, built from the same repo) answers reverse geocoding for all your tools from one cache and can supply track positions and caption context. `docker compose up -d --build` starts suite **and** geo-service together.

* Wiring is automatic: the suite gets `GEO_SERVICE_URL=http://geo-service:8000` by default (compose-internal DNS); `GEO_SERVICE_URL=` (empty) in `.env` switches it off. Both services read the same `.env` (PHOTON_/NOMINATIM_/PELIAS_/TRACCAR_/DAWARICH_/GEO_*), so nothing has to be configured twice.
* `depends_on` is `required: false`: the suite starts and works even if the geo-service is down (own chain as fallback).
* Both containers run as `PUID:PGID` from `.env` (default 1026:100; the suite writes sidecars/renames on the NAS and presets into `./config` as this user, so it must own/may write those; `./config` and `./geo-data` must be writable for it). Cache volume: `./geo-data:/data`. Host port 8430 is optional.
* The old `geo-service/docker-compose.yml` is only for running the service standalone elsewhere.

* `GEO_SERVICE_URL` (+ `GEO_SERVICE_TOKEN`, `GEO_SERVICE_TIMEOUT`) set → `geocore.reverse_geocode()` asks the service first. The local Photon/Nominatim/Pelias chain is only used **while the service is down** (2 failures → skipped for 60 s). "Nothing found" from the service is final for Photon/Nominatim/Pelias – but `MARITIME_SERVER` is **always** the last resort (open sea, coastal waters): inside the service's chain, in the suite's local chain when the service is down, and directly from the suite when the service answers "nothing" (e.g. a standalone service without MARITIME_SERVER).
* `AI_CONTEXT` (compose default **true** since the service runs alongside; set `false` to switch off; needs the service) → AI-Beschreibung adds time of day, season, weather (service-side opt-in) and trip name (folder `<start>-<end> <name>`) to the prompt, with the instruction to use at most `AI_CONTEXT_MAX` items (default 2, `all` possible) and only if they fit the picture (weather/time of day only outdoors, nothing invented, scene description stays the main point).
* **Length** (all in `.env`, read on every call): `AI_SENTENCES=2-3` / `AI_WORDS_MAX=50` without context (as the original CLI); with context `AI_SENTENCES_CONTEXT=3-4` / `AI_WORDS_MAX_CONTEXT=75`. The model's token cap scales with the word limit (`words × 3.2`, at least 220). Recommended with *all* context (`AI_CONTEXT_MAX=all`): 3–4 sentences, 70–80 words; going beyond ~90 words makes captions read like weather reports. The list view shows the passed context ("🕒 Zusatzkontext (Geo-Service)").
* The info page lists the new variables (token masked) and checks the service's `/health`.
* Test: `tests/test_geoservice.py`.
* The new module **GPS füllen** (below) fetches its tracks only through the service. The GPS Sidecar module also asks the service first for its tracks (same windows, same Traccar device ids / Dawarich switches); only when the service is not configured or not reachable does it fall back to fetching from Traccar/Dawarich directly (a note is shown in the result).

### Keeping suite and geo-service in sync

The geo-service lives in `geo-service/` of this repo and has its own image/Dockerfile, but runs as a service of the same compose file (the suite image only copies `app/`).
Parsing of Photon/Nominatim/Pelias answers exists twice on purpose (the suite must run without the service); `tests/test_geoservice_parity.py`
feeds identical backend answers to both and fails if the place fields differ. Change `geocore.py` parsing → run the suite tests → the parity test tells you to mirror it in `geo-service/app/backends.py`.


## GPS füllen (module 8)

`/gpsfill/` gives photos/videos that have **no GPS** a position from your track – the suite port of `geo-service/clients/gps_fill.py` (engine `core/fillcore.py`, maths `core/trackmath.py`).

* **Interpolation** between the two surrounding track points (not just the nearest one), with a **confidence** (0–1) and an error radius per file. Rule of thumb: ≥ 0.7 safe, 0.4–0.7 plausible, below = guess. Files under "Mindest-Konfidenz" are only listed.
* **Neighbour file as last resort** (checkbox "Nachbardatei im Ordner", on by default, `max. Abstand (h)` default 6, 0 = anywhere that day): a file the track cannot place gets the position of the nearest file *in the same folder on the same local day* that has real GPS of its own (EXIF or sidecar). Confidence is capped at 0.5 and falls with the time distance, the source is written as `GpsSource=neighbor-file` (`GpsTrackSources=neighbor:<file>`) and the list names the reference file. Files from other days or folders are never used; files filled in the same scan do not serve as references. Test `tests/test_gps_fill_neighbor.py`.
* **Clock drift** (default `auto`): per camera and day, estimated from files that already have GPS – exact from the EXIF satellite time (`GPSDateTime`), otherwise by shifting the track until it passes through the photo's own position. Used only when the samples agree. `off` = clocks are right, a number = fixed drift in seconds (camera minus true time).
* **Which files**: same rules as the other modules – the Basis-Ordner decides the type (VIDEOS → only videos, PHOTOS → only photos; derived server-side, there is no Typ control), and generated `<video>-thumb/-poster/-fanart.jpg` next to their video are never candidates (nor are `.nfo`/`.txt`/`.gpx`, which are not media at all). Orphaned thumbs without a video stay normal photos.
* **Capture time** is resolved to UTC in this order: sidecar (with offset) > EXIF + offset > file name > EXIF without offset. A missing offset is taken from the folder's most common one, last resort the zone field (default `MTIME_TIMEZONE`); such files are flagged "tz?" in the list (wrong for photos taken abroad).
* **Tracks only via the geo-service** (`GEO_SERVICE_URL`, set by default in the compose file): one place for Traccar/Dawarich access and caching. Without it the module shows a notice instead of working. Fetches are merged into few windows per trip.
* **Writes only the `.xmp` sidecar** (never the media file): GPS, place names (via `geocore` → geo-service), geo keywords, and provenance in an own XMP namespace `XMP-mon:*` (source, confidence, gap, uncertainty, time basis, drift, tool; namespace file `app/data/exiftool_mon.config`, query with `exiftool -config exiftool_mon.config -XMP-mon:all file.xmp`). GPS already in a sidecar is kept unless "ersetzen" is ticked; files marked "kein GPS gefunden" are skipped unless "erneut versuchen".
* **Stepped loading** like the other list modules (`BATCH_SIZE_GPSFILL`): the reference files and the track of the *whole* scan are read once in step 1, so later steps use the same drift model without new track requests.
* The preview plan is computed on the server and kept under a token; "GPS in Sidecars schreiben" writes only what that plan contains for the ticked files.
* Tests: `tests/test_gps_fill.py` (drift, interpolation, sidecar contents, media untouched, stepping), `tests/test_fill_trackmath_identical.py` (suite and service share byte-identical maths).

## Bilder verlustfrei aufrecht drehen (Datei-Zeitstempel)

Optional im Modul **Datei-Zeitstempel** (Checkbox "Bilder finden, die nur per EXIF-Drehung aufrecht angezeigt werden", standardmäßig an). Betrifft **nur JPEGs** mit EXIF-Orientation ≠ 1; Nicht-JPEGs werden gezählt, aber nie angefasst.

- Pro Zeile gibt es die Checkbox "↻ verlustfrei aufrecht drehen"; gedreht wird beim **Ausführen**.
- Werkzeug: `exiftran -a -i -p` (verlustfrei, setzt Orientation auf 1, erhält den Datei-Zeitstempel). Im Docker-Image ist `exiftran` enthalten (Image neu bauen!).
- **Rand:** Ist die Bildgröße kein Vielfaches des JPEG-Blocks (8/16 px), schneidet exiftran bis zu **15 px** am Rand ab (`-trim`-Verhalten, bewusst so gewählt); die Ergebniszeile nennt den Abschnitt ("nichts abgeschnitten" bei passender Größe).
- **Backup:** Checkbox unter der Liste (an) legt vorher `datei.jpg~` an.
- Ein vorhandener `.xmp`-Sidecar bekommt `XMP-tiff:Orientation=1`, damit nicht doppelt gedreht wird.
- Zeilen, deren Datum schon stimmt, aber die gedreht werden sollen, erscheinen trotzdem in der Liste.

## Kontext-Infos im XMP (XMP-mon:Ctx*)

Was beim Erzeugen der KI-Beschreibung als Kontext geholt wird (braucht `AI_CONTEXT=true` und den Geo-Service; die Reise kommt aus dem Ordnernamen), wird jetzt **unabhängig vom KI-Text** beim Ausführen ins `.xmp`-Sidecar geschrieben – auch wenn die KI nichts geliefert hat:

`XMP-mon:CtxDaypart`, `CtxSeason`, `CtxMonth`, `CtxYear`, `CtxWeekday`, `CtxLocalTime`, `CtxWeather`, `CtxTempC`, `CtxTrip`, `CtxFilledBy` (Namespace über `app/data/exiftool_mon.config`, wie die GPS-Herkunftsfelder).

Abfragen: `exiftool -config app/data/exiftool_mon.config -XMP-mon:all datei.jpg.xmp`. Abschalten mit `XMP_CONTEXT=false`. Unbekannte Namespaces ignorieren Immich/Kodi einfach; in das Bild selbst werden diese Felder nicht eingebettet (nur im Sidecar).

## Wetter-Archiv (Schattendatenbank)

> htmx und Alpine.js liegen lokal in `app/static/` (kein CDN/unpkg mehr nötig – die Seiten funktionieren auch ohne Internet im Browser).

**Vorgabe Zeitraum:** *Von* = 01.01.1900, *Bis* = immer das aktuelle Tagesdatum (auch serverseitig, wenn ein Feld leer bleibt). Der geo-service kürzt auf 1940 (Beginn des Open-Meteo-Archivs), schaut sich Tracks in 90-Tage-Blöcken mit **einem** Abruf je Quelle an und überspringt leere Blöcke (Fortschritt „Zeitfenster“ zählt sie als erledigt, Status `blocks_skipped`). Mit Fotos/Videos plus Tracks gilt jetzt ebenfalls dieser Zeitraum, nicht mehr nur die Spanne der Medien.

Im Modul **GPS füllen** gibt es unten das Aufklappfeld **„🌦 Wetter-Archiv füllen“**. Es holt das Wetter für alle Orte und Tage, an denen du warst, **einmal** von Open-Meteo und legt es dauerhaft im geo-service ab (`geo-cache.sqlite`, Tabelle `weather`; Zellen zu 0,1° ≈ 11 km wie bisher, ein Tag = ein Abruf). Danach liefern `/context`, die KI-Beschreibung und die `XMP-mon:Ctx*`-Felder ohne Internet.

- **Quellen:** Fotos/Videos im Pfad oben (nur Dateien mit Zeit **und** GPS im Sidecar/EXIF; gleiche Typregel wie die anderen Module) und/oder die Tracks aus Traccar/Dawarich (über den geo-service). Von/Bis gelten für die Tracks; leer = Zeitraum der gefundenen Medien ±1 Tag.
- **„Nur prüfen“** zählt nur: wie viele Orte×Tage fehlen noch, wie viele sind schon archiviert.
- **Bereits Archiviertes wird übersprungen**; ein erneuter Lauf (oder ein Lauf nach Abbruch) macht dort weiter, wo er aufgehört hat. Alle angefragten Tage bis einschließlich heute werden archiviert; nur Tage in der Zukunft nicht.
- **Limits:** `WEATHER_BACKFILL_DELAY` (0,4 s Pause je Abruf) und `WEATHER_BACKFILL_MAX` (5000 Abrufe je Lauf, dann „Obergrenze erreicht – nochmal starten“); meldet Open-Meteo HTTP 429, stoppt der Lauf sauber (kostenlos: 600/Minute, 10 000/Tag).
- Braucht `GEO_SERVICE_URL` und `WEATHER_PROVIDER=open-meteo` am geo-service. **Datenschutz:** Open-Meteo erfährt dabei die besuchten Zellen (auf ~11 km gerundet) und Tage – dieselbe Information wie bei Einzelabfragen, nur auf einmal.

## GPS Sidecar: Aufnahmezeit mit Offset im Sidecar

Beim Anlegen des `.xmp` aus einem Video wird dessen QuickTime-`CreateDate` (UTC, ohne Offset, z. B. `2026-10-04T10:27:29`) unverändert übernommen. Programme wie Immich lesen so ein Datum als Ortszeit (hier 2 h daneben), und Wetter/KI-Kontext können es nicht sicher verwenden. Beim Schreiben im Modul GPS Sidecar bekommt das Sidecar deshalb jetzt die aufgelöste Ortszeit **mit Offset** (`2026:10:04 12:27:29+02:00`, derselbe Wert, den Timestamp & Rename schreibt) in `XMP-xmp:CreateDate`, `ModifyDate` und `XMP-exif:DateTimeOriginal`. Ein Datum, das schon einen Offset hat, wird nie angefasst.

## Große Videos, Video-Sidecar in Timestamp & Rename, .srt-Löschen

- **Große Videos (Sidecar anlegen):** Früher lief beim Anlegen des `.xmp` immer `exiftool -ee` (liest jedes Sample der eingebetteten Metadaten-Spuren) mit festen 60 s Timeout – bei Videos ab ca. 200 MB (v. a. über NAS) hat das nie gereicht, Ergebnis „Konnte XMP-Sidecar nicht erstellen/aktualisieren“. Jetzt: `-ee` nur bis `XMP_EE_MAX_MB` (Standard 100), ab 50 MB `-fast2`, Timeout wächst mit der Dateigröße (60 s + 0,25 s/MB, höchstens `XMP_TIMEOUT_MAX`, Standard 1800 s) und bei einem Fehlschlag folgt ein zweiter, leichter Versuch. Die Fehlermeldung nennt jetzt den Grund und die Dateigröße. Die Videodatei selbst wird dabei nie verändert.
- **Timestamp & Rename legt für Videos jetzt das `.xmp` an** (wenn „.xmp-Sidecar-Datum setzen“ an ist) und setzt Aufnahmezeit mit Offset. Bisher wurde ein fehlendes Sidecar nur in der Vorschau als „keine .xmp“ markiert, aber nie angelegt. Fotos bleiben unverändert (sie tragen Ortszeit + Offset selbst).
- **GPS Sidecar, „.srt nach erfolgreichem Schreiben löschen“:** die `.srt` wurde nur gelöscht, wenn die Position selbst aus der `.srt` kam. Jetzt wird sie gelöscht, sobald das Sidecar die Position nachweislich enthält (zurückgelesen) – egal ob aus `.srt`, Track, Datei oder festem Ort. Ohne Position oder bei fehlgeschlagenem Sidecar bleibt sie.

## Kamera im XMP-Sidecar (GPS Sidecar, Schritt 1)

Beim Schreiben im Modul GPS Sidecar bekommt das `.xmp` jetzt die Kamera:
- **Roh, Standardfelder:** `XMP-tiff:Make` / `XMP-tiff:Model` (wie bisher, wenn die Datei sie hat oder die Kamera per Byte-Signatur erkannt wird, z. B. Insta360 Luna Ultra). **Neu:** Kameras ohne Make/Model, die sich nur in einem Herstellerfeld nennen, werden daraus abgeleitet – DJI Neo 2 schreibt `XMP-photoshop:Category = pb_file:dvtm_NEO2.proto;model_name:FC9470;…` → `Make=DJI`, `Model=FC9470`; fehlt das Feld, dient der Container-`Encoder` (`DJI NEO2`) → `Make=DJI`, `Model=NEO2` (das Muster `DJI?Neo2` in `camera_names.json` deckt auch das ab).
- **Lesbarer Name:** `XMP-mon:CameraName` (+ `XMP-mon:CameraNameSource` = `override` | `nfo-rule` | `google` | `raw`), Namespace `http://ns.neuhofer.home/media/1.0/` (`app/data/exiftool_mon.config`). Reihenfolge wie überall: eigene Regel in `camera_names.json` › NFO-Maker-Regel › Google-Geräteliste › Rohwert. Bei jedem Lauf neu geschrieben (kein veralteter Name nach einer Regeländerung). Abfrage: `exiftool -config app/data/exiftool_mon.config -XMP-mon:CameraName datei.mp4.xmp`.
- **camera_names.json:** Regel `["FC9470", "DJI?FC9470", "DJI?Neo?2", "DJI?Neo2"]` → „DJI Neo 2“ (auch in `config/camera_names.example.json`).
- Nächster Schritt (noch nicht umgesetzt): NFO Maker liest den Namen aus `XMP-mon:CameraName`, statt ihn selbst zu bestimmen.

### Diagnose „Kamera: … (Rohwert)“
Greift keine Regel aus `camera_names.json`, zeigt GPS Sidecar den Rohwert samt Hinweis
(`devicenames.overrides_status()`): Pfad der aktiven Datei, ob sie existiert, ob das JSON gültig
ist (ein Komma zu viel deaktiviert *alle* Regeln still) und wie viele Regeln keine passen.
Bereits im `.xmp` vorhandene Make/Model-Werte (auch aus früheren Läufen) werden nicht überschrieben.

### AI-Beschreibung: Vorschau zeigt alle Kontexte
Schon die Scan-Vorschau listet neben dem Ortskontext alle Kontexte, die der Prompt bekommt und die
als `XMP-mon:Ctx*` ins Sidecar gehen (Tageszeit, Uhrzeit, Wochentag, Monat, Jahr, Jahreszeit, Wetter,
Temperatur, Reise). Sie kommen vom Geo-Service (`AI_CONTEXT=true`); ohne Dienst/GPS bleibt es beim Ort.

## NFO Maker: Kamera aus dem XMP, Temp/Wetter im Plot (Schritt 2)
- **Kamera:** Steht im `.xmp` `XMP-mon:CameraName` (von GPS Sidecar geschrieben), wird dieser Name für `Cam:` und den CAMERA-Actor genommen. War der Name dort nur der Rohwert (`CameraNameSource=raw`), wird er mit der *aktuellen* `camera_names.json` neu übersetzt – eine später ergänzte Regel greift also ohne neuen Sidecar-Lauf.
- **Ohne XMP-Kamera:** die bisherige Erkennung, aber jetzt prüft sie zuerst die eigenen Regeln aus `camera_names.json` gegen Make/Model und alle Strings im Video (inkl. `model_name:` aus DJI-`Category`). DJI-Encoder wie „DJI NEO2" werden nicht mehr zu „Videoediting Software", sondern bleiben mindestens der Rohwert.
- **Plot:** direkt vor `</plot>` je eine hellblaue Zeile (`[COLOR lightblue]`), getrennt durch `[CR]`: `Temp: 11 °C` und `Wetter: Regen` – nur wenn `XMP-mon:CtxTempC` / `CtxWeather` im Sidecar stehen.

### Wetter-Archiv: auch die letzten Tage
Früher blieben die letzten 7 Tage draußen (Werte noch vorläufig). Jetzt wird **alles Angefragte bis einschließlich heute** dauerhaft gespeichert – Tage ≤ 7 Tage alt kommen von der Forecast-API (Open-Meteo), ältere von der Archiv-API. Die Werte der letzten Tage können sich später minimal ändern; das Archiv behält bewusst, was es zuerst bekommen hat. Nur Tage in der Zukunft werden nicht archiviert. Schon gespeicherte Zeilen bleiben unverändert.

### geo-service: Backend-Status
`GET <geo-service>/health?probe=true` prüft Photon/Nominatim/Pelias live (siehe geo-service/README.md). Ein `"status":"unreachable"` in einer `/reverse`-Antwort mit `"cached": true` ist ein Altwert und kein aktueller Zustand.

### Zeitzone des Containers (`TZ`) aus der .env
`TZ=Europe/Vienna` in die `.env` schreiben – `docker-compose.yml` reicht sie jetzt an die Suite durch (`TZ=${TZ:-}`), und das Image enthält `tzdata`. Einmal neu bauen (`docker compose build`), danach reicht `up -d`. Die Info-Seite zeigt den Wert unter „Container-Zeitzone“ und nennt bei fehlendem Wert den Hinweis. Wirkt auf alles, was lokale Zeit ohne explizite Zone nutzt (Logzeilen, Info-Uhr, NFO-Datum aus der Datei-mtime). `MTIME_TIMEZONE` (Datei-Zeitstempel) und `GEO_DEFAULT_TZ` (geo-service) bleiben davon unabhängig.

### geo-service ersetzt den geonames-wrapper
`findNearbyPlaceName`, `findNearbyPostalCodes`, `search`, `get`, `hierarchy`, `countryInfo`, `address`, `postalCodeSearch` und `postalCodeLookup` (GeoNames-kompatibel, XML + JSON) laufen jetzt im geo-service – Details, Parameter und Umzugsschritte in `geo-service/README.md`. Neue Variablen: `GN_MAX_ROWS_LIMIT`, `GN_POSTAL_PROBE_RINGS`, `GN_POSTAL_PROBE_POINTS_PER_RING`, `GN_POSTAL_PROBE_CONCURRENCY`, `GN_HTTP_TIMEOUT`, `GN_ID_DB`.

## Eine einzige `.env` für beide Services

Die `.env` neben der `docker-compose.yml` genügt für **beide** Container (Suite + `geo-service`):
`cp .env.example .env`, Werte anpassen. Alle Variablen aus der Compose-Datei stehen (aktiv oder
auskommentiert mit Standardwert) in `.env.example`, gegliedert nach Abschnitten; gemeinsam genutzte
(`PHOTON_/NOMINATIM_/PELIAS_/MARITIME_SERVER`, `GEO_LANG`, `TRACCAR_*`, `DAWARICH_*`, `GEO_SERVICE_TOKEN`)
stehen nur einmal. Werte nicht in Anführungszeichen setzen. `geo-service/.env.example` gilt nur für den Standalone-Betrieb.

## Höhe im Sidecar (`XMP-mon:CtxElevation`)

Mit dem geo-service schreibt die Suite die Seehöhe (Meter) zusammen mit den anderen Kontextfeldern ins Sidecar
(`XMP_CONTEXT=true`). Für den KI-Prompt optional: `AI_CONTEXT_ELEVATION=true` ergänzt „auf ca. 372 m Seehöhe“.
Details zu Quellen, Kacheln und Korrekturen (Overrides): `geo-service/README.md`.

## Höhe und Zeitzone neben der Position (Sidecar + Bild)

Überall dort, wo die Suite eine Position ins Sidecar schreibt (**GPS Sidecar**, **GPS Fill**), ergänzt sie – nur wenn noch
nicht vorhanden – `XMP-exif:GPSAltitude`/`GPSAltitudeRef` (zuerst die eigene Höhe der Datei, z. B. aus dem Kamera-EXIF,
sonst die Höhe des geo-service) samt `XMP-mon:AltitudeSource` (`camera` | `dem`) und die Zeitzone `XMP-mon:CtxTimezone`.
Beim Einbetten ins Bild (AI-Beschreibung bzw. „ins Bild einbetten“) schreibt `xmpsync` die Höhe als EXIF-`GPSAltitude`
ins Bild, falls das Bild keine eigene hat. Schalter: `XMP_PLACE_EXTRAS=true`.
Zusätzlich (`IMAGE_OFFSET_FROM_ZONE=true`, Standard **an**): `EXIF:OffsetTimeOriginal` aus Zeitzone + Aufnahmezeit, nur wenn das Bild
keinen hat – setzt voraus, dass die Kamerauhr Ortszeit zeigt. `false` schaltet es ab. Videos bekommen wie immer nur das Sidecar.

## Geo-Overrides in der Suite

Rechts neben **GPS füllen** gibt es den Reiter **Geo-Overrides**: der Override-Editor des geo-service (siehe `geo-service/README.md`)
direkt in der Suite. Adresse oder Koordinaten eingeben, Felder überschreiben, speichern – geschrieben wird in die Overrides-Datei des
geo-service (`./geo-data/overrides.json`). Die Seite selbst kommt aus dem Service (`/ui`), die Aufrufe laufen über einen kleinen Proxy der
Suite (`/geooverrides/api/…`, nur die vom Editor benötigten Pfade; `GEO_SERVICE_TOKEN` bleibt auf dem Server). Ohne `GEO_SERVICE_URL`
bzw. bei nicht erreichbarem Service zeigt der Reiter einen Hinweis.

## IPTC „Credits/Origin“ (IrfanView: Date created / Source / Credits)

Warum „Date created“ leer war: die Suite schrieb bisher nur EXIF-/XMP-Datumsfelder, nie `IPTC:DateCreated` (Samsung-Handys füllen es selbst).
Jetzt werden bei Fotos – **nur wo leer, nie überschrieben** (exiftool im Create-only-Modus `-wm cg`) – ergänzt:
`IPTC:DateCreated` (+ `TimeCreated`, nur wenn der UTC-Offset bekannt ist) aus der Aufnahmezeit, `IPTC:Source` = gemappter Kameraname
(`camera_names.json`, NFO-Regeln, Google-Geräteliste), `IPTC:Credit` = `IPTC_CREDIT` (Standard: leer = kein Credit),
jeweils samt XMP-Zwilling (`XMP-photoshop:DateCreated/Source/Credit`). Wann: nach dem Datums-Schreiben in **Timestamp & Rename** und beim
**Einbetten ins Bild** (GPS Sidecar / AI-Beschreibung). Videos bekommen die XMP-Felder im Sidecar (**GPS Sidecar**). Schalter: `IPTC_FILL=false`.
Bereits verarbeitete Dateien bekommen es beim nächsten Lauf der jeweiligen Module.

## Volumes aus der `.env`, GeoNames-Volume, Karte im Override-Editor

* Alle Host-Pfade der Volume-Mappings stehen in der `.env`: `MEDIA_HOST_DIR` (→ `MEDIA_ROOT`), `CONFIG_HOST_DIR` (→ `/config`), `GEO_DATA_HOST_DIR` (→ `/data` des geo-service), `GEONAMES_HOST_DIR` (→ `/geonames`, nur lesen). Die Container-Pfade sind fest.
* **GeoNames-Volume:** Im geo-service-Image liegt keine GeoNames-Datei mehr; `countryInfo.txt` wird aus `GEONAMES_HOST_DIR` gelesen. Der Maritime-Resolver bleibt extern (`MARITIME_SERVER`).
* **Karte im Editor:** `GEO_TILE_URL` (Raster-Kacheln deines tileserver-GL) schaltet die Karte unter *Geo-Overrides* ein; Overrides gelten für Kreis (Radius) **oder frei gezeichnete Fläche**. Die Karte öffnet auf **Zuhause** (Zoom 15): `GEO_HOME_LAT`/`GEO_HOME_LON`, sonst der Ort der Art „Zuhause“ in *Meine Orte*; ohne beides zeigt sie alle Overrides.


## Kontext-Extras: Licht, Mond, Feiertage, Reisen, Sehenswürdigkeiten, Länder, Uhr-Check

Der geo-service liefert pro Foto zusätzlich (Details in `geo-service/README.md`): Tageslicht (goldene/blaue Stunde, Sonnenauf-/untergang), Mondphase, Feiertag, Wind/Niederschlag/Luftfeuchte, Sehenswürdigkeiten in der Nähe, automatisch erkannte Reisen und – nur im Ausland – Hauptstadt/Währung/Sprachen. Die Suite nutzt das an drei Stellen:

* **XMP-Sidecar** (`XMP_CONTEXT`, immer alles): `CtxDaylight`, `CtxSunrise`, `CtxSunset`, `CtxMoon`, `CtxHoliday`, `CtxWindKmh`, `CtxWindText`, `CtxPrecipMm`, `CtxCloudPct`, `CtxHumidityPct`, `CtxPois`, `CtxCountry`, `CtxCountryInfo`, `CtxTripSource` (`folder` = Ordnername, `track` = aus dem GPS-Verlauf erkannt), `CtxClock` und `CtxClockStatus` (`ok`/`warn`). Der Ordnername einer Reise hat Vorrang vor der Erkennung.
* **KI-Prompt** (`AI_CONTEXT_EXTRAS`, Standard `daylight,moon,holiday,trip,poi`; weitere: `wind`, `precip`, `country`; `all`/`none`). Wie viele Angaben das Modell davon wirklich nutzt, begrenzt weiter `AI_CONTEXT_MAX`.
* **NFO-Plot** (`NFO_PLOT_CONTEXT`): `full` (Standard) = Temp, Wetter, Wind, Niederschlag, Licht (mit Auf-/Untergang), Mond, Feiertag, Reise, In der Nähe, Land; `basic` = nur Temp/Wetter wie bisher; `off`.
* **Kamera-Uhr-Warnung**: Beim Erzeugen der KI-Beschreibung prüft die Suite die Uhr, wenn die Datei GPS-Satellitenzeit oder eine *eigene* Kamera-GPS-Position hat (nicht bei aus der Spur gefüllter Position). Die Warnung (`CtxClock`, z. B. „Kamera-Uhr geht vor um 1 h 00 min gegenüber der GPS-Zeit“) steht in der Kontext-Vorschau der AI-Beschreibung und im Sidecar – korrigiert wird nichts automatisch.
* **Orte vereinheitlichen**: *Meine Orte* (Name/Art/Sehenswürdigkeit im Override-Editor) sind die **einzige** Quelle für feste Orte im GPS Sidecar; die alte Datei `predefined_locations` samt Import-Knopf und `GEO_PREDEFINED_LOCATIONS` wurde entfernt (bereits importierte `legacy-…`-Einträge bleiben normale Orte). Einheitliche Schreibweisen (`GEO_NORMALIZE_NAMES`, `place-names.json`) gelten bereits im Service.
Ehrlich: Sonnenzeiten ±2 Min., regionale Feiertage nur AT/DE, Sehenswürdigkeiten hängen an deinem Photon, Reise-Erkennung an der Qualität des GPS-Verlaufs; Docker-Builds und echte Traccar/Dawarich/Photon-Daten sind in der Entwicklungsumgebung nicht prüfbar (getestet gegen simulierte Gegenstellen).

### GPS Sidecar: „via …“ zeigt die wahre Herkunft des Ortsnamens
Das Kürzel hinter dem Ort sagt jetzt, **woher die Antwort wirklich kam**: `geo-service-Cache, ursprünglich Photon` (aus dem Cache des Services), `Photon frisch, über geo-service` (neu abgefragt), `Override „id“ (geo-service)` (von Hand korrigiert) oder `Photon direkt (geo-service nicht erreichbar)` (der Service war down, die Suite hat selbst gefragt). Ohne `GEO_SERVICE_URL` bleibt es wie bisher (`Photon`, `Nominatim`, …).

## Endpunkt-Seite (🔗)

Links neben dem Info-Icon, ohne Beschriftung: eine eigene Seite mit **allen Endpunkten des geo-service** (so wie `geo-service/ENDPOINTS.md`, aber klickbar). Oben wählst du einen **Beispielort**: die Orte deiner Overrides (🏠 *Zuhause* zuerst, dann alphabetisch; Koordinaten, PLZ, Ort und Land kommen aus dem Override) oder eigene Koordinaten. Alle Links öffnen in einem neuen Tab und enthalten den gewählten Ort; POST/DELETE-Aufrufe stehen als fertige `curl`-Zeilen mit „Kopieren“. Ein Suchfeld filtert die Liste. Der Abschnitt **Schnelltest per curl** zeigt fertige Befehlszeilen: ein Klick auf die ganze Zeile (oder auf „Kopieren“) legt sie in die Zwischenablage, im Terminal nur noch einfügen; „Alles auf einmal“ kopiert alle vier Befehle samt `BASE=…`. Funktioniert auch über `http://` ohne HTTPS.

Die Links gehen direkt vom Browser zum Service. Als Adresse dient der Host, mit dem du die Suite geöffnet hast, plus die Host-Ports 8430/8431/8432; abweichend: `GEO_SERVICE_PUBLIC_URL` (nur Host, z. B. `http://192.168.1.33`) und `GEO_PUBLIC_PORT`, `GEO_PUBLIC_PORT_NOMINATIM`, `GEO_PUBLIC_PORT_PHOTON`. Verlangt der Service einen Token (`GEO_SERVICE_TOKEN`), nennt die Seite den Hinweis (Header `X-Api-Key`). `tests/test_endpoints_page.py` prüft, dass Seite (`app/core/endpointscatalog.py`) und `ENDPOINTS.md` dieselben Endpunkte enthalten – neue Endpunkte bitte in beiden eintragen.

**Info-Seite: Sehenswürdigkeiten-Index.** Die Info-Seite (`/info/`) zeigt unter „🗺 Sehenswürdigkeiten-Index“ den Stand des geo-service (`GET /sights/status`): Anzahl der Sehenswürdigkeiten, Wikidata-Fortschritt, Wikimedia-Wichtigkeit, Wikipedia-Kurztexte, per LLM übersetzte Beschreibungen, den Datenstand der Orte sowie den letzten automatischen Lauf (Schritte mit Dauer und Fehlern, nächster Lauf) und die behaltenen Download-Dateien. Test: `tests/test_info.py`.

## Kontext nachfüllen (Modul `/ctxfill/`)

**Ort & Gewässer (neue Gruppe) und Auffrischen.** Findet Dateien, deren Sidecar keinen brauchbaren Ort hat (kein Land, oder weder Stadt noch Ortsteil – früher z. B. auf See oder in Küstengewässern) und löst die Position über den geo-service neu auf: Land bzw. Meer, nächste Bucht/Insel/Meerenge, Schlagwörter (`dc:Subject`, **andere Schlagwörter bleiben**) und bei Videos die `Ort:`-Zeile der .nfo. Ohne Häkchen „vorhandene Kontext-Werte neu schreiben“ werden nur fehlende Orte gefüllt. Soll alles neu aufgelöst werden (nach einem Update des geo-service: Meere, Sehenswürdigkeiten aus dem Index), im Suchformular **„auch vollständige Dateien anbieten (Auffrischen)“** anhaken, im Ergebnis die Gruppen **Ort & Gewässer** und **Datum & Wo wars** (enthält „In der Nähe“) wählen und **„vorhandene Kontext-Werte neu schreiben“** setzen. Fotos: „ins Bild einbetten“ schreibt den Stand auch ins Bild. Die KI-Beschreibung wird dabei **nicht** neu erzeugt (dafür das Modul AI-Beschreibung); sie liest den neuen Ort aber beim nächsten Lauf aus dem Sidecar.

**Datenstand und Motiv.** Beim Auflösen schreibt das Modul `XMP-mon:CtxPlaceVersion` (Datenstand des geo-service: Zeitpunkt des Index-Neuaufbaus + Auflösungs-Revision, `GET /sights/status` → `place_version`) ins Sidecar. Weicht der Stand später ab (Index neu gebaut, Auflösung geändert), bietet der Scan diese Dateien automatisch wieder an („mit älterem Datenstand aufgelöst“, Gruppen Ort & Gewässer und Datum & Wo wars; zum Überschreiben „vorhandene Kontext-Werte neu schreiben“ setzen). Dateien ohne Stempel (von früheren Läufen) werden nicht allein deshalb angeboten – dafür „auch vollständige Dateien anbieten“. **Motiv:** liegt eine wichtige Sehenswürdigkeit innerhalb `GEO_TOP_SIGHT_M` (Standard 60 m) mit Wikimedia-Wichtigkeit ab `GEO_TOP_SIGHT_MIN_IMPORTANCE` (0,30; eigene Orte immer), landet sie als `XMP-mon:CtxSight` im Sidecar und hängt an die `Ort:`-Zeile („Wien, Stephansdom“).

Für Dateien, die vor den neueren Versionen verarbeitet wurden und deren `.xmp` daher **Höhe, Zeitzone, Wetter, Licht/Mond, Datum/Reise, IPTC-Felder** oder bei Videos die **Kontext-Zeilen der .nfo** nicht hat. Die Seite sucht in Pfad/Tiefe (wie die anderen Module, Basis-Ordner bestimmt Foto/Video) und liest dabei **nur die `.xmp`-Sidecars** (bei Videos zusätzlich den Text der `.nfo`), nie die Mediendateien. Pro Datei steht, was fehlt:

* **Höhe & Zeitzone** – `exif:GPSAltitude` (+ `AltitudeSource`), `XMP-mon:CtxElevation`, `CtxTimezone`.
* **Wetter** – `CtxWeather`, `CtxTempC`, Wind, Niederschlag, Wolken, Luftfeuchte.
* **Licht & Mond** – `CtxDaylight`, `CtxSunrise`, `CtxSunset`, `CtxMoon`.
* **Datum & Reise** – Tageszeit, Jahreszeit, Monat, Jahr, Wochentag, Ortszeit, Feiertag, Reise, Sehenswürdigkeiten, Länderinfo (Reise/Sehenswürdigkeiten/Land gelten nie als „fehlt“, werden aber mitgeschrieben, wenn es sie gibt).
* **IPTC** – `photoshop:DateCreated`, `Source` (Kameraname), `Credit` im Sidecar (nur wenn `IPTC_FILL` an ist; ohne Kamera-Info im Sidecar wird kein `Source` verlangt).
* **NFO-Zeilen** (Videos) – ist in der vorhandenen `.nfo` der hellblaue Kontext-Block (Temp/Wetter/Wind/Licht/Mond/…) nicht oder veraltet, wird nur dieser Block am Ende des `<plot>` ersetzt; alles andere (Kamera, Ort, KI-Text) bleibt. Aus `NFO_PLOT_CONTEXT=off` folgt: NFO unverändert.

Dateien **ohne `.xmp`** oder **ohne Position im `.xmp`** werden nur gezählt (erst „GPS füllen“ / „GPS Sidecar“). Nachgefüllt wird **nur Fehlendes**: vorhandene Werte bleiben, außer „vorhandene Kontext-Werte neu schreiben“ ist angehakt. Die Werte kommen vom geo-service (`GEO_SERVICE_URL`) über die im Sidecar gespeicherte Position und die Aufnahmezeit; der Schalter `AI_CONTEXT` ist dafür **nicht** nötig. Optional „Fotos: ins Bild einbetten“ schreibt danach den Sidecar-Stand (Höhe, Ort, IPTC) ins Foto (`sync_xmp_to_image`); Videos werden nie verändert. `RESTORE_TIMESTAMP=true` stellt die Zeitstempel der geänderten Bilder/NFOs wieder her. Stufenweises Laden: `BATCH_SIZE_CTXFILL`. Hinweis: Wetter, das der geo-service für einen Tag nicht liefern kann, bleibt „fehlend“ und taucht bei einem neuen Scan wieder auf (die Ergebnisliste sagt es dazu). Engine `core/ctxfill.py`, Test `tests/test_ctx_fill.py`.

Aufräumen nebenbei: in `core/iptcfill.py` stand `fill_sidecar` doppelt (die erste, nie benutzte Fassung war unbrauchbar) – entfernt.

**Langsamer erster Abruf (`GEO_CONTEXT_TIMEOUT`, Standard 40 s):** Der erste `/context`-Abruf für einen Ort und Tag dauert mehrere Sekunden (der geo-service fragt dann Open-Meteo, Sehenswürdigkeiten und Reise-Erkennung selbst ab) und speichert das Ergebnis; weitere Abrufe sind schnell. Mit `GEO_SERVICE_TIMEOUT=8` brach die Suite diesen ersten Abruf ab („kein Wetter verfügbar"), obwohl der Dienst weiterarbeitete und es beim nächsten Mal klappte. Nur `/context` nutzt jetzt den längeren Timeout.

## Wetter-Archiv neu holen (`refresh`)

Im Wetter-Panel von **GPS füllen** gibt es bei „Schon gespeicherte Tage" drei Stufen: *überspringen* (Standard), *defekte/unvollständige neu holen* (gespeicherte Tage ohne Stunden, ohne Wettercode oder aus einer alten Zeilenversion) und *alle neu holen* (jeder angefragte Tag wieder von Open-Meteo – viele Abrufe, Limit beachten; schlägt ein Abruf fehl, bleibt die alte Zeile). Am geo-service: `POST /weather/backfill` mit `"refresh": "none|bad|all"` (zusammen mit „Nur prüfen" zeigt es vorab, wie viele Tage neu geholt würden). Test: `geo-service/tests/test_weather_backfill.py`. Das Menü zeigt jetzt „GPS füllen" und „Kontext nachfüllen" rechts von „Collage".

## Startseite / Dashboard (`/`)

`http://<host>:8420/` zeigt jetzt ein Dashboard statt der Weiterleitung auf Timestamp: **Status** (geo-service erreichbar/Version/Laufzeit, Wetter-Provider und Archivgröße, Track-Quellen, Geocoder inkl. offener Circuit-Breaker, Anzahl „Meine Orte"/Overrides), **eine Kachel je Modul** mit Kurzbeschreibung und der Zahl des üblichen Ablaufs (Timestamp → Position → Nachfüllen → Beschreibung → NFO/Collage), die konfigurierten **Basis-Ordner** sowie Build/Laufzeit. Der Status wird nach dem Seitenaufbau per HTMX nachgeladen (`GET /home/status`, alle 60 s) – ein langsamer oder fehlender geo-service verzögert die Startseite nie. Auf jeder Modulseite führt das Haus-Symbol links in der Navigation zurück. Engine: `app/modules/home/`, Test: `tests/test_home_dashboard.py`.

## Jahreslauf (Modul `/year/`, ersetzt `Media_actualyear_nfo_Creator.sh` im Browser)

Ein Jahr auf einen Klick, ohne Rückfragen: **Jahr**, **Typ** (Fotos/Videos/beide), **Ordner** (normal/Urlaube/beide), **Tiefe** (0–7, Standard 7) und **AIM** wählen, „Vorschau" zeigt die bis zu vier Ordner mit Dateizahl, „Jahreslauf starten" verarbeitet sie im Hintergrund (auch wenn du die Seite schließt; ein Lauf zugleich; Fortschritt, Protokoll und Ergebnis bleiben 24 h abrufbar, „Abbrechen" stoppt zwischen zwei Dateien).

Ordner wie im Skript (Basis aus `ROOT_FOLDERS`: erster `IMAGE`- bzw. `VIDEO`-Eintrag): `Foto/<Jahr>`, `Foto/Urlaube/<Jahr>`, `Video/<Jahr>`, `Video/Urlaube/<Jahr>`. Fehlt ein Ordner, wird er übersprungen und im Protokoll vermerkt.

Pro Ordner laufen die gewählten Schritte nacheinander (jeder ist abwählbar):

| Schritt | Entspricht | Details |
|---|---|---|
| Umbenennen (immer zuerst) | wie Timestamp & Rename mit den Standardwerten: Datei nach `YYYY.MM.DD-HH.MM.SS` umbenennen, .xmp-Datum setzen (kein EXIF-Rückschreiben) |
| GPS Sidecar | `Set-GPS… -a1 -f0 -G0 -D1 -R1` | Position aus Datei/Track (Traccar-Standardgeräte aus `TRACCAR_DEVICES` + Dawarich; das „Dawarich-Archiv“-Konto automatisch, sobald `DAWARICH_SERVER` und `DAWARICH_IMPORT_API_KEY` gesetzt sind), Geocoding, Sidecar; Fotos zusätzlich ins Bild eingebettet, Videos nur Sidecar (`-o`), DJI-`.srt` wird genutzt; Bereits fertiges wird übersprungen |
| AI-Beschreibung | `-AI1` / `-AIM` | ohne AIM nur für die in diesem Lauf mit GPS versehenen Dateien, mit AIM im ganzen Ordner (alle ohne Beschreibung); Fotos eingebettet |
| NFO Maker (nur Videos) | `HomeVideo_NFO_Maker_new.sh -d <Tiefe> -A1` | neue `.nfo`; mit AIM zusätzlich `--repair-ai` |
| Kontext nachfüllen | neu | Höhe, Wetter, Licht, Reise, IPTC, NFO-Kontextzeile nur wo es fehlt (braucht `GEO_SERVICE_URL`) |
| Datei-Zeitstempel | neu | Änderungsdatum wieder auf die Aufnahmezeit |
| Collage (nur Foto-Urlaube) | `photo_pile_collage.sh …` | Voreinstellungen aus `COLLAGE_*` (Standard wie im Skript: 4096×2304, 139 Bilder, Rand 6, random, Rotation 25); vorhandene Collagen bleiben |

Ein Schritt, der scheitert, wird im Protokoll vermerkt (✗), die übrigen laufen weiter. Der Stepper (`BATCH_SIZE_*`) spielt hier keine Rolle: der Jahreslauf holt automatisch alle Schritte einer Liste nacheinander.

Technik: Die Hintergrundabläufe der Module (`browse_start_work`, `geocode_start_work`, `generate_start_work`, `scan_start_work`, `apply_work`, `apply_rows`) sind aus den Routern herausgelöst, damit Web-Modul und Jahreslauf **dieselbe** Implementierung nutzen. Engine: `app/modules/year_run/runner.py`, Test: `tests/test_year_run.py`.

### NFO Maker: `repair-missing-ai` und `repair-missing` (Okt 2026)
Zwei neue Modi im Dropdown „Modus“ (wie `--repair-missing-ai` / `--repair-missing` im Skript; brauchen aktivierte AI-Beschreibungen):
- **repair-missing-ai**: .nfo ohne jede AI-Beschreibung im Plot (kein `[COLOR white]`).
- **repair-missing**: wie oben **oder** ohne echten Ort („No Location“ bzw. keine „Ort:“-Zeile), **aber nur wenn das .xmp des Videos inzwischen GPS hat** – sonst käme wieder „No Location“ heraus.
Der Jahreslauf nutzt `repair-missing` im NFO-Schritt automatisch (neu geotaggte Videos bekommen ihren Ort in die alte .nfo); mit AIM kommt zusätzlich `repair-missing-ai` dazu. Test: `tests/test_nfo_repair_missing.py`.

### Jahreslauf merkt sich geprüfte Ordner (Okt 2026)
Ein Schritt, der „nichts mehr zu tun“ fand (bzw. alles ohne Fehler erledigt hat), wird beim nächsten Lauf übersprungen, solange der Ordner **unverändert** ist (Fingerabdruck über Pfad, Größe und mtime aller Dateien bis zur gewählten Tiefe) und dieselben Optionen (AIM, Zeitzone, Tiefe) gelten. Das spart bei großen Jahren das minutenlange Durchlesen aller Sidecars. Im Protokoll steht dann „unverändert seit dem letzten Lauf (…) – übersprungen“.
- **Nicht gemerkt** wird, wo noch etwas offen ist: GPS mit Dateien ohne Position (ein Track kann später kommen), KI-Beschreibungen ohne Ergebnis, Fehler in einem Schritt, „Kontext nachfüllen“ nach dem Nachfüllen (wird einmal bestätigt).
- **„Alles neu prüfen“** im Formular ignoriert den gemerkten Stand einmalig. `YEAR_RUN_STATE_DAYS` (Standard 30, `0` = Funktion aus) begrenzt, wie alt ein gemerktes Ergebnis sein darf.
- Der Stand liegt in `year_run_state.json` im Config-Ordner (neben `presets.json`); Löschen der Datei erzwingt eine komplette Prüfung. Tests: `tests/test_year_run_state.py`.

### NFO-Kontextzeile „Wo wars:“ statt „Reise:“ (Okt 2026)
Die hellblaue Zeile mit dem Ordner-/Reisenamen im NFO-Plot heißt jetzt **„Wo wars: …“** (auch Einzeltage haben einen Ordnernamen, „Reise“ war irreführend). Ebenso in der AI-Vorschau und der Kontext-nachfüllen-Gruppe „Datum & Wo wars“. Alte .nfo mit „Reise:“ werden von **Kontext nachfüllen** (und dem Jahreslauf-Schritt) als „NFO-Zeilen fehlen“ erkannt und die Zeile wird ersetzt, nicht doppelt geschrieben. Die XMP-Felder (`CtxTrip`) und der KI-Text bleiben unverändert.

### Jahreslauf: Zeitstempel sind immer richtig

Der Schritt „Zeitstempel" läuft **immer zuletzt** (auch wenn er nicht angehakt ist) und korrigiert zusätzlich alle Dateien, die der Lauf geschrieben hat (`.nfo`, Thumbs, `.xmp`, Medien). Wird ein Lauf abgebrochen oder bricht er ab, bleibt er im Zustand „Zeitstempel noch nicht richtig" und zeigt den Button **„OK – Zeitstempel richtigstellen"**; damit werden die Zeitstempel aller bisher geänderten Dateien nachgezogen. Collage-Bilder und `folder.jpg` haben kein ermittelbares Aufnahmedatum und bleiben unverändert.

### „Alle verbleibenden laden" (alle Listen-Module)

Neben „Nächste N laden" gibt es in jedem Modul mit Schritt-Ladung den Button **„Alle verbleibenden laden (N)"** (ohne Rückfrage). Er lädt den Rest des Scans in einem Schritt (technisch: `offset = -(Start+1)`, siehe `core/stepper.py`).

### Jahreslauf: „KI-Müll reparieren"

Zusätzliche Option neben AIM. Sie prüft die vorhandenen KI-Beschreibungen im ganzen Ordner (`aicore.ai_description_problem`) und erzeugt unbrauchbare neu. Erkannt werden:
- Absagen oder Entschuldigungen des Modells („Es tut mir leid, …")
- `<think>`-Text, Markdown oder JSON im Text
- Wort- oder Satz-Wiederholungen
- fremde Schrift
- Englisch statt Deutsch
- zu kurze oder mitten im Satz abgeschnittene Texte

Bei Videos wird zusätzlich eine .nfo-Reparatur gefahren (neuer NFO-Modus `repair-bad-ai`). Ohne AIM wird nichts nachgefüllt, nur Defektes ersetzt. Die Funde stehen mit Grund im Protokoll. Neu erzeugte Texte, die eine Absage, Markup, eine Wiederholung oder fremde Schrift enthalten, werden nie geschrieben, sondern zählen als „ohne Ergebnis".

## Neu (Okt 2026): Dokumentation im Browser, Zeitkorrektur, neues Geocoding, NFO-Limit
- **Dokumentation (`/docs/`, Icon in der Navigation vor ⓘ):** README der Suite und des geo-service werden beim Aufruf als gut lesbare HTML-Seiten gerendert (Inhaltsverzeichnis, Tabellen, Suche); neu gebaut wird nur, wenn sich die Datei geändert hat. Die READMEs liegen im Image (`Dockerfile`), nach `git pull` also `docker compose build metadata-suite`.
- **Zeitkorrektur (Timestamp & Rename, GPS Sidecar):** Feld „Zeitkorrektur“ (z. B. `+01:30`, `-00:02:10`) verschiebt die Aufnahmezeit beim Umbenennen/Schreiben; im GPS Sidecar wird der Track zur korrigierten Zeit gesucht.
- **Orte bestehender Dateien neu geocodieren (GPS Sidecar):** Checkbox „Orte bestehender Dateien neu geocodieren“. Dateien mit vorhandenem Ort werden nicht übersprungen, ihre GPS-Position bleibt, der Ort wird am geo-service-Cache vorbei (`/reverse?nocache=true`) neu abgefragt und überschrieben; in der Liste steht der bisherige Ort („bisher: …“). Test: `tests/test_gps_delete_srt_lrf.py`.
- **NFO Maker, Feld „Limit“:** begrenzt die repair-Modi auf N Videos pro Lauf (0 = alle). `repair-missing(-ai)` selbst gab es schon (siehe oben).
- **Collage auf einem Jahresordner (Ordnername `JJJJ`):** Jeder direkte Unterordner (z. B. `2023.11`) ist genau eine Einheit: die Fotos werden rekursiv gesammelt, die Collage `<Unterordner>.jpg` entsteht immer im Jahresordner, nie in Unterordnern, und Tages-Unterordner bekommen keine eigene Collage. Ordner namens `JJJJ.MM` zählen auch an anderer Stelle immer als eine Einheit. Unterordner ohne Fotos werden übersprungen.
- **Geo-Overrides, „Details zu diesem Punkt“:** Ein Klick auf die Karte (oder eine Suche) zeigt über dem Editor ein Infofeld mit allem, was der geo-service zu dem Punkt weiß: Ort und Adresse, Höhe samt Quelle, Zeitzone mit UTC-Versatz und Sommerzeit, lokale Zeit, Sonnenauf-/-untergang, Tageslicht-Phase mit Sonnenhöhe, Mondphase, Feiertag, aktuelles Wetter, Länderinfo, Sehenswürdigkeiten in der Nähe und ein vorhandener Override. Die ersten Werte kommen sofort, der Rest (`/context`, über den Proxy der Suite) lädt nach.
- **Timestamp & Rename, „Bilder verlustfrei aufrecht drehen (JPEG)“:** neue Option unten in der Liste, **standardmäßig an**. Beim Ausführen werden ausgewählte JPEGs, die nur durch das EXIF-Orientation-Tag aufrecht erscheinen, mit `exiftran` verlustfrei gedreht (vor dem Umbenennen/Schreiben; EXIF-Daten, Dateizeit und .xmp-Sidecar bleiben stimmig). Der zweite Schalter „Backup vor dem Drehen (`datei.jpg~`)“ ist **standardmäßig aus**. Der Jahreslauf dreht im Umbenennen-Schritt ebenfalls (ohne Backup). Test: `tests/test_timestamp_rotate.py`.
- **Datei-Zeitstempel (`/mtime/`):** Der Schalter „Backup vor dem Drehen (`datei.jpg~`)“ ist jetzt ebenfalls **standardmäßig aus**.

### Jahreslauf: Monatslauf

Im Jahreslauf gibt es das Feld **Monat** (Standard: „ganzes Jahr“). Wird ein Monat gewählt, verarbeitet der Lauf nur `…/JJJJ/JJJJ.MM` in Foto- **und** Video-Wurzel gleichzeitig (z. B. `/mnt/NAS/photo/2026/2026.11` und `/mnt/NAS/video/Home-Video/2026/2026.11`). Urlaubsordner entfallen, ebenso der Collage-Schritt. „Typ“ (Fotos/Videos) lässt sich weiterhin einschränken. Außerdem behebt die Vorschau jetzt, dass „KI-Müll reparieren“ beim Start verloren ging.

### Kontext-Versionierung, „Neue Daten nachtragen“ und SQLite-Index (Nov 2026)

**Versionsstempel im Sidecar.** Jedes `.xmp` trägt `XMP-mon:CtxSchema` (Version des Kontext-Datensatzes, `ctxfill.CTX_SCHEMA`, derzeit **3**). Pro Gruppe steht in `GROUP_SINCE`, seit welcher Version es sie gibt (Höhe/Zeitzone, Wetter, Licht & Mond, Datum & Wo wars = 1; **Land & Währung = 2**, vorher nie nachgetragen). Ein Sidecar mit älterem Stempel bekommt **nur die neueren Gruppen** nachgetragen, nichts wird neu berechnet. Sidecars ohne Stempel (alt) werden wie bisher an den Leit-Tags geprüft und zusätzlich auf „Land & Währung“. Gestempelt wird nur, wenn der geo-service eindeutig geantwortet hat: gedrosseltes oder fehlgeschlagenes Wetter (HTTP 429, nicht erreichbar) bleibt ungestempelt und wird später erneut versucht; dauerhaft leeres Wetter (kein Stundenwert) wird nicht mehr ständig als „fehlt“ gemeldet. **Neue Datenart später:** `CTX_SCHEMA` um 1 erhöhen und die neue Gruppe mit der neuen Nummer in `GROUP_SINCE` eintragen.

**Veraltete KI-Texte / .nfo.** Bekommt ein Sidecar neue Daten, steht danach `AiStale` (bei Videos auch `NfoStale`) darin: der KI-Text bzw. die .nfo ist älter als diese Daten. Die Markierung liegt in der Datei und übersteht deshalb auch einen unterbrochenen Lauf vom Vortag. Sie wird gelöscht, sobald der Text/die .nfo neu erzeugt wurde.

**Jahreslauf-Option „Neue Daten nachtragen“.** Zwingt „Kontext nachfüllen“ in den Lauf und stellt ihn **vor** AI-Beschreibung und NFO. Danach werden KI-Text und .nfo **nur für markierte Dateien** neu erzeugt (auch aus früheren Läufen); alle anderen bleiben unberührt. Nach einem neuen KI-Text wird die .nfo eines Videos ebenfalls markiert und im NFO-Schritt desselben Laufs nachgezogen. Test: `tests/test_year_run_backfill.py`.

**SQLite-Index** (`config/file_index.sqlite`, `app/core/fileindex.py`). Die **Datei bleibt immer der Master**: der Index merkt sich nur, was ein Scan über eine Datei herausgefunden hat, samt Größe, Änderungszeit und ctime von Sidecar/.nfo. Weicht die Signatur ab (ctime lässt sich nicht per `touch` zurücksetzen, die Zeitstempel-Korrektur des Jahreslaufs täuscht ihn also nicht), wird die Datei neu gelesen; ebenso bei geänderter Umgebung (Datenstand des geo-service, IPTC-Einstellungen, `NFO_PLOT_CONTEXT`), nach `FILE_INDEX_DAYS` (365) Tagen und bei „Alles neu prüfen“. Gelöscht, kaputt oder mit `FILE_INDEX=off` abgeschaltet ändert er nie ein Ergebnis, nur die Geschwindigkeit. Er beschleunigt (a) den Scan von „Kontext nachfüllen“ (kein exiftool-Durchlauf über unveränderte Sidecars), (b) den Scan der AI-Beschreibung („hat schon eine Beschreibung“ wird nicht mehr pro Datei gelesen; „Nein“ wird nie gemerkt) und (c) protokolliert jeden Jahreslauf (Tabellen `runs`, `run_steps`: Optionen, Zeiten, Ergebnis je Schritt). Zusätzlich liest der GPS-Schritt Dateien, deren Sidecar schon einen Ort nennt, nicht mehr aus der (großen) Mediendatei, bevor er sie überspringt. Test: `tests/test_fileindex.py`.

**Aufnahmezeit für Wetter, Licht, Mond, Feiertag (Fix 8. Nov 2026).** Diese Daten brauchen eine *sichere* Aufnahmezeit (Datum **mit Zeitzone** im Sidecar bzw. EXIF-Offset). Fehlt sie – typisch bei Videos, deren Sidecar nur `CreateDate` ohne Offset trägt und die den Schritt „Umbenennen“ (der das Datum mit Zone festlegt) nie durchlaufen haben –, fehlten bisher still Tageszeit, Wetter, Licht und Mond. Jetzt gilt als Ersatz die Zeit im **Dateinamen** `JJJJ.MM.TT-HH.MM.SS` (lokale Zeit, gelesen in der Zeitzone der Position); das steht als `CtxTimeBasis=Dateiname` im Sidecar, und der Kamera-Uhr-Check wird dafür übersprungen. Ohne Dateinamen-Zeit gibt es weiterhin nichts zu berechnen: das Sidecar bekommt dann **keinen** `CtxSchema`-Stempel (wird bei jedem Lauf erneut versucht) und das Ergebnis nennt den Grund („keine sichere Aufnahmezeit … Abhilfe: Schritt Umbenennen mitlaufen lassen“). Stempel mit `CtxSchema` < 3 stammen aus der Zeit, in der auch Sidecars ohne diese Daten als komplett galten; sie werden deshalb wieder über ihre Leit-Tags geprüft und beim nächsten Lauf neu gestempelt.
