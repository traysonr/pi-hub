"""Fetch image URLs from public subreddit JSON endpoints.

This module is intentionally dependency-free: it uses `urllib` from the
stdlib so we don't have to add `requests` to requirements. Reddit's JSON
endpoints (`/r/<sub>/top.json?...`) require nothing more than a polite
User-Agent header.

Images are filtered down to direct image URLs (i.e. files that mpv can
actually display), and downloaded to a per-theme cache directory on disk
so the screensaver can keep running without re-hitting Reddit on every
slide and survives short outages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.config import SCREENSAVER_CACHE_DIR

log = logging.getLogger(__name__)

# Reddit asks for a descriptive User-Agent that identifies the app and a
# contact handle. They actively rate-limit / block the default Python UA.
_USER_AGENT = "pi-hub/0.1 (https://github.com/traysonr/pi-hub)"

_REQUEST_TIMEOUT = 15.0

# Image extensions mpv can render in slideshow mode. We deliberately skip
# .gif (animated, mpv treats them as videos with weird timing) and .webp
# (mpv on the Pi can stutter on them).
_IMAGE_EXTS = (".jpg", ".jpeg", ".png")

# Strip URL query strings before checking extension; some CDNs append
# `?width=...` to image URLs.
_URL_EXT_RE = re.compile(r"\.(jpe?g|png)(?:$|\?)", re.IGNORECASE)


@dataclass(frozen=True)
class RedditImage:
    """A single direct image URL discovered from a subreddit listing."""

    subreddit: str
    post_id: str
    title: str
    url: str

    def cache_filename(self) -> str:
        """Stable, filesystem-safe filename derived from the post id + URL.

        Including the URL hash means that if a post is later edited to
        point at a different image, we'll re-download instead of serving
        a stale file under the same name.
        """
        digest = hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:10]
        ext = ".jpg"
        match = _URL_EXT_RE.search(self.url)
        if match:
            ext = "." + match.group(1).lower().replace("jpeg", "jpg")
        return f"{self.subreddit}_{self.post_id}_{digest}{ext}"


def _theme_cache_dir(subreddit: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", subreddit)
    path = SCREENSAVER_CACHE_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _is_direct_image_url(url: str) -> bool:
    if not url:
        return False
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(_URL_EXT_RE.search(parsed.path))


def fetch_listing(
    subreddit: str,
    *,
    sort: str = "top",
    timeframe: str = "week",
    limit: int = 50,
) -> list[RedditImage]:
    """Pull the top posts from a subreddit and return only image posts.

    Returns an empty list (with a logged warning) on any network or
    parsing error — callers should treat fetch failures as "no new
    images this round" and keep using whatever's already cached.
    """

    safe_sub = re.sub(r"[^A-Za-z0-9_]", "", subreddit)
    if not safe_sub:
        log.warning("Refusing to fetch malformed subreddit name: %r", subreddit)
        return []

    qs = urllib.parse.urlencode({"t": timeframe, "limit": str(limit), "raw_json": "1"})
    url = f"https://www.reddit.com/r/{safe_sub}/{sort}.json?{qs}"

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        log.warning("Reddit %s returned HTTP %s", safe_sub, exc.code)
        return []
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Reddit %s fetch failed: %s", safe_sub, exc)
        return []

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("Reddit %s returned invalid JSON: %s", safe_sub, exc)
        return []

    children = (data.get("data") or {}).get("children") or []
    images: list[RedditImage] = []
    for child in children:
        post = child.get("data") or {}
        if post.get("over_18"):
            # Most "EarthPorn"-style subs are SFW despite the name, but
            # respect the flag if Reddit thinks otherwise.
            continue
        url = post.get("url_overridden_by_dest") or post.get("url") or ""
        if not _is_direct_image_url(url):
            continue
        images.append(
            RedditImage(
                subreddit=safe_sub,
                post_id=str(post.get("id") or ""),
                title=str(post.get("title") or "")[:200],
                url=url,
            )
        )

    log.info("Reddit %s: found %d image posts", safe_sub, len(images))
    return images


def download_image(image: RedditImage, *, dest_dir: Path | None = None) -> Path | None:
    """Download `image` to the per-theme cache. Returns the local path or
    None on failure. Skips the download if the file already exists."""

    target_dir = dest_dir or _theme_cache_dir(image.subreddit)
    target = target_dir / image.cache_filename()
    if target.exists() and target.stat().st_size > 0:
        return target

    req = urllib.request.Request(image.url, headers={"User-Agent": _USER_AGENT})
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp, open(
            tmp, "wb"
        ) as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        tmp.replace(target)
        return target
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Image download failed (%s): %s", image.url, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def refresh_theme(
    subreddit: str,
    *,
    max_images: int = 30,
    timeframe: str = "week",
) -> tuple[int, int]:
    """Fetch the listing and download up to `max_images` to the cache.

    Returns (downloaded_now, total_in_cache). Designed to be cheap on
    repeat calls — already-cached images are skipped instantly.
    """

    started = time.time()
    listing = fetch_listing(subreddit, timeframe=timeframe, limit=max(max_images * 2, 50))
    cache_dir = _theme_cache_dir(subreddit)
    downloaded = 0
    for image in listing[:max_images]:
        path = download_image(image, dest_dir=cache_dir)
        if path is not None and not path.exists():
            continue
        # Count only files that didn't already exist before this call.
        if path is not None:
            mtime = path.stat().st_mtime
            if mtime >= started:
                downloaded += 1

    total = sum(1 for p in cache_dir.iterdir() if p.suffix.lower() in _IMAGE_EXTS)
    log.info(
        "Theme %s refreshed: +%d new, %d total in cache",
        subreddit, downloaded, total,
    )
    return downloaded, total


def list_cached_images(subreddit: str) -> list[Path]:
    """Return all cached images for `subreddit`, sorted for stable order."""
    cache_dir = _theme_cache_dir(subreddit)
    return sorted(
        p
        for p in cache_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )
