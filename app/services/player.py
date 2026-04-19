"""HDMI playback via the mpv subprocess.

Playback is controlled out-of-process via mpv's JSON IPC socket so the
web UI can pause, seek, and adjust volume on a video that is already
running."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_player_lock = threading.Lock()
_current: subprocess.Popen | None = None

# Audio output target. Defaults to ALSA HDMI on the Pi (card 1, vc4hdmi).
# Override with PI_HUB_AUDIO_DEVICE if the system uses a different card,
# e.g. "alsa/plughw:0,0" for the analog jack or "auto" to let mpv decide.
_AUDIO_DEVICE = os.environ.get("PI_HUB_AUDIO_DEVICE", "alsa/plughw:1,0")

_IPC_SOCKET = "/tmp/pi-hub-mpv.sock"
_IPC_TIMEOUT = 1.5


def _mpv_path() -> str | None:
    return shutil.which("mpv")


def _terminate_locked(timeout: float = 3.0) -> None:
    """Stop the active mpv process while holding `_player_lock`."""

    global _current
    proc = _current
    if proc is None:
        return

    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
        except OSError as exc:
            log.warning("Failed to stop mpv pid=%s: %s", proc.pid, exc)

    _current = None

    # Stale socket files can confuse the next mpv launch; clean up best effort.
    try:
        os.unlink(_IPC_SOCKET)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug("Could not remove mpv socket %s: %s", _IPC_SOCKET, exc)


def stop() -> bool:
    """Stop any current playback. Returns True if a process was running."""

    with _player_lock:
        was_running = _current is not None and _current.poll() is None
        _terminate_locked()
        return was_running


def is_playing() -> bool:
    with _player_lock:
        return _current is not None and _current.poll() is None


def play(path: Path) -> int:
    """Stop any current playback and play `path` fullscreen via mpv."""

    binary = _mpv_path()
    if binary is None:
        raise RuntimeError("mpv is not installed on the server")

    if not path.is_file():
        raise FileNotFoundError(f"Video not found: {path}")

    cmd = [
        binary,
        "--fullscreen",
        "--really-quiet",
        "--no-terminal",
        "--no-input-terminal",
        f"--input-ipc-server={_IPC_SOCKET}",
        # Performance tuning for the Pi 3 (software decode, modest GPU).
        "--profile=fast",
        "--vo=gpu",
        "--gpu-context=drm",
        "--hwdec=no",
        "--cache=yes",
        "--cache-secs=10",
        "--framedrop=vo",
        "--video-sync=audio",
    ]
    if _AUDIO_DEVICE and _AUDIO_DEVICE.lower() != "auto":
        cmd.append(f"--audio-device={_AUDIO_DEVICE}")
    cmd.append(str(path))

    global _current
    with _player_lock:
        _terminate_locked()
        log.info("Starting playback: %s", path.name)
        try:
            _current = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            log.exception("Failed to start mpv")
            raise RuntimeError(f"Failed to start mpv: {exc}") from exc
        return _current.pid


# --- IPC control --------------------------------------------------------

class PlayerNotRunning(RuntimeError):
    """Raised when an IPC command is issued but mpv is not playing."""


def _ipc_request(command: list[Any]) -> dict[str, Any]:
    """Send a single JSON IPC request to mpv and return the parsed reply.

    Raises `PlayerNotRunning` if the socket is missing or the connection
    is refused (i.e. nothing is currently playing)."""

    if not is_playing():
        raise PlayerNotRunning("Nothing is playing")

    request_id = uuid.uuid4().int & 0xFFFFFFFF
    payload = json.dumps({"command": command, "request_id": request_id}) + "\n"

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_IPC_TIMEOUT)
    try:
        try:
            sock.connect(_IPC_SOCKET)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            raise PlayerNotRunning(f"mpv IPC unavailable: {exc}") from exc

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
            # Replies are newline-delimited JSON; scan for ours.
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
                # Otherwise it's an async event; keep reading.
        raise RuntimeError("mpv closed IPC before responding")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _set_property(name: str, value: Any) -> None:
    reply = _ipc_request(["set_property", name, value])
    if reply.get("error") not in (None, "success"):
        raise RuntimeError(f"mpv set_property {name} failed: {reply.get('error')}")


def _get_property(name: str) -> Any:
    reply = _ipc_request(["get_property", name])
    if reply.get("error") not in (None, "success"):
        raise RuntimeError(f"mpv get_property {name} failed: {reply.get('error')}")
    return reply.get("data")


def toggle_pause() -> bool:
    """Flip the paused state. Returns the new paused value."""

    current = bool(_get_property("pause"))
    new_value = not current
    _set_property("pause", new_value)
    return new_value


def set_paused(paused: bool) -> bool:
    _set_property("pause", bool(paused))
    return bool(paused)


def seek(seconds: float) -> None:
    """Seek relative to the current position (positive or negative)."""

    reply = _ipc_request(["seek", float(seconds), "relative"])
    if reply.get("error") not in (None, "success"):
        raise RuntimeError(f"mpv seek failed: {reply.get('error')}")


def adjust_volume(delta: float) -> float:
    """Change volume by `delta` (in mpv's 0-100 range). Returns new volume."""

    try:
        current = float(_get_property("volume") or 0.0)
    except (TypeError, ValueError):
        current = 100.0
    new_volume = max(0.0, min(150.0, current + float(delta)))
    _set_property("volume", new_volume)
    return new_volume


def get_state() -> dict[str, Any]:
    """Return a snapshot of the current playback state.

    Always safe to call: returns `{"playing": False}` when mpv isn't running."""

    if not is_playing():
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
            state[key] = _get_property(prop)
        except (PlayerNotRunning, RuntimeError):
            state[key] = None
    return state
