"""Headless audio playback (parallel to the framebuffer-owning display).

The framebuffer is owned by ``display.py`` (a single persistent ``mpv``
that runs the slideshow / yellow fallback / video). For *audio-only*
playback we want the slideshow to keep running on screen while music
comes out of HDMI audio. That requires a second ``mpv`` instance whose
video output is disabled (``--no-video --vo=null``) so it never touches
the framebuffer.

Design notes:

- This module is intentionally independent of ``display.py``. The two
  mpv processes share only the ALSA audio device.
- A single track plays at a time. ``play(path)`` swaps the current
  track via the same ``loadfile ... replace`` IPC pattern.
- ``stop()`` is a real stop (``loadfile`` of nothing isn't a thing in
  mpv; we use ``stop`` IPC to clear the current file). The mpv process
  stays alive in idle so subsequent plays are instant.
- A daemon listener returns us to "idle" on natural EOF so the status
  endpoint reports "not playing" once the track finishes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


_IPC_SOCKET = "/tmp/pi-hub-mpv-audio.sock"
_IPC_TIMEOUT = 1.5
_MPV_LOG_PATH = Path("/tmp/pi-hub-mpv-audio.log")
_MPV_STARTUP_PROBE_SECONDS = 0.6
_RESTART_BACKOFF_SECONDS = 2.0

# Same audio target as the display controller so mute/route is consistent.
_AUDIO_DEVICE = os.environ.get("PI_HUB_AUDIO_DEVICE", "alsa/plughw:1,0")


@dataclass
class _State:
    playing: bool = False
    path: str | None = None
    title: str | None = None
    started_at: float | None = None
    last_error: str | None = None


_lock = threading.RLock()
_state = _State()
_proc: subprocess.Popen | None = None
_listener_thread: threading.Thread | None = None
_listener_stop = threading.Event()
_supervisor_thread: threading.Thread | None = None
_supervisor_stop = threading.Event()

# End-of-track subscribers. Invoked (outside the state lock) whenever a
# track ends naturally (eof) or errors out. Used by the shuffle service
# to queue the next track without polling.
_end_callbacks: list[Callable[[str], None]] = []


def register_end_callback(cb: Callable[[str], None]) -> None:
    """Register a callback fired when a track ends (eof/error)."""

    if cb not in _end_callbacks:
        _end_callbacks.append(cb)


class AudioPlayerNotRunning(RuntimeError):
    """Raised when an IPC command is issued but mpv isn't running."""


# --- Public API --------------------------------------------------------

def init() -> None:
    """Start the persistent audio mpv. Idempotent."""

    with _lock:
        if _is_proc_alive():
            return
        _spawn_mpv_locked()
        if _is_proc_alive():
            _wait_for_socket(_IPC_SOCKET, timeout=2.0)
            _start_supervisor_locked()
            _start_listener_locked()


def shutdown() -> None:
    """Tear down the audio mpv (used by tests)."""

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
        _state.playing = False
        _state.path = None
        _state.title = None
        _state.started_at = None
    _cleanup_socket()


def is_playing() -> bool:
    with _lock:
        return _state.playing and _is_proc_alive()


def play(path: Path, *, title: str | None = None) -> None:
    """Start playing ``path`` headlessly (no framebuffer touch)."""

    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    with _lock:
        if not _ensure_running_locked():
            raise RuntimeError(_state.last_error or "Audio player not available")

        # Make sure pause / mute aren't carried over from a previous track.
        _safe_set("pause", False)
        _safe_set("mute", False)

        try:
            _ipc_request_locked(["loadfile", str(path), "replace"])
        except RuntimeError as exc:
            _state.last_error = f"loadfile failed: {exc}"
            raise

        _state.playing = True
        _state.path = str(path)
        _state.title = title or path.name
        _state.started_at = time.time()
        _state.last_error = None
    log.info("Audio: now playing %s", path.name)


def stop() -> bool:
    """Stop the current track. Returns True if something was playing."""

    with _lock:
        was_playing = _state.playing
        if _is_proc_alive():
            _safe_ipc(["stop"])
        _state.playing = False
        _state.path = None
        _state.title = None
        _state.started_at = None
    if was_playing:
        log.info("Audio: stopped")
    return was_playing


