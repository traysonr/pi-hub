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
import os
import random
import re
import shutil
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

# Target number of cached images per theme at steady state. At 05:00
# local time we drop 75% of the current cache (keeping a random 25%)
# and refill up to this count with Reddit's current top listing. 50 is
# enough variety for an all-day slideshow without being wasteful on the
# SD card; override per-install via the env var if desired.
_DEFAULT_CACHE_TARGET = int(os.environ.get("PI_HUB_THEME_CACHE_TARGET", "50"))
# Fraction of the existing cache retained during a rotation. The rest
# is deleted and re-downloaded. 0.25 => keep a random 25%, drop 75%.
_ROTATION_KEEP_FRACTION = 0.25


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
    current_path = display.get_current_path()
    current_image: str | None = None
    can_delete_current = False
    if display_state.get("mode") == display.MODE_SLIDESHOW and current_path:
        try:
            p = Path(current_path).resolve()
            cache_root = SCREENSAVER_CACHE_DIR.resolve()
            if p.is_file() and cache_root in p.parents:
                # Don't ever expose/delete internal helper files.
                if p.name not in ("_playlist.m3u", "_pi-hub-yellow.png"):
                    current_image = p.name
                    can_delete_current = True
        except OSError:
            pass
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
            "current_image": current_image,
            "can_delete_current_image": can_delete_current,
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


# Matches a valid Reddit subreddit name on its own: letters, digits, and
# underscores, 3-21 characters (Reddit's documented limit). We normalize
# a few user-friendly inputs to this form before validating.
_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,21}$")


def _normalize_subreddit(raw: str) -> str:
    """Turn a user-friendly input into a bare subreddit name.

    Accepts any of:
      - ``robotics``
      - ``r/robotics`` / ``/r/robotics``
      - ``https://www.reddit.com/r/robotics/`` (with or without trailing slash)
      - ``reddit.com/r/robotics``

    Raises ``ValueError`` with a user-facing message if the input can't
    be reduced to a syntactically valid subreddit name.
    """

    text = (raw or "").strip()
    if not text:
        raise ValueError("Subreddit name is required.")

    # Strip URL scheme + host if the user pasted a full Reddit link.
    match = re.search(r"(?:^|/)r/([^/?#\s]+)", text, flags=re.IGNORECASE)
    if match:
        text = match.group(1)

    # Strip any leftover leading ``r/`` or ``/`` after the URL pass.
    text = text.lstrip("/")
    if text.lower().startswith("r/"):
        text = text[2:]
    text = text.strip("/")

    if not _SUBREDDIT_NAME_RE.match(text):
        raise ValueError(
            f"{raw!r} is not a valid subreddit name. Use letters, digits, and "
            "underscores (3-21 characters), e.g. 'robotics'."
        )
    return text


def add_theme(subreddit: str) -> dict[str, Any]:
    """Append a new theme for ``subreddit`` and persist.

    Returns the updated status payload. Raises ``ValueError`` if the
    input is syntactically invalid, or ``KeyError`` if a theme for the
    same subreddit already exists (case-insensitive) -- routes map
    these to 400 / 409 respectively.
    """

    normalized = _normalize_subreddit(subreddit)
    lowered = normalized.lower()

    with _lock:
        for theme in _state.themes:
            if theme.subreddit.lower() == lowered:
                raise KeyError(
                    f"A theme for r/{theme.subreddit} is already in the list."
                )
        _state.themes.append(
            Theme(name=normalized, subreddit=normalized, enabled=True)
        )
        try:
            _save_config_locked()
        except OSError as exc:
            log.warning("Failed to save themes config: %s", exc)

    # Fire a background refresh so the new subreddit's images show up
    # on screen without the user having to press Refresh manually. The
    # refresh worker will call reapply_idle() when it finishes.
    _kick_refresh_async()
    log.info("Added screensaver theme r/%s", normalized)
    return get_status()


def remove_theme(name: str) -> dict[str, Any]:
    """Remove the named theme and delete its cached images on disk.

    Matches on ``name`` (not subreddit) for symmetry with
    ``toggle_theme``. Raises ``KeyError`` if no such theme exists.
    """

    with _lock:
        target: Theme | None = None
        for theme in _state.themes:
            if theme.name == name:
                target = theme
                break
        if target is None:
            raise KeyError(f"No such theme: {name}")
        _state.themes = [t for t in _state.themes if t is not target]
        try:
            _save_config_locked()
        except OSError as exc:
            log.warning("Failed to save themes config: %s", exc)

    # Best-effort cache cleanup. The cache dir is derived from the
    # subreddit name (see ``reddit._theme_cache_dir``); we reproduce
    # the same sanitization here to find it. Failures are logged but
    # don't fail the API call -- the theme is already gone from the
    # config, which is the user-facing contract.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", target.subreddit)
    cache_dir = SCREENSAVER_CACHE_DIR / safe
    if cache_dir.is_dir():
        try:
            shutil.rmtree(cache_dir)
            log.info("Removed cached images for r/%s at %s", target.subreddit, cache_dir)
        except OSError as exc:
            log.warning("Failed to delete cache dir %s: %s", cache_dir, exc)

    # Rebuild the slideshow so images from the removed theme disappear
    # immediately instead of lingering until the next mode transition.
    display.reapply_idle()
    log.info("Removed screensaver theme %s (r/%s)", name, target.subreddit)
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


