"""Folders the suite never descends into: calendars and greeting-card artwork are generated/design
material, not captures. A folder is skipped when its name CONTAINS one of the words (case-insensitive)."""
import os
import re

SKIP_DIR_WORDS = ("kalender", "weihnachtskarte", "osterkarte")


def is_skipped_dir(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in SKIP_DIR_WORDS)


def prune(dirs: list) -> None:
    """In-place filter for os.walk's `dirs` list."""
    dirs[:] = [d for d in dirs if not is_skipped_dir(d)]


def is_trip_collage(filename: str, sibling_dirs) -> bool:
    """A trip collage: the Collage module writes "<trip folder name>.jpg" NEXT TO the trip folder
    ("2023.02.05-2023.02.10 - Maria Alm - Tante Frida/" + "2023.02.05-2023.02.10 - Maria Alm - Tante Frida.jpg").
    Generated, never a capture: it must not be timestamped, renamed or otherwise processed."""
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in (".jpg", ".jpeg"):
        return False
    low = stem.casefold()
    return any(d.casefold() == low for d in sibling_dirs)


MONTH_COLLAGE_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])\.jpe?g$", re.IGNORECASE)


def is_generated_collage(filename: str, sibling_dirs=()) -> bool:
    """Collage output that is never a capture: folder.jpg, a monthly collage ("2023.12.jpg") and a trip collage
    ("<trip folder name>.jpg" next to its folder). One rule for every module that walks folders."""
    return (filename.lower() == "folder.jpg" or bool(MONTH_COLLAGE_RE.match(filename))
            or is_trip_collage(filename, sibling_dirs))
