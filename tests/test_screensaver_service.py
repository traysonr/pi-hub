"""Service-level tests for app.services.screensaver.

These exercise the public functions the HTTP routes call (set_enabled,
start, stop, refresh_now hooks) and confirm they drive the display
controller into the right modes. The display itself is faked via the
``fake_display`` fixture so no real mpv process is involved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeMpv


@pytest.fixture
def cached_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop a single fake image into a fake theme cache and route the
    screensaver's playlist builder at it."""

    from app.services import reddit, screensaver

    image = tmp_path / "Watercolor_abc.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG-ish bytes
    monkeypatch.setattr(reddit, "list_cached_images", lambda sub: [image])

    # Make sure screensaver state has at least one enabled theme.
    screensaver._state.themes = [screensaver.Theme("Watercolor", "Watercolor", True)]
    screensaver._state.enabled = False
    return image


def _boot(fake_display: FakeMpv) -> None:
    """Mirror the boot order from app/main.py: display first, then
    screensaver. The fake_display fixture has already replaced mpv's
    spawn/IPC primitives, so display.init() is safe to call."""

    from app.services import display, screensaver

    display.init()
    screensaver.init()


def test_init_registers_playlist_provider(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import display

    _boot(fake_display)

    # Screensaver is disabled by default, so idle mode is yellow.
    assert display.get_state()["idle_mode"] == display.MODE_YELLOW
    # And the provider got registered.
    playlist = display._slideshow_playlist_provider()  # type: ignore[misc]
    assert playlist is not None
    assert playlist.exists()


def test_set_enabled_true_makes_slideshow_idle(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import display, screensaver

    _boot(fake_display)
    screensaver.set_enabled(True)

    state = display.get_state()
    assert state["idle_mode"] == display.MODE_SLIDESHOW
    assert state["mode"] == display.MODE_SLIDESHOW


def test_set_enabled_false_makes_yellow_idle(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import display, screensaver

    _boot(fake_display)
    screensaver.set_enabled(True)
    screensaver.set_enabled(False)

    state = display.get_state()
    assert state["idle_mode"] == display.MODE_YELLOW
    assert state["mode"] == display.MODE_YELLOW


def test_start_refuses_when_disabled(fake_display: FakeMpv, cached_image: Path) -> None:
    from app.services import screensaver

    _boot(fake_display)
    with pytest.raises(RuntimeError, match="disabled"):
        screensaver.start()


def test_add_theme_appends_and_persists(
    fake_display: FakeMpv, cached_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import screensaver

    _boot(fake_display)
    # Don't fire real network traffic from the background refresh worker
    # that add_theme kicks off.
    monkeypatch.setattr(screensaver, "_kick_refresh_async", lambda: None)
    screensaver._state.themes = []

    status = screensaver.add_theme("robotics")
    names = [t["name"] for t in status["themes"]]
    subs = [t["subreddit"] for t in status["themes"]]
    assert "robotics" in names
    assert "robotics" in subs


def test_add_theme_normalizes_url_and_prefix(
    fake_display: FakeMpv, cached_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import screensaver

    _boot(fake_display)
    monkeypatch.setattr(screensaver, "_kick_refresh_async", lambda: None)
    screensaver._state.themes = []

    screensaver.add_theme("https://www.reddit.com/r/spacex/")
    screensaver.add_theme("r/pics")

    subs = [t.subreddit for t in screensaver._state.themes]
    assert "spacex" in subs
    assert "pics" in subs


def test_add_theme_rejects_invalid_input(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import screensaver

    _boot(fake_display)

    with pytest.raises(ValueError):
        screensaver.add_theme("")
    with pytest.raises(ValueError):
        screensaver.add_theme("not a subreddit!")


def test_add_theme_rejects_duplicates(
    fake_display: FakeMpv, cached_image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import screensaver

    _boot(fake_display)
    monkeypatch.setattr(screensaver, "_kick_refresh_async", lambda: None)

    # Start from a clean theme list so other tests that added "robotics"
    # don't pre-trip the dedupe we're about to exercise.
    screensaver._state.themes = []
    screensaver.add_theme("robotics")
    with pytest.raises(KeyError):
        # Different capitalization -- still a dupe.
        screensaver.add_theme("Robotics")


def test_remove_theme_deletes_cache_dir(
    fake_display: FakeMpv, cached_image: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import SCREENSAVER_CACHE_DIR
    from app.services import screensaver

    _boot(fake_display)
    monkeypatch.setattr(screensaver, "_kick_refresh_async", lambda: None)

    screensaver._state.themes = []
    screensaver.add_theme("robotics")
    cache = SCREENSAVER_CACHE_DIR / "robotics"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    assert cache.exists()

    status = screensaver.remove_theme("robotics")
    assert "robotics" not in [t["name"] for t in status["themes"]]
    assert not cache.exists()


def test_remove_theme_missing_raises_keyerror(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import screensaver

    _boot(fake_display)
    with pytest.raises(KeyError):
        screensaver.remove_theme("nope")


def test_stop_swaps_slideshow_for_yellow_without_changing_enabled(
    fake_display: FakeMpv, cached_image: Path
) -> None:
    from app.services import display, screensaver

    _boot(fake_display)
    screensaver.set_enabled(True)
    assert display.get_state()["mode"] == display.MODE_SLIDESHOW

    screensaver.stop()

    state = display.get_state()
    assert state["mode"] == display.MODE_YELLOW
    # Master toggle still on -- a future video EOF should still come
    # back to the slideshow.
    assert screensaver.get_status()["enabled"] is True
    assert state["idle_mode"] == display.MODE_SLIDESHOW
