"""sightstranslate.py -- fills German descriptions of sights with an LLM (Ollama and/or Google), stored in the sights DB.

  python -m app.sightsindex translate     (resumable: rows already asked are marked and skipped; run again to continue)

Only sights WITHOUT a German Wikidata description are touched. The English text (Wikidata, else the OSM description tag)
is translated into `desc_de_ai`; `ai_src` tells which provider wrote it, so model output stays distinguishable from
attested Wikidata text and can be wiped (UPDATE sight SET desc_de_ai='', ai_state=0, ai_src='').
The model must answer UNKNOWN rather than invent; echoes, prose/reasoning fragments, answers that are still English and
absurd lengths are rejected (and the row marked asked, so it is not asked again unless GEO_SIGHTS_TR_RETRY=1).
Names are NOT translated (proper nouns); only the descriptions.

Settings (same names as the suite): AI_PROVIDER (ollama,google -- priority list), OLLAMA_URL, OLLAMA_MODEL,
GOOGLE_AI_API_KEY, GOOGLE_AI_MODEL (a text model for this job: OLLAMA_MODEL_TEXT / GOOGLE_AI_MODEL_TEXT win when set,
so the vision model of the image descriptions is not used for translating); own: GEO_SIGHTS_TR_SCOPE (notable|all), GEO_SIGHTS_TR_MAX, GEO_SIGHTS_TR_SLEEP,
GEO_SIGHTS_TR_TIMEOUT, GEO_SIGHTS_TR_RETRY (1 = also ask rejected/unknown ones again, only = ONLY those), GEO_SIGHTS_TR_BATCH (descriptions per request), GEO_SIGHTS_TR_WORKERS (parallel requests), GEO_SIGHTS_TR_LOG (1 = one log line per sight, default).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from typing import Callable, Optional

import requests

AI_COLUMNS = (("ai_state", "INTEGER DEFAULT 0"), ("desc_de_ai", "TEXT DEFAULT ''"), ("ai_src", "TEXT DEFAULT ''"))

_PROMPT = (
    "Translate the short description (it may be in any language) of the place \"{name}\" into German.\n"
    "Description: {text}\n\n"
    "Rules: translate every ordinary word (e.g. \"unitary authority area\" -> \"Verwaltungsgebiet\", \"National Register of Historic Places\" -> \"Nationales Register historischer Stätten\"); only proper names, numbers and codes stay unchanged. "
    "Proper names, titles and distinctive foreign terms without a common German equivalent (e.g. \"Kofun\", \"Stupa\", \"Chafariz\") stay in their original form; "
    "if a German reader would probably not know such a term, add a short German explanation in parentheses right after it (e.g. \"Kofun (japanischer Hügelgrabtyp)\"). "
    "Keep it short; reply with the German translation ONLY, "
    "on a single line, without quotes or commentary. If you cannot translate it reliably, reply with exactly: UNKNOWN"
)

_ENGLISH_WORDS = re.compile(r"\b(the|of|and|with|is|was|are|built|located|which|that|from|for|by|where|who|at|on|to|a|near|first|old)\b", re.IGNORECASE)
_PROSE_START = re.compile(r"^(first|i|let|note|sure|okay|ok|well|hmm|here|translation|based on|according to|however|"
                          r"unfortunately|actually|the answer|this (is|text))\b", re.IGNORECASE)


class Truncated(Exception):
    """The model hit its output limit: the answer ends in the middle of a sentence and must never be stored."""
    prov = ""


def _token_budget(src: str) -> int:
    """Output tokens for one description: generous, the model stops by itself at the end of the sentence."""
    return min(8192, max(400, int(len(src) * 1.5) + 200))


def ensure_columns(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(sight)")}
    for col, typ in AI_COLUMNS:
        if col not in have:
            c.execute(f"ALTER TABLE sight ADD COLUMN {col} {typ}")
    c.commit()


_GERMAN_WORDS = re.compile(r"\b(der|die|das|dem|den|des|und|ein|eine|einer|im|von|vom|mit|zu|zur|zum|am|auf|aus|bei|nach|für|ist|wurde|über|auch|nicht|oder)\b|[äöüß]", re.IGNORECASE)


def looks_german(text: str) -> bool:
    return len(_GERMAN_WORDS.findall(text)) >= 1 and not looks_english(text)


def looks_english(text: str, minimum: int = 1) -> bool:
    return len(_ENGLISH_WORDS.findall(text)) >= minimum


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uff00-\uffef]")


def _max_len(source: str) -> int:
    """Longest plausible German answer. Chinese / Japanese / Korean text is far shorter in characters than its German
    translation (100 characters -> 350 are normal), and a short source is often just a name or a date.
    GEO_SIGHTS_TR_LEN_FACTOR (default 4) and GEO_SIGHTS_TR_LEN_MIN (default 200) tune it; CJK sources get 1.5 x the factor."""
    def num(name, default):
        try:
            return float(os.environ.get(name) or default)
        except ValueError:
            return default
    cjk = len(_CJK.findall(source))
    factor = num("GEO_SIGHTS_TR_LEN_FACTOR", 4.0) * (1.5 if cjk >= 0.3 * max(len(source), 1) else 1.0)
    return int(max(num("GEO_SIGHTS_TR_LEN_MIN", 200.0), factor * len(source)))


def check_answer(raw: str, source: str) -> tuple:
    """Model output -> (German text, "") or (None, reason it was rejected)."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    text = next((ln.strip() for ln in text.splitlines() if ln.strip()), "").strip("\"'„“” ")
    if not text:
        return None, "empty answer"
    if text.upper().startswith("UNKNOWN"):
        return None, "model said UNKNOWN"
    if text.casefold() == source.casefold():
        # hyphenated proper names ("Stratford-on-Avon") contain English function words but are not English prose
        if looks_english(re.sub(r"\b[A-ZÄÖÜ][\w']*(?:-[\w']+)+", " ", text)):
            return None, f"echo of the English text: {text[:60]!r}"
        # no English words: the text is already German or a name (e.g. 'Museum in Tainan, Taiwan') -- nothing to translate
        return None, f"same as the source, nothing to translate: {text[:60]!r}"
    # a trailing , ; : is only a sign of a chatty answer when the source did not end that way itself (OSM texts often end in ';')
    if _PROSE_START.match(text) or (text.endswith((",", ";", ":")) and not source.rstrip().endswith(text[-1])):
        return None, f"explanation instead of a translation: {text[:60]!r}"
    if len(text) > _max_len(source) or len(text) < 3:
        return None, f"implausible length ({len(text)} characters for {len(source)}): {text[:60]!r}"
    if looks_english(text, 3) and len(_ENGLISH_WORDS.findall(text)) > len(_GERMAN_WORDS.findall(text)):
        return None, f"still English: {text[:60]!r}"                # German function words outweigh stray English ones (names, 'a', 'was')
    letters = [ch for ch in text if ch.isalpha()]
    foreign = sum(1 for ch in letters if ord(ch) > 0x24F and not 0x1E00 <= ord(ch) <= 0x1EFF)
    if foreign >= 6 and foreign > 0.25 * len(letters):             # Chinese / Cyrillic words mixed into the German text
        return None, f"untranslated characters in the answer: {text[:60]!r}"
    return text, ""


