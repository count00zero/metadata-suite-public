"""
collagecore.py — ports photo_pile_collage.py (the "trip based collage generator" CLI
script) into plain, return-value functions the web module (modules/collage/router.py)
can call directly, instead of shelling out to the script:

- Six layouts (polaroid default, postcard, cascade, magazine, mosaic, random) -- same
  image prep/rendering code as the CLI, byte-for-byte where it doesn't need to change.
- Trip-folder auto-discovery (discover_trip_folders / has_direct_images / find_images),
  unchanged -- this is what makes the module "trip based": point it at a year/Urlaube
  folder full of trip folders, or straight at one trip folder, and it does the right
  thing either way.
- Metadata copy from a randomly chosen source image onto the finished collage (GPS,
  location, description, date) via one exiftool -TagsFromFile call -- unchanged.
- Collage file mtime restore: the CLI shells out to restore_file_timestamp.py as a
  separate subprocess; this calls core.mtimecore.restore_one() directly instead, since
  that's this app's own in-process port of the exact same logic (see mtimecore.py's own
  docstring) -- one less subprocess, and it was never anything but a plain function call
  away already.
- --photo-root/--video-root mirroring to the Home-Video library: unchanged, but reads
  settings.photo_root()/video_root() instead of argparse args.

Deliberately NOT ported as subprocess calls: nothing here shells out to the collage
script itself or to restore_file_timestamp.py -- every piece of logic that script needed
is either reused directly from this app's own core (mtimecore) or ported inline below.
"""
from __future__ import annotations

from . import skipdirs
import hashlib
import json
import re
import math
import os
import random
import shutil
import subprocess
from . import exifpool
import sys
from dataclasses import dataclass, asdict, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import mtimecore

# These are the maintainer's own trusted photos (including large panoramas/stitched shots from
# action cams), so disable Pillow's decompression-bomb guard instead of skipping
# oversized images -- same as the CLI script.
Image.MAX_IMAGE_PIXELS = None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
EXIFTOOL_TIMEOUT = 60

AVAILABLE_LAYOUTS = ["polaroid", "postcard", "cascade", "magazine", "mosaic"]
ALL_LAYOUT_CHOICES = AVAILABLE_LAYOUTS + ["random"]


# ---------------------------------------------------------------------------
# Trip-folder discovery -- unchanged from the CLI script.
# ---------------------------------------------------------------------------

YEAR_FOLDER_RE = re.compile(r"^(19|20)\d\d$")
MONTH_FOLDER_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])$")


def find_images(folder: str) -> list[str]:
    """Find every image anywhere under `folder`, at any depth -- not just directly
    inside it. Trip folders sometimes have their own nested subfolders (e.g. "... -
    Bilder Anna"), and those extra photos should still be candidates for that trip's
    collage rather than getting silently ignored. Skips folder.jpg specifically, since
    that's this module's own generated thumbnail (copied into the trip folder itself by
    generate_trip_collage) rather than a source photo -- otherwise a trip folder's own
    previous collage would end up as one of the inputs for its next one."""
    files = []
    for root, _dirs, filenames in os.walk(folder):
        sibling_dirs = list(_dirs)
        skipdirs.prune(_dirs)
        for fn in filenames:
            if skipdirs.is_generated_collage(fn, sibling_dirs):
                continue            # folder.jpg, monthly collage ("2023.12.jpg"), trip collage: never a source photo
            if os.path.splitext(fn)[1].lower() in IMAGE_EXTENSIONS:
                files.append(os.path.join(root, fn))
    return sorted(set(files))


def has_direct_images(folder: str) -> bool:
    """Quick check: does `folder` contain any image files directly inside it (not
    counting images that only exist in its subfolders)? Ignores folder.jpg and any
    "<subfolder-name>.jpg" that matches one of this folder's own subfolders, since those
    are almost certainly this module's own previously generated collage outputs sitting
    in a year folder -- without this, a year folder would stop being auto-expanded into
    its trip folders after the very first run that populated it with outputs."""
    try:
        with os.scandir(folder) as it:
            entries = list(it)
    except OSError:
        return False

    sibling_dir_names = [e.name for e in entries if e.is_dir()]

    for entry in entries:
        if not entry.is_file() or skipdirs.is_generated_collage(entry.name, sibling_dir_names):
            continue
        if os.path.splitext(entry.name)[1].lower() in IMAGE_EXTENSIONS:
            return True
    return False


