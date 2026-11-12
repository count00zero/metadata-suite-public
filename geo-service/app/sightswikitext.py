"""sightswikitext.py -- the first sentences of a sight's Wikipedia article (de / en) into the sights DB.

  python -m app.sightsindex wikitext     (resumable: rows done are marked in ex_state; run again to continue)

Wikidata's description is a two-word label ("Kirche in Wien"); the opening of the Wikipedia article is what a photo caption,
the AI description and /nearby can really use. MediaWiki action API, 20 titles per request (prop=extracts, intro only),
polite pace and User-Agent like the Wikidata import. ex_state bit 1 = German done, bit 2 = English done.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests

BIT = {"de": 1, "en": 2}
COL = {"de": "ex_de", "en": "ex_en"}
TITLE_COL = {"de": "wp_de", "en": "wp_en"}


def title_expr(lang: str) -> str:
    """SQL for the article title: the Wikidata sitelink, else the OSM tag 'wikipedia=de:Titel' of that language."""
    col = TITLE_COL[lang]
    return f"CASE WHEN {col} != '' THEN {col} WHEN wikipedia LIKE '{lang}:%' THEN substr(wikipedia, 4) ELSE '' END"


def ensure_columns(c: sqlite3.Connection) -> None:
    have = {r[1] for r in c.execute("PRAGMA table_info(sight)")}
    for col, typ in (("ex_de", "TEXT DEFAULT ''"), ("ex_en", "TEXT DEFAULT ''"), ("ex_state", "INTEGER DEFAULT 0")):
        if col not in have:
            c.execute(f"ALTER TABLE sight ADD COLUMN {col} {typ}")
    c.commit()


def shorten(text: str, max_chars: int = 400) -> str:
    """Whitespace collapsed, cut at a sentence end below max_chars (at a word end when one sentence is longer)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    if ends and ends[-1] > max_chars * 0.4:
        return cut[:ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + " …"


def fetch_extracts(lang: str, titles: list, sentences: int, ua: str, log: Callable, sleep: Callable, get=requests.get) -> Optional[dict]:
    """{requested title: intro text} for up to 20 titles; None when the API is not reachable (retry later)."""
    params = {"action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1", "exsentences": str(sentences),
              "exlimit": str(len(titles)), "redirects": "1", "titles": "|".join(titles), "format": "json", "formatversion": "2"}
    for attempt in range(4):
        try:
            r = get(f"https://{lang}.wikipedia.org/w/api.php", params=params, headers={"User-Agent": ua}, timeout=60)
        except requests.RequestException as e:
            log(f"  request failed ({type(e).__name__}); retry {attempt + 1}/4")
            sleep(5 * (attempt + 1))
            continue
        if r.status_code in (429, 503) or r.status_code >= 500:
            wait = int(r.headers.get("Retry-After", "0") or 0) or 10 * (attempt + 1)
            log(f"  HTTP {r.status_code}, waiting {wait} s")
            sleep(wait)
            continue
        if r.status_code != 200:
            log(f"  HTTP {r.status_code} from {lang}.wikipedia.org -- {(r.text or '')[:120]!r}")
            return None
        try:
            q = (r.json() or {}).get("query") or {}
        except ValueError:
            log("  Wikipedia answered 200 but not with JSON")
            return None
        alias = {}                                                    # requested -> final title (normalized, redirected)
        for key in ("normalized", "redirects"):
            for m in q.get(key) or []:
                alias[m["from"]] = m["to"]
        text = {p.get("title"): p.get("extract") or "" for p in q.get("pages") or []}
        out = {}
        for t in titles:
            final = t
            for _ in range(3):
                final = alias.get(final, final)
            out[t] = text.get(final, "")
        return out
    return None


def wikitext(db_path: str, langs=("de", "en"), scope: str = "notable", limit: int = 0, delay: float = 0.5, workers: int = 2,
             sentences: int = 3, max_chars: int = 400, log: Callable = lambda m: None, sleep: Callable = time.sleep,
             ua: str = "", get=requests.get) -> dict:
    from .sightsindex import rw_connect, _user_agent
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"{db_path} does not exist -- run 'build' first")
    ua = ua or _user_agent()
    c = rw_connect(db_path)
    ensure_columns(c)
    done = got = 0
    stopped = False
    for lang in [l for l in langs if l in BIT]:
        te = title_expr(lang)
        where = f"{te} != '' AND (ex_state & {BIT[lang]}) = 0" + ("" if scope == "all" else " AND importance > 0")
        todo = c.execute(f"SELECT COUNT(*) FROM sight WHERE {where}").fetchone()[0]
        log(f"{lang}.wikipedia: {todo:,} articles to read ({workers} parallel)")
        if not todo:
            tot = c.execute("SELECT COUNT(*) FROM sight").fetchone()[0]
            titled = c.execute(f"SELECT COUNT(*) FROM sight WHERE {te} != ''").fetchone()[0]
            imp = c.execute(f"SELECT COUNT(*) FROM sight WHERE {te} != '' AND importance > 0").fetchone()[0]
            done_n = c.execute(f"SELECT COUNT(*) FROM sight WHERE {te} != '' AND (ex_state & {BIT[lang]}) != 0").fetchone()[0]
            log(f"  nothing to do for {lang}: {tot:,} sights, {titled:,} with a {lang} article title, "
                f"{imp:,} of those with Wikimedia importance, {done_n:,} already read")
            if not titled:
                log("  -> no article titles: run 'wikidata' first (it fills them), or the index has no wikipedia/wikidata tags")
            elif scope != "all" and not imp:
                log("  -> scope 'notable' needs importance: run 'importance' first, or set GEO_SIGHTS_WP_SCOPE=all")
        bad = 0
        lang_done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            while not stopped:
                if limit and done >= limit:
                    break
                rows = c.execute(f"SELECT id, {te} FROM sight WHERE {where} ORDER BY importance DESC, id LIMIT ?",
                                 (20 * max(1, workers) * 5,)).fetchall()
                if not rows:
                    break
                chunks = [rows[i:i + 20] for i in range(0, len(rows), 20)]

                def work(chunk):
                    res = fetch_extracts(lang, sorted({r[1] for r in chunk}), sentences, ua, log, sleep, get)
                    if res is not None:
                        sleep(delay)
                    return res
                results = list(pool.map(work, chunks))
                failed = 0
                for chunk, res in zip(chunks, results):
                    if res is None:
                        failed += 1
                        continue
                    for rid, title in chunk:
                        ex = shorten(res.get(title, ""), max_chars)
                        c.execute(f"UPDATE sight SET {COL[lang]}=?, ex_state = ex_state | {BIT[lang]} WHERE id=?", (ex, rid))
                        got += 1 if ex else 0
                    done += len(chunk)
                    lang_done += len(chunk)
                c.commit()
                log(f"  {lang}: {min(lang_done, todo):,} of {todo:,} read  (all languages so far: {done:,} read, {got:,} with text)")
                if failed:
                    bad += 1
                    if bad >= 4:
                        log("stopping (Wikipedia not reachable / throttled) -- run again later to continue")
                        stopped = True
                        break
                    sleep(60 * bad)
                else:
                    bad = 0
    c.execute("INSERT OR REPLACE INTO meta VALUES ('wikitext_run', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    c.commit()
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    c.close()
    return {"read": done, "with_text": got, "stopped": stopped}
