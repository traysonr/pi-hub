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

TEMPLATES_DIR: Path = PROJECT_ROOT / "templates"
STATIC_DIR: Path = PROJECT_ROOT / "static"

# Extensions that the catalogue treats as playable video files.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
)


def ensure_runtime_dirs() -> None:
    """Create directories that the app expects to exist at runtime."""

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)


def configure_logging() -> None:
    """Configure a simple, readable log format for the app."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