def discover_trip_folders(input_dir: str) -> list[str]:
    """List the folders to each generate one collage for, automatically handling an
    extra "year folder" level of grouping:

    For every immediate subfolder of input_dir, if it has images directly inside it (or,
    per find_images, anywhere below it), it's treated as a trip folder itself. But if it
    has NO images directly inside it and its own contents are just further subfolders,
    it's treated as a grouping/year folder instead, and ITS subfolders are used as the
    trip folders in its place. This lets input_dir be a year folder (.../2026/) full of
    trip folders, or a level above that (.../Urlaube/) full of year folders full of trip
    folders, without needing a separate flag or a fixed depth."""
    trip_folders = []
    year_mode = bool(YEAR_FOLDER_RE.match(os.path.basename(os.path.normpath(input_dir))))
    for entry in sorted(os.scandir(input_dir), key=lambda e: e.name.lower()):
        if not entry.is_dir() or skipdirs.is_skipped_dir(entry.name):
            continue
        if year_mode or MONTH_FOLDER_RE.match(entry.name):
            # A year folder (or a "JJJJ.MM" month folder anywhere): every direct subfolder is ONE unit. The collage is
            # written next to it (= in the year folder), its photos are collected recursively -- never one collage per
            # day/sub-subfolder inside it.
            if find_images(entry.path):
                trip_folders.append(entry.path)
            continue
        if has_direct_images(entry.path):
            trip_folders.append(entry.path)
            continue

        sub_dirs = sorted(
            (e for e in os.scandir(entry.path) if e.is_dir() and not skipdirs.is_skipped_dir(e.name)),
            key=lambda e: e.name.lower(),
        )
        if sub_dirs:
            trip_folders.extend(d.path for d in sub_dirs)
        else:
            trip_folders.append(entry.path)
    return trip_folders


@dataclass
class TripFolder:
    path: str
    name: str
    image_count: int
    output_path: str
    already_exists: bool


def discover_trips(input_dir: str) -> list[TripFolder]:
    """Phase 1 for the web module: figure out which folder(s) a run against `input_dir`
    would generate one collage each for, and report what's already there -- mirrors the
    CLI's own --recursive fallback exactly (see photo_pile_collage.py's main()): if
    discover_trip_folders() finds real trip folders below input_dir, use those; if it
    finds nothing at all, fall back to treating input_dir itself as the one trip folder,
    exactly like the CLI does when pointed directly at a trip folder without
    --recursive. Cheap (no image decoding, just os.walk/os.scandir), so this is safe to
    run on every "Collagen suchen" click without a background job -- though the caller may
    still want one for a very large library."""
    trip_paths = discover_trip_folders(input_dir)
    if not trip_paths:
        trip_paths = [input_dir] if find_images(input_dir) else []

    out = []
    for folder in trip_paths:
        folder = os.path.normpath(folder)
        name = os.path.basename(folder)
        out_dir = os.path.dirname(folder) or "."
        output_path = os.path.join(out_dir, f"{name}.jpg")
        out.append(TripFolder(
            path=folder, name=name, image_count=len(find_images(folder)),
            output_path=output_path, already_exists=os.path.exists(output_path),
        ))
    return out


def select_images(files: list[str], count: Optional[int], seed: Optional[int] = None) -> list[str]:
    """Pick `count` images at random out of `files`. If count is None or >= len(files),
    just return all files shuffled."""
    if seed is not None:
        random.seed(seed)

    shuffled = files[:]
    random.shuffle(shuffled)

    if not count or count >= len(shuffled):
        return shuffled
    return shuffled[:count]


# ---------------------------------------------------------------------------
# Per-tile image prep -- unchanged from the CLI script.
# ---------------------------------------------------------------------------

def load_and_prep(path, target_short_side, border):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    scale = target_short_side / min(img.size)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(new_size, Image.LANCZOS)

    img = ImageOps.expand(img, border=border, fill="white")
    return img


