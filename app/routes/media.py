"""HTTP API for media catalogue, downloads, and playback."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Path as PathParam
from pydantic import BaseModel, Field, field_validator

from app.services import catalogue, cec, downloader, player, screensaver

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["media"])


class DownloadRequest(BaseModel):
    """All downloads are locked to H.264 <=720p (the only format the Pi 3
    can play smoothly), so no quality option is exposed."""

    url: str = Field(..., min_length=4, max_length=2048)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("URL must start with http:// or https://")
        return value


class PlayRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=512)


class SeekRequest(BaseModel):
    seconds: float = Field(..., ge=-3600, le=3600)


class VolumeRequest(BaseModel):
    delta: float = Field(..., ge=-100, le=100)


class PauseRequest(BaseModel):
    # Optional. When omitted, the endpoint toggles the current state.
    paused: bool | None = None


@router.get("/videos")
def get_videos() -> dict[str, Any]:
    videos = [v.to_dict() for v in catalogue.list_videos()]
    return {"videos": videos, "count": len(videos)}


@router.delete("/videos/{filename:path}")
def delete_video(
    filename: str = PathParam(..., min_length=1, max_length=512),
) -> dict[str, Any]:
    try:
        path = catalogue.resolve_video(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # If the file currently being played is removed, stop playback first so
    # mpv doesn't keep a stale handle.
    if player.is_playing():
        player.stop()

    try:
        path.unlink()
    except OSError as exc:
        log.exception("Failed to delete %s", path)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc

    log.info("Deleted %s", path.name)
    return {"status": "deleted", "filename": path.name}


@router.post("/download")
def post_download(payload: DownloadRequest) -> dict[str, Any]:
    job = downloader.start_download(payload.url)
    log.info("Queued download: id=%s url=%s", job.id, job.url)
    return {"job": job.to_dict()}


@router.get("/downloads")
def get_downloads() -> dict[str, Any]:
    return {"jobs": [j.to_dict() for j in downloader.list_jobs()]}


@router.get("/downloads/{job_id}")
def get_download(job_id: str) -> dict[str, Any]:
    job = downloader.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job.to_dict()}


@router.post("/play")
def post_play(payload: PlayRequest) -> dict[str, Any]:
    try:
        path = catalogue.resolve_video(payload.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # No more process kill / spawn handoff: the persistent display
    # controller swaps the slideshow/yellow content for the requested
    # video over IPC, so the TV never falls back to the Linux console
    # between modes. Recording the prior screensaver state is still
    # useful for log analysis.
    was_slideshow = screensaver.stop_for_video()

    # Wake/switch the TV in the background so playback never waits on the
    # CEC bus. Failures are logged but don't block playback.
    cec.wake_async()

    try:
        pid = player.play(path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    log.info(
        "Playing %s (pid=%s, was_slideshow=%s)", path.name, pid, was_slideshow
    )
    return {"status": "playing", "filename": path.name, "pid": pid}


@router.post("/stop")
def post_stop() -> dict[str, Any]:
    was_running = player.stop()
    return {"status": "stopped", "was_playing": was_running}


@router.get("/status")
def get_status() -> dict[str, Any]:
    return player.get_state()


def _ensure_playing() -> None:
    if not player.is_playing():
        raise HTTPException(status_code=409, detail="Nothing is playing")


@router.post("/control/pause")
def post_pause(payload: PauseRequest) -> dict[str, Any]:
    _ensure_playing()
    try:
        if payload.paused is None:
            new_paused = player.toggle_pause()
        else:
            new_paused = player.set_paused(payload.paused)
    except player.PlayerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "paused": new_paused}


@router.post("/control/seek")
def post_seek(payload: SeekRequest) -> dict[str, Any]:
    _ensure_playing()
    try:
        player.seek(payload.seconds)
    except player.PlayerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "seconds": payload.seconds}


@router.post("/control/volume")
def post_volume(payload: VolumeRequest) -> dict[str, Any]:
    _ensure_playing()
    try:
        new_volume = player.adjust_volume(payload.delta)
    except player.PlayerNotRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok", "volume": new_volume}


@router.post("/tv/wake")
def post_tv_wake() -> dict[str, Any]:
    ok, message = cec.wake()
    if not ok:
        raise HTTPException(status_code=500, detail=message)
    return {"status": "ok", "message": message}


@router.post("/tv/sleep")
def post_tv_sleep() -> dict[str, Any]:
    ok, message = cec.standby()
    if not ok:
        raise HTTPException(status_code=500, detail=message)
    return {"status": "ok", "message": message}
