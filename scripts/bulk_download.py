#!/usr/bin/env python3
"""Bulk YouTube downloader for Pi Hub.

Reads a list of URLs (one per line) and downloads each into the
appropriate library:

    - ``video`` mode  -> media/videos/   (H.264 <=720p, mp4)
    - ``audio`` mode  -> media/music/    (best audio, no re-encode)

IMPORTANT — KEEP IN SYNC WITH THE WEB UI:
This script MUST derive its download logic from the single, user-facing
downloader (the "Add" tab in the web UI). Both code paths execute through
``app.services.downloader._run_download`` — do NOT shell out to ``yt-dlp``
directly here, do NOT build a parallel argv, and do NOT re-implement
cookies / player_client / format selection. If you need to change download
behaviour (a new yt-dlp flag, a different player_client, a new error
message), change ``app/services/downloader.py`` and both this script AND
the "Add" tab pick it up automatically. That is the whole point of the
shared service: there is one place where downloads are configured.

Usage:

    # from the project root, with the venv activated:
    .venv/bin/python scripts/bulk_download.py video bulk.txt
    .venv/bin/python scripts/bulk_download.py audio bulk.txt

The input file may contain blank lines and ``#`` comments; both are
skipped. Lines that start with whitespace are stripped. URLs are not
deduplicated, so the same URL listed twice will be downloaded twice.

On failure, the script prints Pi Hub's short summary plus yt-dlp's
stderr (truncated) and environment hints (cookies file, Deno, yt-dlp
version) so you can see *why* something broke without digging through logs.

Exit code is 0 if every URL succeeded, 1 otherwise (a per-URL summary
is printed at the end so a partial failure is easy to diagnose).
"""

# Usage example (copy/paste):
#   # 1) Create a URL list (blank lines and # comments are ignored)
#   cat > bulk.txt <<'EOF'
#   https://www.youtube.com/watch?v=dQw4w9WgXcQ
#   # https://www.youtube.com/watch?v=... (more URLs)
#   EOF
#
#   # 2) Run from the project root (venv assumed at .venv/)
#   .venv/bin/python scripts/bulk_download.py video bulk.txt
#   .venv/bin/python scripts/bulk_download.py audio bulk.txt
#
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# Make the script runnable directly from the project root or from
# anywhere via an absolute path: prepend the project root to sys.path
# so ``import app...`` works without installing the package.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.config import PROJECT_ROOT  # noqa: E402
from app.services import downloader  # noqa: E402


