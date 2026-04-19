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


@router.post("/reload")
def post_reload() -> dict[str, Any]:
    return screensaver.reload_config()
