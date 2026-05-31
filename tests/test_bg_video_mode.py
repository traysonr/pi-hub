"""Regression tests for background-video mode (BG Play).

Key contracts pinned here:

1. ``display.play_video_bg`` loads the video muted; ``mute`` is set to
   True, ``aid`` is enabled (so the video is actually decoded), and the
   controller enters video mode with ``is_bg_video=True``.

2. ``player.play_bg_video`` does NOT cancel shuffle or stop the headless
   audio player — those remain live during the background video.

3. ``player.stop_bg_video`` stops only the display layer; it does not
   touch audio.

4. When a background video finishes naturally (EOF), the display returns
   to the configured idle mode (slideshow or yellow) — identical to the
   normal-video EOF path.

5. ``player.get_state`` reports ``bg_video_active=True`` and
   ``kind="audio"`` (audio is the primary control surface) when both
   audio and a bg video are running simultaneously.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import FakeMpv


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def yellow_asset_path() -> str:
    from app.services import display
    return str(display._ensure_yellow_asset())


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    p = tmp_path / "drone.mp4"
    p.write_bytes(b"not a real video")
    return p


@pytest.fixture
def fake_slideshow_playlist(tmp_path: Path) -> Path:
    p = tmp_path / "slides.m3u"
    p.write_text("/tmp/img1.jpg\n/tmp/img2.jpg\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# display.py layer
# ---------------------------------------------------------------------------

class TestDisplayBgVideo:
    def test_play_video_bg_sets_is_bg_video_flag(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display

        display.init()
        display.play_video_bg(fake_video, title="drone")

        assert display.is_video_mode() is True
        assert display.is_bg_video_mode() is True
        state = display.get_state()
        assert state["is_bg_video"] is True
        assert state["mode"] == display.MODE_VIDEO

    def test_play_video_bg_sets_mute_true(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display

        display.init()
        display.play_video_bg(fake_video)

        assert fake_display.properties["mute"] is True, (
            "Background video must be muted on the display mpv"
        )

    def test_play_video_bg_still_enables_audio_track(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        """aid=auto so the video's audio track is decoded (even though muted)
        and mpv doesn't complain about missing audio data."""
        from app.services import display

        display.init()
        display.play_video_bg(fake_video)

        assert fake_display.properties.get("aid") == "auto"

    def test_stop_video_clears_is_bg_video(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display

        display.init()
        display.play_video_bg(fake_video)
        assert display.is_bg_video_mode() is True

        display.stop_video()
        assert display.is_bg_video_mode() is False
        assert display.get_state()["is_bg_video"] is False

    def test_normal_play_video_clears_bg_flag(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        """If a normal video is launched while a bg video was running
        (caller responsibility, but defensiveness is good), the bg flag
        must be cleared so subsequent status reads are consistent."""
        from app.services import display

        display.init()
        display.play_video_bg(fake_video)
        assert display.is_bg_video_mode() is True

        display.play_video(fake_video, title="foreground")
        assert display.is_bg_video_mode() is False
        assert display.get_state()["is_bg_video"] is False
        # Normal video must be unmuted.
        assert fake_display.properties["mute"] is False

    def test_bg_video_eof_returns_to_slideshow(
        self,
        fake_display: FakeMpv,
        yellow_asset_path: str,
        fake_video: Path,
        fake_slideshow_playlist: Path,
    ) -> None:
        """EOF on a background video must return to the configured idle mode,
        just like a normal video, and must clear the is_bg_video flag."""
        from app.services import display

        display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
        display.init()
        display.set_idle_mode(display.MODE_SLIDESHOW)

        display.play_video_bg(fake_video)
        assert display.is_bg_video_mode() is True

        display._on_end_file({"event": "end-file", "reason": "eof"})

        assert display.is_video_mode() is False
        assert display.is_bg_video_mode() is False
        assert display.get_state()["mode"] == display.MODE_SLIDESHOW
        assert display.get_state()["is_bg_video"] is False

    def test_bg_video_eof_returns_to_yellow_when_disabled(
        self,
        fake_display: FakeMpv,
        yellow_asset_path: str,
        fake_video: Path,
    ) -> None:
        from app.services import display

        display.init()  # default idle_mode is yellow
        display.play_video_bg(fake_video)

        display._on_end_file({"event": "end-file", "reason": "eof"})

        assert display.get_state()["mode"] == display.MODE_YELLOW
        assert display.get_state()["is_bg_video"] is False

    def test_enter_slideshow_clears_bg_flag(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
        fake_slideshow_playlist: Path,
    ) -> None:
        from app.services import display

        display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
        display.init()
        display.play_video_bg(fake_video)

        display.show_slideshow_now()

        assert display.get_state()["is_bg_video"] is False

    def test_mode_transitions_for_bg_video(
        self,
        fake_display: FakeMpv,
        yellow_asset_path: str,
        fake_video: Path,
        fake_slideshow_playlist: Path,
    ) -> None:
        """The sequence of loadfile/loadlist calls (mode transitions) for a
        bg video session must be identical to a normal video session from the
        display controller's perspective — the only difference is ``mute``."""
        from app.services import display

        display.set_slideshow_playlist_provider(lambda: fake_slideshow_playlist)
        display.init()
        display.set_idle_mode(display.MODE_SLIDESHOW)

        display.play_video_bg(fake_video)
        display._on_end_file({"event": "end-file", "reason": "eof"})

        transitions = fake_display.mode_transitions(
            yellow_path=yellow_asset_path, video_path=str(fake_video)
        )
        assert transitions == ["yellow", "slideshow", "video", "slideshow"]


# ---------------------------------------------------------------------------
# player.py layer — policy contracts
# ---------------------------------------------------------------------------

class TestPlayerBgVideoPolicy:
    def test_play_bg_video_does_not_stop_audio(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        """The most critical contract: BG Play must leave the headless audio
        player completely untouched."""
        from app.services import player

        with (
            patch("app.services.player.audio_player") as mock_audio,
            patch("app.services.player.shuffle") as mock_shuffle,
        ):
            mock_audio.is_playing.return_value = True
            mock_audio.get_state.return_value = {
                "playing": True, "title": "track.m4a", "filename": "track.m4a",
            }

            fake_display.alive = True
            from app.services import display
            display.init()

            player.play_bg_video(fake_video)

            mock_audio.stop.assert_not_called()
            mock_shuffle.stop.assert_not_called()

    def test_play_bg_video_refuses_over_foreground_video(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video(fake_video, title="foreground")
        assert display.is_video_mode() is True
        assert display.is_bg_video_mode() is False

        with pytest.raises(RuntimeError, match="foreground video"):
            player.play_bg_video(fake_video)

    def test_play_bg_video_replaces_existing_bg_video(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
        tmp_path: Path,
    ) -> None:
        """Calling BG Play while a bg video is already running should replace
        it (not raise)."""
        from app.services import display, player

        display.init()
        player.play_bg_video(fake_video)
        assert display.is_bg_video_mode() is True

        second_video = tmp_path / "second.mp4"
        second_video.write_bytes(b"also not real")
        player.play_bg_video(second_video)

        assert display.is_bg_video_mode() is True
        assert display.get_state()["video_path"] == str(second_video)

    def test_stop_bg_video_does_not_stop_audio(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video_bg(fake_video)

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = True
            was_active = player.stop_bg_video()

        assert was_active is True
        mock_audio.stop.assert_not_called()

    def test_stop_bg_video_returns_false_when_not_in_bg_mode(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        # Not in bg mode; stop_bg_video should be a no-op.
        result = player.stop_bg_video()
        assert result is False

    def test_stop_bg_video_returns_false_for_foreground_video(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video(fake_video, title="foreground")
        assert display.is_bg_video_mode() is False

        result = player.stop_bg_video()
        assert result is False
        # Foreground video must still be running.
        assert display.is_video_mode() is True


# ---------------------------------------------------------------------------
# player.get_state — status contracts for the remote UI
# ---------------------------------------------------------------------------

class TestPlayerGetStateBgVideo:
    def test_get_state_reports_audio_as_primary_kind_in_bg_mode(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video_bg(fake_video, title="drone.mp4")

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = True
            mock_audio.get_state.return_value = {
                "playing": True,
                "title": "track.m4a",
                "filename": "track.m4a",
                "pause": False,
                "volume": 100.0,
            }
            with patch("app.services.player.shuffle") as mock_shuffle:
                mock_shuffle.is_active.return_value = True
                state = player.get_state()

        assert state["playing"] is True
        assert state["kind"] == "audio", (
            "Audio must be the primary kind in bg-video mode so the remote "
            "controls keep targeting the music player."
        )
        assert state["bg_video_active"] is True
        assert state["bg_video_title"] == "drone.mp4"
        assert state["shuffle_active"] is True

    def test_get_state_bg_video_only_no_audio(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        """Edge case: bg video running but music was stopped externally."""
        from app.services import display, player

        display.init()
        display.play_video_bg(fake_video, title="drone.mp4")

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = False
            with patch("app.services.player.shuffle") as mock_shuffle:
                mock_shuffle.is_active.return_value = False
                state = player.get_state()

        assert state["playing"] is True
        assert state["kind"] == "bg_video"
        assert state["bg_video_active"] is True

    def test_get_state_normal_video_reports_bg_video_active_false(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video(fake_video)

        with patch("app.services.player.shuffle") as mock_shuffle:
            mock_shuffle.is_active.return_value = False
            state = player.get_state()

        assert state["kind"] == "video"
        assert state["bg_video_active"] is False

    def test_get_state_idle_reports_bg_video_active_false(
        self,
        fake_display: FakeMpv,
    ) -> None:
        from app.services import display, player

        display.init()

        with (
            patch("app.services.player.audio_player") as mock_audio,
            patch("app.services.player.shuffle") as mock_shuffle,
        ):
            mock_audio.is_playing.return_value = False
            mock_shuffle.is_active.return_value = False
            state = player.get_state()

        assert state["playing"] is False
        assert state["bg_video_active"] is False


# ---------------------------------------------------------------------------
# active_kind helper
# ---------------------------------------------------------------------------

class TestActiveKind:
    def test_active_kind_audio_during_bg_video(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video_bg(fake_video)

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = True
            kind = player.active_kind()

        assert kind == "audio"

    def test_active_kind_bg_video_when_no_audio(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video_bg(fake_video)

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = False
            kind = player.active_kind()

        assert kind == "bg_video"

    def test_active_kind_video_for_foreground(
        self,
        fake_display: FakeMpv,
        fake_video: Path,
    ) -> None:
        from app.services import display, player

        display.init()
        display.play_video(fake_video)

        with patch("app.services.player.audio_player") as mock_audio:
            mock_audio.is_playing.return_value = False
            kind = player.active_kind()

        assert kind == "video"