def clean_answer(raw: str, source: str) -> Optional[str]:
    return check_answer(raw, source)[0]


# ----------------------------------------------------------------------------------------------- providers
class Providers:
    def __init__(self, chain, ollama_url, ollama_model, google_key, google_model, timeout=60.0, sleep=time.sleep, give_up=600.0, clock=time.time):
        self.chain = [p.strip().lower() for p in chain if p.strip()]
        self.ollama_url, self.ollama_model = (ollama_url or "http://localhost:11434").rstrip("/"), ollama_model or "qwen2.5:7b"
        self.google_key, self.google_model = google_key or "", google_model or "gemini-2.5-flash-lite"
        self.timeout, self.sleep = timeout, sleep
        self.clock = clock
        self.fails: dict = {}
        self.rest: dict = {}
        self.stalled_since: Optional[float] = None
        self.give_up = give_up
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env=os.environ.get) -> "Providers":
        return cls((env("AI_PROVIDER") or "ollama").split(","), env("OLLAMA_URL"), env("OLLAMA_MODEL_TEXT") or env("OLLAMA_MODEL"),
                   env("GOOGLE_AI_API_KEY"), env("GOOGLE_AI_MODEL_TEXT") or env("GOOGLE_AI_MODEL"), float(env("GEO_SIGHTS_TR_TIMEOUT") or 60))

    def _ollama(self, prompt: str, tokens: int = 160, timeout: float = 60.0) -> str:
        r = requests.post(f"{self.ollama_url}/api/generate", timeout=timeout, json={
            "model": self.ollama_model, "prompt": prompt, "stream": False, "think": False,
            "options": {"temperature": 0, "num_predict": tokens}})
        if r.status_code == 400:                                # model without "think" support
            r = requests.post(f"{self.ollama_url}/api/generate", timeout=timeout, json={
                "model": self.ollama_model, "prompt": prompt, "stream": False, "options": {"temperature": 0, "num_predict": tokens}})
        r.raise_for_status()
        j = r.json()
        if j.get("done_reason") == "length":
            raise Truncated()
        return j.get("response", "")

    def _google(self, prompt: str, tokens: int = 160, timeout: float = 60.0) -> str:
        if not self.google_key:
            raise RuntimeError("GOOGLE_AI_API_KEY is not set")
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.google_model}:generateContent",
                          params={"key": self.google_key}, timeout=timeout,
                          json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": tokens}})
        r.raise_for_status()
        try:
            cand = r.json()["candidates"][0]
            if cand.get("finishReason") == "MAX_TOKENS":
                raise Truncated()
            return cand["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return ""                                           # safety-filtered = a legitimate "no answer"

    def _bench(self, name: str, secs: float, why: str, log: Callable) -> None:
        """A failing provider rests for a while (longer after repeated failures) instead of being written off for the run."""
        with self._lock:
            n = self.fails[name] = self.fails.get(name, 0) + 1
            wait = min(secs * n, 600.0)
            self.rest[name] = self.clock() + wait
        log(f"  {name}: {why} -- resting {wait:.0f} s (failure #{n} in a row)")

    def generate(self, prompt: str, log: Callable, skip=(), tokens: int = 160) -> tuple:
        """(text, provider). Providers that fail rest for a while and are tried again later; 429/5xx back off and retry.
        When every provider rests, waits for the first one to come back; RuntimeError only after `give_up` seconds
        without a single success (or when `skip` leaves no provider)."""
        timeout = self.timeout * (1 + tokens / 400)             # a batch of answers needs longer than one
        while True:
            usable = [n for n in self.chain if n in ("ollama", "google") and n not in skip]
            if not usable:
                raise RuntimeError(f"no provider left in AI_PROVIDER={self.chain!r} for this request")
            last: Optional[Exception] = None
            for name in usable:
                if self.rest.get(name, 0) > self.clock():
                    continue
                try:
                    out = (self._ollama if name == "ollama" else self._google)(prompt, tokens, timeout)
                    self.fails[name] = 0
                    self.stalled_since = None
                    return out, name
                except Truncated as e:
                    e.prov = name
                    raise
                except requests.HTTPError as e:
                    code = e.response.status_code if e.response is not None else 0
                    if code in (429, 500, 502, 503, 504):
                        wait = 5.0
                        try:
                            wait = float(e.response.headers.get("Retry-After") or 5)
                        except (TypeError, ValueError):
                            pass
                        log(f"  {name}: HTTP {code} (busy / rate limited) -- waiting {wait:.0f} s")
                        self.sleep(wait)
                    else:
                        self._bench(name, 120.0, f"HTTP {code}", log)
                    last = e
                except (requests.RequestException, RuntimeError) as e:
                    self._bench(name, 30.0, f"{type(e).__name__}: {str(e)[:120]}", log)
                    last = e
            if self.stalled_since is None:
                self.stalled_since = self.clock()
            if self.clock() - self.stalled_since > self.give_up:
                raise RuntimeError(f"no usable provider for {self.give_up:.0f} s in AI_PROVIDER={self.chain!r}") from last
            nxt = min(self.rest.get(n, 0) for n in usable)
            self.sleep(max(1.0, min(nxt - self.clock(), 30.0)))


# ----------------------------------------------------------------------------------------------- run
_BATCH_PROMPT = (
    "Translate each of the following short place descriptions (any language) into German.\n"
    "Rules: translate every ordinary word; only proper names, numbers and codes stay unchanged. Proper names, titles and distinctive foreign terms "
    "without a common German equivalent (e.g. \"Kofun\", \"Stupa\") stay in their original form; if a German reader would probably not know such a term, "
    "add a short German explanation in parentheses right after it (e.g. \"Kofun (japanischer Hügelgrabtyp)\"). Keep each short; "
    "no quotes and no commentary outside the translation. Reply with EXACTLY one line per item in the form `<number>: <German translation>`; "
    "if you cannot translate an item reliably, write `<number>: UNKNOWN`.\n\n{items}"
)


def _parse_batch(raw: str, n: int) -> dict:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL)
    out = {}
    for ln in raw.splitlines():
        m = re.match(r"\s*(\d+)\s*[:.)\]-]\s*(.+)", ln)
        if m and 1 <= int(m.group(1)) <= n:
            out.setdefault(int(m.group(1)), m.group(2).strip())
    return out


