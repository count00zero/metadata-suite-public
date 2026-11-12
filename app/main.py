"""
main.py — host FastAPI app for the Metadata Suite container.

Each tool lives under modules/<name>/ with its own router, mounted here under its own
URL prefix. This is module #1 (timestamp/rename). Future tools (GPS sidecar writer, NFO
maker, collage generator) get added the same way: build modules/<name>/router.py, then
one more app.include_router(...) line below.
"""
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, FileResponse

from .modules.timestamp_tool.router import router as timestamp_router
from .modules.gps_sidecar.router import router as gps_sidecar_router
from .modules.ai_description.router import router as ai_description_router
from .modules.mtime_restore.router import router as mtime_restore_router
from .modules.collage.router import router as collage_router
from .modules.nfo_maker.router import router as nfo_maker_router
from .modules.short_video_cleanup.router import router as short_video_cleanup_router
from .modules.gps_fill.router import router as gps_fill_router
from .modules.geo_overrides.router import router as geo_overrides_router
from .modules.ctx_fill.router import router as ctx_fill_router
from .modules.year_run.router import router as year_run_router
from .modules.info.router import router as info_router
from .modules.docs.router import router as docs_router
from .modules.endpoints.router import router as endpoints_router
from .modules.home.router import router as home_router

app = FastAPI(title="Metadata Suite")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
class _NoCacheStatic(StaticFiles):
    """CSS/JS changes must show up after an update without a hard reload (browsers otherwise cache heuristically)."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"      # revalidate every time (ETag/Last-Modified -> cheap 304)
        return resp


app.mount("/static", _NoCacheStatic(directory=STATIC_DIR), name="static")

app.include_router(timestamp_router, prefix="/timestamp", tags=["timestamp"])
app.include_router(gps_sidecar_router, prefix="/gps", tags=["gps_sidecar"])
app.include_router(ai_description_router, prefix="/ai", tags=["ai_description"])
app.include_router(mtime_restore_router, prefix="/mtime", tags=["mtime_restore"])
app.include_router(collage_router, prefix="/collage", tags=["collage"])
app.include_router(nfo_maker_router, prefix="/nfo", tags=["nfo_maker"])
app.include_router(short_video_cleanup_router, prefix="/shortvideos", tags=["short_video_cleanup"])
app.include_router(gps_fill_router, prefix="/gpsfill", tags=["gps_fill"])
app.include_router(geo_overrides_router, prefix="/geooverrides", tags=["geo_overrides"])
app.include_router(ctx_fill_router, prefix="/ctxfill", tags=["ctx_fill"])
app.include_router(year_run_router, prefix="/year", tags=["year_run"])
app.include_router(endpoints_router, prefix="/endpoints", tags=["endpoints"])
app.include_router(info_router, prefix="/info", tags=["info"])
app.include_router(docs_router, prefix="/docs", tags=["docs"])


app.include_router(home_router, tags=["home"])      # "/" = dashboard, "/home/status" = its status strip


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Browsers request this at the bare root path regardless of what a page's own
    # <link rel="icon"> says (and some tools -- browser tab previews, bookmark
    # managers -- only ever check here, never a page's <head>) -- served alongside the
    # per-page <link> tags in every module's index.html (see app/static/favicon*.png),
    # not instead of them.
    return FileResponse(os.path.join(STATIC_DIR, "favicon.ico"))
