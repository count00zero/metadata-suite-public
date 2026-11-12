"""
mtimecore.py — resets a file's (or its .xmp sidecar's) filesystem modification time
back to the real capture time after exiftool's own -overwrite_original bumps it to
"now" during a write — ported from the CLI toolchain's restore_file_timestamp.py,
which is run there as a manual follow-up pass after
Set-GPS-Data-via-traccar-and-dawarich.sh's exiftool writes.

Timestamp & Rename (module #1) never needs this: executor.apply_resolution() already
sets the correct mtime directly (os.utime to the resolved timestamp) right after its own
exiftool write, as an inherent part of execution rather than a separate step. GPS
Sidecar and AI Description, however, only ever write GPS/location/description tags via
exiftool — without this, every "Ausführen" leaves the touched file's (and/or its .xmp
sidecar's) mtime at "now" instead of the original capture time. Both of those modules
offer this as an explicit opt-in button on their results screen (see each module's
router.py `restore_mtime()`) rather than running it automatically, so it's easy to see
exactly what changed.

Unlike the CLI script (dry-run by default, `--apply` to actually change anything), this
is always "apply" — it's only ever invoked here as a deliberate, already-confirmed
button click, with the result shown per file either way.

Timezone handling: filenames/EXIF here encode a real-world LOCAL wall-clock time (e.g.
"2026.09.27-12.11.17.MP4" means 12:11:17 in the maintainer's own locale), but a file's actual
filesystem mtime is stored as a plain UTC epoch, as always. Converting between the two
(epoch <-> wall-clock) needs an explicit timezone -- using a naive datetime for that
conversion (datetime.fromtimestamp()/.timestamp() with no tzinfo) implicitly falls back to
whatever system timezone the *container* happens to have, which is UTC by default on the
stock python:3.13-slim image this project builds from (no TZ/tzdata configured in the
Dockerfile). That silently shifted every comparison/write here by the container-vs-locale
offset (2 hours for Europe/Vienna in CEST) -- so every date/time value in this module is
made explicitly timezone-aware via settings.mtime_timezone() (ZoneInfo, default
"Europe/Vienna") instead, exactly like core/timecore.py's own resolution already does.
"""
from __future__ import annotations

import os
import re
import subprocess
from . import exifpool
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import settings

EXIFTOOL_TIMEOUT = 60


def zone() -> ZoneInfo:
    """Re-reads settings.mtime_timezone() fresh each call (same "never cached" convention
    as the rest of settings.py) so MTIME_TIMEZONE takes effect on the next request, no
    restart needed. Falls back to Europe/Vienna if the configured name is invalid, rather
    than crashing a scan/restore over a typo'd env var. Public (not just used internally)
    so a caller re-applying a previewed "target" string verbatim -- mtime_restore's own
    execute(), which deliberately doesn't call back into resolve_target()/restore_one() --
    can attach the same tzinfo rather than falling back to a naive (and therefore wrong)
    conversion of its own."""
    try:
        return ZoneInfo(settings.mtime_timezone())
    except Exception:
        return ZoneInfo("Europe/Vienna")


FILENAME_RE = re.compile(
    r"^(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})(?:[\s_].*)?\.\w+$"
)

# HomeVideo_NFO_Maker_new.sh-style generated images ("<core name>-fanart.jpg" etc.) —
# kept for parity with the CLI script even though no current module writes these yet.
GENERATED_IMAGE_SUFFIXES = ("-fanart", "-poster", "-thumb")


def _date_from_filename(path: str) -> Optional[datetime]:
    name = os.path.basename(path)
    if name.lower().endswith(".xmp"):
        # "<original filename>.xmp" -- strip the .xmp to recover the inner media
        # filename, which the pattern below matches fine (it accepts any extension).
        name = name[: -len(".xmp")]

    stem = Path(name).stem
    for suffix in GENERATED_IMAGE_SUFFIXES:
        if stem.lower().endswith(suffix):
            name = stem[: -len(suffix)] + Path(name).suffix
            break

    m = FILENAME_RE.match(name)
    if not m:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        # Filename LOOKS like the pattern but encodes an impossible date/time.
        return None


