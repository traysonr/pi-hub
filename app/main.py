"""Pi Hub FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import (
    STATIC_DIR,
    TEMPLATES_DIR,
    configure_logging,
    ensure_runtime_dirs,
)
from app.routes import media as media_routes

configure_logging()
ensure_runtime_dirs()

log = logging.getLogger("pi-hub")

app = FastAPI(title="Pi Hub", version="0.1.0")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app.include_router(media_routes.router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
