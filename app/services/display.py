"""Persistent HDMI display controller.

A single long-lived `mpv` process owns the framebuffer. Switching between
video, slideshow, and yellow-fallback "modes" is done through mpv's JSON
IPC instead of killing and respawning the process. That eliminates the
brief flash to the Linux console that otherwise appears whenever no
fullscreen client is bound to DRM.

Three logical modes:

- ``video``: a single video file with audio, fast profile.
- ``slideshow``: an mpv playlist of cached images, looped, no audio.
- ``yellow_fallback``: a single solid-yellow image, looped, no audio.

The controller automatically returns to the configured idle mode
(slideshow when the user has the screensaver enabled, otherwise the
yellow fallback) when video playback ends -- whether that's a manual
stop or the file reaching EOF. The latter is detected by listening for
mpv's ``end-file`` events on the IPC socket from a background thread.

Threading model:

- ``_lock`` guards all controller state and serializes mode transitions
  so two requests can't race over the IPC socket.
- A daemon ``event-listener`` thread reads the IPC socket and feeds
  ``end-file`` events back to the controller for auto-transition.
- A daemon ``supervisor`` thread restarts mpv if it dies unexpectedly so
  the TV is never left on the console.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import SCREENSAVER_CACHE_DIR

log = logging.getLogger(__name__)


# --- Constants ---------------------------------------------------------

# Single IPC socket for the persistent mpv process. The previous
# per-feature sockets are obsolete now that one mpv handles everything.
_IPC_SOCKET = "/tmp/pi-hub-mpv.sock"
_IPC_TIMEOUT = 1.5

# How long to wait between restart attempts if mpv keeps dying. Long
# enough to avoid a tight crash loop, short enough to recover quickly
# from a transient failure.
_RESTART_BACKOFF_SECONDS = 2.0

# Brief startup probe to surface immediate launch failures (bad args,
# missing display) instead of silently looping in the supervisor.
_MPV_STARTUP_PROBE_SECONDS = 0.6

_MPV_LOG_PATH = Path("/tmp/pi-hub-mpv.log")

# Pi 3 (Broadcom VC4 V3D 2.1) advertises GL_MAX_TEXTURE_SIZE = 2048.
# Any image plane larger than this fails its glTexImage2D upload and the
# slide renders with corrupted colours (the original "red/black" bug).
# We downscale every slideshow image to fit inside this box before
# upload. Override via env if running on a Pi with a larger cap.
_MAX_TEXTURE_DIM = int(os.environ.get("PI_HUB_MAX_TEXTURE_DIM", "2048"))

# Solid yellow placeholder image written under SCREENSAVER_CACHE_DIR so
# the user never sees the underlying console. Generated on first start
# if it doesn't already exist; safe to delete (will be regenerated).
_YELLOW_PNG_NAME = "_pi-hub-yellow.png"

# Audio output target for video playback. Reused from the legacy player
# config so existing PI_HUB_AUDIO_DEVICE overrides keep working.
_AUDIO_DEVICE = os.environ.get("PI_HUB_AUDIO_DEVICE", "alsa/plughw:1,0")


# --- Public types ------------------------------------------------------

MODE_OFF = "off"
MODE_VIDEO = "video"
MODE_SLIDESHOW = "slideshow"
MODE_YELLOW = "yellow_fallback"


@dataclass
class _State:
    """Snapshot of what the controller is currently doing."""

    mode: str = MODE_OFF
    # Idle mode the controller falls back to when nothing is playing.
    # Updated whenever the screensaver "enabled" flag flips.
    idle_mode: str = MODE_YELLOW
    # The currently loaded video file path (when mode == video).
    video_path: str | None = None
    # Optional metadata for the currently loaded video (display title).
    video_title: str | None = None
    last_error: str | None = None
    started_at: float | None = None
    # Image hold time used the last time we entered slideshow mode, so
    # the screensaver service can change it via reload.
    slideshow_image_seconds: int = 60
    # mpv assigns a stable playlist_entry_id to each started file.
    # Tracking the active one lets us ignore stale end-file events that
    # belong to content we intentionally replaced.
    active_playlist_entry_id: int | None = None
    # While swapping idle content for a requested video, the outgoing
    # slideshow image (and, in unlucky timing windows, one more slide
    # that started before the replace settled) can still emit start/end
    # events. We remember those entry IDs so a slideshow EOF can't be
    # misread as "the requested video already ended".
    stale_playlist_entry_ids: set[int] = field(default_factory=set)
    pending_video_path: str | None = None


# --- Module state (guarded by _lock) -----------------------------------

_lock = threading.RLock()
_state = _State()
_proc: subprocess.Popen | None = None
_listener_thread: threading.Thread | None = None
_listener_stop = threading.Event()
_supervisor_thread: threading.Thread | None = None
_supervisor_stop = threading.Event()

# Pluggable hook so the screensaver service can supply the current
# playlist when the controller wants to (re)enter slideshow mode. The
# controller intentionally doesn't reach back into screensaver itself
# to avoid an import cycle. Returns a Path to an m3u file or None if no
# images are cached yet.
_slideshow_playlist_provider: Callable[[], Path | None] | None = None


# --- Public API --------------------------------------------------------

def init() -> None:
    """Start the persistent mpv process and the supervisor.

    Idempotent: a second call is a no-op if the controller is already
    running. Called from FastAPI startup so the TV is showing *something*
    (slideshow if available, otherwise yellow) within a second of boot.
    """

    with _lock:
        _ensure_yellow_asset()
        if _proc is not None and _proc.poll() is None:
            return
        _spawn_mpv_locked()
        # Give mpv a moment to bind the IPC socket before we issue the
        # first command. Short enough to feel instant; long enough that
        # the first loadfile in `_apply_idle_locked` doesn't race the
        # socket coming up.
        _wait_for_socket(_IPC_SOCKET, timeout=2.0)
        _start_supervisor_locked()
        _start_listener_locked()
        _apply_idle_locked()


def shutdown() -> None:
    """Stop the supervisor and tear down mpv. Used by tests; the running
    server intentionally keeps mpv alive for the lifetime of the
    process."""

    global _proc
    _supervisor_stop.set()
    _listener_stop.set()
    with _lock:
        proc = _proc
        _proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3.0)
            except OSError:
                pass
        _state.mode = MODE_OFF
        _state.video_path = None
        _state.video_title = None
        _state.started_at = None
        _state.active_playlist_entry_id = None
        _state.stale_playlist_entry_ids.clear()
        _state.pending_video_path = None
    _cleanup_socket()


def get_state() -> dict[str, Any]:
    """Return a JSON-friendly snapshot of the controller state."""

    with _lock:
        return {
            "mode": _state.mode,
            "idle_mode": _state.idle_mode,
            "video_path": _state.video_path,
            "video_title": _state.video_title,
            "last_error": _state.last_error,
            "started_at": _state.started_at,
            "slideshow_image_seconds": _state.slideshow_image_seconds,
            "mpv_alive": _is_proc_alive(),
        }


def is_video_mode() -> bool:
    with _lock:
        return _state.mode == MODE_VIDEO and _is_proc_alive()


def set_slideshow_playlist_provider(
    provider: Callable[[], Path | None] | None,
) -> None:
    """Register the callback used to (re)build the slideshow playlist.

    Called by the screensaver service so this module doesn't need to
    import it (which would create an import cycle).
    """

    global _slideshow_playlist_provider
    _slideshow_playlist_provider = provider


def set_idle_mode(idle_mode: str) -> dict[str, Any]:
    """Change the idle fallback mode (slideshow vs yellow).

    If the controller is currently in an idle state, applies it
    immediately so the TV reflects the change without waiting for the
    next video to end.
    """

    if idle_mode not in (MODE_SLIDESHOW, MODE_YELLOW):
        raise ValueError(f"Invalid idle mode: {idle_mode!r}")

    with _lock:
        _state.idle_mode = idle_mode
        # Don't kick the user out of a video they're actively watching.
        if _state.mode != MODE_VIDEO:
            _apply_idle_locked()
    return get_state()


def set_slideshow_image_seconds(seconds: int) -> None:
    """Update the per-image hold time for slideshow mode.

    If the slideshow is currently running, applies live via IPC so the
    new timing takes effect on the next slide without restarting mpv.
    """

    seconds = int(seconds)
    with _lock:
        _state.slideshow_image_seconds = seconds
        if _state.mode == MODE_SLIDESHOW and _is_proc_alive():
            try:
                _set_property_locked("image-display-duration", seconds)
            except RuntimeError as exc:
                log.warning("Failed to update image-display-duration live: %s", exc)


def play_video(path: Path, *, title: str | None = None) -> None:
    """Switch into video mode and start playing ``path``.

    Re-uses the existing mpv process: a single ``loadfile`` IPC command
    swaps the slideshow/yellow content for the video without ever
    releasing the framebuffer.
    """

    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    with _lock:
        if not _ensure_running_locked():
            raise RuntimeError(_state.last_error or "Display controller not available")

        # Configure mpv for video playback BEFORE loadfile. mpv applies
        # property values to the file as it loads, so things like
        # loop-playlist (carried over from slideshow mode) need to be
        # cleared before the load or the video is treated as one
        # iteration of an infinite image loop -- mpv reports it as
        # eof'ing immediately and the listener kicks us back to idle.
        _configure_for_video_locked()

        # Mark the state BEFORE loadfile so the listener thread has the
        # right context if mpv emits start/end-file events for the
        # outgoing idle content during the replace handoff.
        _state.mode = MODE_VIDEO
        _state.video_path = str(path)
        _state.video_title = title or path.name
        _state.started_at = time.time()
        _state.last_error = None
        _state.pending_video_path = str(path)
        _state.stale_playlist_entry_ids.clear()
        if _state.active_playlist_entry_id is not None:
            _state.stale_playlist_entry_ids.add(_state.active_playlist_entry_id)

        _ipc_request_locked(["loadfile", str(path), "replace"])

        # mpv applies the *current* pause property to the new file,
        # which can leave videos stuck on frame 1 if anything ever
        # toggled pause. Force playback to start.
        _safe_set("pause", False)
    log.info("Display: entering video mode (%s)", path.name)


def stop_video() -> bool:
    """Exit video mode and immediately apply the idle fallback.

    Returns True if a video was actually playing.
    """

    with _lock:
        was_playing = _state.mode == MODE_VIDEO
        if was_playing:
            log.info("Display: stop video, returning to %s", _state.idle_mode)
        _apply_idle_locked()
        return was_playing


def show_slideshow_now() -> bool:
    """Force the slideshow on screen, regardless of the current mode.

    Returns True if the slideshow actually started, False if no images
    were available (in which case the yellow fallback is shown instead
    so the TV is never blank).
    """

    with _lock:
        if not _ensure_running_locked():
            raise RuntimeError(_state.last_error or "Display controller not available")
        if _enter_slideshow_locked():
            return True
        _enter_yellow_locked()
        return False


def show_yellow_now() -> dict[str, Any]:
    """Force the yellow fallback on screen, regardless of mode."""

    with _lock:
        if not _ensure_running_locked():
            raise RuntimeError(_state.last_error or "Display controller not available")
        _enter_yellow_locked()
    return get_state()


def reapply_idle() -> dict[str, Any]:
    """Force-refresh the current idle mode (e.g. after slideshow images
    were refreshed and we want the new playlist live)."""

    with _lock:
        if _state.mode != MODE_VIDEO:
            _apply_idle_locked()
    return get_state()


# --- IPC primitives ----------------------------------------------------

class DisplayNotRunning(RuntimeError):
    """Raised when an IPC command is issued but mpv isn't running."""