def _generate_whole(providers, prompt, log, skip, tokens):
    """generate(), asking once more with a bigger limit when the answer was cut off. Raises Truncated if it still is."""
    try:
        return providers.generate(prompt, log, skip=skip, tokens=tokens)
    except Truncated:
        return providers.generate(prompt, log, skip=skip, tokens=min(8192, tokens * 3))


def _single(providers, name, src, log, verbose, tried=None):
    """One sight, provider chain with fallback on rejected answers and on UNKNOWN (the next provider is asked once)
    -> (text|None, why, raw, prov). `tried`: providers that already answered UNKNOWN in a batch."""
    prompt = _PROMPT.format(name=name, text=src)
    tried = list(tried or [])
    text, why, raw, prov = None, ("model said UNKNOWN" if tried else ""), "", (tried[-1] if tried else "")
    while True:                                                 # a rejected / UNKNOWN answer is retried with the next provider in the chain
        try:
            raw2, prov2 = _generate_whole(providers, prompt, log, tried, _token_budget(src))
        except Truncated as e:                                  # even the bigger limit was not enough: never keep half a sentence
            text, why, raw, prov = None, "answer cut off (token limit)", "", e.prov or prov
            if prov in tried or not prov:
                break
            tried.append(prov)
            continue
        except RuntimeError:
            if not tried:
                raise
            break
        text, why, raw, prov = (*check_answer(raw2, src), raw2, prov2)
        if text or why.startswith("same") or prov in tried:
            break
        if verbose:
            log(f"  [{prov}] {str(name)[:40]}: {'UNKNOWN' if why == 'model said UNKNOWN' else 'rejected (' + why + ')'} -- trying the next provider")
        tried.append(prov)
    return text, why, raw, prov


