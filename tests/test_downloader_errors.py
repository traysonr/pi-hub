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


def test_youtube_extraction_failure_not_misreported_as_h264() -> None:
    """When nsig fails, stderr still ends with 'requested format is not available'."""

    stderr = """
WARNING: [youtube] vid: nsig extraction failed: Some formats may be missing
WARNING: Only images are available for download. use --list-formats to see them
ERROR: [youtube] vid: Requested format is not available. Use --list-formats for a list of available formats
"""
    msg = downloader._yt_dlp_failure_user_message(
        stderr,
        cookies_path=Path("/tmp/cookies.txt"),
        cookies_present=True,
    )
    assert "format extraction failed" in msg.lower()
    assert "720" not in msg and "h.264" not in msg.lower()