def ipc_request(command: list[Any]) -> dict[str, Any]:
    """Public IPC helper used by the legacy player facade for pause /
    seek / volume / get_property calls against the active video.

    The lock isn't held across the network round trip so a slow mpv
    can't block other readers (e.g. the status endpoint)."""

    if not _is_proc_alive():
        raise DisplayNotRunning("Display controller not running")
    return _ipc_request_unlocked(command)


def set_property(name: str, value: Any) -> None:
    reply = ipc_request(["set_property", name, value])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv set_property {name} failed: {err}")


def get_property(name: str) -> Any:
    reply = ipc_request(["get_property", name])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv get_property {name} failed: {err}")
    return reply.get("data")


# --- Internals: mpv lifecycle -----------------------------------------

def _is_proc_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _ensure_running_locked() -> bool:
    """Make sure mpv is up; spawn it if necessary. Returns True on
    success, False if the launch failed."""

    if _is_proc_alive():
        return True
    _spawn_mpv_locked()
    if _is_proc_alive():
        _wait_for_socket(_IPC_SOCKET, timeout=2.0)
        _start_supervisor_locked()
        _start_listener_locked()
        return True
    return False


def _spawn_mpv_locked() -> None:
    """Launch the persistent mpv process in idle/force-window mode.

    No content is loaded yet; callers issue ``loadfile`` / ``loadlist``
    over IPC to put something on screen.
    """

    global _proc

    binary = shutil.which("mpv")
    if binary is None:
        _state.last_error = "mpv is not installed on the server"
        log.error(_state.last_error)
        _proc = None
        return

    _cleanup_socket()

    cmd = [
        binary,
        "--idle=yes",
        "--force-window=yes",
        "--fullscreen",
        # mpv writes its diagnostic output to a real log file (instead
        # of stdout/stderr which --no-terminal silences). We need the
        # diagnostics to debug end-file events; without them the
        # listener thread sees `eof` 1ms after loadfile with no
        # explanation in any log.
        f"--log-file={_MPV_LOG_PATH}",
        "--msg-level=all=info",
        "--no-terminal",
        "--no-input-terminal",
        # GPU/DRM path identical to the legacy player so HDMI behavior
        # on the Pi is unchanged.
        "--vo=gpu",
        "--gpu-context=drm",
        "--hwdec=no",
        # Hide the "no file" UI when nothing is loaded; we always have
        # something queued via loadfile within milliseconds anyway.
        "--osd-level=0",
        # Solid black background for any moment a frame hasn't been
        # rendered yet. (Yellow is loaded as content; this is just the
        # mpv chrome behind it.)
        "--background=color",
        "--background-color=#000000",
        # Skip undecodable files instead of bailing the whole playlist.
        "--keep-open=no",
        # Always start playback unpaused regardless of what the
        # previous file's pause state was.
        "--pause=no",
        f"--input-ipc-server={_IPC_SOCKET}",
    ]
    if _AUDIO_DEVICE and _AUDIO_DEVICE.lower() != "auto":
        cmd.append(f"--audio-device={_AUDIO_DEVICE}")

    try:
        log_fh: Any = open(_MPV_LOG_PATH, "wb")
    except OSError as exc:
        log.warning("Could not open mpv log %s: %s", _MPV_LOG_PATH, exc)
        log_fh = subprocess.DEVNULL

    try:
        _proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        _state.last_error = f"Failed to start mpv: {exc}"
        log.exception("mpv launch failed")
        _proc = None
        if hasattr(log_fh, "close"):
            try:
                log_fh.close()
            except OSError:
                pass
        return

    if hasattr(log_fh, "close"):
        try:
            log_fh.close()
        except OSError:
            pass

    # Catch immediate startup failure so we don't claim success.
    try:
        rc = _proc.wait(timeout=_MPV_STARTUP_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        rc = None

    if rc is not None:
        tail = _read_mpv_log_tail()
        _state.last_error = f"mpv exited immediately (rc={rc}): {tail or 'no output'}"
        log.error("mpv died at startup (rc=%s): %s", rc, tail)
        _proc = None
        return

    log.info("Display mpv started (pid=%s)", _proc.pid)
    _state.last_error = None


def _start_supervisor_locked() -> None:
    """Background watchdog that restarts mpv if it crashes."""

    global _supervisor_thread

    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return

    _supervisor_stop.clear()

    def _worker() -> None:
        while not _supervisor_stop.is_set():
            time.sleep(1.0)
            with _lock:
                if _supervisor_stop.is_set():
                    return
                if _is_proc_alive():
                    continue
                log.warning("Display mpv died; restarting in %.1fs", _RESTART_BACKOFF_SECONDS)
            time.sleep(_RESTART_BACKOFF_SECONDS)
            with _lock:
                if _supervisor_stop.is_set():
                    return
                _spawn_mpv_locked()
                if _is_proc_alive():
                    _wait_for_socket(_IPC_SOCKET, timeout=2.0)
                    _start_listener_locked()
                    # On crash recovery, always go to idle. Anything
                    # mid-video is lost; the user can press Play again.
                    if _state.mode == MODE_VIDEO:
                        _state.mode = MODE_OFF
                        _state.video_path = None
                        _state.video_title = None
                    _apply_idle_locked()

    _supervisor_thread = threading.Thread(
        target=_worker, name="display-supervisor", daemon=True
    )
    _supervisor_thread.start()


def _start_listener_locked() -> None:
    """Subscribe to mpv events on a dedicated socket connection so
    natural EOF triggers an automatic return to the idle mode."""

    global _listener_thread

    if _listener_thread is not None and _listener_thread.is_alive():
        return

    _listener_stop.clear()

    def _worker() -> None:
        # Brief retry loop while the socket comes up after a restart.
        sock = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not _listener_stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(_IPC_TIMEOUT)
                sock.connect(_IPC_SOCKET)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                sock = None
                time.sleep(0.1)

        if sock is None:
            log.warning("Display event listener could not connect to mpv IPC")
            return

        try:
            sock.settimeout(None)
            buf = bytearray()
            while not _listener_stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except OSError as exc:
                    log.debug("Display event socket closed: %s", exc)
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    event = msg.get("event")
                    if event == "start-file":
                        _on_start_file(msg)
                    elif event == "file-loaded":
                        _on_file_loaded(msg)
                    elif event == "end-file":
                        _on_end_file(msg)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    _listener_thread = threading.Thread(
        target=_worker, name="display-event-listener", daemon=True
    )
    _listener_thread.start()


def _on_start_file(msg: dict[str, Any]) -> None:
    """Record the playlist entry ID of the file mpv just started.

    During a slideshow -> video handoff we temporarily treat started
    entries as stale until the requested video path becomes current.
    That covers the timing window where a slideshow frame ends naturally
    while a replace is in flight.
    """

    entry_id = _coerce_playlist_entry_id(msg)
    with _lock:
        if entry_id is not None:
            _state.active_playlist_entry_id = entry_id
        if _state.pending_video_path is not None:
            if not _clear_pending_video_if_current_locked() and entry_id is not None:
                _state.stale_playlist_entry_ids.add(entry_id)


def _on_file_loaded(_msg: dict[str, Any]) -> None:
    """Best-effort hook to settle a pending video handoff.

    ``start-file`` gives us the new entry ID; ``file-loaded`` is another
    reliable point where the ``path`` property is expected to reflect the
    file that actually won the handoff race.
    """

    with _lock:
        _clear_pending_video_if_current_locked()


def _on_end_file(msg: dict[str, Any]) -> None:
    """Called from the listener thread when mpv emits an end-file event.

    We only care about end-file in video mode -- slideshow files end
    constantly as mpv advances through the playlist, and the yellow
    fallback never ends because mpv loops the single image.

    The mpv ``reason`` field tells us *why* the file ended:
    - ``eof`` -> natural completion -> back to idle
    - ``error`` -> couldn't decode -> back to idle so the TV isn't
      stuck on a broken file
    - ``stop`` -> the *previous* file was unloaded because we called
      ``loadfile ... replace`` to swap in a new one. This fires ~6ms
      after every play_video call and is NOT a signal that the user's
      video ended; ignoring it is what keeps the loaded video on
      screen instead of bouncing right back to yellow/slideshow.
    - ``redirect`` -> mpv resolved a stream redirect and is loading
      the new URL; not an end of playback.
    - ``quit`` -> mpv is shutting down -> ignore; supervisor handles it
    """

    reason = msg.get("reason")
    if reason not in ("eof", "error"):
        log.debug("Ignoring end-file (reason=%s, msg=%s)", reason, msg)
        return

    entry_id = _coerce_playlist_entry_id(msg)
    with _lock:
        if _state.mode != MODE_VIDEO:
            log.debug(
                "end-file (reason=%s) outside video mode (mode=%s); ignoring",
                reason, _state.mode,
            )
            return
        if entry_id is not None and entry_id in _state.stale_playlist_entry_ids:
            log.debug(
                "Ignoring end-file for replaced entry_id=%s during video handoff",
                entry_id,
            )
            return
        if _state.pending_video_path is not None and not _clear_pending_video_if_current_locked():
            if reason == "error" and entry_id is not None:
                log.warning(
                    "Requested video failed before becoming current "
                    "(entry_id=%s, file_error=%s); returning to idle",
                    entry_id,
                    msg.get("file_error"),
                )
                _state.last_error = (
                    f"Failed to start video: {msg.get('file_error') or 'mpv error'}"
                )
                _state.pending_video_path = None
                _state.stale_playlist_entry_ids.clear()
                _apply_idle_locked()
            else:
                log.debug("Ignoring end-file while video handoff is still pending: %s", msg)
            return
        if (
            entry_id is not None
            and _state.active_playlist_entry_id is not None
            and entry_id != _state.active_playlist_entry_id
        ):
            log.debug(
                "Ignoring end-file for stale entry_id=%s (active=%s)",
                entry_id,
                _state.active_playlist_entry_id,
            )
            return
        log.info(
            "Video ended (reason=%s, file_error=%s); applying idle mode %s",
            reason, msg.get("file_error"), _state.idle_mode,
        )
        _apply_idle_locked()


# --- Internals: mode transitions --------------------------------------

def _apply_idle_locked() -> None:
    """Apply whichever idle mode is currently configured."""

    if not _is_proc_alive():
        return
    if _state.idle_mode == MODE_SLIDESHOW:
        # Slideshow can fall back to yellow if no images are cached
        # yet, so the screen is never empty.
        if not _enter_slideshow_locked():
            _enter_yellow_locked()
    else:
        _enter_yellow_locked()


def _enter_slideshow_locked() -> bool:
    """Switch mpv into slideshow mode. Returns False if no images are
    available (caller should fall back to yellow)."""

    playlist = _resolve_slideshow_playlist()
    if playlist is None:
        return False

    _configure_for_slideshow_locked()
    try:
        _ipc_request_locked(["loadlist", str(playlist), "replace"])
    except RuntimeError as exc:
        log.warning("Failed to load slideshow playlist: %s", exc)
        return False

    _state.mode = MODE_SLIDESHOW
    _state.video_path = None
    _state.video_title = None
    _state.started_at = time.time()
    _state.pending_video_path = None
    _state.stale_playlist_entry_ids.clear()
    _state.active_playlist_entry_id = None
    log.info("Display: entered slideshow mode (%s)", playlist)
    return True


def _enter_yellow_locked() -> None:
    """Show the solid-yellow fallback image."""

    yellow = _ensure_yellow_asset()
    _configure_for_yellow_locked()
    try:
        _ipc_request_locked(["loadfile", str(yellow), "replace"])
    except RuntimeError as exc:
        log.warning("Failed to load yellow fallback: %s", exc)
        return

    _state.mode = MODE_YELLOW
    _state.video_path = None
    _state.video_title = None
    _state.started_at = time.time()
    _state.pending_video_path = None
    _state.stale_playlist_entry_ids.clear()
    _state.active_playlist_entry_id = None
    log.info("Display: entered yellow fallback mode")


def _configure_for_video_locked() -> None:
    """Set the mpv properties that should be active during video.

    Note: ``ao`` (audio output driver) is a startup-only mpv option; we
    set it via ``--audio-device`` on the command line, not here. Audio
    output is enabled by ``aid=auto`` (track selection) plus unmuting.
    """

    # Slideshow mode may leave a ``vf`` filter on the VO to work around
    # incorrect colours on some stills (see ``_configure_for_slideshow_locked``).
    # Clear it for video so we do not force an RGB conversion on every frame.
    _safe_set("vf", "")
    _safe_set("aid", "auto")
    _safe_set("vid", "auto")
    _safe_set("mute", False)
    _safe_set("loop-file", "no")
    _safe_set("loop-playlist", "no")
    _safe_set("image-display-duration", 0)
    _safe_set("keep-open", "no")


def _configure_for_slideshow_locked() -> None:
    # Pi 3 hardware reality: the VC4 GLES driver advertises
    # ``GL_MAX_TEXTURE_SIZE = 2048``. Any image whose decoded plane
    # exceeds 2048 px in either dimension fails its texture upload with
    # ``OpenGL error INVALID_VALUE`` (visible in /tmp/pi-hub-mpv.log)
    # and the slide renders as a heavily red-tinted / dark mess --
    # which is the "wrong color domain" symptom users see. Reddit
    # photos are routinely 3-5K on a side, so a large fraction of the
    # cache hits this.
    #
    # We pre-shrink in lavfi so neither dimension exceeds ``_MAX_DIM``,
    # while preserving aspect ratio. ``force_original_aspect_ratio
    # =decrease`` makes the scaler fit *inside* the box rather than
    # stretching to it, and ``-2`` is unused here because we now bound
    # both axes explicitly. Lanczos keeps the downscale crisp.
    #
    # ``lavfi=[...]`` is required (vs. a top-level ``scale=...``)
    # because the IPC ``vf`` parser chokes on the commas inside
    # ``min(...)`` when the filter is set at the top level.
    _safe_set(
        "vf",
        f"lavfi=[scale={_MAX_TEXTURE_DIM}:{_MAX_TEXTURE_DIM}"
        f":force_original_aspect_ratio=decrease:flags=lanczos]",
    )
    _safe_set("aid", "no")
    _safe_set("mute", True)
    _safe_set("loop-file", "no")
    _safe_set("loop-playlist", "inf")
    _safe_set("image-display-duration", _state.slideshow_image_seconds)
    _safe_set("keep-open", "no")


def _configure_for_yellow_locked() -> None:
    _safe_set("vf", "")
    _safe_set("aid", "no")
    _safe_set("mute", True)
    # Hold the single yellow image forever (not just for image-seconds).
    _safe_set("loop-file", "inf")
    _safe_set("loop-playlist", "no")
    _safe_set("image-display-duration", "inf")
    _safe_set("keep-open", "yes")


def _safe_set(name: str, value: Any) -> None:
    """``set_property`` that swallows individual failures.

    mpv occasionally returns ``property unavailable`` for properties
    that don't apply yet (e.g. ``aid`` before any file is loaded).
    Those are non-fatal for our purposes.
    """

    try:
        _set_property_locked(name, value)
    except RuntimeError as exc:
        log.debug("Ignoring set_property %s=%r failure: %s", name, value, exc)


def _safe_ipc(command: list[Any]) -> None:
    """``_ipc_request_locked`` that swallows individual failures.

    Used for housekeeping commands (``stop``, ``playlist-clear``)
    where a non-success reply just means there was nothing to clear --
    not something we want to abort the caller for.
    """

    try:
        _ipc_request_locked(command)
    except RuntimeError as exc:
        log.debug("Ignoring ipc %r failure: %s", command, exc)


def _resolve_slideshow_playlist() -> Path | None:
    if _slideshow_playlist_provider is None:
        return None
    try:
        return _slideshow_playlist_provider()
    except Exception:  # noqa: BLE001 -- never let a provider crash the controller
        log.exception("Slideshow playlist provider raised")
        return None


def _coerce_playlist_entry_id(msg: dict[str, Any]) -> int | None:
    raw = msg.get("playlist_entry_id")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _clear_pending_video_if_current_locked() -> bool:
    """Clear the "video handoff pending" marker once mpv reports the
    requested path as current."""

    target = _state.pending_video_path
    if not target or not _is_proc_alive():
        return False

    try:
        current = _get_path_locked()
    except RuntimeError as exc:
        log.debug("Could not query current mpv path while settling handoff: %s", exc)
        return False

    if current != target:
        return False

    _state.pending_video_path = None
    _state.stale_playlist_entry_ids.clear()
    log.debug("Video handoff settled on %s", current)
    return True


# --- Internals: IPC ----------------------------------------------------

def _ipc_request_locked(command: list[Any]) -> dict[str, Any]:
    """IPC call that assumes the caller already validated mpv is up."""

    return _ipc_request_unlocked(command)


def _set_property_locked(name: str, value: Any) -> None:
    reply = _ipc_request_locked(["set_property", name, value])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv set_property {name} failed: {err}")


def _get_path_locked() -> str | None:
    reply = _ipc_request_locked(["get_property", "path"])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv get_property path failed: {err}")
    data = reply.get("data")
    return data if isinstance(data, str) else None


def _ipc_request_unlocked(command: list[Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().int & 0xFFFFFFFF
    payload = json.dumps({"command": command, "request_id": request_id}) + "\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_IPC_TIMEOUT)
    try:
        try:
            sock.connect(_IPC_SOCKET)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise DisplayNotRunning(f"mpv IPC unavailable: {exc}") from exc

        sock.sendall(payload.encode("utf-8"))

        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise RuntimeError("mpv IPC timed out") from exc
            if not chunk:
                break
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if msg.get("request_id") == request_id:
                    return msg
                # Other messages on this socket are async events; we
                # drop them here because the listener thread on its own
                # connection is the canonical event consumer.
        raise RuntimeError("mpv closed IPC before responding")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _wait_for_socket(path: str, *, timeout: float) -> bool:
    """Poll until a Unix socket is connectable (or the timeout elapses)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.2)
        try:
            sock.connect(path)
            sock.close()
            return True
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            try:
                sock.close()
            except OSError:
                pass
            time.sleep(0.05)
    return False


def _cleanup_socket() -> None:
    try:
        os.unlink(_IPC_SOCKET)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("Could not remove mpv socket %s: %s", _IPC_SOCKET, exc)


def _read_mpv_log_tail(max_chars: int = 400) -> str:
    try:
        data = _MPV_LOG_PATH.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) > max_chars:
        text = "..." + text[-max_chars:]
    return " | ".join(line for line in text.splitlines() if line.strip())


# --- Internals: yellow PNG generator -----------------------------------

def _ensure_yellow_asset() -> Path:
    """Return the path to a solid-yellow PNG, generating it if missing.

    Implemented with stdlib zlib + struct so we don't add a Pillow
    dependency just to draw a single colored rectangle. The image is
    intentionally tiny (1x1 logical pixel) -- mpv stretches it to fill
    the screen, which is exactly what we want.
    """

    SCREENSAVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = SCREENSAVER_CACHE_DIR / _YELLOW_PNG_NAME
    if target.exists() and target.stat().st_size > 0:
        return target

    # Match Cursor's "yellow" warm and saturated rather than the harsh
    # primary-yellow you'd get from #FFFF00. RGB chosen to look pleasant
    # on a typical TV.
    rgb = (0xFF, 0xD7, 0x00)  # gold

    width = 64
    height = 64
    raw = bytearray()
    row = bytes((0,)) + bytes(rgb) * width  # filter byte + pixels
    for _ in range(height):
        raw.extend(row)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(bytes(raw), 9)
    png = signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(png)
    tmp.replace(target)
    log.info("Wrote yellow fallback image to %s", target)
    return target