def _work(providers, rows, batch_mode, log, verbose):
    """rows: [(id, name, src)] -> [(id, name, src, text, why, raw, prov)]. Batch request first, rejected/missing/UNKNOWN items one by one."""
    res = {}
    unknown_in_batch = {}
    if batch_mode and len(rows) > 1:
        items = "\n".join(f"{i}: [{n}] {src}" for i, (_id, n, src) in enumerate(rows, 1))
        try:
            raw, prov = _generate_whole(providers, _BATCH_PROMPT.format(items=items), log, (),
                                        min(8192, sum(_token_budget(src) for (_i, _n, src) in rows)))
            got = _parse_batch(raw, len(rows))
        except Truncated:
            got = {}                                            # a cut-off batch is thrown away, every item is asked on its own
        for i, (rid, n, src) in enumerate(rows, 1):
            if i in got:
                t, why = check_answer(got[i], src)
                if t or why.startswith("same"):
                    res[rid] = (t, why, got[i], prov)
                elif why == "model said UNKNOWN":
                    unknown_in_batch[rid] = prov                # ask the other provider(s) once, not the same one again
    for rid, n, src in rows:
        if rid not in res:
            res[rid] = _single(providers, n, src, log, verbose, tried=[unknown_in_batch[rid]] if rid in unknown_in_batch else None)
    return [(rid, n, src, *res[rid]) for rid, n, src in rows]


