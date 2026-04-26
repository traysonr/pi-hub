"""Per-file metadata catalog (category + play_count) for video/audio.

This module owns two JSON files:

    config/video-catalog.json   -- one entry per file in media/videos
    config/audio-catalog.json   -- one entry per file in media/music

Each entry is keyed by the on-disk filename (the same value that the
HTTP catalogue API exposes as ``filename``) and tracks two fields:

    {
      "<filename>": { "category": "", "play_count": 0 }
    }

Invariants the rest of the app relies on:

- Every file present in the corresponding media directory has a JSON
  entry. ``sync_all()`` runs at app boot to seed entries for any files
  that pre-date this feature, and to prune entries whose file was
  removed outside the app (e.g. manual ``rm``).
- ``register()`` is called by the downloader on every successful
  download (single + bulk paths) so new arrivals get an entry.
- ``remove()`` is called by the delete route handlers so the JSON
  shrinks alongside the media directory.
- ``increment_play_count()`` is called by the play route so the
  counter reflects user-initiated playback.

All public functions take a ``kind`` of ``"video"`` or ``"audio"`` and
are safe to call concurrently from background threads (downloads run
on worker threads).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Literal

from app.config import (
    AUDIO_EXTENSIONS,
    CONFIG_DIR,
    MUSIC_DIR,
    VIDEO_DIR,
    VIDEO_EXTENSIONS,
)

log = logging.getLogger(__name__)

Kind = Literal["video", "audio"]

_VIDEO_FILE = CONFIG_DIR / "video-catalog.json"
_AUDIO_FILE = CONFIG_DIR / "audio-catalog.json"

_lock = threading.Lock()


def _paths(kind: Kind) -> tuple[Path, Path, frozenset[str]]:
    if kind == "video":
        return _VIDEO_FILE, VIDEO_DIR, VIDEO_EXTENSIONS
    if kind == "audio":
        return _AUDIO_FILE, MUSIC_DIR, AUDIO_EXTENSIONS
    raise ValueError(f"Unknown metadata kind: {kind!r}")


def _default_entry() -> dict[str, Any]:
    return {"category": "", "play_count": 0}


def _normalize_entry(value: Any) -> dict[str, Any]:
    """Coerce a stored entry into the canonical shape."""

    entry = _default_entry()
    if isinstance(value, dict):
        category = value.get("category", "")
        if isinstance(category, str):
            entry["category"] = category
        play_count = value.get("play_count", 0)
        if isinstance(play_count, bool):
            play_count = int(play_count)
        if isinstance(play_count, int) and play_count >= 0:
            entry["play_count"] = play_count
    return entry


def _load(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s (%s); starting fresh", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("Unexpected shape in %s; starting fresh", path)
        return {}
    return {str(k): _normalize_entry(v) for k, v in raw.items()}


def _save(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True)
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(path)


def _is_playable(name: str, exts: frozenset[str]) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in exts


def register(filename: str, kind: Kind) -> dict[str, Any]:
    """Ensure an entry exists for ``filename``. Returns the entry."""

    path, _media_dir, _exts = _paths(kind)
    with _lock:
        data = _load(path)
        entry = data.get(filename)
        if entry is None:
            entry = _default_entry()
            data[filename] = entry
            _save(path, data)
            log.info("metadata: registered %s entry for %s", kind, filename)
        return dict(entry)


def set_category(filename: str, kind: Kind, category: str) -> dict[str, Any]:
    """Set the category for ``filename`` and persist. Auto-registers."""

    path, _media_dir, _exts = _paths(kind)
    with _lock:
        data = _load(path)
        entry = data.get(filename)
        if entry is None:
            entry = _default_entry()
            data[filename] = entry
        entry["category"] = category
        _save(path, data)
        log.info(
            "metadata: set %s category for %s -> %r", kind, filename, category
        )
        return dict(entry)


def remove(filename: str, kind: Kind) -> bool:
    """Drop ``filename`` from the catalog. Returns True if removed."""

    path, _media_dir, _exts = _paths(kind)
    with _lock:
        data = _load(path)
        if filename in data:
            del data[filename]
            _save(path, data)
            log.info("metadata: removed %s entry for %s", kind, filename)
            return True
    return False


def increment_play_count(filename: str, kind: Kind) -> int | None:
    """Increment play_count for ``filename``. Returns new count, or None
    if the file isn't in the catalog (caller can decide to register)."""

    path, _media_dir, _exts = _paths(kind)
    with _lock:
        data = _load(path)
        entry = data.get(filename)
        if entry is None:
            entry = _default_entry()
            data[filename] = entry
        entry["play_count"] = int(entry.get("play_count", 0)) + 1
        _save(path, data)
        return entry["play_count"]


def sync(kind: Kind) -> tuple[int, int]:
    """Reconcile the JSON file with the on-disk media directory.

    Adds default entries for files that lack one and removes entries
    whose file no longer exists. Returns ``(added, removed)``.
    """

    path, media_dir, exts = _paths(kind)
    added = 0
    removed = 0
    with _lock:
        data = _load(path)
        present: set[str] = set()
        if media_dir.is_dir():
            for child in media_dir.iterdir():
                if not child.is_file():
                    continue
                if not _is_playable(child.name, exts):
                    continue
                present.add(child.name)

        for name in present:
            if name not in data:
                data[name] = _default_entry()
                added += 1

        for name in list(data.keys()):
            if name not in present:
                del data[name]
                removed += 1

        if added or removed or not path.is_file():
            _save(path, data)

    if added or removed:
        log.info(
            "metadata: synced %s catalog (added=%d removed=%d)",
            kind, added, removed,
        )
    return added, removed


def sync_all() -> None:
    """Run ``sync`` for both catalogs. Safe to call at app boot."""

    sync("video")
    sync("audio")


def load(kind: Kind) -> dict[str, dict[str, Any]]:
    """Return a snapshot of the catalog (callers must not mutate)."""

    path, _media_dir, _exts = _paths(kind)
    with _lock:
        return _load(path)


def get_entry(filename: str, kind: Kind) -> dict[str, Any]:
    """Return the metadata entry for ``filename`` (default if missing).

    Always returns a fresh dict callers can safely embed in a response
    payload.
    """

    data = load(kind)
    raw = data.get(filename)
    if raw is None:
        return _default_entry()
    return dict(raw)


def list_categories(kind: Kind) -> list[dict[str, Any]]:
    """Return ``[{name, count}, ...]`` sorted by count desc, name asc.

    Useful for populating filter dropdowns in the UI.
    """

    data = load(kind)
    counts: dict[str, int] = {}
    for entry in data.values():
        cat = entry.get("category", "") or ""
        counts[cat] = counts.get(cat, 0) + 1
    items = [{"name": name, "count": n} for name, n in counts.items()]
    items.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return items
