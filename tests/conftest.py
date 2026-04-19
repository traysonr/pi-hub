"""Shared pytest fixtures.

The fixtures here let us exercise the real display/screensaver/player
state machines without spawning an actual `mpv` process, which would
require a DRM-capable display the test runner doesn't have.

The substitution strategy is intentionally surgical: we replace exactly
the three boundary functions in `app.services.display` that touch the
outside world (process spawn, socket wait, IPC round trip) and leave
all the actual mode-transition / configure-for-X / playlist logic
running for real. That way the tests assert against the same code paths
production runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest


# Ensure the project root is importable as a package root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Point runtime caches at a tmp dir so tests don't pollute the user's
# real screensaver cache. Must happen BEFORE importing app.config.
_TEST_RUNTIME_DIR = Path("/tmp/pi-hub-tests")
os.environ.setdefault("PI_HUB_MEDIA_DIR", str(_TEST_RUNTIME_DIR / "media"))
os.environ.setdefault("PI_HUB_CONFIG_DIR", str(_TEST_RUNTIME_DIR / "config"))


class FakeMpv:
    """In-memory stand-in for the mpv subprocess + IPC socket.

    Records every IPC command issued by the controller so tests can
    assert on the sequence of `loadfile` / `loadlist` / `set_property`
    calls that would have hit a real mpv. Also tracks whether the
    process is "alive" so the controller's startup probe sees a live
    process.
    """

    def __init__(self) -> None:
        self.alive = True
        self.commands: list[list[Any]] = []
        # Properties the controller may query via get_property. Only
        # ``pause`` and ``volume`` are realistically asked for outside
        # video mode; defaults match a fresh mpv.
        self.properties: dict[str, Any] = {
            "pause": False,
            "volume": 100.0,
            "time-pos": 0.0,
            "duration": 0.0,
            "media-title": None,
            "filename": None,
            "path": None,
        }

    def poll(self) -> int | None:
        return None if self.alive else 0

    def kill(self) -> None:
        self.alive = False

    def respond(self, command: list[Any]) -> dict[str, Any]:
        self.commands.append(command)
        if not command:
            return {"error": "invalid"}
        verb = command[0]
        if verb == "set_property" and len(command) >= 3:
            self.properties[command[1]] = command[2]
            return {"error": "success"}
        if verb == "get_property" and len(command) >= 2:
            return {"error": "success", "data": self.properties.get(command[1])}
        if verb in ("loadfile", "loadlist") and len(command) >= 2:
            loaded = str(command[1])
            self.properties["path"] = loaded
            self.properties["filename"] = Path(loaded).name
            self.properties["media-title"] = Path(loaded).name
            self.properties["time-pos"] = 0.0
            return {"error": "success"}
        # Everything else (loadfile, loadlist, seek, etc.) just succeeds
        # for our purposes; we only assert on the recorded command list.
        return {"error": "success"}

    def loaded_paths(self) -> list[str]:
        """Helper: just the file/list paths that were ever loaded, in
        order. Useful for verifying mode transitions."""

        out: list[str] = []
        for cmd in self.commands:
            if cmd and cmd[0] in ("loadfile", "loadlist") and len(cmd) >= 2:
                out.append(str(cmd[1]))
        return out

    def mode_transitions(self, *, yellow_path: str, video_path: str | None = None) -> list[str]:
        """Translate the recorded loads into mode names: ``slideshow``,
        ``yellow``, or ``video``."""

        out: list[str] = []
        for path in self.loaded_paths():
            if path == yellow_path:
                out.append("yellow")
            elif video_path is not None and path == video_path:
                out.append("video")
            elif path.endswith(".m3u"):
                out.append("slideshow")
            else:
                out.append("video")
        return out


@pytest.fixture
def fake_display(monkeypatch: pytest.MonkeyPatch) -> FakeMpv:
    """Replace the display module's process / socket / IPC primitives
    with the in-memory FakeMpv. Yields the FakeMpv so tests can inspect
    recorded commands."""

    # Import here (after the env vars above are set) so app.config picks
    # up the test paths.
    from app.services import display

    fake = FakeMpv()

    def _fake_spawn() -> None:
        display._proc = fake  # type: ignore[assignment]
        display._state.last_error = None

    def _fake_wait_for_socket(path: str, *, timeout: float) -> bool:
        return True

    def _fake_ipc(command: list[Any]) -> dict[str, Any]:
        if not fake.alive:
            raise display.DisplayNotRunning("fake mpv died")
        return fake.respond(command)

    def _fake_start_supervisor() -> None:
        # The supervisor would otherwise sit in a sleep loop watching
        # for a real subprocess; not useful in unit tests.
        return None

    def _fake_start_listener() -> None:
        # The natural-EOF tests trigger _on_end_file directly.
        return None

    monkeypatch.setattr(display, "_spawn_mpv_locked", _fake_spawn)
    monkeypatch.setattr(display, "_wait_for_socket", _fake_wait_for_socket)
    monkeypatch.setattr(display, "_ipc_request_unlocked", _fake_ipc)
    monkeypatch.setattr(display, "_start_supervisor_locked", _fake_start_supervisor)
    monkeypatch.setattr(display, "_start_listener_locked", _fake_start_listener)

    # Fresh module state for every test.
    display._proc = None
    display._state = display._State()
    display._slideshow_playlist_provider = None
    display._listener_stop.clear()
    display._supervisor_stop.clear()

    yield fake

    # Best-effort teardown so leaked state doesn't bleed across tests.
    display._proc = None
    display._state = display._State()
    display._slideshow_playlist_provider = None