def translate(db_path: str, providers: Providers, scope: str = "notable", limit: int = 0, retry=False,
              sleep_s: float = 0.0, verbose: bool = True, log: Callable = lambda m: None, sleep: Callable = time.sleep,
              batch: int = 1, workers: int = 1) -> dict:
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"{db_path} does not exist -- run 'build' first")
    from .sightsindex import rw_connect
    c = rw_connect(db_path)
    c.row_factory = sqlite3.Row
    ensure_columns(c)
    where = "desc_de = '' AND (desc_en != '' OR description != '') AND ai_state " + ("= 2" if retry == "only" else "IN (0, 2)" if retry else "= 0")
    if scope != "all":
        where += " AND (importance > 0 OR sitelinks > 0)"
    total = c.execute(f"SELECT COUNT(*) FROM sight WHERE {where}").fetchone()[0]
    batch, workers = max(1, int(batch)), max(1, int(workers))
    log(f"{total:,} sights without a German description ({scope}) -- providers {providers.chain}, {batch} per request, {workers} parallel")
    ids = [r[0] for r in c.execute(f"SELECT id FROM sight WHERE {where} ORDER BY importance DESC, sitelinks DESC, id" + (f" LIMIT {int(limit)}" if limit else ""))]
    done = ok = unknown = skipped = 0
    stopped = False
    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            rows = c.execute("SELECT id, COALESCE(NULLIF(name_de,''), NULLIF(label_de,''), name) AS n, desc_en, description FROM sight "
                             f"WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            order = {x: k for k, x in enumerate(chunk)}
            todo = []
            for r in sorted(rows, key=lambda r: order[r["id"]]):
                src = r["desc_en"] or r["description"]
                if not r["desc_en"] and looks_german(src):      # already German (OSM tag): keep it; any other language is translated
                    c.execute("UPDATE sight SET ai_state=3 WHERE id=?", (r["id"],))
                    skipped += 1
                    done += 1
                    if verbose:
                        log(f"  [kept] {str(r['n'])[:40]}: {src[:70]!r}  (already German, left as it is)")
                else:
                    todo.append((r["id"], r["n"], src))
            groups = [todo[k:k + batch] for k in range(0, len(todo), batch)]
            futures = [pool.submit(_work, providers, g, batch > 1, log, verbose) for g in groups]
            for f in futures:
                try:
                    results = f.result()
                except RuntimeError as e:
                    log(f"stopping: {e}")
                    stopped = True
                    continue
                for rid, n, src, text, why, raw, prov in results:
                    if verbose:
                        log(f"  [{prov}] {str(n)[:40]}: {src[:70]!r}  ->  " + (repr(text) if text else
                            f"REJECTED ({why}); raw {raw.strip()[:70]!r}" if why.startswith(("echo", "expl", "impl", "still", "untr", "answer")) else
                            f"kept as it is ({why})" if why.startswith("same") else f"no translation ({why})"))
                    if text:
                        c.execute("UPDATE sight SET desc_de_ai=?, ai_state=1, ai_src=? WHERE id=?", (text, prov, rid))
                        ok += 1
                    elif why.startswith("same"):                # nothing to translate: shown as it is, not asked again
                        c.execute("UPDATE sight SET ai_state=3 WHERE id=?", (rid,))
                        skipped += 1
                    else:
                        c.execute("UPDATE sight SET ai_state=2, ai_src=? WHERE id=?", (prov, rid))
                        unknown += 1
                    done += 1
                c.commit()
                el = max(time.time() - t0, 1e-6)
                rate = done / el * 60
                log(f"  … {done:,}/{len(ids):,} done, {ok:,} translated, {unknown:,} unknown/rejected "
                    f"[{rate:.0f} sights/min, about {max((len(ids) - done) / rate / 60, 0) if rate else 0:.1f} h left]")
                if sleep_s:
                    sleep(sleep_s)
            c.commit()
            if stopped:
                break
    c.commit()
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    left = c.execute(f"SELECT COUNT(*) FROM sight WHERE {where.replace('IN (0, 2)', '= 0').replace('ai_state = 2', 'ai_state = 0')}").fetchone()[0]
    c.close()
    return {"asked": done, "translated": ok, "unknown": unknown, "kept_as_is": skipped, "left": left, "stopped": stopped}
