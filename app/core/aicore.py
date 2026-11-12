"""
aicore.py — AI-generated German scene-description core, ported from
Set-GPS-Data-via-traccar-and-dawarich.sh's -AIM/-AIF passes and the
generate_ai_description.py helper script it shells out to.

Scope of this port: given an image (or a video, via an extracted frame) and
whatever location context is already on disk, generate a short German scene
description via Ollama and/or Google Gemini (same priority-list fallback as
the CLI: AI_PROVIDER="ollama,google" tries Ollama first, falls through to
Gemini on failure), with the exact same prompts and post-processing the CLI
used, then write it to the .xmp sidecar (always) and, for images, directly
into the file too (XMP-dc:Description + IPTC:Caption-Abstract, plus
EXIF:ImageDescription when that tag already exists and would otherwise keep
showing something else, e.g. the camera's own filename -- see
image_description_needs_sync). Videos only ever get the sidecar, same
convention as every other write in this app.

One deliberate difference from the CLI: the CLI's -AIM pass skips a video
entirely if it has no .xmp sidecar yet ("run the normal geotagging pass
first"). This module instead creates the sidecar on demand at write time via
geocore.ensure_xmp_sidecar(), the same way the GPS Sidecar module already
does -- there's no reason this module should have a stricter prerequisite
than that one when the underlying exiftool operation is identical.

Not ported here: AI_FAILURE_LOG_DIR file logging (failures are shown
directly in the web UI's result list instead, which is more immediate than
tailing a log file), and the garbled-description *repair* framing from
run_ai_fill_missing_pass's log lines (the detection itself,
ai_description_is_garbled(), is ported and still drives the same "treat a
garbled description as missing" behavior).
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from . import exifpool
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

FFMPEG_TIMEOUT = 30
AI_HTTP_TIMEOUT = 60
EXIFTOOL_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Existing-description detection: whether a file already has a usable AI
# description (so it can be skipped in fill-missing mode) or not (missing,
# or present but garbled -- same exact set of corruption signatures the CLI
# checked for, from a historical log-leak bug where log output or an error
# message ended up written into XMP-dc:Description instead of a real
# description).
# ---------------------------------------------------------------------------

def ai_description_is_garbled(text: str) -> bool:
    """Port of ai_description_is_garbled(): a long run of the same repeated
    character, an embedded log timestamp, or one of a handful of literal
    strings that only ever show up when a log line or error message leaked
    into the field instead of a real description."""
    if not text:
        return False
    if re.search(r"(.)\1{14,}", text):
        return True
    if re.search(r"\[\d{4}-\d{2}-\d{2}[ \t]\d{2}:\d{2}:\d{2}\]", text):
        return True
    for marker in ("AI-Description:", "Requesting AI description", "skipping regeneration", "ERROR:"):
        if marker in text:
            return True
    return False


_REFUSAL_RE = re.compile(
    r"^\W*(es tut mir leid|leider (kann|darf|vermag)|ich kann (dieses|das|auf|keine|nicht)|ich bin (nicht in der lage|ein ki)|"
    r"entschuldigung|tut mir leid|als (ki|künstliche|sprachmodell)|i'?m sorry|i am sorry|i cannot|i can'?t|sorry,|as an ai|"
    r"unable to (describe|view|see)|i (do not|don'?t) see)", re.IGNORECASE)
_MARKUP_RE = re.compile(r"(<\/?(think|thinking|answer|response|image|description)\b|```|^\s*[\[{]\s*\"|^\s*(\*\*|#+\s)|"
                        r"^\s*(beschreibung|description|bildbeschreibung|antwort|answer)\s*:)", re.IGNORECASE)
_LOOP_RE = re.compile(r"\b(\w+(?:\s+\w+){0,3}?)(?:[\s,.;:–-]+\1\b){3,}", re.IGNORECASE)
_FOREIGN_SCRIPT_RE = re.compile(r"[\u0400-\u04FF\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF\u0600-\u06FF]")
_EN_WORDS = {"the", "and", "with", "of", "in", "is", "are", "a", "an", "on", "to", "this", "image", "shows", "there", "that", "his", "her", "while"}
_DE_WORDS = {"der", "die", "das", "und", "mit", "ein", "eine", "einem", "einer", "im", "ist", "sind", "auf", "von", "zu", "den", "dem", "im", "vor", "hinter", "sieht", "zeigt"}


def ai_description_problem(text: str, strict: bool = False) -> str:
    """Why a (non-empty) AI description is unusable, or "" if it looks fine. Unambiguous checks always run
    (model refusal, leaked <think>/markdown/JSON, word/phrase repetition loop, foreign script); strict=True adds
    the fuzzier ones (English instead of German, cut off mid-sentence, far too short, almost no distinct words)."""
    t = (text or "").strip()
    if not t:
        return ""
    if ai_description_is_garbled(t):
        return "Log-/Fehlertext oder Zeichen-Wiederholung"
    if _REFUSAL_RE.search(t):
        return "Absage/Entschuldigung des Modells"
    if _MARKUP_RE.search(t):
        return "Markup/JSON/<think> im Text"
    if _LOOP_RE.search(t):
        return "Wort-/Satz-Wiederholung"
    if _FOREIGN_SCRIPT_RE.search(t):
        return "fremde Schrift (kyrillisch/asiatisch/arabisch)"
    if not strict:
        return ""
    words = re.findall(r"[A-Za-zÄÖÜäöüß']+", t)
    low = [w.lower() for w in words]
    if len(t) < 15 or len(words) < 3:
        return "viel zu kurz"
    en, de = sum(w in _EN_WORDS for w in low), sum(w in _DE_WORDS for w in low)
    if en >= 3 and en > de:
        return "Englisch statt Deutsch"
    if len(low) >= 20 and len(set(low)) / len(low) < 0.4:
        return "kaum unterschiedliche Wörter"
    if len(t) >= 60 and t[-1] not in ".!?…\")»“":
        return "mitten im Satz abgeschnitten"
    return ""


def _read_tag(path: str, tag: str, timeout: int = EXIFTOOL_TIMEOUT) -> str:
    try:
        res = exifpool.run(
            ["exiftool", "-m", "-n", "-T", "-api", "LargeFileSupport=1", tag, path],
            capture_output=True, text=True, timeout=timeout,
        )
        val = (res.stdout or "").strip()
        return "" if val in ("", "-") else val
    except Exception:
        return ""


def get_existing_ai_description(file: str, strict: bool = False) -> str:
    """Port of get_existing_ai_description(): prefers the .xmp sidecar
    (authoritative for videos, which never get an embedded description),
    falls back to the file's own embedded XMP-dc:Description. Empty return
    means "needs (re)generating" -- including when a description exists on
    disk but is garbled, so both a normal run and fill-missing mode
    automatically replace a faulty description, not just fill in a missing
    one."""
    xmp_path = file + ".xmp"
    desc = ""
    if os.path.isfile(xmp_path):
        desc = _read_tag(xmp_path, "-XMP-dc:Description")
    if not desc:
        desc = _read_tag(file, "-XMP-dc:Description")
    if desc and ai_description_is_garbled(desc):
        return ""
    if desc and strict and ai_description_problem(desc, strict=True):
        return ""
    return desc


def build_ai_geo_context_from_existing(file: str) -> str:
    """Port of build_ai_geo_context_from_existing(): reads whatever location
    fields (Country/State/City) are already stored for <file> -- .xmp sidecar
    first, falling back to the file itself -- and formats them with the same
    Austria-conditional rule (city only) vs. "Country, State, City" used
    everywhere else in this app, so a description generated from
    already-resolved location data reads the same as one generated live
    during GPS Sidecar's own geotagging pass."""
    xmp_path = file + ".xmp"
    src = xmp_path if os.path.isfile(xmp_path) else file
    try:
        res = exifpool.run(
            ["exiftool", "-m", "-n", "-T", "-api", "LargeFileSupport=1",
             "-XMP-photoshop:Country", "-XMP-photoshop:State", "-XMP-photoshop:City", "-XMP-iptcCore:Location", src],
            capture_output=True, text=True, timeout=EXIFTOOL_TIMEOUT,
        )
        parts = (res.stdout or "").rstrip("\n").split("\t")
    except Exception:
        parts = []

    def _clean(i: int) -> str:
        return parts[i] if len(parts) > i and parts[i] != "-" else ""

    country, state, city, place = _clean(0), _clean(1), _clean(2), _clean(3)
    if country in ("Österreich", "Osterreich", "Austria"):
        return city
    # No city: at sea / in a bay the locality is the specific place (the country field then carries the sea).
    if not city and place and place not in (country, state):
        city = place
    ctx = country
    if state and state != country:
        ctx = f"{ctx}, {state}" if ctx else state
    if city:
        ctx = f"{ctx}, {city}" if ctx else city
    return ctx


