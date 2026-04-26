#!/usr/bin/env python3
"""Backfill thumbnails for videos that pre-date the thumbnail feature.

The Video tab renders a sibling ``<stem>.jpg`` next to each video file.
New downloads get one for free (yt-dlp ``--write-thumbnail`` is wired
into ``app/services/downloader.py``), but anything downloaded before
that change has no image on disk.

This script reconstructs the YouTube id from the bracketed token in
each filename and pulls the thumbnail from YouTube's CDN directly
(``i.ytimg.com``). We deliberately do **not** shell out to yt-dlp here:
yt-dlp would run the full player-JS extractor (Deno + nsig solving +
cookies) just to discover a URL we already know, which on a Pi 3
routinely takes longer than the 120s subprocess timeout. The CDN URL
pattern has been stable for ~15 years and works without auth, so a
plain HTTPS GET is both faster and more reliable than the extractor
path.

Filename convention (set by the downloader's ``-o`` template):

    "<safe_title> [<youtube_id>] [720p].<ext>"

Files that don't match the convention are skipped with a warning so
we never act on a hand-managed file we can't identify.

Usage (from the project root, with the venv activated):

    .venv/bin/python scripts/backfill_thumbnails.py
    .venv/bin/python scripts/backfill_thumbnails.py --dry-run
    .venv/bin/python scripts/backfill_thumbnails.py --force   # overwrite

Exit code is 0 if every missing thumbnail was either fetched or
intentionally skipped, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import VIDEO_DIR, VIDEO_EXTENSIONS  # noqa: E402
from app.services import catalogue  # noqa: E402

# YouTube ids are exactly 11 chars: A–Z a–z 0–9 _ -
_ID_TOKEN_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")


def _extract_youtube_id(stem: str) -> str | None:
    """Pull the YouTube id out of a filename stem, if one is present.

    The downloader templates filenames as ``Title [<id>] [720p]`` so
    we look for the *first* 11-char id-shaped bracketed token. We
    deliberately do not use the last bracket because it's the quality
    tag (``[720p]``) which happens to also be 4 chars.
    """

    m = _ID_TOKEN_RE.search(stem)
    return m.group(1) if m else None


def _has_thumbnail(video: Path) -> bool:
    return any(
        video.with_suffix(ext).is_file()
        for ext in (".jpg", ".jpeg", ".png", ".webp")
    )


# YouTube exposes thumbnails on i.ytimg.com under a fixed name ladder.
# Try the highest-resolution variant first and fall back through the
# tiers; ``hqdefault`` is the only one guaranteed to exist for every
# uploaded video, so it's the last and most reliable rung.
_THUMBNAIL_URL_LADDER: tuple[str, ...] = (
    "maxresdefault.jpg",  # 1280x720, present for most modern uploads
    "sddefault.jpg",      # 640x480, present when SD encoded
    "hqdefault.jpg",      # 480x360, always present (final fallback)
)

# Some hosts respond differently to default urllib UA strings; spoof a
# benign browser UA so the CDN never serves a 403/empty payload. The
# value is intentionally generic — there's nothing identifying here.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _download_url(url: str, dest: Path, *, timeout: float = 15.0) -> tuple[bool, str]:
    """GET ``url`` and write the body to ``dest`` atomically. Returns (ok, msg)."""

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            payload = response.read()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return False, f"network: {exc.reason}"
    except TimeoutError:
        return False, "network: timed out"

    # YouTube returns a tiny 120x90 placeholder JPG (~1.5 KB) when the
    # variant doesn't exist for that video, instead of a 404. Treat
    # suspiciously small payloads as "not available" so we fall through
    # to the next ladder rung.
    if len(payload) < 5 * 1024:
        return False, f"placeholder payload ({len(payload)} bytes)"

    # Atomic write: ``.tmp`` then rename, so a partial download never
    # leaves a half-written jpg the catalogue would happily serve.
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_bytes(payload)
        tmp.replace(dest)
    except OSError as exc:
        return False, f"write failed: {exc}"
    return True, f"{len(payload) // 1024} KB"


def _fetch_thumbnail(video: Path, video_id: str, *, dry_run: bool) -> tuple[bool, str]:
    """Pull the YouTube thumbnail for ``video_id`` to ``<video stem>.jpg``."""

    dest = video.with_suffix(".jpg")

    if dry_run:
        urls = [
            f"https://i.ytimg.com/vi/{video_id}/{name}"
            for name in _THUMBNAIL_URL_LADDER
        ]
        return True, f"would try {urls[0]} → {dest.name}"

    last_error = "no variants tried"
    for name in _THUMBNAIL_URL_LADDER:
        url = f"https://i.ytimg.com/vi/{video_id}/{name}"
        ok, message = _download_url(url, dest)
        if ok:
            return True, f"{name} ({message})"
        last_error = f"{name}: {message}"
    return False, last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the yt-dlp commands that would run without invoking them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch thumbnails even for videos that already have one.",
    )
    args = parser.parse_args(argv)

    if not VIDEO_DIR.is_dir():
        print(f"Video directory missing: {VIDEO_DIR}", file=sys.stderr)
        return 1

    videos: list[Path] = sorted(
        p for p in VIDEO_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        and not catalogue._is_ytdlp_intermediate(p.name)  # noqa: SLF001
    )

    if not videos:
        print("No videos to backfill.")
        return 0

    fetched = 0
    skipped_have_thumb = 0
    skipped_no_id = 0
    failed = 0

    for video in videos:
        if not args.force and _has_thumbnail(video):
            skipped_have_thumb += 1
            continue

        video_id = _extract_youtube_id(video.stem)
        if video_id is None:
            print(f"SKIP  {video.name} (no [id] token in filename)", file=sys.stderr)
            skipped_no_id += 1
            continue

        ok, message = _fetch_thumbnail(video, video_id, dry_run=args.dry_run)
        if ok:
            fetched += 1
            print(f"OK    {video.name}  ({message})")
        else:
            failed += 1
            print(f"FAIL  {video.name}  ({message})", file=sys.stderr)

    print(
        f"\nFetched: {fetched}, already had: {skipped_have_thumb}, "
        f"no id: {skipped_no_id}, failed: {failed}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