def toggle_pause() -> bool:
    _ensure_playing_or_raise()
    try:
        current = bool(get_property("pause"))
    except AudioPlayerNotRunning:
        raise
    new_value = not current
    set_property("pause", new_value)
    return new_value


def set_paused(paused: bool) -> bool:
    _ensure_playing_or_raise()
    set_property("pause", bool(paused))
    return bool(paused)


def seek(seconds: float) -> None:
    _ensure_playing_or_raise()
    reply = _ipc_request(["seek", float(seconds), "relative"])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv seek failed: {err}")


def adjust_volume(delta: float) -> float:
    _ensure_playing_or_raise()
    try:
        current = float(get_property("volume") or 0.0)
    except (TypeError, ValueError):
        current = 100.0
    new_volume = max(0.0, min(150.0, current + float(delta)))
    set_property("volume", new_volume)
    return new_volume


def get_state() -> dict[str, Any]:
    with _lock:
        if not _state.playing or not _is_proc_alive():
            return {"playing": False}
        snapshot: dict[str, Any] = {
            "playing": True,
            "title": _state.title,
            "filename": Path(_state.path).name if _state.path else None,
        }
    for prop, key in (
        ("pause", "paused"),
        ("volume", "volume"),
        ("time-pos", "position"),
        ("duration", "duration"),
        ("media-title", "media_title"),
    ):
        try:
            snapshot[key] = get_property(prop)
        except (AudioPlayerNotRunning, RuntimeError):
            snapshot[key] = None
    if not snapshot.get("title") and snapshot.get("media_title"):
        snapshot["title"] = snapshot["media_title"]
    return snapshot


# --- IPC helpers -------------------------------------------------------

def set_property(name: str, value: Any) -> None:
    reply = _ipc_request(["set_property", name, value])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv set_property {name} failed: {err}")


def get_property(name: str) -> Any:
    reply = _ipc_request(["get_property", name])
    err = reply.get("error")
    if err not in (None, "success"):
        raise RuntimeError(f"mpv get_property {name} failed: {err}")
    return reply.get("data")


def _ipc_request(command: list[Any]) -> dict[str, Any]:
    if not _is_proc_alive():
        raise AudioPlayerNotRunning("Audio player not running")
    return _ipc_request_unlocked(command)


def _ensure_playing_or_raise() -> None:
    if not is_playing():
        raise AudioPlayerNotRunning("Nothing is playing")


# --- Internals ---------------------------------------------------------

