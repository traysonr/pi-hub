"""Video playback API.

Thin facade over `app.services.display`, which owns the persistent mpv
process. This module exists to keep the public function names the rest
of the app already imports (`play`, `stop`, `is_playing`, `toggle_pause`,
`seek`, `adjust_volume`, `get_state`) while the heavy lifting moved
into the display controller.

The single mpv process means starting and stopping a video is now an
IPC ``loadfile`` (or transition back to the idle mode) rather than
fork+exec, which is what eliminates the brief flash to the Linux
console on every transition.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.services import display

log = logging.getLogger(__name__)


class PlayerNotRunning(RuntimeError):
    """Raised when an IPC command is issued but no video is playing."""


def is_playing() -> bool:
    return display.is_video_mode()


def play(path: Path) -> int:
    """Play ``path`` fullscreen on HDMI.

    Returns the mpv pid as an opaque integer for compatibility with the
    previous return type. Callers used to use this only for logging.
    """

    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    try:
        display.play_video(path, title=path.name)
    except FileNotFoundError:
        raise
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc

    state = display.get_state()
    # The display controller doesn't expose the mpv pid directly; the
    # value is now informational only. Return 0 as a stable placeholder
    # so JSON responses keep their shape.
    log.info("Playback requested: %s (mode=%s)", path.name, state.get("mode"))
    return 0


def stop() -> bool:
    """Stop the current video and return to the idle mode.

    Returns True if a video was actually playing before the call.
    """

    return display.stop_video()


# --- IPC control --------------------------------------------------------

def _ensure_video_or_raise() -> None:
    if not display.is_video_mode():
        raise PlayerNotRunning("Nothing is playing")


def toggle_pause() -> bool:
    _ensure_video_or_raise()
    try:
        current = bool(display.get_property("pause"))
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    new_value = not current
    try:
        display.set_property("pause", new_value)
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    return new_value


def set_paused(paused: bool) -> bool:
    _ensure_video_or_raise()
    try:
        display.set_property("pause", bool(paused))
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    return bool(paused)


def seek(seconds: float) -> None:
    _ensure_video_or_raise()
    try:
        reply = display.ipc_request(["seek", float(seconds), "relative"])
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv seek failed: {err}")


def adjust_volume(delta: float) -> float:
    _ensure_video_or_raise()
    try:
        try:
            current = float(display.get_property("volume") or 0.0)
        except (TypeError, ValueError):
            current = 100.0
        new_volume = max(0.0, min(150.0, current + float(delta)))
        display.set_property("volume", new_volume)
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    return new_volume


def get_state() -> dict[str, Any]:
    """Snapshot of playback state for the status endpoint.

    Always safe to call: returns ``{"playing": False}`` when nothing is
    in video mode (which now also implies the TV is on the idle screen,
    not the Linux console).
    """

    if not display.is_video_mode():
        return {"playing": False}

    state: dict[str, Any] = {"playing": True}
    for prop, key in (
        ("pause", "paused"),
        ("volume", "volume"),
        ("time-pos", "position"),
        ("duration", "duration"),
        ("media-title", "title"),
        ("filename", "filename"),
    ):
        try:
            state[key] = display.get_property(prop)
        except (display.DisplayNotRunning, RuntimeError):
            state[key] = None
    return state
