"""Playback API: dispatches between video (framebuffer) and audio (headless).

Two physical mpv backends sit behind this facade:

- ``display.py`` — owns the framebuffer. Plays video fullscreen on HDMI.
  Also runs the slideshow / yellow idle modes between videos.
- ``audio_player.py`` — runs headless (``--no-video --vo=null``). Plays
  audio over the same HDMI/ALSA device. Because it never touches the
  framebuffer, the slideshow on screen keeps running while music plays.

At any moment at most one of the two backends is "playing":

- ``play_video(path)`` stops audio first (the user clearly wants the TV
  to switch to that video; we don't want music underneath it).
- ``play_audio(path)`` stops video first (mutual exclusion of the audio
  device, and the user just chose a different track).

The status / control entry points pick whichever backend is active. So
the existing remote (Pause / Seek / Volume / Stop) works for both,
unmodified, and the front-end only needs to know what kind of media is
playing for cosmetic labelling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.services import audio_player, display

log = logging.getLogger(__name__)


class PlayerNotRunning(RuntimeError):
    """Raised when an IPC command is issued but nothing is playing."""


# --- Mode helpers -------------------------------------------------------

def _video_active() -> bool:
    return display.is_video_mode()


def _audio_active() -> bool:
    return audio_player.is_playing()


def is_playing() -> bool:
    return _video_active() or _audio_active()


def active_kind() -> str | None:
    """Return ``"video"``, ``"audio"``, or ``None``."""

    if _video_active():
        return "video"
    if _audio_active():
        return "audio"
    return None


# --- Start / stop -------------------------------------------------------

def play(path: Path) -> int:
    """Backwards-compatible alias for ``play_video``."""

    return play_video(path)


def play_video(path: Path) -> int:
    """Play ``path`` as a video on the framebuffer."""

    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    # If audio is currently playing, stop it so we don't get music under
    # the new video.
    if _audio_active():
        audio_player.stop()

    try:
        display.play_video(path, title=path.name)
    except FileNotFoundError:
        raise
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc

    state = display.get_state()
    log.info("Video playback requested: %s (mode=%s)", path.name, state.get("mode"))
    return 0


def play_audio(path: Path) -> int:
    """Play ``path`` as audio without disturbing the framebuffer.

    The slideshow / yellow idle screen on the TV keeps running; only the
    audio device is taken over.
    """

    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    # Mutual exclusion with video: if a video is on screen the user just
    # asked us to switch to a music track instead, so stop the video and
    # let the display fall back to its idle (slideshow / yellow).
    if _video_active():
        display.stop_video()

    try:
        audio_player.play(path, title=path.name)
    except FileNotFoundError:
        raise
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc

    log.info("Audio playback requested: %s", path.name)
    return 0


def stop() -> bool:
    """Stop whichever backend is playing. Returns True if anything was."""

    stopped = False
    if _video_active():
        stopped = display.stop_video() or stopped
    if _audio_active():
        stopped = audio_player.stop() or stopped
    return stopped


# --- Remote-control dispatch -------------------------------------------

def _ensure_playing_or_raise() -> None:
    if not is_playing():
        raise PlayerNotRunning("Nothing is playing")


def toggle_pause() -> bool:
    _ensure_playing_or_raise()
    if _audio_active():
        try:
            return audio_player.toggle_pause()
        except audio_player.AudioPlayerNotRunning as exc:
            raise PlayerNotRunning(str(exc)) from exc
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
    _ensure_playing_or_raise()
    if _audio_active():
        try:
            return audio_player.set_paused(paused)
        except audio_player.AudioPlayerNotRunning as exc:
            raise PlayerNotRunning(str(exc)) from exc
    try:
        display.set_property("pause", bool(paused))
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    return bool(paused)


def seek(seconds: float) -> None:
    _ensure_playing_or_raise()
    if _audio_active():
        try:
            audio_player.seek(seconds)
        except audio_player.AudioPlayerNotRunning as exc:
            raise PlayerNotRunning(str(exc)) from exc
        return
    try:
        reply = display.ipc_request(["seek", float(seconds), "relative"])
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv seek failed: {err}")


def adjust_volume(delta: float) -> float:
    _ensure_playing_or_raise()
    if _audio_active():
        try:
            return audio_player.adjust_volume(delta)
        except audio_player.AudioPlayerNotRunning as exc:
            raise PlayerNotRunning(str(exc)) from exc
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


# --- Status -------------------------------------------------------------

def get_state() -> dict[str, Any]:
    """Snapshot of playback state for the status endpoint.

    Audio takes priority over video for reporting only when video isn't
    on screen, but the two are mutually exclusive in practice (the start
    paths stop the other backend) so order rarely matters.
    """

    if _video_active():
        state: dict[str, Any] = {"playing": True, "kind": "video"}
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

    if _audio_active():
        state = audio_player.get_state()
        if state.get("playing"):
            state["kind"] = "audio"
        return state

    return {"playing": False}
