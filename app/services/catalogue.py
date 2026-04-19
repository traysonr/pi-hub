"""Filesystem-backed media catalogue."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.config import VIDEO_DIR, VIDEO_EXTENSIONS

log = logging.getLogger(__name__)

# yt-dlp leaves stream fragments like "title [id].f251.webm" or ".part" files
# behind when a merge fails. Hide them so they don't clutter the catalogue.
_YTDLP_FRAGMENT_RE = re.compile(r"\.f\d+\.[A-Za-z0-9]+$")
_YTDLP_TEMP_SUFFIXES = (".part", ".ytdl", ".tmp")

# The downloader names files like "Title_With_Underscores [id] [720p].mp4".
# Strip the bracketed id/quality tags and convert underscores back to spaces
# so the catalogue displays just the YouTube video title.
_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\[\]]*\]\s*$")


def _is_ytdlp_intermediate(name: str) -> bool:
    if name.endswith(_YTDLP_TEMP_SUFFIXES):
        return True
    if _YTDLP_FRAGMENT_RE.search(name):
        return True
    return False


def _display_title(stem: str) -> str:
    """Derive a clean display title from a yt-dlp filename stem."""

    title = stem
    # Repeatedly trim trailing "[...]" segments (resolution, id, etc.).
    while True:
        stripped = _TRAILING_BRACKET_RE.sub("", title)
        if stripped == title:
            break
        title = stripped
    title = title.replace("_", " ").strip()
    return title or stem


@dataclass(frozen=True)
class VideoEntry:
    filename: str
    title: str
    size_bytes: int
    modified: float

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "title": self.title,
            "size_bytes": self.size_bytes,
            "modified": self.modified,
        }


def list_videos() -> list[VideoEntry]:
    """Return every playable video in the catalogue, newest first."""

    if not VIDEO_DIR.exists():
        return []

    entries: list[VideoEntry] = []
    for path in VIDEO_DIR.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if _is_ytdlp_intermediate(path.name):
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            log.warning("Skipping %s: %s", path, exc)
            continue
        entries.append(
            VideoEntry(
                filename=path.name,
                title=_display_title(path.stem),
                size_bytes=stat.st_size,
                modified=stat.st_mtime,
            )
        )

    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def resolve_video(filename: str) -> Path:
    """Resolve `filename` to an absolute path inside the video directory.

    Raises `ValueError` if the path escapes the media directory or the file
    does not exist / is not a regular file.
    """

    if not filename or "\x00" in filename:
        raise ValueError("Invalid filename")

    candidate = (VIDEO_DIR / filename).resolve()

    try:
        candidate.relative_to(VIDEO_DIR)
    except ValueError as exc:
        raise ValueError("Filename escapes media directory") from exc

    if not candidate.is_file():
        raise ValueError("Video file not found")

    return candidate
