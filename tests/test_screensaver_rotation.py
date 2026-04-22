"""Tests for the daily FIFO-ish cache rotation in screensaver.

The rotation is "keep a random 25%, delete the rest, refill to target
from Reddit". These tests run against a real on-disk cache directory
(under tmp_path) with ``reddit.list_cached_images`` pointed at it and
``reddit.refresh_theme`` monkey-patched to a fake downloader, so the
rotation code exercises all of its filesystem ops without hitting
Reddit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeMpv


def _seed_cache(dir_: Path, count: int) -> list[Path]:
    """Drop ``count`` tiny fake .jpg files into ``dir_`` and return
    them sorted (matching ``reddit.list_cached_images`` behavior)."""
    dir_.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        p = dir_ / f"img_{i:03d}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xd9")
        paths.append(p)
    return sorted(paths)


@pytest.fixture
def rotate_env(
    fake_display: FakeMpv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Boot display+screensaver, seed one theme, and replace the
    reddit listing + refresh functions with filesystem fakes."""

    from app.services import display, reddit, screensaver

    cache_root = tmp_path / "cache"
    theme_dir = cache_root / "Watercolor"
    theme_dir.mkdir(parents=True)

    # list_cached_images returns whatever is on disk at call time so
    # the "delete 75%" step is actually observable.
    def _list(sub: str) -> list[Path]:
        d = cache_root / sub
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.suffix == ".jpg")

    monkeypatch.setattr(reddit, "list_cached_images", _list)

    # Fake refresh: drop N new files into the theme dir. Reports (new,
    # total) like the real one. Tests override the N via attribute on
    # the function so different cases can simulate Reddit returning
    # plenty vs. barely anything.
    def _refresh(sub: str, *, max_images: int = 30, timeframe: str = "week"):
        d = cache_root / sub
        d.mkdir(parents=True, exist_ok=True)
        supply = getattr(_refresh, "_supply", max_images)
        n = min(max_images, supply)
        existing_n = len(_list(sub))
        for i in range(n):
            p = d / f"fresh_{existing_n + i:03d}.jpg"
            p.write_bytes(b"\xff\xd8\xff\xd9")
        return n, len(_list(sub))

    monkeypatch.setattr(reddit, "refresh_theme", _refresh)

    display.init()
    screensaver.init()
    screensaver._state.themes = [
        screensaver.Theme("Watercolor", "Watercolor", True)
    ]

    return {
        "cache_root": cache_root,
        "theme_dir": theme_dir,
        "refresh_fn": _refresh,
        "list_fn": _list,
    }


def test_rotate_theme_keeps_25_percent_and_refills_to_target(rotate_env) -> None:
    from app.services import screensaver

    _seed_cache(rotate_env["theme_dir"], 50)
    rotate_env["refresh_fn"]._supply = 100  # plenty of fresh images available

    summary = screensaver.rotate_theme("Watercolor", target=50)

    # floor(50 * 0.25) = 12 kept, 38 deleted, 38 downloaded -> 50 total.
    assert summary["before"] == 50
    assert summary["kept"] == 12
    assert summary["deleted"] == 38
    assert summary["downloaded"] == 38
    assert summary["after"] == 50
    assert summary["target"] == 50

    on_disk = rotate_env["list_fn"]("Watercolor")
    assert len(on_disk) == 50


def test_rotate_theme_handles_empty_cache(rotate_env) -> None:
    from app.services import screensaver

    rotate_env["refresh_fn"]._supply = 50
    summary = screensaver.rotate_theme("Watercolor", target=50)

    # Nothing to keep, nothing to delete, refill the whole target.
    assert summary["before"] == 0
    assert summary["kept"] == 0
    assert summary["deleted"] == 0
    assert summary["downloaded"] == 50
    assert summary["after"] == 50


def test_rotate_theme_handles_tiny_cache(rotate_env) -> None:
    """With 3 cached images, floor(3 * 0.25) = 0 keeps. We should not
    crash on ``random.sample(seq, 0)`` and still trigger a refill."""
    from app.services import screensaver

    _seed_cache(rotate_env["theme_dir"], 3)
    rotate_env["refresh_fn"]._supply = 50
    summary = screensaver.rotate_theme("Watercolor", target=50)

    assert summary["before"] == 3
    assert summary["kept"] == 0
    assert summary["deleted"] == 3
    assert summary["downloaded"] == 50
    assert summary["after"] == 50


def test_rotate_theme_handles_partial_reddit_supply(rotate_env) -> None:
    """When Reddit's top listing has churned less than the shortfall
    (common -- "top of the week" doesn't change much day-to-day), we
    end up under target for today and fill back up later."""
    from app.services import screensaver

    _seed_cache(rotate_env["theme_dir"], 50)
    rotate_env["refresh_fn"]._supply = 10  # Reddit only has 10 new

    summary = screensaver.rotate_theme("Watercolor", target=50)

    assert summary["kept"] == 12
    assert summary["deleted"] == 38
    assert summary["downloaded"] == 10
    assert summary["after"] == 22  # 12 kept + 10 fresh


def test_rotate_all_themes_skips_disabled(rotate_env, monkeypatch) -> None:
    from app.services import screensaver

    screensaver._state.themes = [
        screensaver.Theme("Watercolor", "Watercolor", True),
        screensaver.Theme("EarthPorn", "EarthPorn", False),
    ]
    _seed_cache(rotate_env["theme_dir"], 20)
    rotate_env["refresh_fn"]._supply = 50

    result = screensaver.rotate_all_themes(target=50)

    processed = [t["subreddit"] for t in result["themes"]]
    assert processed == ["Watercolor"]
    # The disabled theme's dir must remain untouched (it doesn't even
    # exist in this test, which is fine -- the point is we didn't
    # create it by attempting a rotation).
    assert not (rotate_env["cache_root"] / "EarthPorn").exists()


def test_rotate_all_themes_updates_last_refresh_summary(rotate_env) -> None:
    from app.services import screensaver

    _seed_cache(rotate_env["theme_dir"], 50)
    rotate_env["refresh_fn"]._supply = 100

    screensaver.rotate_all_themes(target=50)

    status = screensaver.get_status()
    assert status["last_refresh_summary"]
    assert "rotate" in status["last_refresh_summary"]
    assert status["last_refresh_at"] is not None
