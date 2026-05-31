"""Fetch image URLs from subreddits via old.reddit.com HTML scraping.

Reddit's unauthenticated JSON endpoints (``/r/<sub>/top.json``) return
HTTP 403 on some networks/IPs.  The old.reddit.com HTML interface serves
the same top-post listings without requiring authentication and is
accessible even when the JSON API is blocked.

We extract direct image URLs from the ``data-url`` attribute that
old.reddit embeds in each post's container ``<div>``.  Everything
downstream (download_image, refresh_theme, list_cached_images) is
unchanged — only fetch_listing switches from JSON parsing to HTML
scraping.

The module remains intentionally dependency-free (stdlib only).
"""

from __future__ import annotations

import hashlib
import html as html_lib
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

# old.reddit.com rejects obviously bot-like requests.  A realistic
# browser UA + Accept headers is sufficient to get through.
#
# Two header sets are needed:
#  - _PAGE_HEADERS  for fetching old.reddit.com HTML listing pages
#    (Accept: text/html, which is what a browser sends for a web page)
#  - _IMAGE_HEADERS for downloading images from i.redd.it
#    (Accept: image/*, because i.redd.it does content-negotiation and
#    returns its HTML UI instead of the JPEG when the client advertises
#    text/html as a preferred type)
_UA = "Mozilla/5.0 (X11; Linux aarch64; rv:109.0) Gecko/20100101 Firefox/115.0"

_PAGE_HEADERS = {
    "User-Agent": _UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

_IMAGE_HEADERS = {
    "User-Agent": _UA,
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

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
    """Scrape old.reddit.com for image posts in ``subreddit``.

    Each post on old.reddit is rendered as a ``<div>`` with a
    ``data-url`` attribute containing the post's link URL.  We collect
    the ones whose URL is a direct image (.jpg / .jpeg / .png).

    Returns an empty list (with a logged warning) on any network or
    parsing error — callers should treat fetch failures as "no new
    images this round" and keep using whatever's already cached.
    """

    safe_sub = re.sub(r"[^A-Za-z0-9_]", "", subreddit)
    if not safe_sub:
        log.warning("Refusing to fetch malformed subreddit name: %r", subreddit)
        return []

    # old.reddit honours the same ?t= and ?limit= parameters as the
    # JSON API, capped at 100 per page.
    qs = urllib.parse.urlencode({"t": timeframe, "limit": str(min(limit, 100))})
    url = f"https://old.reddit.com/r/{safe_sub}/{sort}/?{qs}"

    req = urllib.request.Request(url, headers=_PAGE_HEADERS)
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
        html_text = raw.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("Reddit %s: failed to decode response: %s", safe_sub, exc)
        return []

    images: list[RedditImage] = []

    # old.reddit renders each post as a <div> whose opening tag contains
    # both data-fullname="t3_<id>" and data-url="<link>".  We iterate
    # over all div opening tags, keep only post containers (class
    # includes both "thing" and "link"), and extract the two attributes.
    for tag_m in re.finditer(r"<div\b([^>]+)>", html_text):
        attrs = tag_m.group(1)

        cls_m = re.search(r'\bclass="([^"]*)"', attrs)
        if not cls_m:
            continue
        classes = cls_m.group(1)
        if "thing" not in classes or "link" not in classes:
            continue

        fn_m = re.search(r'\bdata-fullname="t3_([A-Za-z0-9]+)"', attrs)
        if not fn_m:
            continue
        post_id = fn_m.group(1)

        url_m = re.search(r'\bdata-url="([^"]+)"', attrs)
        if not url_m:
            continue
        img_url = html_lib.unescape(url_m.group(1))

        if not _is_direct_image_url(img_url):
            continue

        images.append(
            RedditImage(
                subreddit=safe_sub,
                post_id=post_id,
                title="",
                url=img_url,
            )
        )

        if len(images) >= limit:
            break

    log.info("Reddit %s: found %d image posts (HTML scrape)", safe_sub, len(images))
    return images


def download_image(image: RedditImage, *, dest_dir: Path | None = None) -> Path | None:
    """Download `image` to the per-theme cache. Returns the local path or
    None on failure. Skips the download if the file already exists."""

    target_dir = dest_dir or _theme_cache_dir(image.subreddit)
    target = target_dir / image.cache_filename()
    if target.exists() and target.stat().st_size > 0:
        return target

    req = urllib.request.Request(image.url, headers=_IMAGE_HEADERS)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            # Guard against CDNs that do content-negotiation and return an
            # HTML page instead of the image when the Accept header is wrong.
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct and not ct.startswith("image/"):
                log.warning(
                    "Image download skipped (%s): server returned Content-Type=%s",
                    image.url, ct,
                )
                return None
            with open(tmp, "wb") as fh:
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

    If the requested ``timeframe`` returns no image posts (e.g. a small
    or quiet subreddit with nothing in the past week), we widen the
    search progressively (week -> month -> year -> all) so the user
    still gets *something* to look at instead of a permanently empty
    cache.
    """

    started = time.time()

    # Build the fallback chain starting from the requested timeframe.
    # Reddit accepts hour/day/week/month/year/all; we only widen, never
    # narrow, since the caller's choice is the *minimum* freshness.
    _WIDENING = ["hour", "day", "week", "month", "year", "all"]
    try:
        start_idx = _WIDENING.index(timeframe)
    except ValueError:
        start_idx = _WIDENING.index("week")
    timeframes = _WIDENING[start_idx:]

    # Keep widening until we have a healthy listing. Some quiet subs
    # have only a handful of posts in the past week/month even though
    # t=year or t=all has plenty -- stopping at the first non-empty
    # timeframe would leave us with e.g. 2 images forever. We accept
    # whatever the widest timeframe returns as the floor.
    listing: list[RedditImage] = []
    for tf in timeframes:
        candidate = fetch_listing(subreddit, timeframe=tf, limit=max(max_images * 2, 50))
        if len(candidate) > len(listing):
            listing = candidate
        if len(listing) >= max_images:
            if tf != timeframe:
                log.info(
                    "Theme %s: widened t=%s -> t=%s to reach %d images",
                    subreddit, timeframe, tf, len(listing),
                )
            break
    else:
        if listing and timeframes[0] != timeframes[-1]:
            log.info(
                "Theme %s: only %d images available even at t=%s",
                subreddit, len(listing), timeframes[-1],
            )

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