def _parse_urls(path: Path) -> list[str]:
    """Read URLs from ``path``. Blank lines and ``#`` comments are skipped."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Could not read URL list {path}: {exc}") from exc

    urls: list[str] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Permissive but sane: anything that doesn't look remotely like a
        # URL is most likely a typo and worth flagging early.
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            print(
                f"warning: line {lineno} doesn't look like a URL, skipping: {stripped!r}",
                file=sys.stderr,
            )
            continue
        urls.append(stripped)
    return urls


def _format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _venv_yt_dlp() -> Path | None:
    cand = _PROJECT_ROOT / ".venv" / "bin" / "yt-dlp"
    return cand if cand.is_file() else None


def _print_environment_banner() -> None:
    """One-time context that explains common Pi Hub / YouTube failures."""

    cookies_path = Path(
        os.environ.get(
            "PI_HUB_YT_COOKIES",
            str(PROJECT_ROOT / "secrets" / "youtube-cookies.txt"),
        )
    )
    print("--- environment ---")
    venv_yd = _venv_yt_dlp()
    if venv_yd is not None:
        try:
            ver = subprocess.run(
                [str(venv_yd), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            line = (ver.stdout or ver.stderr or "").strip().splitlines()
            print(f"yt-dlp (venv): {line[0] if line else 'unknown'}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"yt-dlp (venv): could not run ({exc})")
    else:
        which = shutil.which("yt-dlp")
        print(f"yt-dlp: {which or 'not found on PATH'}")

    if cookies_path.is_file():
        print(f"cookies: {cookies_path} (present)")
    else:
        print(f"cookies: {cookies_path} (missing — bot/age-gated videos may fail)")

    deno = shutil.which("deno")
    if deno:
        try:
            dv = subprocess.run(
                [deno, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            dv_line = (dv.stdout or "").strip().splitlines()
            print(f"deno: {dv_line[0] if dv_line else deno} ({deno})")
        except (OSError, subprocess.TimeoutExpired):
            print(f"deno: {deno}")
    else:
        home_local = Path.home() / ".local" / "bin" / "deno"
        if home_local.is_file():
            print(f"deno: not on PATH — try: export PATH=\"$HOME/.local/bin:$PATH\"")
        else:
            print(
                "deno: not found — YouTube may need a JS runtime for challenge "
                "solving (see https://github.com/yt-dlp/yt-dlp/wiki/EJS )"
            )

    node = shutil.which("node")
    if node:
        try:
            nv = subprocess.run(
                [node, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            print(f"node: {(nv.stdout or '').strip() or node}")
        except (OSError, subprocess.TimeoutExpired):
            print(f"node: {node}")
    print("---")


def _failure_hints(stderr_lower: str) -> list[str]:
    hints: list[str] = []
    if (
        "javascript runtime" in stderr_lower
        or "ejs" in stderr_lower
        or "signature solving" in stderr_lower
        or "nsig extraction failed" in stderr_lower
        or "n challenge solving failed" in stderr_lower
        or "forcing sabr streaming" in stderr_lower
        or "only images are available" in stderr_lower
    ):
        hints.append(
            "YouTube extraction: put Deno on PATH (export PATH=\"$HOME/.local/bin:$PATH\" "
            "if you installed via deno install script), then "
            "`pip install -U 'yt-dlp[default]'` and `.venv/bin/yt-dlp -U`."
        )
    if "not a bot" in stderr_lower or "sign in to confirm" in stderr_lower:
        hints.append(
            "Authentication: export a fresh Netscape cookies.txt for "
            "youtube.com into secrets/youtube-cookies.txt (or set PI_HUB_YT_COOKIES)."
        )
    if (
        "requested format is not available" in stderr_lower
        and "nsig extraction failed" not in stderr_lower
        and "only images are available" not in stderr_lower
    ):
        hints.append(
            "Format: Pi Hub video mode requires H.264 at 720p or below; try "
            "another URL or use audio mode if only audio exists."
        )
    return hints


def _print_job_failure(job: downloader.DownloadJob, *, verbose: bool) -> None:
    print(f"  summary: {job.message or 'unknown error'}", file=sys.stderr)
    if job.yt_dlp_returncode is not None:
        print(f"  yt-dlp exit code: {job.yt_dlp_returncode}", file=sys.stderr)

    stderr_combined = job.yt_dlp_stderr or ""
    if verbose and job.yt_dlp_stdout:
        print("  --- yt-dlp stdout (tail) ---", file=sys.stderr)
        for line in job.yt_dlp_stdout.strip().splitlines():
            print(f"    {line}", file=sys.stderr)

    if stderr_combined.strip():
        print("  --- yt-dlp stderr (tail) ---", file=sys.stderr)
        for line in stderr_combined.strip().splitlines():
            print(f"    {line}", file=sys.stderr)

    hints = _failure_hints(stderr_combined.lower())
    for h in hints:
        print(f"  hint: {h}", file=sys.stderr)


def _download_one(url: str, *, audio_only: bool) -> downloader.DownloadJob:
    """Run a single download synchronously and return the resulting job.

    Re-uses ``downloader._run_download`` (the same code path the web UI
    uses inside its background thread) instead of ``start_download`` so
    we can wait inline and show clean per-URL progress.
    """

    job = downloader.DownloadJob(
        id=uuid.uuid4().hex,
        url=url,
        audio_only=audio_only,
    )
    downloader._run_download(job)  # noqa: SLF001 -- intentional internal reuse
    return job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-download a list of YouTube URLs into the Pi Hub library."
    )
    parser.add_argument(
        "kind",
        choices=("video", "audio"),
        help="Download videos (mp4 to media/videos/) or audio only (to media/music/).",
    )
    parser.add_argument(
        "url_file",
        type=Path,
        help="Path to a text file with one YouTube URL per line. "
             "Blank lines and lines starting with '#' are ignored.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        default=True,
        help="Keep going after a failed URL (default).",
    )
    parser.add_argument(
        "--stop-on-error",
        dest="continue_on_error",
        action="store_false",
        help="Abort the batch on the first failed URL.",
    )
    parser.add_argument(
        "--no-env-banner",
        action="store_true",
        help="Skip printing yt-dlp / cookies / deno diagnostics at startup.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="On failure, print only the short summary (no stderr tail or hints).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="On failure, also print any yt-dlp stdout tail (rarely needed).",
    )
    args = parser.parse_args(argv)

    audio_only = args.kind == "audio"
    verbose_fail = args.verbose and not args.quiet

    urls = _parse_urls(args.url_file)
    if not urls:
        print(f"No URLs found in {args.url_file}", file=sys.stderr)
        return 1

    print(f"Bulk download: {len(urls)} url(s), kind={args.kind}")
    print(f"Source: {args.url_file.resolve()}")
    if not args.no_env_banner:
        _print_environment_banner()

    results: list[tuple[str, downloader.DownloadJob]] = []
    batch_started = time.monotonic()

    for index, url in enumerate(urls, start=1):
        prefix = f"[{index}/{len(urls)}]"
        print(f"\n{prefix} {url}")
        started = time.monotonic()
        try:
            job = _download_one(url, audio_only=audio_only)
        except KeyboardInterrupt:
            print("\nInterrupted by user.", file=sys.stderr)
            return 130
        elapsed = time.monotonic() - started

        if job.status == "success":
            print(f"{prefix} OK   ({_format_seconds(elapsed)}) -> {job.filename}")
        else:
            print(
                f"{prefix} FAIL ({_format_seconds(elapsed)})",
                file=sys.stderr,
            )
            if args.quiet:
                print(f"  {job.message or 'unknown error'}", file=sys.stderr)
            else:
                _print_job_failure(job, verbose=verbose_fail)
            if not args.continue_on_error:
                results.append((url, job))
                break

        results.append((url, job))

    total_elapsed = time.monotonic() - batch_started
    successes = sum(1 for _, job in results if job.status == "success")
    failures = len(results) - successes
    skipped = len(urls) - len(results)

    print("\n--- summary ---")
    print(
        f"{successes} succeeded, {failures} failed, {skipped} skipped "
        f"in {_format_seconds(total_elapsed)}"
    )
    if failures:
        print("\nFailed URLs (short message):", file=sys.stderr)
        for url, job in results:
            if job.status != "success":
                print(f"  {url}", file=sys.stderr)
                print(f"    -> {job.message or 'unknown error'}", file=sys.stderr)

    return 0 if failures == 0 and skipped == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
