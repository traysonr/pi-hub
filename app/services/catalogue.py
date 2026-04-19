"""Filesystem-backed media catalogue."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import (
    AUDIO_EXTENSIONS,
    MUSIC_DIR,
    VIDEO_DIR,
    VIDEO_EXTENSIONS,
)

# How long a file must be idle (no writes) before the catalogue is willing
# to expose it. yt-dlp's audio extraction streams ffmpeg output directly
# into the final filename, so the destination file appears as soon as
# extraction starts -- but reading it then yields a truncated/invalid
# file. Hiding files whose mtime is very recent avoids surfacing
# half-written tracks in the UI.
_MIN_IDLE_SECONDS = 3.0

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
class MediaEntry:
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


# Backwards-compatible alias: existing code/tests refer to VideoEntry.
VideoEntry = MediaEntry


def _list_dir(directory: Path, extensions: frozenset[str]) -> list[MediaEntry]:
    """Return every playable file in `directory`, newest first."""

    if not directory.exists():
        return []

    now = time.time()
    entries: list[MediaEntry] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        if _is_ytdlp_intermediate(path.name):
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            log.warning("Skipping %s: %s", path, exc)
            continue
        # Skip files that look like an in-progress write so the UI doesn't
        # offer half-extracted audio for playback (which mpv refuses).
        if (now - stat.st_mtime) < _MIN_IDLE_SECONDS:
            continue
        entries.append(
            MediaEntry(
                filename=path.name,
                title=_display_title(path.stem),
                size_bytes=stat.st_size,
                modified=stat.st_mtime,
            )
        )

    entries.sort(key=lambda e: e.modified, reverse=True)
    return entries


def _resolve_in(directory: Path, filename: str, kind: str) -> Path:
    """Resolve `filename` to an absolute path inside `directory`.

    Raises `ValueError` if the path escapes the directory or the file does
    not exist / is not a regular file.
    """

    if not filename or "\x00" in filename:
        raise ValueError("Invalid filename")

    candidate = (directory / filename).resolve()

    try:
        candidate.relative_to(directory)
    except ValueError as exc:
        raise ValueError(f"Filename escapes {kind} directory") from exc

    if not candidate.is_file():
        raise ValueError(f"{kind.capitalize()} file not found")

    return candidate


def list_videos() -> list[MediaEntry]:
    """Return every playable video in the catalogue, newest first."""

    return _list_dir(VIDEO_DIR, VIDEO_EXTENSIONS)


def resolve_video(filename: str) -> Path:
    """Resolve `filename` to an absolute path inside the video directory."""

    return _resolve_in(VIDEO_DIR, filename, "video")


def list_music() -> list[MediaEntry]:
    """Return every playable audio track in the catalogue, newest first."""

    return _list_dir(MUSIC_DIR, AUDIO_EXTENSIONS)


def resolve_music(filename: str) -> Path:
    """Resolve `filename` to an absolute path inside the music directory."""

    return _resolve_in(MUSIC_DIR, filename, "music")