def _is_proc_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _ensure_running_locked() -> bool:
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
    """Launch the headless audio mpv. No video output, no terminal."""

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
        # Headless: no framebuffer touch at all. This is what lets the
        # slideshow keep running on screen while we play audio.
        "--no-video",
        "--vo=null",
        # Disable any default OSC/UI surfaces.
        "--osd-level=0",
        "--no-terminal",
        "--no-input-terminal",
        # Skip undecodable files instead of bailing entirely.
        "--keep-open=no",
        "--pause=no",
        f"--log-file={_MPV_LOG_PATH}",
        "--msg-level=all=info",
        f"--input-ipc-server={_IPC_SOCKET}",
    ]
    if _AUDIO_DEVICE and _AUDIO_DEVICE.lower() != "auto":
        cmd.append(f"--audio-device={_AUDIO_DEVICE}")

    try:
        log_fh: Any = open(_MPV_LOG_PATH, "wb")
    except OSError as exc:
        log.warning("Could not open audio mpv log %s: %s", _MPV_LOG_PATH, exc)
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
        _state.last_error = f"Failed to start audio mpv: {exc}"
        log.exception("audio mpv launch failed")
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

    try:
        rc = _proc.wait(timeout=_MPV_STARTUP_PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        rc = None

    if rc is not None:
        tail = _read_log_tail()
        _state.last_error = f"audio mpv exited immediately (rc={rc}): {tail or 'no output'}"
        log.error("audio mpv died at startup (rc=%s): %s", rc, tail)
        _proc = None
        return

    log.info("Audio mpv started (pid=%s)", _proc.pid)
    _state.last_error = None


def _start_supervisor_locked() -> None:
    """Restart the audio mpv if it dies; do NOT auto-resume playback."""

    global _supervisor_thread

    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return

    _supervisor_stop.clear()

    def _worker() -> None:
        while not _supervisor_stop.is_set():
            time.sleep(1.0)
            with _lock:
                if _is_proc_alive():
                    continue
                log.warning("Audio mpv missing; restarting after backoff")
                time.sleep(_RESTART_BACKOFF_SECONDS)
                _spawn_mpv_locked()
                if _is_proc_alive():
                    _wait_for_socket(_IPC_SOCKET, timeout=2.0)
                    _start_listener_locked()
                    # Crashed mid-track: drop the playing flag; the user
                    # can press Play again. We don't try to resume.
                    _state.playing = False
                    _state.path = None
                    _state.title = None

    _supervisor_thread = threading.Thread(
        target=_worker, name="audio-supervisor", daemon=True
    )
    _supervisor_thread.start()


def _start_listener_locked() -> None:
    """Listen for end-file so we can mark the player idle on natural EOF."""

    global _listener_thread

    if _listener_thread is not None and _listener_thread.is_alive():
        return

    _listener_stop.clear()

    def _worker() -> None:
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
            log.warning("Audio event listener could not connect to mpv IPC")
            return

        try:
            sock.settimeout(None)
            buf = bytearray()
            while not _listener_stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except OSError:
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
                    if msg.get("event") == "end-file":
                        _on_end_file(msg)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    _listener_thread = threading.Thread(
        target=_worker, name="audio-event-listener", daemon=True
    )
    _listener_thread.start()


def _on_end_file(msg: dict[str, Any]) -> None:
    """Mark the player idle when a track finishes naturally.

    ``stop`` reasons fire when we replace one file with another; only
    natural ``eof`` and decoding ``error`` should clear the playing
    flag (which is the only state we expose to the UI).
    """

    reason = msg.get("reason")
    if reason not in ("eof", "error"):
        return
    with _lock:
        if not _state.playing:
            return
        log.info("Audio: end-file (reason=%s); going idle", reason)
        _state.playing = False
        _state.path = None
        _state.title = None
        _state.started_at = None

    # Fire subscribers OUTSIDE the lock so callbacks can legally call back
    # into play()/stop() without deadlocking.
    for cb in list(_end_callbacks):
        try:
            cb(reason)
        except Exception:
            log.exception("audio end-file callback failed")


# --- Low-level IPC primitives -----------------------------------------

def _ipc_request_locked(command: list[Any]) -> dict[str, Any]:
    return _ipc_request_unlocked(command)


def _safe_set(name: str, value: Any) -> None:
    try:
        reply = _ipc_request_locked(["set_property", name, value])
        err = reply.get("error")
        if err not in (None, "success"):
            log.debug("Audio set_property %s=%r: %s", name, value, err)
    except RuntimeError as exc:
        log.debug("Audio set_property %s=%r failed: %s", name, value, exc)


def _safe_ipc(command: list[Any]) -> None:
    try:
        _ipc_request_locked(command)
    except RuntimeError as exc:
        log.debug("Audio ipc %r failed: %s", command, exc)


def _ipc_request_unlocked(command: list[Any]) -> dict[str, Any]:
    request_id = uuid.uuid4().int & 0xFFFFFFFF
    payload = json.dumps({"command": command, "request_id": request_id}) + "\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_IPC_TIMEOUT)
    try:
        try:
            sock.connect(_IPC_SOCKET)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise AudioPlayerNotRunning(f"audio mpv IPC unavailable: {exc}") from exc

        sock.sendall(payload.encode("utf-8"))

        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise RuntimeError("audio mpv IPC timed out") from exc
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
        raise RuntimeError("audio mpv closed IPC before responding")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _wait_for_socket(path: str, *, timeout: float) -> bool:
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
        log.debug("Could not remove audio mpv socket %s: %s", _IPC_SOCKET, exc)


def _read_log_tail(max_chars: int = 400) -> str:
    try:
        data = _MPV_LOG_PATH.read_bytes()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace").strip()
    if len(text) > max_chars:
        text = "..." + text[-max_chars:]
    return " | ".join(line for line in text.splitlines() if line.strip())
