"""Playback API: dispatches between video (framebuffer) and audio (headless).

Two physical mpv backends sit behind this facade:

- ``display.py`` — owns the framebuffer. Plays video fullscreen on HDMI.
  Also runs the slideshow / yellow idle modes between videos.
- ``audio_player.py`` — runs headless (``--no-video --vo=null``). Plays
  audio over the same HDMI/ALSA device. Because it never touches the
  framebuffer, the slideshow on screen keeps running while music plays.

Three playback kinds:

- ``play_video(path)`` — foreground video. Stops audio and shuffle first;
  the TV shows the video with sound.
- ``play_audio(path)`` — headless audio. Leaves the framebuffer alone;
  the slideshow / yellow idle keeps showing while music plays.
- ``play_bg_video(path)`` — background video. Loads the video muted on
  the framebuffer (replacing the slideshow), but does NOT stop the headless
  audio player or cancel shuffle. This lets visually pleasing content play
  on screen while the music shuffle sequence continues uninterrupted.

The status / control entry points pick whichever backend is active. In
bg-video mode audio is reported as the primary kind so the remote tab
controls (Pause / Seek / Volume / Stop) keep targeting the music player.
``bg_video_active`` in the status payload signals the UI to show the
separate "Stop BG Video" control.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.services import audio_player, display, shuffle

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


def is_bg_video_active() -> bool:
    """True only when a muted background video is on screen."""
    return display.is_bg_video_mode()


def active_kind() -> str | None:
    """Return ``"video"``, ``"audio"``, ``"bg_video"``, or ``None``.

    In bg-video mode with audio also playing, ``"audio"`` is returned so
    callers route control commands to the music player rather than the
    silent video on the framebuffer.
    """

    if display.is_bg_video_mode():
        # Audio is the primary control surface when bg video is on screen.
        return "audio" if _audio_active() else "bg_video"
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

    # Starting a video always cancels shuffle (user's explicit intent
    # per the UI contract). Clear the flag before we stop audio so an
    # in-flight end-file event doesn't try to queue another track.
    shuffle.stop(also_stop_audio=False)

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

    # Explicitly picking a single track also exits shuffle — otherwise
    # the next natural end-of-file would snap the user back into random
    # rotation, which is surprising after a deliberate "play this one".
    shuffle.stop(also_stop_audio=False)

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


def play_bg_video(path: Path) -> int:
    """Play ``path`` as a muted background video on the framebuffer.

    The headless audio player and shuffle are intentionally left alone so
    the music sequence keeps running while the video plays silently on
    screen. The slideshow is replaced by the video; when it ends (manually
    or on EOF) the display returns to its configured idle mode.

    Raises ``RuntimeError`` if a foreground video is currently playing —
    the caller should stop it first.
    """

    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    if _video_active() and not display.is_bg_video_mode():
        raise RuntimeError(
            "A foreground video is currently playing. Stop it before starting a background video."
        )

    try:
        display.play_video_bg(path, title=path.name)
    except FileNotFoundError:
        raise
    except RuntimeError as exc:
        raise RuntimeError(str(exc)) from exc

    state = display.get_state()
    log.info("BG video playback requested: %s (mode=%s)", path.name, state.get("mode"))
    return 0


def stop_bg_video() -> bool:
    """Stop the background video only; audio and shuffle are unaffected.

    Returns True if a background video was actually stopped.
    """

    if not display.is_bg_video_mode():
        return False
    stopped = display.stop_video()
    log.info("BG video stopped")
    return stopped


def stop() -> bool:
    """Stop whichever backend is playing. Returns True if anything was."""

    # Clear shuffle first so the imminent audio stop doesn't race with a
    # new track being queued by the end-of-file hook.
    shuffle_was_active = shuffle.stop(also_stop_audio=False)

    stopped = False
    if _video_active():
        stopped = display.stop_video() or stopped
    if _audio_active():
        stopped = audio_player.stop() or stopped
    return stopped or shuffle_was_active


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


# --- Background-video remote controls ----------------------------------

def _ensure_bg_video_or_raise() -> None:
    if not display.is_bg_video_mode():
        raise PlayerNotRunning("No background video is playing")


def toggle_pause_bg_video() -> bool:
    """Toggle pause on the background video. Returns the new paused state."""
    _ensure_bg_video_or_raise()
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


def set_paused_bg_video(paused: bool) -> bool:
    """Set the pause state of the background video explicitly."""
    _ensure_bg_video_or_raise()
    try:
        display.set_property("pause", bool(paused))
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    return bool(paused)


def seek_bg_video(seconds: float) -> None:
    """Seek the background video by a relative number of seconds."""
    _ensure_bg_video_or_raise()
    try:
        reply = display.ipc_request(["seek", float(seconds), "relative"])
    except display.DisplayNotRunning as exc:
        raise PlayerNotRunning(str(exc)) from exc
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv seek failed: {err}")


# --- Status -------------------------------------------------------------

def _bg_video_fields() -> dict[str, Any]:
    """Extra fields describing the active background video."""
    ds = display.get_state()
    from pathlib import Path as _Path
    raw_path = ds.get("video_path") or ""
    paused: bool | None = None
    try:
        paused = bool(display.get_property("pause"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "bg_video_active": True,
        "bg_video_title": ds.get("video_title"),
        "bg_video_filename": _Path(raw_path).name if raw_path else None,
        "bg_video_paused": paused,
    }


def get_state() -> dict[str, Any]:
    """Snapshot of playback state for the status endpoint.

    In background-video mode audio is the primary reporting surface so
    the remote tab controls keep targeting the music player. The extra
    ``bg_video_active`` / ``bg_video_title`` / ``bg_video_filename``
    fields tell the UI to show the "Stop BG Video" control.
    """

    shuffle_active = shuffle.is_active()
    bg_video = display.is_bg_video_mode()

    # --- Background-video mode: audio is primary ---
    if bg_video:
        if _audio_active():
            state: dict[str, Any] = audio_player.get_state()
            if state.get("playing"):
                state["kind"] = "audio"
        else:
            # Bg video with no audio (edge case: music stopped mid-session).
            state = {"playing": True, "kind": "bg_video"}
        state["shuffle_active"] = shuffle_active
        state.update(_bg_video_fields())
        return state

    # --- Normal foreground video ---
    if _video_active():
        state = {"playing": True, "kind": "video"}
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
        state["shuffle_active"] = shuffle_active
        state["bg_video_active"] = False
        return state

    # --- Headless audio only ---
    if _audio_active():
        state = audio_player.get_state()
        if state.get("playing"):
            state["kind"] = "audio"
        state["shuffle_active"] = shuffle_active
        state["bg_video_active"] = False
        return state

    return {"playing": False, "shuffle_active": shuffle_active, "bg_video_active": False}
