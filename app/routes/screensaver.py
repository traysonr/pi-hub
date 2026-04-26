"""HTTP API for the screensaver subsystem."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import screensaver

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screensaver", tags=["screensaver"])


class EnabledRequest(BaseModel):
    enabled: bool


class AddThemeRequest(BaseModel):
    # Accept whatever the user typed: bare name ("robotics"), "r/robotics",
    # or a full Reddit URL. The service normalizes and validates.
    subreddit: str


@router.get("")
def get_status() -> dict[str, Any]:
    return screensaver.get_status()


@router.post("/enabled")
def post_enabled(payload: EnabledRequest) -> dict[str, Any]:
    return screensaver.set_enabled(payload.enabled)


@router.post("/start")
def post_start() -> dict[str, Any]:
    try:
        return screensaver.start()
    except RuntimeError as exc:
        # 409 because this is a state conflict (video playing, no images
        # cached, or master toggle off), not a bad request.
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/stop")
def post_stop() -> dict[str, Any]:
    return screensaver.stop()


@router.post("/refresh")
def post_refresh() -> dict[str, Any]:
    return screensaver.refresh_now()


@router.post("/themes/{name}/toggle")
def post_toggle_theme(name: str) -> dict[str, Any]:
    try:
        return screensaver.toggle_theme(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/themes")
def post_add_theme(payload: AddThemeRequest) -> dict[str, Any]:
    try:
        return screensaver.add_theme(payload.subreddit)
    except ValueError as exc:
        # Syntactically bad input -- 400 so the UI can show the message
        # the service returned verbatim.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        # Duplicate -- 409 so the UI can distinguish "bad name" from
        # "already configured".
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/themes/{name}")
def delete_theme(name: str) -> dict[str, Any]:
    try:
        return screensaver.remove_theme(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reload")
def post_reload() -> dict[str, Any]:
    return screensaver.reload_config()


@router.post("/rotate")
def post_rotate() -> dict[str, Any]:
    """Manual trigger for the daily FIFO-ish rotation.

    The scheduler calls the same underlying function every morning at
    05:00 local time; exposing it here lets the user force a rotation
    on demand (useful for testing, or for "I want new images now").
    Returns the per-theme summary along with current screensaver state
    so the UI can re-render in one round-trip.
    """

    result = screensaver.rotate_all_themes()
    return {"rotation": result, "status": screensaver.get_status()}


@router.post("/current/delete")
def post_delete_current_image() -> dict[str, Any]:
    try:
        return screensaver.delete_current_image()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/scheduler")
def get_scheduler() -> dict[str, Any]:
    """Expose the shared scheduler snapshot -- currently just the
    screensaver rotation job, but this is the single place future
    daily/weekly tasks will surface too."""

    from app.services import scheduler

    return scheduler.get_status()