def _date_from_exif(path: str, timeout: int = EXIFTOOL_TIMEOUT) -> Optional[datetime]:
    inner_suffix = os.path.splitext(path)[1].lower()
    if inner_suffix == ".xmp":
        # The relevant extension for deciding which tag to read is the INNER one,
        # e.g. "video.mp4.xmp" should read -CreateDate just like "video.mp4" would.
        inner_suffix = os.path.splitext(Path(path).stem)[1].lower()
    is_video = inner_suffix not in (".jpg", ".jpeg")
    tag = "-CreateDate" if is_video else "-DateTimeOriginal"
    try:
        result = exifpool.run(
            ["exiftool", "-m", "-n", "-T", "-api", "LargeFileSupport=1", tag, path],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None

    raw = result.stdout.strip()
    if not raw or raw == "-":
        return None
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


@dataclass
class ResolvedTarget:
    target: Optional[datetime] = None
    source: str = ""        # "Dateiname" or "EXIF/QuickTime" -- empty if unresolved


def resolve_target(path: str, timeout: int = EXIFTOOL_TIMEOUT) -> ResolvedTarget:
    """Figures out what a file's (or .xmp sidecar's) mtime SHOULD be -- filename pattern
    first (cheap, no subprocess), EXIF/QuickTime capture date as fallback -- without
    touching the file itself. Shared by restore_one() below (which applies it right away,
    for GPS Sidecar's and AI Description's own "fix what I just wrote" button) and by
    core.jobs-driven scan_one() (which only reports it, for the standalone Datei-
    Zeitstempel module's preview-before-you-touch-anything workflow)."""
    dt = _date_from_filename(path)
    if dt is not None:
        return ResolvedTarget(target=dt, source="Dateiname")
    dt = _date_from_exif(path, timeout)
    if dt is not None:
        return ResolvedTarget(target=dt, source="EXIF/QuickTime")
    return ResolvedTarget()


@dataclass
class RestoreResult:
    path: str
    changed: bool = False
    skipped: bool = False
    source: str = ""        # "Dateiname" or "EXIF/QuickTime" -- only set if changed
    restored_to: str = ""   # human-readable timestamp -- only set if changed
    error: str = ""


def restore_one(path: str, timeout: int = EXIFTOOL_TIMEOUT) -> RestoreResult:
    """Resets one file's (or .xmp sidecar's) mtime to its real capture time -- see
    resolve_target() for how that's determined. Reports the file as skipped, untouched,
    if neither source is available or the mtime is already correct -- same "never guess"
    rule as the CLI script."""
    result = RestoreResult(path=path)
    if not os.path.exists(path):
        result.error = "Datei nicht mehr gefunden."
        return result

    resolved = resolve_target(path, timeout)
    if resolved.target is None:
        result.skipped = True
        return result

    tz = zone()
    try:
        # Aware on both sides: the file's real epoch read as a local wall-clock time in
        # `tz` (not the container's own system timezone), and resolved.target (naive,
        # meaning "this wall-clock time in `tz`") given that same tzinfo explicitly --
        # see the module docstring for why a naive conversion here would be wrong.
        current_mtime = datetime.fromtimestamp(os.stat(path).st_mtime, tz=tz)
        target_aware = resolved.target.replace(tzinfo=tz)
        if current_mtime == target_aware:
            result.skipped = True  # already correct -- nothing to do
            return result
        ts = target_aware.timestamp()
        os.utime(path, (ts, ts))
        result.changed = True
        result.source = resolved.source
        result.restored_to = resolved.target.strftime("%Y-%m-%d %H:%M:%S")
    except OSError as e:
        result.error = f"Zeitstempel konnte nicht gesetzt werden: {e}"
    return result


def scan_one(path: str, timeout: int = EXIFTOOL_TIMEOUT) -> dict:
    """Non-destructive preview for the standalone Datei-Zeitstempel module: resolves the
    target the same way restore_one() would, without touching the file, and reports it
    next to the file's current mtime so the UI can show "current -> target" and let the
    person pick what to actually apply, before anything changes on disk."""
    filename = os.path.basename(path)
    tz = zone()
    try:
        # Aware read: the file's real epoch, as a wall-clock time in `tz` -- not
        # whatever timezone the container itself happens to be running in. See the
        # module docstring for why a naive datetime.fromtimestamp() here would be wrong.
        current_mtime = datetime.fromtimestamp(os.stat(path).st_mtime, tz=tz)
    except OSError:
        return {
            "path": path, "filename": filename, "current_mtime": None,
            "target": None, "source": "", "differs": False, "unresolved": True,
        }

    resolved = resolve_target(path, timeout)
    if resolved.target is None:
        return {
            "path": path, "filename": filename,
            "current_mtime": current_mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "target": None, "source": "", "differs": False, "unresolved": True,
        }

    target_aware = resolved.target.replace(tzinfo=tz)
    return {
        "path": path, "filename": filename,
        "current_mtime": current_mtime.strftime("%Y-%m-%d %H:%M:%S"),
        "target": resolved.target.strftime("%Y-%m-%d %H:%M:%S"),
        "source": resolved.source,
        "differs": current_mtime != target_aware,
        "unresolved": False,
    }


def restore_many(paths: list[str], timeout: int = EXIFTOOL_TIMEOUT, job=None) -> list[RestoreResult]:
    """With `job` (core.jobs.Job): reports per-file progress and stops between files when
    the job's cancel_event is set."""
    if job is None:
        return [restore_one(p, timeout) for p in paths]
    job.total, job.done = len(paths), 0
    out = []
    for p in paths:
        if job.cancel_event.is_set():
            break
        job.current_name = os.path.basename(p)
        out.append(restore_one(p, timeout))
        job.done += 1
    return out
