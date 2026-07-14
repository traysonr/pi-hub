"""Unit tests for the yt-dlp self-updater.

We don't touch the network or the real venv: the boundary functions
(``installed_version``, ``update``) are monkeypatched so we can assert on
the version-age math and the startup-freshness decision in isolation.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services import ytdlp_updater


@pytest.mark.parametrize(
    "version,expected",
    [
        ("2026.7.4", date(2026, 7, 4)),
        ("2026.03.17", date(2026, 3, 17)),
        ("2025.4.30", date(2025, 4, 30)),
        ("2021.1.24.post1", date(2021, 1, 24)),
    ],
)
def test_version_to_date_parses_dated_releases(version, expected) -> None:
    assert ytdlp_updater._version_to_date(version) == expected


@pytest.mark.parametrize("version", [None, "", "nightly", "1.2", "abc.def.ghi"])
def test_version_to_date_rejects_non_dates(version) -> None:
    assert ytdlp_updater._version_to_date(version) is None


def test_installed_age_days_uses_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp_updater, "installed_version", lambda: "2026.7.4")

    class _FixedDate(date):
        @classmethod
        def today(cls):  # pragma: no cover - not used directly
            return cls(2026, 8, 3)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 3, 12, 0, 0)

    monkeypatch.setattr(ytdlp_updater, "datetime", _FixedDatetime)
    assert ytdlp_updater.installed_age_days() == 30


def test_startup_skips_when_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp_updater, "installed_age_days", lambda: 5)
    called = {"update": False}
    monkeypatch.setattr(
        ytdlp_updater, "update", lambda: called.__setitem__("update", True)
    )
    ytdlp_updater.maybe_update_on_startup()
    assert called["update"] is False


def test_startup_updates_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp_updater, "installed_age_days", lambda: 120)
    called = {"update": False}
    monkeypatch.setattr(
        ytdlp_updater, "update", lambda: called.__setitem__("update", True)
    )
    ytdlp_updater.maybe_update_on_startup()
    assert called["update"] is True


def test_startup_skips_when_age_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp_updater, "installed_age_days", lambda: None)
    called = {"update": False}
    monkeypatch.setattr(
        ytdlp_updater, "update", lambda: called.__setitem__("update", True)
    )
    ytdlp_updater.maybe_update_on_startup()
    assert called["update"] is False


def test_startup_swallows_update_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdlp_updater, "installed_age_days", lambda: 200)

    def _boom() -> str:
        raise RuntimeError("pip exploded")

    monkeypatch.setattr(ytdlp_updater, "update", _boom)
    # Must not propagate: a failed self-update can't take down the app.
    ytdlp_updater.maybe_update_on_startup()
