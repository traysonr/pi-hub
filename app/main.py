"""Pi Hub FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
    STATIC_DIR,
    TEMPLATES_DIR,
    configure_logging,
    ensure_runtime_dirs,
)
from app.routes import media as media_routes
from app.routes import screensaver as screensaver_routes
from app.services import audio_player, display, scheduler, screensaver, shuffle

configure_logging()
ensure_runtime_dirs()
# Start the persistent mpv display controller before the screensaver
# subsystem registers its playlist provider. The controller is what
# keeps the TV from ever falling back to the Linux console.
display.init()
# Headless second mpv for music playback. Pre-spawning at boot keeps
# the first /api/play of an audio track snappy (loadfile over IPC
# instead of fork+exec on the Pi 3).
audio_player.init()
# Register shuffle's end-of-track hook on the audio player so the music
# shuffle mode can queue the next random track automatically.
shuffle.init()
screensaver.init()

# Daily FIFO-ish rotation of cached subreddit images. Keeps a random
# 25% of yesterday's cache and refills the rest with Reddit's current
# top listing, so the slideshow stays fresh without losing all
# familiar images overnight. Running in-app (rather than via
# systemd/cron) keeps schedule + logic colocated and makes adding the
# next daily/weekly task a one-liner -- see app/services/scheduler.py.
scheduler.register(
    "screensaver_rotate",
    scheduler.daily("05:00"),
    screensaver.rotate_all_themes,
)
scheduler.start()

log = logging.getLogger("pi-hub")

app = FastAPI(title="Pi Hub", version="0.1.0")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.include_router(media_routes.router)
app.include_router(screensaver_routes.router)


def _asset_version(name: str) -> str:
    """Cache-busting token derived from a static file's mtime.

    Phones aggressively cache CSS/JS, so without a versioned URL users keep
    seeing stale UI after server-side changes. Falls back to "0" if the file
    is missing so template rendering never breaks."""

    path = STATIC_DIR / name
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return "0"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "css_version": _asset_version("style.css"),
            "js_version": _asset_version("app.js"),
        },
    )
    # Belt-and-suspenders: tell browsers/proxies the index page itself is
    # never cacheable so it always re-fetches the (versioned) asset URLs.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Suppress noisy 404s for /favicon.ico without serving a real icon.
@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
