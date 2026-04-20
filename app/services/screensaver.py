"""Screensaver subsystem.

Themes, image cache, and the user-facing "slideshow vs yellow"
preference. The actual fullscreen rendering is delegated to
`app.services.display`, which owns the persistent mpv process so
transitions between slideshow, video, and yellow fallback never expose
the underlying Linux console.

User model (unchanged from the previous implementation):

- ``enabled`` is the master toggle. When True, the TV's idle screen is
  the slideshow; when False, the idle screen is a solid yellow
  placeholder. The user explicitly never sees the terminal either way.
- ``start()`` immediately puts the slideshow on screen. Refuses if a
  video is playing or the master toggle is off.
- ``stop()`` immediately swaps the slideshow for the yellow fallback
  without changing the master toggle. Useful for "show me a blank
  screen for a minute" without losing the configured theme list.

Themes are managed via a JSON config file (see
`config/screensaver-themes.json.example`). The file format is:

    {
      "themes": [
        {"name": "Watercolor", "subreddit": "Watercolor", "enabled": true},
        {"name": "EarthPorn",  "subreddit": "EarthPorn",  "enabled": true}
      ],
      "image_seconds": 60
    }
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import (
    SCREENSAVER_CACHE_DIR,
    SCREENSAVER_THEMES_EXAMPLE,
    SCREENSAVER_THEMES_FILE,
)
from app.services import display, reddit

log = logging.getLogger(__name__)

_DEFAULT_IMAGE_SECONDS = 60
_MIN_IMAGE_SECONDS = 5
_MAX_IMAGE_SECONDS = 60 * 60


@dataclass
class Theme:
    name: str
    subreddit: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _State:
    # User toggle: "the screensaver feature is on". Defaults to True so a
    # fresh boot lands on the slideshow rather than the bare yellow
    # placeholder; toggle off in the UI to keep the yellow idle screen.
    enabled: bool = True
    last_error: str | None = None
    last_refresh_at: float | None = None
    last_refresh_summary: str | None = None
    image_seconds: int = _DEFAULT_IMAGE_SECONDS
    themes: list[Theme] = field(default_factory=list)


_lock = threading.Lock()
_state = _State()
_playlist_path: Path | None = None
_refresh_thread: threading.Thread | None = None


# --- Config file IO ----------------------------------------------------

def _write_example_if_missing() -> None:
    """Drop a starter example file in `config/` so the user has something
    to copy from. Safe to call repeatedly."""

    if SCREENSAVER_THEMES_EXAMPLE.exists():
        return
    try:
        SCREENSAVER_THEMES_EXAMPLE.parent.mkdir(parents=True, exist_ok=True)
        SCREENSAVER_THEMES_EXAMPLE.write_text(
            json.dumps(
                {
                    "image_seconds": 60,
                    "themes": [
                        {"name": "Watercolor", "subreddit": "Watercolor", "enabled": True},
                        {"name": "EarthPorn", "subreddit": "EarthPorn", "enabled": True},
                        {"name": "ArtPorn", "subreddit": "ArtPorn", "enabled": True},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        log.info("Wrote screensaver themes example to %s", SCREENSAVER_THEMES_EXAMPLE)
    except OSError as exc:
        log.warning("Could not write screensaver themes example: %s", exc)


def _load_config_locked() -> None:
    """Populate `_state.themes` and `_state.image_seconds` from disk.

    Falls back to a hardcoded default (the three themes the user asked
    for) if the file is missing or malformed, so the UI always has
    something to toggle.
    """
    _write_example_if_missing()

    fallback_themes = [
        Theme("Watercolor", "Watercolor", True),
        Theme("EarthPorn", "EarthPorn", True),
        Theme("ArtPorn", "ArtPorn", True),
    ]

    if not SCREENSAVER_THEMES_FILE.exists():
        _state.themes = fallback_themes
        _state.image_seconds = _DEFAULT_IMAGE_SECONDS
        return

    try:
        raw = json.loads(SCREENSAVER_THEMES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read %s: %s — using defaults", SCREENSAVER_THEMES_FILE, exc)
        _state.themes = fallback_themes
        _state.image_seconds = _DEFAULT_IMAGE_SECONDS
        return

    seconds_raw = raw.get("image_seconds", _DEFAULT_IMAGE_SECONDS)
    try:
        seconds = int(seconds_raw)
    except (TypeError, ValueError):
        seconds = _DEFAULT_IMAGE_SECONDS
    _state.image_seconds = max(_MIN_IMAGE_SECONDS, min(_MAX_IMAGE_SECONDS, seconds))

    themes: list[Theme] = []
    for entry in raw.get("themes", []) or []:
        if not isinstance(entry, dict):
            continue
        sub = str(entry.get("subreddit") or "").strip()
        if not sub:
            continue
        name = str(entry.get("name") or sub).strip() or sub
        themes.append(Theme(name=name, subreddit=sub, enabled=bool(entry.get("enabled", True))))

    _state.themes = themes or fallback_themes


def _save_config_locked() -> None:
    """Persist current themes + image_seconds back to disk."""
    SCREENSAVER_THEMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "image_seconds": _state.image_seconds,
        "themes": [t.to_dict() for t in _state.themes],
    }
    tmp = SCREENSAVER_THEMES_FILE.with_suffix(SCREENSAVER_THEMES_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SCREENSAVER_THEMES_FILE)


# --- Public API --------------------------------------------------------

def init() -> None:
    """Load config from disk and register the playlist provider with the
    display controller. Called once at app startup."""

    with _lock:
        _load_config_locked()
        image_seconds = _state.image_seconds
        enabled = _state.enabled

    # Lock released before calling into display, because display will
    # call back into _build_playlist (which acquires our _lock again).
    display.set_slideshow_playlist_provider(_build_playlist)
    display.set_slideshow_image_seconds(image_seconds)
    display.set_idle_mode(
        display.MODE_SLIDESHOW if enabled else display.MODE_YELLOW
    )


def get_status() -> dict[str, Any]:
    # Pull the display snapshot first, outside our lock, so we never
    # nest screensaver._lock inside display._lock (the inverse order is
    # used by the playlist-provider callback path).
    display_state = display.get_state()
    with _lock:
        return {
            "enabled": _state.enabled,
            # "running" historically meant "slideshow mpv is alive". Now
            # it means "the TV is currently showing the slideshow", which
            # is the same observable property to the user.
            "running": display_state.get("mode") == display.MODE_SLIDESHOW,
            "started_at": display_state.get("started_at"),
            "last_error": _state.last_error or display_state.get("last_error"),
            "last_refresh_at": _state.last_refresh_at,
            "last_refresh_summary": _state.last_refresh_summary,
            "image_seconds": _state.image_seconds,
            "themes": [
                {**t.to_dict(), "cached_images": _count_cached(t.subreddit)}
                for t in _state.themes
            ],
            "video_playing": display_state.get("mode") == display.MODE_VIDEO,
            "display_mode": display_state.get("mode"),
            "idle_mode": display_state.get("idle_mode"),
        }


def set_enabled(enabled: bool) -> dict[str, Any]:
    """Flip the master toggle.

    Enabling the screensaver makes slideshow the post-video idle mode;
    disabling it makes the yellow fallback the post-video idle mode.
    Either way the change applies to the TV immediately *unless* a
    video is currently playing -- we never yank a user out of mid-video
    to refresh the idle screen.
    """

    with _lock:
        _state.enabled = bool(enabled)
        new_idle = display.MODE_SLIDESHOW if _state.enabled else display.MODE_YELLOW
    # Drop our lock before calling into display: the controller will
    # call back into _build_playlist (the registered provider), which
    # also takes _lock.
    display.set_idle_mode(new_idle)
    log.info("Screensaver enabled=%s", enabled)
    return get_status()


def toggle_theme(name: str) -> dict[str, Any]:
    """Flip the enabled flag on the named theme and persist."""
    with _lock:
        for theme in _state.themes:
            if theme.name == name:
                theme.enabled = not theme.enabled
                try:
                    _save_config_locked()
                except OSError as exc:
                    log.warning("Failed to save themes config: %s", exc)
                break
        else:
            raise KeyError(f"No such theme: {name}")
    # If the slideshow is currently the active idle screen, rebuild it
    # so the toggle takes effect right away without a manual restart.
    display.reapply_idle()
    return get_status()


def reload_config() -> dict[str, Any]:
    """Re-read the themes config file from disk."""
    with _lock:
        _load_config_locked()
        image_seconds = _state.image_seconds
    display.set_slideshow_image_seconds(image_seconds)
    display.reapply_idle()
    return get_status()


def start() -> dict[str, Any]:
    """Force the slideshow on screen now.

    Refuses if a video is currently playing (mutual exclusion is
    enforced by the display controller too, but rejecting here gives
    the UI a clean 409). Refuses if the master toggle is off so the
    "Enabled" switch behaves like a real safety.
    """

    with _lock:
        if not _state.enabled:
            raise RuntimeError("Screensaver is disabled. Enable it first.")
    if display.is_video_mode():
        raise RuntimeError(
            "Cannot start screensaver while a video is playing. Stop the video first."
        )

    # Lock released for the display call: show_slideshow_now invokes
    # the registered playlist provider (_build_playlist), which takes
    # our _lock again.
    try:
        started = display.show_slideshow_now()
    except RuntimeError as exc:
        with _lock:
            _state.last_error = str(exc)
        raise

    with _lock:
        if started:
            _state.last_error = None
        else:
            # No cached images yet: the controller fell back to the
            # yellow placeholder so the TV is never blank, but the
            # user asked for the slideshow so surface a hint.
            _state.last_error = (
                "No cached images yet. Press Refresh to download "
                "images. Showing yellow placeholder in the meantime."
            )

    # Best-effort background refresh so the cache stays warm.
    _kick_refresh_async()
    return get_status()


def stop() -> dict[str, Any]:
    """Swap the slideshow for the yellow fallback immediately.

    This intentionally does *not* change the ``enabled`` flag, so a
    subsequent video will still end back into slideshow mode. Use
    ``set_enabled(False)`` if you want the yellow fallback to be
    permanent.
    """

    if not display.is_video_mode():
        # Don't override an active video; the controller already
        # guards against this but failing fast keeps the API obvious.
        try:
            display.show_yellow_now()
        except RuntimeError as exc:
            log.warning("Failed to switch to yellow fallback: %s", exc)
    return get_status()


def refresh_now() -> dict[str, Any]:
    """Trigger a synchronous refresh of all enabled themes."""
    with _lock:
        themes = [t for t in _state.themes if t.enabled]

    started = time.time()
    summary_parts: list[str] = []
    for theme in themes:
        try:
            new, total = reddit.refresh_theme(theme.subreddit)
            summary_parts.append(f"{theme.subreddit}: +{new} ({total} total)")
        except Exception:  # noqa: BLE001
            log.exception("Theme refresh failed: %s", theme.subreddit)
            summary_parts.append(f"{theme.subreddit}: error")

    with _lock:
        _state.last_refresh_at = time.time()
        _state.last_refresh_summary = (
            "; ".join(summary_parts) if summary_parts else "no themes enabled"
        )
    log.info("Refresh done in %.1fs: %s", time.time() - started, _state.last_refresh_summary)

    # Pick up freshly downloaded images live without forcing the user
    # to press Start again.
    display.reapply_idle()
    return get_status()


def stop_for_video() -> bool:
    """Compatibility shim retained for the media routes.

    The display controller automatically swaps slideshow content for the
    requested video, so there's nothing to "stop" anymore. We still
    return whether a slideshow *was* on screen because callers (and
    logs) read it as a state-transition signal.
    """

    return display.get_state().get("mode") == display.MODE_SLIDESHOW


# --- Internals ---------------------------------------------------------

def _count_cached(subreddit: str) -> int:
    try:
        return len(reddit.list_cached_images(subreddit))
    except OSError:
        return 0


def _build_playlist() -> Path | None:
    """Concatenate cached images across all enabled themes into one
    shuffled mpv playlist file. Returns the playlist path, or None if
    no images are available.

    Called by the display controller (registered via
    ``display.set_slideshow_playlist_provider``) every time slideshow
    mode is (re)entered, so theme toggles and cache refreshes show up
    on the very next slide.
    """

    global _playlist_path

    with _lock:
        themes = list(_state.themes)

    paths: list[Path] = []
    for theme in themes:
        if not theme.enabled:
            continue
        try:
            paths.extend(reddit.list_cached_images(theme.subreddit))
        except OSError:
            continue

    if not paths:
        return None

    random.shuffle(paths)
    SCREENSAVER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    playlist = SCREENSAVER_CACHE_DIR / "_playlist.m3u"
    playlist.write_text(
        "\n".join(str(p) for p in paths) + "\n",
        encoding="utf-8",
    )
    _playlist_path = playlist
    return playlist


def _kick_refresh_async() -> None:
    """Fire a background refresh thread. Only one runs at a time."""
    global _refresh_thread

    if _refresh_thread is not None and _refresh_thread.is_alive():
        return

    def _worker() -> None:
        try:
            refresh_now()
        except Exception:  # noqa: BLE001
            log.exception("Background refresh worker failed")

    _refresh_thread = threading.Thread(
        target=_worker, name="screensaver-refresh", daemon=True
    )
    _refresh_thread.start()