def rotate_theme(
    subreddit: str,
    *,
    target: int = _DEFAULT_CACHE_TARGET,
    keep_fraction: float = _ROTATION_KEEP_FRACTION,
) -> dict[str, Any]:
    """Drop 75% of ``subreddit``'s cached images (random), refill up to
    ``target``.

    Returns a per-theme summary dict with the counts at each step. The
    "keep N random" semantics are deliberate: the user asked for
    variety-over-time, not "newest always wins", so on a day when
    Reddit's top listing barely changed we still end up with some fresh
    bytes on disk and don't repeat yesterday's exact slideshow.

    This is safe to call concurrently with the slideshow running: the
    display controller holds its own references to the playlist file
    and mpv re-reads files lazily. We call ``display.reapply_idle()``
    at the end so the running slideshow picks up the rotated set
    immediately.
    """

    before = reddit.list_cached_images(subreddit)
    before_count = len(before)

    # How many to keep = floor(current * keep_fraction), but never more
    # than what's currently there and never more than the target.
    keep_n = min(before_count, target, int(before_count * keep_fraction))
    kept: list[Path] = []
    if keep_n > 0:
        kept = random.sample(before, keep_n)
    keep_set = {p.resolve() for p in kept}

    deleted = 0
    for path in before:
        if path.resolve() in keep_set:
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            log.warning("Rotate: could not delete %s: %s", path, exc)

    # Refill. refresh_theme caps its own download count, so ask for
    # exactly the shortfall -- no point hammering Reddit for 50 images
    # when we're only missing 38.
    shortfall = max(0, target - len(kept))
    downloaded = 0
    fetched_total = 0
    if shortfall > 0:
        try:
            downloaded, fetched_total = reddit.refresh_theme(
                subreddit, max_images=shortfall
            )
        except Exception:  # noqa: BLE001
            log.exception("Rotate: refresh failed for %s", subreddit)

    after = reddit.list_cached_images(subreddit)
    summary = {
        "subreddit": subreddit,
        "before": before_count,
        "kept": len(kept),
        "deleted": deleted,
        "downloaded": downloaded,
        "after": len(after),
        "target": target,
    }
    log.info(
        "Rotate %s: kept %d/%d, deleted %d, +%d new, now %d (target %d)",
        subreddit, len(kept), before_count, deleted, downloaded,
        len(after), target,
    )
    return summary


def rotate_all_themes(
    *,
    target: int = _DEFAULT_CACHE_TARGET,
    keep_fraction: float = _ROTATION_KEEP_FRACTION,
) -> dict[str, Any]:
    """Run :func:`rotate_theme` for every *enabled* theme, update the
    last-refresh bookkeeping, and push the new playlist to the TV.

    Disabled themes are skipped: they're not contributing to the
    slideshow anyway, so there's no value burning bandwidth rotating
    their cache.
    """

    started = time.time()
    with _lock:
        themes = [t for t in _state.themes if t.enabled]

    per_theme: list[dict[str, Any]] = []
    for theme in themes:
        try:
            per_theme.append(
                rotate_theme(
                    theme.subreddit,
                    target=target,
                    keep_fraction=keep_fraction,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Rotate: theme %s failed", theme.subreddit)
            per_theme.append(
                {"subreddit": theme.subreddit, "error": str(exc) or "error"}
            )

    total_kept = sum(p.get("kept", 0) for p in per_theme)
    total_deleted = sum(p.get("deleted", 0) for p in per_theme)
    total_downloaded = sum(p.get("downloaded", 0) for p in per_theme)
    total_after = sum(p.get("after", 0) for p in per_theme)

    summary = (
        f"kept {total_kept}, deleted {total_deleted}, "
        f"downloaded {total_downloaded}, now {total_after} cached"
    )
    with _lock:
        _state.last_refresh_at = time.time()
        _state.last_refresh_summary = f"rotate: {summary}"

    # Slideshow catches the new set on its next mode refresh.
    display.reapply_idle()
    log.info(
        "Rotation done in %.1fs across %d themes: %s",
        time.time() - started, len(themes), summary,
    )
    return {
        "summary": summary,
        "themes": per_theme,
        "target": target,
    }


def stop_for_video() -> bool:
    """Compatibility shim retained for the media routes.

    The display controller automatically swaps slideshow content for the
    requested video, so there's nothing to "stop" anymore. We still
    return whether a slideshow *was* on screen because callers (and
    logs) read it as a state-transition signal.
    """

    return display.get_state().get("mode") == display.MODE_SLIDESHOW


def delete_current_image() -> dict[str, Any]:
    """Delete the image currently being displayed by the slideshow.

    Only applies when the display controller is in slideshow mode, and only
    deletes files under the screensaver cache directory.
    """

    display_state = display.get_state()
    if display_state.get("mode") != display.MODE_SLIDESHOW:
        raise RuntimeError("Screensaver is not currently showing images.")

    current = display.get_current_path()
    if not current:
        raise RuntimeError("Could not determine the current image.")

    try:
        path = Path(current).resolve()
        cache_root = SCREENSAVER_CACHE_DIR.resolve()
    except OSError as exc:
        raise RuntimeError(f"Invalid current image path: {exc}") from exc

    if cache_root not in path.parents:
        raise RuntimeError("Current image is not part of the screensaver cache.")
    if path.name in ("_playlist.m3u", "_pi-hub-yellow.png"):
        raise RuntimeError("Current image is not deletable.")
    if not path.is_file():
        raise RuntimeError("Current image file no longer exists.")

    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError(f"Failed to delete image: {exc}") from exc

    # Rebuild playlist so the deleted image disappears immediately.
    display.reapply_idle()
    return get_status()


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
