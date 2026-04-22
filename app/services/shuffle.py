"""Continuous random-playback mode for the music library.

Design:

- Shuffle is a pure coordinator on top of ``audio_player``. It holds a
  single boolean ("is the user in shuffle mode?") and a pointer to the
  track currently playing. It never owns an mpv process of its own.
- Starting shuffle picks a random track and hands it to
  ``audio_player.play``. We then subscribe to the audio end-file event
  so the next track is queued automatically when the current one ends
  naturally. No background polling required.
- Shuffle is implicitly cancelled whenever something interrupts audio
  playback from outside shuffle: pressing Stop on the remote, playing a
  video, or manually starting a single track. Those call sites use
  ``stop(also_stop_audio=False)`` so shuffle's flag gets cleared without
  double-stopping the audio mpv.
"""

from __future__ import annotations

import logging
import random
import threading
from typing import Any

from app.services import audio_player, catalogue

log = logging.getLogger(__name__)

_lock = threading.Lock()
_active = False
_current_filename: str | None = None
_initialized = False
# Chronological list of previously-played track filenames (oldest first).
# Bounded so a long shuffle session doesn't grow memory without end.
_history: list[str] = []
_HISTORY_MAX = 50


def init() -> None:
    """Register the end-of-track hook. Idempotent."""

    global _initialized
    if _initialized:
        return
    audio_player.register_end_callback(_on_track_end)
    _initialized = True


def is_active() -> bool:
    with _lock:
        return _active


def current_filename() -> str | None:
    with _lock:
        return _current_filename


def start() -> dict[str, Any]:
    """Enter shuffle mode and kick off the first random track."""

    tracks = catalogue.list_music()
    if not tracks:
        raise RuntimeError("No music tracks available to shuffle")

    global _active
    with _lock:
        _active = True

    try:
        _play_next()
    except Exception:
        # Roll back the flag if we couldn't start anything at all.
        with _lock:
            _active = False
        raise

    return {"active": is_active(), "current": current_filename()}


def stop(*, also_stop_audio: bool = True) -> bool:
    """Exit shuffle mode. Returns True if it was previously active.

    Pass ``also_stop_audio=False`` from call sites that are already
    tearing down audio themselves (``player.stop``, ``player.play_video``,
    ``player.play_audio``) to avoid redundant IPC work.
    """

    global _active, _current_filename
    with _lock:
        was_active = _active
        _active = False
        _current_filename = None
        _history.clear()

    if was_active and also_stop_audio and audio_player.is_playing():
        try:
            audio_player.stop()
        except Exception:
            log.exception("shuffle: failed to stop audio on shuffle-stop")

    if was_active:
        log.info("Shuffle: stopped")
    return was_active


def next_track() -> dict[str, Any]:
    """Skip to a new random track. No-op if shuffle isn't active."""

    if not is_active():
        raise RuntimeError("Shuffle is not active")
    _play_next()
    return {"active": is_active(), "current": current_filename()}


def prev_track() -> dict[str, Any]:
    """Replay the previously-played track. No-op if nothing in history."""

    global _current_filename

    if not is_active():
        raise RuntimeError("Shuffle is not active")

    with _lock:
        if not _history:
            return {
                "active": True,
                "current": _current_filename,
                "went_back": False,
            }
        target = _history.pop()

    # Resolve outside the lock (filesystem ops shouldn't hold state lock).
    try:
        path = catalogue.resolve_music(target)
    except ValueError:
        # Track disappeared from disk since we queued it; skip forward
        # instead of dead-ending the remote.
        log.info("Shuffle: previous track %s missing; advancing", target)
        _play_next()
        return {"active": is_active(), "current": current_filename(), "went_back": False}

    # Play without pushing the current track onto history — going back
    # and then forward again will pick a fresh random track, which is
    # the behaviour you want for shuffle.
    audio_player.play(path, title=target)

    with _lock:
        _current_filename = target
    log.info("Shuffle: went back to %s", target)
    return {"active": True, "current": target, "went_back": True}


# --- Internals ---------------------------------------------------------

def _on_track_end(reason: str) -> None:
    """Called by audio_player when a track finishes (eof/error)."""

    global _active
    with _lock:
        if not _active:
            return
    try:
        _play_next()
    except Exception:
        log.exception("shuffle: failed to queue next track; stopping shuffle")
        with _lock:
            _active = False


def _play_next() -> None:
    """Pick a random track and start it. Assumes ``_active`` is True.

    Avoids repeating the same track twice in a row whenever the library
    has more than one track. Any other policy (weighted, no-repeat
    history, etc.) can be added here later without touching callers.
    """

    global _current_filename, _active

    tracks = catalogue.list_music()
    if not tracks:
        log.info("Shuffle: music library empty; disabling shuffle")
        with _lock:
            _active = False
        return

    pool = tracks
    with _lock:
        last = _current_filename
    if last and len(tracks) > 1:
        filtered = [t for t in tracks if t.filename != last]
        if filtered:
            pool = filtered

    choice = random.choice(pool)
    path = catalogue.resolve_music(choice.filename)
    title = choice.title or choice.filename

    audio_player.play(path, title=title)

    with _lock:
        # Push the previous track onto history so "prev" can get back to
        # it. Cap the list to avoid unbounded growth over long sessions.
        if _current_filename and _current_filename != choice.filename:
            _history.append(_current_filename)
            if len(_history) > _HISTORY_MAX:
                del _history[: len(_history) - _HISTORY_MAX]
        _current_filename = choice.filename
    log.info("Shuffle: now playing %s", choice.filename)