# ---------------------------------------------------------------------------
# Frame extraction (video) + resize (image), both via ffmpeg -- identical
# approach to generate_ai_description.py's resize_image() and the bash
# script's own frame-at-1-second-with-fallback logic.
# ---------------------------------------------------------------------------

def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def get_video_thumb_path(video_path: str) -> str:
    """Where this video's persistent thumbnail sidecar lives (or would live) --
    "<stem>-thumb.jpg" next to the video, same naming convention the existing
    NAS toolchain's NFO writer already uses, and the same file
    app/modules/timestamp_tool/router.py's shared /thumb endpoint already looks
    for when showing a video row's thumbnail in every module's file list."""
    return f"{os.path.splitext(video_path)[0]}-thumb.jpg"


def extract_video_frame(video_path: str, save_as_thumb: bool = False) -> Optional[str]:
    """Port of the CLI's frame-extraction step: grabs the frame 1 second in
    (avoids a black/fade-in first frame some cameras start with), falling
    back to the very first frame for clips shorter than that.

    If a "<stem>-thumb.jpg" sidecar already sits next to the video -- from the
    existing NAS toolchain or an NFO Maker run -- that frame is reused as-is,
    no ffmpeg call at all. Otherwise a new frame is extracted into a plain temp
    file and returned; by default it is NOT written to "<stem>-thumb.jpg" (see
    save_as_thumb below) -- generating and persisting a video's real thumbnail
    is the NFO Maker module's job alone (core/nfocore.py's
    generate_poster_fanart_thumb(), which writes poster/fanart/thumb together
    in one ffmpeg call). AI Description used to also persist its own copy here,
    but that just meant every video got frame-extracted twice -- once here,
    once again (unconditionally, overwriting this one) whenever NFO Maker
    later ran -- for no benefit, so it was removed.

    save_as_thumb=True restores the old persist-permanently behavior, kept
    only in case some future caller actually wants that (none currently do --
    aicore.generate_description(), the only call site today, always passes
    False). When True and a NEW frame had to be extracted, it's saved at
    "<stem>-thumb.jpg" at the exact size/quality HomeVideo_NFO_Maker_new.sh
    itself uses for "$ThumbJPG" (quarter of the video's own width, qscale:v 11).

    Returns the frame's path -- the caller must NOT delete it when it matches
    get_video_thumb_path(video_path), since that means it's either a reused
    pre-existing thumb or one just persisted by save_as_thumb=True, both of
    which are meant to stay on disk -- or None if ffmpeg couldn't produce a
    usable frame at all."""
    thumb_path = get_video_thumb_path(video_path)
    if os.path.isfile(thumb_path):
        return thumb_path

    fd, frame_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)

    def _run(extra_args: list) -> bool:
        try:
            res = subprocess.run(
                ["ffmpeg", "-y", "-nostdin", *extra_args, "-i", video_path,
                 "-vf", "scale=iw/4:-1", "-frames:v", "1", "-qscale:v", "11", frame_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT,
            )
            return res.returncode == 0
        except Exception:
            return False

    ok = _run(["-ss", "1"])
    if not ok or not os.path.exists(frame_path) or os.path.getsize(frame_path) == 0:
        _run([])
    if not os.path.exists(frame_path) or os.path.getsize(frame_path) == 0:
        if os.path.exists(frame_path):
            os.remove(frame_path)
        return None

    if save_as_thumb:
        try:
            shutil.move(frame_path, thumb_path)
            return thumb_path
        except OSError:
            # Couldn't save it permanently (e.g. a read-only mount) -- still fine
            # to use from its temp location for this one AI call.
            return frame_path
    return frame_path


def _resize_image_for_ai(file_path: str) -> Optional[str]:
    """Port of generate_ai_description.py's resize_image(): two-tier ffmpeg
    resize (640px wide, then 400px if still over 200KB) so the image sent to
    the AI provider stays small. Returns a path to a new temp jpg (caller
    must delete it), or None on failure."""
    fd, resized_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)

    def _run_ffmpeg(scale: str, qscale: str) -> bool:
        try:
            res = subprocess.run(
                ["ffmpeg", "-y", "-nostdin", "-i", file_path, "-vf", f"scale={scale}:-1", "-qscale:v", qscale, resized_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT,
            )
            return res.returncode == 0
        except Exception:
            return False

    if not _run_ffmpeg("640", "20"):
        if os.path.exists(resized_path):
            os.remove(resized_path)
        return None

    try:
        size = os.path.getsize(resized_path)
    except OSError:
        size = 0
    if size > 204800:
        _run_ffmpeg("400", "25")  # best-effort, same as the original ("|| true")

    return resized_path


# ---------------------------------------------------------------------------
# Length / context budget (env, read on every call)
#   AI_SENTENCES            "2-3"   sentences without extra context (as the original CLI)
#   AI_WORDS_MAX            50      word limit without extra context
#   AI_SENTENCES_CONTEXT    "3-4"   sentences when extra context (AI_CONTEXT) is passed
#   AI_WORDS_MAX_CONTEXT    75      word limit when extra context is passed
#   AI_CONTEXT_MAX          2       how many context items (time of day, season, weather, trip) at most;
#                                   a number or "all"
# More context must not push out the actual description of the scene, hence the larger but still
# fixed budget; the model is told to spend the extra words only on context that fits the picture.
# ---------------------------------------------------------------------------

def _env_text(name: str) -> str:
    return (os.environ.get(name) or "").strip().strip("\"'").strip()


def _env_int(name: str, default: int, lo: int = 10, hi: int = 400) -> int:
    try:
        return max(lo, min(hi, int(_env_text(name) or default)))
    except ValueError:
        return default


def _sentence_range(name: str, default: str) -> tuple:
    m = re.fullmatch(r"(\d)\s*[-–]\s*(\d)", _env_text(name) or default)
    if not m:
        m = re.fullmatch(r"(\d)\s*[-–]\s*(\d)", default)
    lo, hi = int(m.group(1)), int(m.group(2))
    lo = max(1, min(lo, 6)); hi = max(lo, min(hi, 8))
    return lo, hi


def length_budget(with_context: bool) -> tuple:
    """(min_sentences, max_sentences, max_words)"""
    if with_context:
        lo, hi = _sentence_range("AI_SENTENCES_CONTEXT", "3-4")
        return lo, hi, _env_int("AI_WORDS_MAX_CONTEXT", 75)
    lo, hi = _sentence_range("AI_SENTENCES", "2-3")
    return lo, hi, _env_int("AI_WORDS_MAX", 50)


def context_max() -> int:
    """0 = all items, else the number of items the prompt allows (default 2)."""
    raw = _env_text("AI_CONTEXT_MAX").lower()
    if raw in ("all", "alle", "0"):
        return 0
    try:
        return max(1, min(6, int(raw))) if raw else 2
    except ValueError:
        return 2


_tls = threading.local()


def _current_max_tokens() -> int:
    """Token cap for the provider call currently being prepared by generate_description()
    (thread-local so the provider callers keep their (image, system, prompt, cfg) signature)."""
    return getattr(_tls, "max_tokens", 220)


def output_token_budget(max_words: int) -> int:
    """num_predict / maxOutputTokens: German needs ~2 tokens per word; never below the old 220."""
    return max(220, int(max_words * 3.2))


# ---------------------------------------------------------------------------
# Prompts + post-processing -- verbatim from generate_ai_description.py, so
# the output reads exactly the same as the CLI's.
# ---------------------------------------------------------------------------

def build_prompts(geo_context: str, is_video: bool = False, extra_context: str = "") -> tuple:
    """Exact same German prompts as the original; extra_context (time of day, weather, trip name
    from the geo-service, see geoservice.caption_extra) is an optional addition, default off."""
    system_prompt = (
        "Du bist ein muttersprachlicher deutscher Texter mit Expertise in "
        "präziser, kurzer Bildbeschreibung. Antworte ausschließlich in "
        "grammatikalisch einwandfreiem, natürlichem Deutsch: korrekte "
        "Groß-/Kleinschreibung bei Substantiven, korrekte Verbstellung, "
        "natürliche deutsche Wortzusammensetzungen (Komposita) statt "
        "wörtlicher Übersetzungen aus dem Englischen, keine Anglizismen "
        "oder englischen Satzbaumuster. Formuliere kurz, klar und "
        "sachlich -- keine Schachtelsätze."
    )

    prompt = ("Du siehst gerade einen einzelnen Moment (Frame) aus einem privaten Heimvideo."
              if is_video else "Du siehst gerade ein Foto aus einer privaten Fotosammlung.")
    if geo_context:
        prompt += (
            f' Der Aufnahmeort ist bereits bekannt (z.B. durch GPS-Daten): '
            f'"{geo_context}". Baue diesen Ort natürlich in einen der Sätze '
            f'ein, z.B. "... in {geo_context}." -- der Ort muss dafür NICHT '
            f'im Bild selbst sichtbar oder erkennbar sein, da er bereits '
            f'gesichert feststeht, genau wie ein Zeitstempel.'
        )
    if extra_context:
        n = context_max()
        use = "alle passenden Angaben" if n == 0 else ("höchstens eine Angabe" if n == 1 else f"höchstens {n} Angaben")
        prompt += (
            f' Weitere gesicherte Rahmeninformationen zur Aufnahme (nicht im Bild '
            f'erkennbar): {extra_context}. Nutze davon {use}, und nur '
            f'wenn sie zur sichtbaren Szene passt und den Satz natürlicher macht -- '
            f'Wetter und Tageszeit nur bei Aufnahmen im Freien, nie bei Innenräumen. '
            f'Erfinde nichts hinzu, zähle die Angaben nicht auf und verwende den '
            f'Ordner- und Ortsnamen ("Wo wars") nicht als Überschrift. Die Beschreibung der Szene selbst '
            f'bleibt die Hauptsache; die Rahmeninformationen sind nur Beiwerk.'
        )
    lo, hi, words = length_budget(bool(extra_context))
    prompt += (
        f" Beschreibe in {lo} bis {hi} klaren Sätzen auf Deutsch (insgesamt "
        f"maximal {words} Wörter), was in diesem Moment passiert -- die "
        "Handlung, die beteiligten Personen, der Ort und ggf. weitere "
        "erkennbare Details. JEDER Satz MUSS vollständig und mit einem "
        "Punkt abgeschlossen sein -- lieber ein Satz weniger, aber dafür "
        "alle vollständig, als ein zusätzlicher Satz, der mittendrin "
        "abbricht. WICHTIGSTE REGEL (gilt für JEDEN der Sätze): Das "
        "Subjekt darf NIEMALS 'das Bild', 'das Standbild', 'das Foto', "
        "'die Aufnahme' oder 'der Frame' sein, sondern immer die "
        "handelnde Person, das Tier, der Ort oder die Handlung selbst. "
        "Das gilt für JEDE Verbform, egal ob 'zeigt', 'sieht man', 'wird "
        "gezeigt', 'ist zu sehen', 'ist erkennbar', 'zeigt sich' oder "
        "irgendeine andere -- sobald 'Bild'/'Foto'/'Aufnahme' das Subjekt "
        "ist, ist die Formulierung falsch, unabhängig vom Verb. Beispiel: "
        "NICHT 'Auf dem Foto wird ein Mann beim Radfahren gezeigt', "
        "SONDERN DIREKT 'Ein Mann fährt Fahrrad.' Beschreibe die Handlung "
        "so, wie ein Live-Kommentator das Geschehen direkt beschreiben "
        "würde, ohne je das Trägermedium (Bild, Video, Foto, Aufnahme, "
        "Standbild, Frame) überhaupt zu erwähnen. Spekuliere nicht über "
        "das hinaus, was tatsächlich sichtbar ist. Antworte ausschließlich "
        "auf Deutsch."
    )
    return system_prompt, prompt


_STRIP_PATTERNS = [
    re.compile(r"^(Das|Auf dem|Auf der|Im|In diesem)\s+(Bild|Standbild|Foto|Aufnahme|Frame)\s+ist\s+(einen|eine|ein)?\s*", re.IGNORECASE),
    re.compile(r"^(Das|Auf dem|Auf der|Im|In diesem)\s+(Bild|Standbild|Foto|Aufnahme|Frame)\s+(zeigt sich|zeigt|sieht man|wird|erkennt man)\s+(einen|eine|ein|hier|es|dabei)?\s*", re.IGNORECASE),
    re.compile(r"^(Man sieht|Zu sehen ist|Zu sehen sind)\s+(einen|eine|ein)?\s*", re.IGNORECASE),
]
_TRAILING_FIX = re.compile(r"^([A-ZÄÖÜ][a-zäöüß]+) (gezeigt|zu sehen),\s*", re.IGNORECASE)


def post_process(description: str) -> Optional[str]:
    """Exact same cleanup as the original: collapse whitespace, strip leading
    "Auf dem Bild ist/zeigt ..." style intro phrases, capitalize the first
    letter, and reject repetition-loop garbage. Returns None if nothing
    usable survives (caller treats that as a failure)."""
    description = re.sub(r"\s+", " ", description.replace("\n", " ")).strip()

    for pat in _STRIP_PATTERNS:
        description = pat.sub("", description, count=1)
    description = _TRAILING_FIX.sub(r"\1, ", description, count=1)

    if description:
        description = description[0].upper() + description[1:]
    if not description:
        return None

    distinct_chars = len(set(re.sub(r"\s", "", description)))
    if distinct_chars < 10:
        return None
    if ai_description_problem(description):       # refusal / <think> / markup / loop: never write that as a description
        return None

    return description


# ---------------------------------------------------------------------------
# Provider calls -- same request shapes/response parsing as
# generate_ai_description.py's call_google()/call_ollama(), ported from
# urllib to requests for consistency with the rest of this app (geocore.py).
# ---------------------------------------------------------------------------

def _call_google(image_b64: str, system_prompt: str, prompt: str, cfg) -> tuple:
    """Returns (description, failure_reason) -- exactly one is non-None."""
    api_key, model = cfg.google_api_key, cfg.google_model
    if not api_key or not model:
        return None, "AI-Description: Gemini ist als Provider konfiguriert, aber GOOGLE_AI_API_KEY oder GOOGLE_AI_MODEL ist leer."
    if requests is None:
        return None, "AI-Description: Python-Paket 'requests' ist nicht installiert."

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
        ]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": _current_max_tokens()},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    try:
        resp = requests.post(url, json=payload, timeout=AI_HTTP_TIMEOUT)
    except Exception as e:
        return None, f"AI-Description: Aufruf gegen Gemini fehlgeschlagen: {str(e).replace(api_key, 'REDACTED')}"

    if resp.status_code != 200:
        redacted = (resp.text or "").replace(api_key, "REDACTED")
        return None, f"AI-Description: Aufruf gegen Gemini fehlgeschlagen (HTTP {resp.status_code}): {redacted}"

    try:
        data = resp.json()
    except ValueError:
        return None, f"AI-Description: Keine Antwort von Gemini erhalten. Rohantwort: {resp.text}"

    error_message = (data.get("error") or {}).get("message")
    if error_message:
        return None, f"AI-Description: Gemini meldete einen Fehler: {error_message}"

    try:
        description = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        description = ""
    if not description:
        return None, f"AI-Description: Keine Antwort von Gemini erhalten. Rohantwort: {resp.text}"

    return description, None


