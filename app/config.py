"""Shared configuration and filesystem paths for Pi Hub."""

from __future__ import annotations

import logging
import os
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

MEDIA_DIR: Path = Path(
    os.environ.get("PI_HUB_MEDIA_DIR", PROJECT_ROOT / "media")
).resolve()
VIDEO_DIR: Path = (MEDIA_DIR / "videos").resolve()
MUSIC_DIR: Path = (MEDIA_DIR / "music").resolve()
SCREENSAVER_CACHE_DIR: Path = (MEDIA_DIR / "screensaver-cache").resolve()

TEMPLATES_DIR: Path = PROJECT_ROOT / "templates"
STATIC_DIR: Path = PROJECT_ROOT / "static"

CONFIG_DIR: Path = Path(
    os.environ.get("PI_HUB_CONFIG_DIR", PROJECT_ROOT / "config")
).resolve()
SCREENSAVER_THEMES_FILE: Path = Path(
    os.environ.get(
        "PI_HUB_SCREENSAVER_THEMES",
        str(CONFIG_DIR / "screensaver-themes.json"),
    )
).resolve()
SCREENSAVER_THEMES_EXAMPLE: Path = (
    CONFIG_DIR / "screensaver-themes.json.example"
).resolve()

# Extensions that the catalogue treats as playable video files.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
)

# Extensions that the catalogue treats as playable audio files. mpv plays
# all of these through the same HDMI pipeline as video.
AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac"}
)


def ensure_runtime_dirs() -> None:
    """Create directories that the app expects to exist at runtime."""

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSAVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Configure a simple, readable log format for the app."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
