"""Transition tests for the persistent display controller.

These tests pin down the user-facing promise: the TV never falls back
to the Linux console. Concretely, every transition between video,
slideshow, and yellow fallback corresponds to a single ``loadfile`` /
``loadlist`` IPC call against the same long-lived mpv process -- the
process is never killed and re-spawned, so DRM ownership is never
released.

The "no flash" guarantee is therefore equivalent to:

1. Mode transitions are issued as IPC commands (asserted via the
   FakeMpv command log).
2. The recorded mode sequence matches what a user would expect for
   each scenario from the plan: play -> stop -> idle, play -> EOF ->
   idle, with idle resolving to slideshow when enabled and yellow
   otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeMpv


@pytest.fixture
def yellow_asset_path() -> str:
    from app.services import display
    return str(display._ensure_yellow_asset())


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    # Real file on disk so display.play_video's is_file() check passes.
    p = tmp_path / "movie.mp4"
    p.write_bytes(b"not a real video")
    return p


@pytest.fixture
def fake_slideshow_playlist(tmp_path: Path) -> Path:
    # The playlist content doesn't matter for the test; only its path,
    # which gets recorded as the loadlist target.
    p = tmp_path / "slides.m3u"
    p.write_text("/tmp/img1.jpg\n/tmp/img2.jpg\n", encoding="utf-8")
    return p


def test_play_then_stop_returns_to_slideshow(
    fake_display: FakeMpv,
    yellow_asset_path: str,
    fake_video: Path,
    fake_slideshow_playlist: Path,
) -> None:
    """User has slideshow enabled. They play a video and press Stop.
    The TV should go straight back to the slideshow without ever
    reverting to the yellow placeholder (which would briefly look like
    a console flash) or, worse, exposing the real terminal."""

    from app.services import display

    display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
    display.init()  # Boot: idle starts in yellow because default idle_mode is yellow.
    display.set_idle_mode(display.MODE_SLIDESHOW)

    display.play_video(fake_video, title="movie")
    assert display.is_video_mode() is True

    was_playing = display.stop_video()
    assert was_playing is True
    assert display.is_video_mode() is False
    assert display.get_state()["mode"] == display.MODE_SLIDESHOW

    transitions = fake_display.mode_transitions(
        yellow_path=yellow_asset_path, video_path=str(fake_video)
    )
    assert transitions == ["yellow", "slideshow", "video", "slideshow"], (
        "Expected: boot->yellow, enable->slideshow, play->video, stop->slideshow. "
        f"Got {transitions}"
    )


def test_play_then_natural_eof_returns_to_slideshow(
    fake_display: FakeMpv,
    yellow_asset_path: str,
    fake_video: Path,
    fake_slideshow_playlist: Path,
) -> None:
    """User has slideshow enabled. The video reaches EOF on its own
    (mpv emits an ``end-file`` event). The TV should land back on the
    slideshow with no manual intervention from the client."""

    from app.services import display

    display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
    display.init()
    display.set_idle_mode(display.MODE_SLIDESHOW)
    display.play_video(fake_video, title="movie")

    # Simulate the listener thread receiving an end-file event from mpv
    # (this is the same callback the real listener thread invokes).
    display._on_end_file({"event": "end-file", "reason": "eof"})

    assert display.is_video_mode() is False
    assert display.get_state()["mode"] == display.MODE_SLIDESHOW

    transitions = fake_display.mode_transitions(
        yellow_path=yellow_asset_path, video_path=str(fake_video)
    )
    assert transitions == ["yellow", "slideshow", "video", "slideshow"], (
        "Expected: boot->yellow, enable->slideshow, play->video, EOF->slideshow. "
        f"Got {transitions}"
    )


def test_play_then_stop_returns_to_yellow_when_disabled(
    fake_display: FakeMpv,
    yellow_asset_path: str,
    fake_video: Path,
) -> None:
    """User has the screensaver disabled (idle_mode=yellow). They play
    a video and press Stop. The TV should land on the yellow fallback
    instead of any console output."""

    from app.services import display

    # No slideshow playlist registered -- a real user with screensaver
    # disabled may also have never enabled it, so the provider is
    # never set.
    display.init()
    assert display.get_state()["idle_mode"] == display.MODE_YELLOW

    display.play_video(fake_video, title="movie")
    display.stop_video()

    assert display.get_state()["mode"] == display.MODE_YELLOW

    transitions = fake_display.mode_transitions(
        yellow_path=yellow_asset_path, video_path=str(fake_video)
    )
    assert transitions == ["yellow", "video", "yellow"], (
        f"Expected boot->yellow, play->video, stop->yellow. Got {transitions}"
    )


def test_eof_during_yellow_idle_does_not_reload(
    fake_display: FakeMpv,
    yellow_asset_path: str,
) -> None:
    """The end-file listener should be a no-op when we're not in video
    mode. Otherwise slideshow advancing through its playlist or the
    yellow loop ending would trigger spurious idle reapplications."""

    from app.services import display

    display.init()  # boots into yellow
    fake_display.commands.clear()

    # Pretend mpv emitted an end-file while we're already in yellow
    # mode. Nothing should happen.
    display._on_end_file({"event": "end-file", "reason": "stop"})

    assert fake_display.commands == [], (
        "Listener should ignore end-file events outside video mode; "
        f"got commands {fake_display.commands}"
    )


def test_play_video_explicitly_unpauses(
    fake_display: FakeMpv,
    fake_video: Path,
) -> None:
    """Regression test: after a video swap, mpv must not be left paused.

    The bug we're guarding against: mpv applies the *current* pause
    property to a freshly loaded file. If anything ever set ``pause``
    to True (manual user toggle, supervisor restart inheriting state,
    etc.) the next ``play_video`` would land on the first frame and
    require the user to manually press Play. We always force pause
    off after every loadfile."""

    from app.services import display

    display.init()

    # Sneak the "stuck paused" precondition into mpv so we can observe
    # whether play_video clears it. The default test FakeMpv starts
    # with pause=False so we have to provoke the broken state ourselves.
    fake_display.properties["pause"] = True

    display.play_video(fake_video, title="movie")

    assert fake_display.properties["pause"] is False, (
        "Video must start playing immediately after loadfile -- the "
        "user shouldn't have to press Play on the remote."
    )

    # And confirm we issued the command in the right order: loadfile
    # first, then set pause=False (otherwise mpv would re-pause after).
    pause_set_index = next(
        i for i, c in enumerate(fake_display.commands)
        if c[:2] == ["set_property", "pause"] and c[2] is False
    )
    loadfile_index = next(
        i for i, c in enumerate(fake_display.commands)
        if c and c[0] == "loadfile" and str(fake_video) in c
    )
    assert loadfile_index < pause_set_index, (
        "pause=False must be set AFTER loadfile, otherwise mpv applies "
        "the new file's saved pause state on top of our reset."
    )


def test_swap_induced_end_file_stop_does_not_drop_video(
    fake_display: FakeMpv,
    yellow_asset_path: str,
    fake_video: Path,
) -> None:
    """Regression test: when ``play_video`` issues ``loadfile ... replace``
    over IPC, mpv first unloads the previously-loaded file (the yellow
    placeholder) and emits ``end-file`` with ``reason=stop`` for *that*
    file. The listener must not interpret that as the user's video
    ending -- otherwise the new video would be swapped right back out
    for the idle screen and the user sees nothing play."""

    from app.services import display

    display.init()
    display.play_video(fake_video, title="movie")
    assert display.is_video_mode() is True

    # The exact event mpv emits ~6ms after our loadfile call.
    display._on_end_file({"event": "end-file", "reason": "stop"})

    assert display.is_video_mode() is True, (
        "Stop-reason end-file events fire whenever we swap files; they "
        "must not be treated as the loaded video ending."
    )
    transitions = fake_display.mode_transitions(
        yellow_path=yellow_asset_path, video_path=str(fake_video)
    )
    assert transitions == ["yellow", "video"], (
        "Video must remain loaded; should not have re-entered yellow. "
        f"Got {transitions}"
    )


def test_slideshow_eof_during_video_handoff_does_not_drop_video(
    fake_display: FakeMpv,
    yellow_asset_path: str,
    fake_video: Path,
    fake_slideshow_playlist: Path,
) -> None:
    """Regression test: the outgoing slideshow frame can hit its timed
    EOF while a video replace is in flight.

    That EOF belongs to the slideshow image we are *replacing*, not to
    the requested video. The controller must ignore it; otherwise the TV
    bounces straight back to slideshow and the UI reports "Nothing
    playing" even though the user just pressed Play.
    """

    from app.services import display

    display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
    display.init()
    display.set_idle_mode(display.MODE_SLIDESHOW)

    # Simulate the currently displayed slideshow image having a stable
    # mpv playlist entry ID before the user presses Play.
    display._on_start_file({"event": "start-file", "playlist_entry_id": 101})

    display.play_video(fake_video, title="movie")
    assert display.is_video_mode() is True

    # The outgoing slideshow frame reaches its image-display-duration
    # right as the replace happens. This used to be misread as "the
    # requested video already ended", which immediately reloaded idle.
    display._on_end_file(
        {"event": "end-file", "reason": "eof", "playlist_entry_id": 101}
    )

    assert display.is_video_mode() is True, (
        "A slideshow EOF during the handoff must not kick us back to "
        "the idle screen."
    )
    assert display.get_state()["mode"] == display.MODE_VIDEO

    # Once mpv announces the new file, its own EOF should still return
    # to the configured idle mode.
    display._on_start_file({"event": "start-file", "playlist_entry_id": 202})
    display._on_file_loaded({"event": "file-loaded"})
    display._on_end_file(
        {"event": "end-file", "reason": "eof", "playlist_entry_id": 202}
    )

    assert display.get_state()["mode"] == display.MODE_SLIDESHOW
    transitions = fake_display.mode_transitions(
        yellow_path=yellow_asset_path, video_path=str(fake_video)
    )
    assert transitions == ["yellow", "slideshow", "video", "slideshow"], (
        "The slideshow EOF during handoff must be ignored; only the "
        "requested video's EOF should return to idle. "
        f"Got {transitions}"
    )


def test_set_enabled_during_video_does_not_interrupt_playback(
    fake_display: FakeMpv,
    fake_video: Path,
    fake_slideshow_playlist: Path,
) -> None:
    """Toggling the screensaver master switch while a video is playing
    should change the *future* idle mode but never yank the user out
    of the current video."""

    from app.services import display

    display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
    display.init()
    display.play_video(fake_video, title="movie")

    # Flip idle from default-yellow to slideshow mid-playback.
    display.set_idle_mode(display.MODE_SLIDESHOW)
    assert display.is_video_mode() is True, "Video must keep playing"

    # When the video ends, *now* slideshow takes over.
    display._on_end_file({"event": "end-file", "reason": "eof"})
    assert display.get_state()["mode"] == display.MODE_SLIDESHOW