def _call_ollama(image_b64: str, system_prompt: str, prompt: str, cfg) -> tuple:
    """Returns (description, failure_reason) -- exactly one is non-None."""
    url, model = cfg.ollama_url, cfg.ollama_model
    if not url or not model:
        return None, "AI-Description: Ollama ist als Provider konfiguriert, aber OLLAMA_URL oder OLLAMA_MODEL ist leer."
    if requests is None:
        return None, "AI-Description: Python-Paket 'requests' ist nicht installiert."

    payload = {
        "model": model, "stream": False, "think": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt, "images": [image_b64]},
        ],
        "options": {"temperature": 0.2, "num_predict": _current_max_tokens()},
    }
    endpoint = url.rstrip("/") + "/api/chat"

    try:
        resp = requests.post(endpoint, json=payload, timeout=AI_HTTP_TIMEOUT)
    except Exception as e:
        return None, f"AI-Description: Aufruf gegen '{endpoint}' fehlgeschlagen: {e}"

    if resp.status_code != 200:
        return None, f"AI-Description: Aufruf gegen '{endpoint}' fehlgeschlagen (HTTP {resp.status_code}): {resp.text}"

    try:
        data = resp.json()
    except ValueError:
        return None, f"AI-Description: Keine '.message.content' in der Ollama-Antwort gefunden. Rohantwort: {resp.text}"

    error = data.get("error")
    if error:
        return None, f"AI-Description: Ollama meldete einen Fehler: {error}"

    description = (data.get("message") or {}).get("content", "")
    if not description:
        thinking = (data.get("message") or {}).get("thinking", "")
        if thinking:
            return None, f"AI-Description: Modell '{model}' hat das Token-Budget für internes Thinking verbraucht, ohne eine Antwort zu liefern."
        return None, f"AI-Description: Keine '.message.content' in der Ollama-Antwort gefunden. Rohantwort: {resp.text}"

    return description, None


