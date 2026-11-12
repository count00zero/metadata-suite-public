"""router.py -- the documentation page (the book icon next to Info, icon only): the project READMEs rendered at request time
as a readable HTML page. Nothing is generated or stored; the page always shows the README files as they are (re-rendered when
a file changes), so it can never be out of date with the code.

  GET /docs/              the page   (?doc=suite | geo | endpoints)
"""
from __future__ import annotations

import html
import os
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))          # /srv in the image, the checkout in development

# key, tab title, file names to try (image copy first, then the repository layout)
DOCS = (
    ("suite", "Metadata Suite", ("docs/README.md", "README.md")),
    ("geo", "geo-service", ("docs/geo-service-README.md", "geo-service/README.md")),
    ("endpoints", "Endpunkte", ("docs/geo-service-ENDPOINTS.md", "geo-service/ENDPOINTS.md")),
)
_LINKS = {"README.md": "suite", "geo-service/README.md": "geo", "ENDPOINTS.md": "endpoints", "geo-service/ENDPOINTS.md": "endpoints"}
_CACHE: dict = {}


def _find(names) -> str:
    for n in names:
        p = os.path.join(_REPO, n)
        if os.path.isfile(p):
            return p
    return ""


def _split_sections(body: str) -> list:
    """[(id, title, html)] -- the part before the first h2 is the intro; every h2 starts a section (search hides sections)."""
    parts = re.split(r"(?=<h2 )", body)
    out = []
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        m = re.match(r'<h2 id="([^"]*)">(.*?)</h2>', part, re.S)
        title = re.sub(r"<[^>]+>", "", m.group(2)) if m else ""
        out.append({"id": m.group(1) if m else f"intro{i}", "title": title, "html": part,
                    "text": html.unescape(re.sub(r"<[^>]+>", " ", part)).lower()})
    return out


def render(key: str) -> dict:
    """{key, title, sections, toc, source, missing}. Cached per file mtime."""
    title, names = next(((t, n) for k, t, n in DOCS if k == key), (key, ()))
    path = _find(names)
    if not path:
        return {"key": key, "title": title, "sections": [], "toc": [], "source": "", "missing": True}
    mt = os.path.getmtime(path)
    hit = _CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        import markdown
        md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
                               extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
        body = md.convert(text)
        toc = []
        for h2 in md.toc_tokens:
            toc.append({"id": h2["id"], "name": h2["name"], "children": [{"id": c["id"], "name": c["name"]} for c in h2.get("children", [])]})
    except ImportError:            # library missing: still readable
        body, toc = f"<pre>{html.escape(text)}</pre>", []
    # links between the documents stay inside the page, external links open in a new tab
    def fix(m):
        href = m.group(1)
        base = href.split("#")[0]
        frag = href[len(base):]
        if base in _LINKS:
            return f'href="/docs/?doc={_LINKS[base]}{frag}"'
        if href.startswith(("http://", "https://")):
            return f'href="{href}" target="_blank" rel="noopener"'
        return m.group(0)
    body = re.sub(r'href="([^"]+)"', fix, body)
    res = {"key": key, "title": title, "sections": _split_sections(body), "toc": toc, "source": os.path.relpath(path, _REPO), "missing": False}
    _CACHE[path] = (mt, res)
    return res


@router.get("/", response_class=HTMLResponse)
def index(request: Request, doc: str = "suite"):
    if doc not in {k for k, _t, _n in DOCS}:
        doc = "suite"
    tabs = [{"key": k, "title": t, "available": bool(_find(n))} for k, t, n in DOCS]
    return templates.TemplateResponse(request, "index.html", {"d": render(doc), "tabs": tabs, "cur": doc})
