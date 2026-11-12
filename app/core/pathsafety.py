"""
pathsafety.py — shared path-containment checks used by every module that browses the NAS:
the MEDIA_ROOT safety fence, "path must be the selected Basis-Ordner itself or a descendant
of it" enforcement, and the Pfad field's sub-folder suggestions. Factored out of
timestamp_tool/router.py (which had its own copies) so the GPS Sidecar module -- and any
future module -- gets the exact same guarantees for free instead of re-implementing them
slightly differently each time.
"""
from __future__ import annotations

import os

from fastapi import HTTPException

from . import settings

# Optional safety fence: if set, browsing/thumbnails/execution are restricted to this root
# (matches the maintainer's NAS layout, e.g. /mnt/NAS). Unset = no restriction (single-user homelab
# tool behind his own reverse proxy).
MEDIA_ROOT = os.environ.get("MEDIA_ROOT", "").rstrip("/") or None


def check_allowed(path: str) -> None:
    if MEDIA_ROOT is None:
        return
    real = os.path.realpath(path)
    root = os.path.realpath(MEDIA_ROOT)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail=f"Pfad außerhalb von MEDIA_ROOT ({MEDIA_ROOT})")


def check_within_root(path: str, root_path: str) -> None:
    """The browse path must be the selected Basis-Ordner itself or a sub-folder of it --
    never shallower (that's also how the file-type filter is derived, see root_for_path)."""
    if not root_path:
        return
    real = os.path.realpath(path)
    root = os.path.realpath(root_path)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(
            status_code=400,
            detail=f"Pfad muss innerhalb von '{root_path}' liegen (gleiche oder tiefere Ebene).",
        )


def root_for_path(root_path: str):
    """Looks up the configured RootFolder matching `root_path` (by normalized path), so the
    server derives the type filter itself rather than trusting whatever the client submitted."""
    if not root_path:
        return None
    target = os.path.realpath(root_path)
    for rf in settings.load_root_folders():
        if os.path.realpath(rf.path) == target:
            return rf
    return None


def rel_subdir(base_path: str, file_path: str) -> str:
    """The subfolder a file sits in, relative to whatever folder was actually scanned
    (base_path) -- empty string if the file is directly inside base_path itself (Tiefe=0
    territory), or if base_path isn't a directory at all (a single-file scan). Every
    module's file list shows this (when non-empty) next to a row once Tiefe > 0 actually
    found something below the typed folder, so it's clear which subfolder a given file
    came from without exposing its full absolute path."""
    if not os.path.isdir(base_path):
        return ""
    rel = os.path.relpath(os.path.dirname(file_path), base_path)
    return "" if rel == "." else rel


def list_subfolders(path: str, root_path: str, limit: int = 50) -> list[str]:
    """Immediate sub-folders of whatever directory is currently typed (or of root_path
    itself if nothing valid is typed yet), restricted to root_path so the Pfad field stays
    "browsable" without ever escaping the selected Basis-Ordner. Returns full paths."""
    if not root_path:
        return []
    root_real = os.path.realpath(root_path)
    typed = (path or root_path).rstrip("/")

    if os.path.isdir(typed):
        base, prefix = typed, ""
    else:
        base, prefix = os.path.dirname(typed) or root_path, os.path.basename(typed)

    base_real = os.path.realpath(base)
    if base_real != root_real and not base_real.startswith(root_real + os.sep):
        base, prefix = root_path, ""  # typed value escaped the root -> fall back to it

    try:
        check_allowed(base)
        entries = sorted(
            e for e in os.listdir(base)
            if e.lower().startswith(prefix.lower()) and os.path.isdir(os.path.join(base, e))
        )
    except (OSError, HTTPException):
        entries = []

    return [os.path.join(base, e) for e in entries[:limit]]
