"""Unit tests for yt-dlp failure message mapping."""

from __future__ import annotations

from pathlib import Path

from app.services import downloader


def test_age_restricted_shorts_message() -> None:
    stderr = (
        "ERROR: [youtube] xyz: Sign in to confirm your age. "
        "This video may be inappropriate for some users."
    )
    msg = downloader._yt_dlp_failure_user_message(
        stderr,
        cookies_path=Path("/home/gilberto/pi-hub/secrets/youtube-cookies.txt"),
        cookies_present=True,
    )
    assert "age-restricted" in msg.lower()
    assert "rejected the cookies" not in msg.lower()


def test_bot_detection_with_cookies() -> None:
    stderr = "ERROR: Sign in to confirm you're not a bot"
    msg = downloader._yt_dlp_failure_user_message(
        stderr,
        cookies_path=Path("/tmp/cookies.txt"),
        cookies_present=True,
    )
    assert "rejected the cookies" in msg.lower()


def test_bot_detection_without_cookies() -> None:
    stderr = "ERROR: Sign in to confirm you're not a bot"
    msg = downloader._yt_dlp_failure_user_message(
        stderr,
        cookies_path=Path("/tmp/cookies.txt"),
        cookies_present=False,
    )
    assert "cookies" in msg.lower() and "configured" in msg.lower()