_PROVIDER_CALLERS = {"ollama": _call_ollama, "google": _call_google}


# ---------------------------------------------------------------------------
# Single choke point: resize/extract -> try each configured provider in
# order -> post-process -> first success wins. Mirrors
# generate_image_ai_description()/generate_video_ai_description() combined
# (video just extracts a frame first, then follows the same image path).
# ---------------------------------------------------------------------------

@dataclass
class GenerationResult:
    description: Optional[str] = None
    provider: str = ""
    model: str = ""
    failures: list = field(default_factory=list)


def generate_description(file_path: str, geo_context: str, cfg, is_video: bool = False,
                         extra_context: str = "") -> GenerationResult:
    result = GenerationResult()
    providers = cfg.providers
    if not providers:
        return result

    if not _which("ffmpeg"):
        result.failures.append(
            "AI-Description: 'ffmpeg' nicht gefunden -- "
            + ("kann keinen Frame für die Video-Beschreibung extrahieren." if is_video
               else "Bildbeschreibung übersprungen.")
        )
        return result

    frame_path = None
    image_path = file_path
    if is_video:
        # save_as_thumb=False: only ever a throwaway frame for THIS ONE AI call, never
        # written to disk as "<stem>-thumb.jpg" -- that used to happen here too, but
        # generating video thumbnails is now the NFO Maker module's job alone (the maintainer's
        # own call: doing it twice -- once here, then again when NFO Maker generates
        # poster/fanart/thumb together in one ffmpeg call anyway -- just wasted an extra
        # ffmpeg invocation per video for no benefit, since NFO Maker overwrites it the
        # moment it runs regardless). If a thumb already sits on disk (an earlier NFO
        # Maker run, or the old CLI toolchain), extract_video_frame() still reuses it
        # as-is with no ffmpeg call at all -- only the WRITING of a new one is removed.
        frame_path = extract_video_frame(file_path, save_as_thumb=False)
        if frame_path is None:
            result.failures.append(
                "AI-Description: ffmpeg konnte keinen Frame aus dem Video extrahieren "
                "(weder bei 1s noch beim ersten Frame) -- Datei evtl. beschädigt oder Codec nicht unterstützt."
            )
            return result
        image_path = frame_path

    resized_path = _resize_image_for_ai(image_path)
    if frame_path and frame_path != get_video_thumb_path(file_path):
        # Only an already-existing, reused "<stem>-thumb.jpg" (see comment above) is
        # left alone -- anything else here is this call's own throwaway temp frame.
        try:
            os.remove(frame_path)
        except OSError:
            pass
    if resized_path is None:
        result.failures.append("AI-Description: ffmpeg konnte kein verkleinertes Bild erzeugen -- Bildbeschreibung übersprungen.")
        return result

    try:
        with open(resized_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        try:
            os.remove(resized_path)
        except OSError:
            pass

    if not image_b64:
        result.failures.append("AI-Description: Base64-Kodierung des Bildes ist leer -- Bildbeschreibung übersprungen.")
        return result

    system_prompt, prompt = build_prompts(geo_context, is_video, extra_context)
    _tls.max_tokens = output_token_budget(length_budget(bool(extra_context))[2])
    models = {"ollama": cfg.ollama_model, "google": cfg.google_model}

    for provider in providers:
        raw_description, failure_reason = _PROVIDER_CALLERS[provider](image_b64, system_prompt, prompt, cfg)
        if failure_reason is not None:
            # Recorded even when a later provider in the list goes on to succeed --
            # e.g. "Google's quota is exhausted, Ollama quietly covered for it" is
            # still worth knowing, not hidden just because the call ultimately worked.
            result.failures.append(failure_reason)
            continue

        cleaned = post_process(raw_description)
        if cleaned is None:
            result.failures.append(
                f"AI-Description: Antwort von {provider} war nach Bereinigung leer oder sah nach "
                f"einer Wiederholungsschleife aus -- verworfen."
            )
            continue

        result.description = cleaned
        result.provider = provider
        result.model = models[provider]
        return result

    return result


# ---------------------------------------------------------------------------
# Writing: .xmp sidecar always, direct image embedding (images only, outside
# -o/sidecar-only mode) same as every other write in this app. Ported from
# exiftool_write_image()/maybe_backfill_ai_description().
# ---------------------------------------------------------------------------

def image_description_needs_sync(file: str, new_desc: str) -> bool:
    """Port of image_description_needs_sync(): some cameras write
    EXIF:ImageDescription set to the file's own filename rather than leaving
    it empty. Immich shows THIS field in preference to IPTC:Caption-Abstract
    or XMP-dc:Description, so writing a real description to those alone
    would leave Immich still showing the filename. True only if
    EXIF:ImageDescription already exists (never creates the field where it's
    genuinely absent) AND differs from the description about to be written."""
    current = _read_tag(file, "-EXIF:ImageDescription")
    return bool(current) and current != new_desc


def exiftool_write_image(file: str, args: list, timeout: int = EXIFTOOL_TIMEOUT) -> tuple:
    """Port of exiftool_write_image(): writes the given tag args to an image
    file, retrying with MakerNotes stripped out entirely if exiftool's
    automatic MakerNotes-rebuild step fails outright ("Error rebuilding
    maker notes" / "No writable tags set" / "Duplicate MakerNote") -- seen on
    some cameras (Kuna Ultra, DJI Neo 2) whose MakerNotes block exiftool
    can't safely rewrite. Only retried for files that actually hit this,
    and only loses that file's own vendor-specific MakerNotes data (autofocus
    points, lens corrections, etc.), never anything else.

    Unlike the original bash function (which always `return 0`s -- a
    best-effort, fire-and-forget write with nothing downstream checking the
    result), this returns (success, message) like every other exiftool write
    in this app, so the web UI can show an accurate pass/fail per file
    instead of a blanket "done"."""
    cmd = ["exiftool", "-m", "-overwrite_original", *args, file]
    try:
        res = exifpool.run(cmd, capture_output=True, text=True, timeout=timeout)
        output = (res.stdout or "") + (res.stderr or "")
        ok = res.returncode == 0
        msg = (res.stderr or "").strip()
    except Exception as e:
        return False, str(e)
    if any(marker in output for marker in ("No writable tags set", "rebuilding maker notes", "Duplicate MakerNote")):
        try:
            retry = exifpool.run(["exiftool", "-m", "-overwrite_original", "-MakerNotes=", *args, file],
                                    capture_output=True, text=True, timeout=timeout)
            ok = retry.returncode == 0
            msg = (retry.stderr or "").strip()
        except Exception as e:
            return False, str(e)
    return ok, msg


def write_description_to_xmp(xmp_path: str, description: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple:
    try:
        res = exifpool.run(
            ["exiftool", "-m", "-overwrite_original", f"-XMP-dc:Description={description}", xmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
        stderr = (res.stderr or "").strip()
        return res.returncode == 0, stderr
    except Exception as e:
        return False, str(e)


def write_description_to_image(file: str, description: str, timeout: int = EXIFTOOL_TIMEOUT) -> tuple:
    """Port of maybe_backfill_ai_description()'s image-write branch:
    XMP-dc:Description + IPTC:Caption-Abstract always, plus
    EXIF:ImageDescription when image_description_needs_sync says it's
    needed. Never call this for videos -- same convention as the rest of
    this app (direct video modification is considered too risky/slow to
    redo).

    NOT used by ai_description/router.py's own execute() anymore -- that now calls
    xmpsync.sync_xmp_to_image() instead, which embeds everything currently in the .xmp
    sidecar (this function's own description fields included, plus GPS/location if
    GPS Sidecar already wrote them) in one call. Kept here as the focused,
    single-purpose primitive xmpsync.py's full sync is built from conceptually; no
    current caller uses it directly."""
    args = [f"-XMP-dc:Description={description}", f"-IPTC:Caption-Abstract={description}"]
    if image_description_needs_sync(file, description):
        args.append(f"-EXIF:ImageDescription={description}")
    return exiftool_write_image(file, args, timeout)