def load_and_prep_postcard(path, target_short_side, border):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    scale = target_short_side / min(img.size)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(new_size, Image.LANCZOS)

    img = ImageOps.expand(img, border=max(1, border // 5), fill=(60, 50, 40))
    img = ImageOps.expand(img, border=border, fill=(0, 0, 0))
    return img


def make_torn_edge_mask(size, roughness=10):
    w, h = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)

    points = []
    steps_w = max(3, w // 25)
    steps_h = max(3, h // 25)

    for i in range(steps_w + 1):
        points.append((w * i / steps_w, random.uniform(0, roughness)))
    for i in range(1, steps_h + 1):
        points.append((w - random.uniform(0, roughness), h * i / steps_h))
    for i in range(1, steps_w + 1):
        points.append((w - w * i / steps_w, h - random.uniform(0, roughness)))
    for i in range(1, steps_h):
        points.append((random.uniform(0, roughness), h - h * i / steps_h))

    draw.polygon(points, fill=255)
    return mask


def load_and_prep_magazine(path, target_short_side, border=6):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    scale = target_short_side / min(img.size)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(new_size, Image.LANCZOS)

    img = img.convert("RGBA")
    mask = make_torn_edge_mask(img.size)
    img.putalpha(mask)

    pad = max(2, border)
    padded_size = (img.width + 2 * pad, img.height + 2 * pad)
    dilate_size = pad * 2 + 1
    if dilate_size % 2 == 0:
        dilate_size += 1

    padded_mask = Image.new("L", padded_size, 0)
    padded_mask.paste(mask, (pad, pad))
    dilated_mask = padded_mask.filter(ImageFilter.MaxFilter(dilate_size))

    backing = Image.new("RGBA", padded_size, (255, 255, 255, 255))
    backing.putalpha(dilated_mask)
    backing.paste(img, (pad, pad), img)
    return backing


SCATTERED_STYLE_CONFIG = {
    "polaroid": {"shadow": False, "overlap": 1.15, "fallback_cell": 220},
    "postcard": {"shadow": True, "overlap": 1.15, "fallback_cell": 320},
    "cascade": {"shadow": True, "overlap": 1.45, "fallback_cell": 260},
    "magazine": {"shadow": True, "overlap": 1.15, "fallback_cell": 240},
}

ROTATION_DEFAULTS = {"postcard": 8, "cascade": 12, "magazine": 20, "polaroid": 25}


def make_drop_shadow(tile, blur_radius=5, max_alpha=100):
    alpha_channel = tile.split()[-1]
    faded_alpha = alpha_channel.point(lambda a: min(a, max_alpha))
    shadow = Image.new("RGBA", tile.size, (0, 0, 0, 0))
    shadow.putalpha(faded_alpha)
    return shadow.filter(ImageFilter.GaussianBlur(blur_radius))


def compute_scattered_cell_size(num_images, canvas_w, canvas_h, fallback=220):
    if num_images <= 0:
        return fallback

    ideal = math.sqrt((canvas_w * canvas_h) / num_images)
    cell = max(60, int(ideal))
    while cell > 60:
        cols = max(1, canvas_w // cell)
        rows = max(1, canvas_h // cell)
        if cols * rows >= num_images:
            return cell
        cell -= 2
    return 60


def render_scattered_canvas(files, canvas_w, canvas_h, cell, border, max_rotation, jitter, style="polaroid", size_variation=0.0,
                            stats=None):
    """Scattered, overlapping, randomly-rotated pile layout shared by 'polaroid',
    'postcard', 'cascade' and 'magazine'. Every placement is clamped so the whole photo
    stays inside the canvas -- no image is ever cut off at the edges.
    `stats` (optional dict) receives `placed` (pasted photos) and `unreadable` (files that could not be opened)."""
    if stats is None:
        stats = {}
    stats["placed"] = stats["unreadable"] = 0
    cfg = SCATTERED_STYLE_CONFIG.get(style, SCATTERED_STYLE_CONFIG["polaroid"])
    canvas = Image.new("RGB", (canvas_w, canvas_h), "black")

    cols = max(1, canvas_w // cell)
    rows = max(1, canvas_h // cell)

    centers = []
    for gy in range(rows):
        for gx in range(cols):
            cx = gx * cell + cell // 2 + random.randint(-int(cell * jitter), int(cell * jitter))
            cy = gy * cell + cell // 2 + random.randint(-int(cell * jitter), int(cell * jitter))
            centers.append((cx, cy))

    random.shuffle(centers)

    usable = files[:]
    random.shuffle(usable)

    num_tiles = min(len(centers), len(usable))

    for i in range(num_tiles):
        cx, cy = centers[i]
        src = usable[i]

        scale_mult = 1.0
        if size_variation:
            scale_mult = random.uniform(1 - size_variation, 1 + size_variation)
        target_short_side = max(20, int(cell * cfg["overlap"] * scale_mult))

        try:
            if style == "postcard":
                img = load_and_prep_postcard(src, target_short_side=target_short_side, border=border)
            elif style == "magazine":
                img = load_and_prep_magazine(src, target_short_side=target_short_side, border=border)
            else:
                img = load_and_prep(src, target_short_side=target_short_side, border=border)
        except Exception:
            stats["unreadable"] += 1
            continue

        img = img.convert("RGBA")
        angle = random.uniform(-max_rotation, max_rotation)
        img = img.rotate(angle, expand=True, resample=Image.BICUBIC)

        max_x = max(0, canvas_w - img.width)
        max_y = max(0, canvas_h - img.height)
        x = max(0, min(cx - img.width // 2, max_x))
        y = max(0, min(cy - img.height // 2, max_y))

        if cfg["shadow"]:
            shadow = make_drop_shadow(img)
            canvas.paste(shadow, (x + 6, y + 6), shadow)

        canvas.paste(img, (x, y), img)
        stats["placed"] += 1

    return canvas


def get_display_size(path):
    with Image.open(path) as img:
        w, h = img.size
        try:
            orientation = img.getexif().get(0x0112, 1)
        except Exception:
            orientation = 1
        if orientation in (5, 6, 7, 8):
            w, h = h, w
        return w, h


def simulate_mosaic_total_height(aspect_ratios, canvas_w, row_height, spacing, border_overhead):
    n = len(aspect_ratios)
    idx = 0
    total_height = 0.0
    while idx < n:
        row_natural_width = 0.0
        row_count = 0
        ran_out = False
        while row_natural_width < canvas_w:
            if idx >= n:
                ran_out = True
                break
            ar = aspect_ratios[idx]
            idx += 1
            tile_w = int(row_height * ar) + border_overhead
            row_natural_width += tile_w + spacing
            row_count += 1
            if row_count > 200:
                break
        if row_count == 0:
            break
        row_natural_width -= spacing
        if ran_out:
            total_height += row_height
        else:
            scale = canvas_w / row_natural_width if row_natural_width > 0 else 1.0
            scaled_row_height = max(1, int(row_height * scale))
            total_height += scaled_row_height + spacing
    return total_height


def compute_mosaic_layout(files, canvas_w, canvas_h, border=12, spacing=6, fallback_h=220):
    items = []
    for f in files:
        try:
            w, h = get_display_size(f)
            if w > 0 and h > 0:
                items.append((f, w / h))
        except Exception:
            continue

    if not items:
        return fallback_h, files

    border_overhead = 2 * border
    max_h = int(max(canvas_w, canvas_h) * 1.5)

    eval_budget = 4_000_000
    num_shuffles = max(4, min(25, eval_budget // (max(1, max_h) * max(1, len(items)))))

    best_fill = -1.0
    best_h = fallback_h
    best_order = [p for p, _ in items]

    for _ in range(num_shuffles):
        random.shuffle(items)
        ars = [ar for _, ar in items]
        for h in range(20, max_h):
            total = simulate_mosaic_total_height(ars, canvas_w, h, spacing, border_overhead)
            if total <= canvas_h and total > best_fill:
                best_fill = total
                best_h = h
                best_order = [p for p, _ in items]

    return best_h, best_order


def render_mosaic_canvas(files, canvas_w, canvas_h, border, spacing=6, row_height=None, size_variation=0.0, stats=None):
    if stats is None:
        stats = {}
    stats["placed"] = stats["unreadable"] = 0
    if row_height is None:
        row_height, pool = compute_mosaic_layout(files, canvas_w, canvas_h, border=border, spacing=spacing)
    else:
        pool = files[:]
        random.shuffle(pool)

    base_row_height = row_height
    canvas = Image.new("RGB", (canvas_w, canvas_h), "black")

    idx = 0
    total = len(pool)

    def next_image():
        nonlocal idx
        while idx < total:
            path = pool[idx]
            idx += 1
            try:
                img = Image.open(path)
                img = ImageOps.exif_transpose(img).convert("RGB")
                return img
            except Exception:
                stats["unreadable"] += 1
                continue
        return None

    def place_row_fitted(row_tiles, y):
        if not row_tiles:
            return y

        natural_width = sum(t.width for t in row_tiles) + spacing * (len(row_tiles) - 1)
        scale_factor = canvas_w / natural_width if natural_width > 0 else 1.0
        row_height_ref = row_tiles[0].height
        scaled_h = row_height_ref * scale_factor

        available_h = canvas_h - y
        extra_shrink = min(1.0, available_h / scaled_h) if scaled_h > 0 else 1.0
        total_scale = scale_factor * extra_shrink

        x = 0
        bottom = y
        for tile in row_tiles:
            w = max(1, int(tile.width * total_scale))
            h = max(1, int(tile.height * total_scale))
            tile_to_place = tile.resize((w, h), Image.LANCZOS)
            if x + w > canvas_w or y + h > canvas_h:
                break
            canvas.paste(tile_to_place, (x, y))
            stats["placed"] += 1
            bottom = max(bottom, y + h)
            x += w + spacing
        return bottom

    y = 0
    content_bottom = 0
    while y < canvas_h:
        row_tiles = []
        row_width = 0
        ran_out = False
        if size_variation:
            row_height = max(20, int(base_row_height * random.uniform(1 - size_variation, 1 + size_variation)))
        else:
            row_height = base_row_height
        while row_width < canvas_w:
            img = next_image()
            if img is None:
                ran_out = True
                break
            scale = row_height / img.height
            new_w = max(1, int(img.width * scale))
            resized = img.resize((new_w, row_height), Image.LANCZOS)
            tile = ImageOps.expand(resized, border=border, fill="black")
            row_tiles.append(tile)
            row_width += tile.width + spacing
            if len(row_tiles) > 200:
                break

        if not row_tiles:
            break

        if ran_out:
            content_bottom = max(content_bottom, place_row_fitted(row_tiles, y))
            break

        natural_width = sum(t.width for t in row_tiles) + spacing * (len(row_tiles) - 1)
        scale_factor = canvas_w / natural_width if natural_width > 0 else 1.0
        scaled_row_height = max(1, int(row_height * scale_factor))

        if y + scaled_row_height > canvas_h:
            content_bottom = max(content_bottom, place_row_fitted(row_tiles, y))
            break

        x = 0
        for tile in row_tiles:
            w_scaled = max(1, int(tile.width * scale_factor))
            tile_scaled = tile.resize((w_scaled, scaled_row_height), Image.LANCZOS)
            canvas.paste(tile_scaled, (x, y))
            stats["placed"] += 1
            x += tile_scaled.width + spacing
        content_bottom = y + scaled_row_height
        y += scaled_row_height + spacing

        if idx >= total:
            break

    if 0 < content_bottom < canvas_h:
        canvas = canvas.crop((0, 0, canvas_w, content_bottom)).resize((canvas_w, canvas_h), Image.LANCZOS)

    return canvas


# ---------------------------------------------------------------------------
# Metadata copy + timestamp restore.
# ---------------------------------------------------------------------------

_OFFSET_IN_DATE = re.compile(r"([+-]\d{2}:\d{2})\s*$")


def _probe_gps_date(paths: list[str]) -> dict:
    """{path: (has_gps, date)} for photos AND their .xmp sidecars in ONE exiftool call per chunk. A photo counts when
    the image itself or its sidecar carries real coordinates / a capture date (the GPS Sidecar module writes to the .xmp
    and does not embed into the image by default, so the image alone often shows nothing)."""
    out: dict = {}
    for i in range(0, len(paths), 100):
        chunk = paths[i:i + 100]
        targets = [x for p in chunk for x in (p, p + ".xmp") if os.path.exists(x)]
        try:
            res = exifpool.run(["exiftool", "-m", "-j", "-n", "-GPSLatitude", "-GPSLongitude", "-DateTimeOriginal", "-CreateDate", *targets],
                               capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
            rows = json.loads(res.stdout or "[]")
        except Exception:                       # noqa: BLE001  probing is an optimisation, never fatal
            continue
        have: dict = {}
        for r in rows:
            src = r.get("SourceFile", "")
            base = src[:-4] if src.endswith(".xmp") else src
            lat, lon = r.get("GPSLatitude"), r.get("GPSLongitude")
            try:
                gps = lat not in (None, "") and lon not in (None, "") and not (float(lat) == 0 and float(lon) == 0)
            except (TypeError, ValueError):
                gps = False
            date = r.get("DateTimeOriginal") or r.get("CreateDate") or ""
            g0, d0 = have.get(base, (False, ""))
            have[base] = (g0 or gps, d0 or str(date))
        out.update(have)
    return out


def pick_metadata_source(files: list[str], max_probe: int = 200) -> str:
    """The photo whose GPS + capture date the collage inherits. Random like before, but only among photos that HAVE both
    (in the image or its .xmp sidecar), so a collage is not left without position/date because the dice picked a bare photo.
    No photo has both -> one with GPS, else with a date, else any."""
    if not files:
        return ""
    sample = files if len(files) <= max_probe else random.sample(files, max_probe)
    info = _probe_gps_date(sample)
    for ok in (lambda g, d: g and d, lambda g, d: g, lambda g, d: bool(d)):
        good = [p for p in sample if ok(*info.get(p, (False, "")))]
        if good:
            return random.choice(good)
    return random.choice(files)


def _fill_from_sidecar(source_path: str, output_path: str) -> None:
    """GPS / capture date that live only in the source's .xmp sidecar (not in the image) are added to the collage.
    `-wm cg` = create missing tags only, never overwrite what the image copy already brought. Best effort."""
    xmp = source_path + ".xmp"
    if not os.path.isfile(xmp):
        return
    try:
        exifpool.run(["exiftool", "-m", "-q", "-q", "-overwrite_original", "-wm", "cg", "-TagsFromFile", xmp,
                      "-GPSLatitude", "-GPSLongitude", "-GPSAltitude", "-DateTimeOriginal", output_path],
                     capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
        got = exifpool.run(["exiftool", "-m", "-s3", "-DateTimeOriginal", xmp], capture_output=True, text=True,
                           timeout=EXIFTOOL_TIMEOUT)
        m = _OFFSET_IN_DATE.search((got.stdout or "").strip())
        if m:       # the sidecar date carries the UTC offset; EXIF DateTimeOriginal cannot -> OffsetTimeOriginal
            exifpool.run(["exiftool", "-m", "-q", "-q", "-overwrite_original", "-wm", "cg", f"-OffsetTimeOriginal={m.group(1)}", output_path],
                         capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT)
    except Exception:           # noqa: BLE001
        pass


def copy_metadata_from_source(source_path: str, output_path: str) -> tuple[bool, str]:
    """Copy GPS coordinates, GPS date/time, location fields (Country/State/City/Street),
    description, and photo date straight from `source_path` onto `output_path` via
    exiftool -- no reverse geocoding, just a direct metadata copy. Unchanged from the CLI
    script, besides returning (ok, message) instead of printing."""
    if not shutil.which("exiftool"):
        return False, "exiftool nicht gefunden -- Metadaten-Kopie übersprungen."

    args = [
        "exiftool",
        "-overwrite_original",
        "-q", "-q",
        "-TagsFromFile", source_path,
        "-GPS:all",
        "-IPTC:all",
        "-XMP-iptcExt:all",
        "-XMP-photoshop:all",
        "-XMP-dc:Description",
        "-XMP-dc:Title",
        "-EXIF:ImageDescription",
        "-EXIF:DateTimeOriginal",
        "-EXIF:DateTimeDigitized",
        "-EXIF:OffsetTimeOriginal",
        output_path,
    ]
    try:
        res = exifpool.run(args, timeout=EXIFTOOL_TIMEOUT)
        if res.returncode != 0:
            return False, f"exiftool-Fehler: {res.stderr or res.returncode}"
        _fill_from_sidecar(source_path, output_path)
        return True, f"Metadaten kopiert von: {source_path}"
    except subprocess.TimeoutExpired:
        return False, f"exiftool-Timeout ({EXIFTOOL_TIMEOUT}s)"


# ---------------------------------------------------------------------------
# Home-Video mirroring -- unchanged from the CLI script, besides taking explicit
# photo_root/video_root/force parameters instead of an argparse args object.
# ---------------------------------------------------------------------------

def mirror_path_to_video(path: str, photo_root: str, video_root: str) -> Optional[str]:
    """Map a path that lives under photo_root to the corresponding path under
    video_root, preserving everything after photo_root unchanged. Returns None if `path`
    isn't actually under photo_root, or if either root isn't configured."""
    if not photo_root or not video_root:
        return None
    try:
        rel = os.path.relpath(os.path.normpath(path), os.path.normpath(photo_root))
    except ValueError:
        return None
    if rel == os.curdir or rel.startswith(os.pardir):
        return None
    return os.path.normpath(os.path.join(video_root, rel))


def mirror_file_to_video(path: str, photo_root: str, video_root: str, force: bool, label: str) -> tuple[str, str]:
    """Copy `path` to its corresponding location under video_root, if photo_root/
    video_root are configured, `path` is actually under photo_root, AND the destination
    directory already exists on the video side. Never creates folders on the video side
    and never copies into a year/trip folder that doesn't already exist there. Returns
    (dest_path, note) -- dest_path is "" if mirroring didn't apply/happen; note explains
    why when it didn't."""
    dest_path = mirror_path_to_video(path, photo_root, video_root)
    if dest_path is None:
        return "", ""
    dest_dir = os.path.dirname(dest_path)
    if not os.path.isdir(dest_dir):
        return "", f"{dest_dir} existiert nicht im Home-Video-Baum (wird nicht angelegt)"
    if os.path.exists(dest_path) and not force:
        return "", f"{dest_path} existiert bereits (force nicht aktiv)"
    try:
        shutil.copy2(path, dest_path)
        return dest_path, ""
    except Exception as e:
        return "", f"Konnte {label} nicht nach {dest_path} kopieren: {e}"


# ---------------------------------------------------------------------------
# Seed/layout resolution -- unchanged from the CLI script.
# ---------------------------------------------------------------------------

def derive_folder_seed(base_seed: Optional[int], folder_name: str) -> Optional[int]:
    """Derive a per-folder seed from a base seed and the folder's name, so --layout
    random picks a different (but reproducible, given the same base seed) layout for
    each trip folder, instead of every folder getting the identical choice. Returns None
    if base_seed is None, so "no seed configured" still means "fully random each run"."""
    if base_seed is None:
        return None
    digest = hashlib.md5(folder_name.encode("utf-8")).hexdigest()
    return (base_seed + int(digest, 16)) % (2**31)


def resolve_layout(layout_arg: str) -> str:
    if layout_arg != "random":
        return layout_arg
    return random.choice(AVAILABLE_LAYOUTS)


# ---------------------------------------------------------------------------
# Settings + result types for the web module.
# ---------------------------------------------------------------------------

@dataclass
class CollageSettings:
    layout: str = "random"
    width: int = 4096
    height: int = 2304
    cell: Optional[int] = None
    border: int = 6
    rotation: Optional[float] = 25
    jitter: float = 0.55
    seed: Optional[int] = None
    count: Optional[int] = 139
    spacing: int = 0
    tile_size: Optional[int] = None
    size_variation: float = 0.35
    force: bool = False
    sync_folder_jpg: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CollageResult:
    trip_path: str
    trip_name: str
    status: str                   # created | skipped_exists | no_images | error
    output_path: str = ""
    layout_used: str = ""
    image_count: int = 0
    placed_count: int = 0         # photos actually pasted onto the collage
    skipped_count: int = 0        # selected photos that did not make it (no room, or unreadable)
    unreadable_count: int = 0     # part of skipped_count: files that could not be opened
    error: str = ""
    folder_jpg_copied: bool = False
    mirrored_path: str = ""
    mirror_note: str = ""
    metadata_source: str = ""
    metadata_note: str = ""
    mtime_restored: bool = False
    # A DOM-safe id for this result's row, derived from trip_path -- NOT built with
    # plain character substitution (replace '/'/' '/'.' with '_') like an earlier version
    # did, because a trip name can contain other characters (e.g. "&") that are invalid
    # inside an unescaped CSS id selector. htmx's hx-target="#<id>" calls
    # querySelector(), which throws on such a selector, silently breaking the
    # "Neu erzeugen" button for any trip whose name needs it. A hash of trip_path is
    # always a valid id/selector, whatever characters the trip name contains.
    row_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.row_id = "collage-row-" + hashlib.md5(self.trip_path.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


def render_collage_canvas(files: list[str], settings: CollageSettings, chosen_layout: str, stats: Optional[dict] = None):
    """Build the actual canvas image for one collage, given the already-selected `files`
    and an already-resolved (non-"random") `chosen_layout`."""
    if chosen_layout == "mosaic":
        return render_mosaic_canvas(
            files, settings.width, settings.height, border=settings.border,
            spacing=settings.spacing, row_height=settings.tile_size,
            size_variation=settings.size_variation, stats=stats,
        )
    if chosen_layout in SCATTERED_STYLE_CONFIG:
        max_rotation = settings.rotation
        if max_rotation is None:
            max_rotation = ROTATION_DEFAULTS.get(chosen_layout, 16)
        effective_cell = settings.cell
        if effective_cell is None:
            fallback = SCATTERED_STYLE_CONFIG[chosen_layout]["fallback_cell"]
            effective_cell = compute_scattered_cell_size(len(files), settings.width, settings.height, fallback=fallback)
        return render_scattered_canvas(
            files, settings.width, settings.height, cell=effective_cell, border=settings.border,
            max_rotation=max_rotation, jitter=settings.jitter, style=chosen_layout,
            size_variation=settings.size_variation, stats=stats,
        )
    raise ValueError(f"Unbekanntes Layout: {chosen_layout!r}")


def generate_trip_collage(
    trip: TripFolder,
    settings: CollageSettings,
    photo_root: str,
    video_root: str,
) -> CollageResult:
    """Build one collage for one trip folder -- the web-module equivalent of the CLI's
    process_folder()+build_collage() combined. Writes <trip_name>.jpg next to the trip
    folder (trip.output_path), copies it in as folder.jpg (both next to the trip folder
    itself and into the trip folder, exactly matching the CLI's own two copies), mirrors
    it to Home-Video when configured, copies GPS/location/description/date metadata from
    a randomly chosen source image, and restores the collage file's own mtime to match
    that source's capture date (via core.mtimecore.restore_one(), in-process -- the CLI's
    equivalent subprocess-calls restore_file_timestamp.py for this). Never raises for an
    expected condition (no images, already exists) -- only for a genuine bug."""
    folder = trip.path
    out_dir = os.path.dirname(trip.output_path) or "."
    output_path = trip.output_path
    result = CollageResult(trip_path=folder, trip_name=trip.name, status="error", output_path=output_path)

    if os.path.exists(output_path) and not settings.force:
        result.status = "skipped_exists"
        if settings.sync_folder_jpg:
            try:
                shutil.copy2(output_path, os.path.join(folder, "folder.jpg"))
                result.folder_jpg_copied = True
            except Exception as e:
                result.error = f"Konnte vorhandene Collage nicht als folder.jpg übernehmen: {e}"
            dest, note = mirror_file_to_video(output_path, photo_root, video_root, settings.force, "Collage")
            result.mirrored_path, result.mirror_note = dest, note
        return result

    all_files = find_images(folder)
    if not all_files:
        result.status = "no_images"
        return result

    if settings.layout == "random":
        effective_seed = derive_folder_seed(settings.seed, trip.name)
    else:
        effective_seed = settings.seed
    if effective_seed is not None:
        random.seed(effective_seed)
    chosen_layout = resolve_layout(settings.layout)

    files = select_images(all_files, settings.count, seed=effective_seed)
    result.image_count = len(files)
    result.layout_used = chosen_layout

    try:
        stats: dict = {}
        canvas = render_collage_canvas(files, settings, chosen_layout, stats)
        result.placed_count = int(stats.get("placed", 0))
        result.unreadable_count = int(stats.get("unreadable", 0))
        result.skipped_count = max(0, len(files) - result.placed_count)
        canvas.save(output_path, "JPEG", quality=92)
    except Exception as e:
        result.status = "error"
        result.error = f"Rendern/Speichern fehlgeschlagen: {e}"
        return result

    result.status = "created"

    if files:
        metadata_source = pick_metadata_source(files)
        copied, note = copy_metadata_from_source(metadata_source, output_path)
        result.metadata_source = metadata_source if copied else ""
        result.metadata_note = note
        if copied:
            restore = mtimecore.restore_one(output_path)
            result.mtime_restored = restore.changed

    dest, note = mirror_file_to_video(output_path, photo_root, video_root, settings.force, "Collage")
    result.mirrored_path, result.mirror_note = dest, note

    try:
        shutil.copy2(output_path, os.path.join(out_dir, "folder.jpg"))
        shutil.copy2(output_path, os.path.join(folder, "folder.jpg"))
        result.folder_jpg_copied = True
    except Exception as e:
        result.error = f"Collage erstellt, aber folder.jpg konnte nicht kopiert werden: {e}"

    return result
