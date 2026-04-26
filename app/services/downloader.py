"""Background YouTube downloader using yt-dlp."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.config import MUSIC_DIR, PROJECT_ROOT, VIDEO_DIR
from app.services import metadata

log = logging.getLogger(__name__)

JobStatus = Literal["queued", "downloading", "success", "error"]


# The Pi 3 only hardware-decodes H.264, and software-decoding 1080p is
# unreliable on this hardware. We hard-lock downloads to H.264 <=720p.
_MAX_HEIGHT = 720

# YouTube increasingly requires authenticated cookies ("Sign in to confirm
# you're not a bot"). We point yt-dlp at a Netscape-format cookies.txt
# exported from a throwaway Google account. Path is overridable via env so
# ops can relocate it without code changes.
_COOKIES_PATH = Path(
    os.environ.get(
        "PI_HUB_YT_COOKIES",
        str(PROJECT_ROOT / "secrets" / "youtube-cookies.txt"),
    )
)

# YouTube periodically rotates its player JS in ways that break yt-dlp's
# default `web` client (formats are gated behind an n-signature challenge
# the released yt-dlp can't yet solve, so extraction returns "No video
# formats found"). The `tv` and `web_embedded` clients use a different
# player path that consistently exposes the H.264 DASH ladder we need on
# the Pi 3, so we pin those as our primary + fallback. Empirically tested
# 2026-04-19 against player `4b0d80ee`: tv -> full ladder, web_embedded
# -> full ladder, default web -> broken. Override via env if YouTube
# breaks these next; comma-separated list, tried in order by yt-dlp.
_YT_PLAYER_CLIENTS = os.environ.get(
    "PI_HUB_YT_PLAYER_CLIENTS",
    "tv,web_embedded",
).strip()


def _cookies_file() -> Path | None:
    """Return the cookies path if it exists and is readable, else None."""
    try:
        if _COOKIES_PATH.is_file():
            return _COOKIES_PATH
    except OSError:
        return None
    return None


def _yt_dlp_failure_user_message(
    stderr: str,
    *,
    cookies_path: Path,
    cookies_present: bool,
    audio_only: bool = False,
) -> str:
    """Map yt-dlp stderr to a user-facing string (last few lines only)."""

    full = (stderr or "").lower()
    stderr_tail = (stderr or "").strip().splitlines()[-5:]
    joined = "; ".join(stderr_tail).lower()

    # YouTube sometimes ends with "Requested format is not available" even when
    # the real problem is failed signature/n challenge solving (no formats listed
    # except storyboards). Scan the *full* stderr so we don't mis-report as
    # "no H.264" when extraction never succeeded.
    extraction_broken = (
        "nsig extraction failed" in full
        or "signature solving failed" in full
        or "n challenge solving failed" in full
        or "only images are available for download" in full
        or "forcing sabr streaming" in full
        or "javascript runtime" in full
        # New (2026-04) failure mode: nsig solver silently produces no
        # playable formats and yt-dlp aborts with this generic message.
        or "no video formats found" in full
    )
    if extraction_broken:
        return (
            "YouTube format extraction failed (player rotation / nsig / SABR). "
            "We pin player_client=tv,web_embedded to dodge most of these; "
            "if it persists, try `PI_HUB_YT_PLAYER_CLIENTS=tv_embedded,mweb` "
            "or update yt-dlp: `.venv/bin/pip install -U --pre yt-dlp[default] "
            "yt-dlp-ejs` and re-run. Make sure Deno is on PATH "
            "(export PATH=\"$HOME/.local/bin:$PATH\")."
        )

    if "requested format is not available" in joined:
        if audio_only:
            return (
                "No downloadable audio stream is available for this URL."
            )
        return (
            f"No H.264 video at {_MAX_HEIGHT}p or lower is available "
            "for this URL. The Pi 3 can only play H.264 smoothly, so "
            "this video cannot be downloaded."
        )
    if "confirm your age" in joined:
        return (
            "This YouTube video is age-restricted. In a normal browser, sign in "
            "to the same throwaway Google account you use for Pi Hub cookies, "
            "complete any age verification YouTube shows, then export a fresh "
            f"cookies.txt for youtube.com and replace {cookies_path}."
        )
    if (
        "confirm you're not a bot" in joined
        or "sign in to confirm you're not a bot" in joined
        or "not a bot" in joined
    ):
        if not cookies_present:
            return (
                "YouTube requires authentication for this video and no "
                "cookies file is configured. See README (YouTube "
                "authentication section) to set one up."
            )
        return (
            "YouTube rejected the cookies (likely expired). Re-export "
            "cookies.txt from your throwaway account and replace "
            f"{cookies_path}."
        )
    return "; ".join(stderr_tail) or "yt-dlp failed"


def _truncate_output(text: str, *, max_lines: int = 80, max_chars: int = 24000) -> str:
    """Keep the tail of yt-dlp output for debugging without unbounded memory."""

    text = text or ""
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        text = f"... ({omitted} earlier line(s) omitted) ...\n" + "\n".join(lines[-max_lines:])
    else:
        text = "\n".join(lines)
    if len(text) > max_chars:
        text = "... (truncated) ...\n" + text[-max_chars:]
    return text


@dataclass
class DownloadJob:
    id: str
    url: str
    audio_only: bool = False
    status: JobStatus = "queued"
    message: str = ""
    filename: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Populated on yt-dlp failure for CLI / bulk tooling (not exposed in ``to_dict``).
    yt_dlp_returncode: int | None = None
    yt_dlp_stderr: str | None = None
    yt_dlp_stdout: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "audio_only": self.audio_only,
            "kind": "audio" if self.audio_only else "video",
            "status": self.status,
            "message": self.message,
            "filename": self.filename,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_jobs: dict[str, DownloadJob] = {}
_jobs_lock = threading.Lock()


def _set_status(job: DownloadJob, status: JobStatus, message: str = "", filename: str | None = None) -> None:
    with _jobs_lock:
        job.status = status
        job.message = message
        if filename is not None:
            job.filename = filename
        job.updated_at = time.time()


def _yt_dlp_path() -> str | None:
    """Resolve the yt-dlp binary, preferring the project venv.

    Without this, ``shutil.which("yt-dlp")`` returns ``/usr/bin/yt-dlp``
    (the OS-packaged version, often a year+ stale) before the venv's
    binary, because ``.venv/bin`` is only on ``PATH`` when the venv has
    been activated. The systemd service and ``scripts/bulk_download.py``
    both invoke Python directly without sourcing the activate script,
    so they would silently fall back to the stale system yt-dlp and
    fail on YouTube's latest player JS rotation. We always look at the
    venv first so the version pinned in ``requirements.txt`` is what
    actually runs.
    """

    venv_binary = PROJECT_ROOT / ".venv" / "bin" / "yt-dlp"
    if venv_binary.is_file() and os.access(venv_binary, os.X_OK):
        return str(venv_binary)
    return shutil.which("yt-dlp")


def _build_video_cmd(binary: str) -> tuple[list[str], Path]:
    """Build the yt-dlp argv for a 720p H.264 video download."""

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)

    # Restrict filenames to safe ASCII and tag with the locked resolution
    # so future quality changes can coexist without filename collisions.
    output_template = str(
        VIDEO_DIR / f"%(title).200B [%(id)s] [{_MAX_HEIGHT}p].%(ext)s"
    )

    # H.264 only, capped at _MAX_HEIGHT. Three layered selectors all stay on
    # the avc1 path; if the source has no H.264 stream at this height,
    # yt-dlp will exit non-zero and we surface a clear error to the user
    # instead of silently downloading an unplayable VP9/AV1 file.
    fmt = (
        f"bv*[vcodec^=avc1][height<={_MAX_HEIGHT}]+ba[ext=m4a]"
        f"/bv*[vcodec^=avc1][height<={_MAX_HEIGHT}]+ba"
        f"/b[vcodec^=avc1][height<={_MAX_HEIGHT}]"
    )

    cmd = [
        binary,
        "--no-playlist",
        "--restrict-filenames",
        "--no-progress",
        "--newline",
        "--print", "after_move:filepath",
        "-f", fmt,
        "--merge-output-format", "mp4",
        # Save the YouTube thumbnail next to the video as ``<stem>.jpg``.
        # The Video tab renders this on each card; mapping is implicit
        # because both files share a stem (filesystem == catalog). If
        # thumbnail conversion ever fails (no ffmpeg), yt-dlp keeps the
        # source webp/png, which catalogue.py also recognises.
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "-o", output_template,
    ]
    return cmd, VIDEO_DIR


def _build_audio_cmd(binary: str) -> tuple[list[str], Path]:
    """Build the yt-dlp argv for a best-quality audio extraction.

    We deliberately DO NOT pass ``--audio-format <codec>``. Forcing a
    specific codec triggers a full ffmpeg re-encode which on a Pi 3
    takes ~15 minutes for a long track. Instead we let yt-dlp keep the
    source audio stream as-is (typically Opus in a WebM container, or
    AAC in M4A), which is just a fast remux. mpv plays both containers
    natively over the existing HDMI/ALSA pipeline.
    """

    MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    output_template = str(
        MUSIC_DIR / "%(title).200B [%(id)s] [audio].%(ext)s"
    )

    cmd = [
        binary,
        "--no-playlist",
        "--restrict-filenames",
        "--no-progress",
        "--newline",
        "--print", "after_move:filepath",
        # Prefer m4a when available (so the file plays everywhere with
        # zero re-encode), then fall back to any best-audio stream.
        "-f", "bestaudio[ext=m4a]/bestaudio/ba/b",
        "-x",
        "-o", output_template,
    ]
    return cmd, MUSIC_DIR


def _run_download(job: DownloadJob) -> None:
    binary = _yt_dlp_path()
    if binary is None:
        log.error("yt-dlp binary not found on PATH")
        _set_status(job, "error", "yt-dlp is not installed on the server")
        return

    if job.audio_only:
        cmd, _out_dir = _build_audio_cmd(binary)
    else:
        cmd, _out_dir = _build_video_cmd(binary)

    cookies = _cookies_file()
    if cookies is not None:
        cmd.extend(["--cookies", str(cookies)])
        log.info("Using YouTube cookies from %s", cookies)
    else:
        log.warning(
            "No YouTube cookies file at %s — downloads may be blocked by "
            "YouTube's bot detection. See README for setup.",
            _COOKIES_PATH,
        )

    # See _YT_PLAYER_CLIENTS comment: pin to clients whose player path is
    # currently working, so a YouTube-side player rotation doesn't silently
    # turn every download into "No video formats found".
    if _YT_PLAYER_CLIENTS:
        cmd.extend([
            "--extractor-args",
            f"youtube:player_client={_YT_PLAYER_CLIENTS}",
        ])
        log.info("Using YouTube player_client=%s", _YT_PLAYER_CLIENTS)

    cmd.append(job.url)

    _set_status(job, "downloading", "Starting download")
    log.info(
        "Download starting: id=%s kind=%s url=%s",
        job.id,
        "audio" if job.audio_only else "video",
        job.url,
    )

    # Make sure yt-dlp can find Deno. Deno is required for solving
    # YouTube's nsig JS challenges; without it on PATH, the venv yt-dlp
    # silently falls back to the bundled regex extractor which breaks on
    # every player rotation. The user's interactive shell already has
    # ~/.local/bin on PATH (via .profile), but the systemd unit running
    # the web UI may not, so we union it in unconditionally.
    env = os.environ.copy()
    extra_paths = []
    home_local = Path.home() / ".local" / "bin"
    if home_local.is_dir():
        extra_paths.append(str(home_local))
    if extra_paths:
        env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])

    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60 * 60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.exception("Download timed out: id=%s", job.id)
        job.yt_dlp_returncode = None
        job.yt_dlp_stderr = "Download timed out after 60 minutes (subprocess killed)."
        job.yt_dlp_stdout = None
        _set_status(job, "error", "Download timed out")
        return
    except OSError as exc:
        log.exception("Download failed to start: id=%s", job.id)
        job.yt_dlp_returncode = None
        job.yt_dlp_stderr = str(exc)
        job.yt_dlp_stdout = None
        _set_status(job, "error", f"Failed to start yt-dlp: {exc}")
        return

    if completed.returncode != 0:
        stderr_raw = completed.stderr or ""
        stdout_raw = completed.stdout or ""
        job.yt_dlp_returncode = completed.returncode
        job.yt_dlp_stderr = _truncate_output(stderr_raw)
        job.yt_dlp_stdout = _truncate_output(stdout_raw) if stdout_raw.strip() else None
        message = _yt_dlp_failure_user_message(
            stderr_raw,
            cookies_path=_COOKIES_PATH,
            cookies_present=_cookies_file() is not None,
            audio_only=job.audio_only,
        )
        log.warning(
            "Download failed: id=%s rc=%s msg=%s",
            job.id, completed.returncode, message,
        )
        _set_status(job, "error", message)
        return

    final_path = (completed.stdout or "").strip().splitlines()
    filename = None
    if final_path:
        filename = final_path[-1].rsplit("/", 1)[-1]

    log.info("Download complete: id=%s filename=%s", job.id, filename)

    # Register the new file in the metadata catalog so it's filterable
    # right away (category="" and play_count=0). Both the web "Add" tab
    # and scripts/bulk_download.py call _run_download, so this single
    # hook keeps both import paths in sync with the JSON catalog.
    if filename:
        try:
            metadata.register(filename, "audio" if job.audio_only else "video")
        except Exception:
            log.exception(
                "metadata: failed to register %s entry for %s",
                "audio" if job.audio_only else "video", filename,
            )

    _set_status(job, "success", "Download complete", filename=filename)


def start_download(url: str, *, audio_only: bool = False) -> DownloadJob:
    """Create a job and start the download in a background thread."""

    job = DownloadJob(id=uuid.uuid4().hex, url=url, audio_only=audio_only)
    with _jobs_lock:
        _jobs[job.id] = job

    thread = threading.Thread(
        target=_run_download,
        args=(job,),
        name=f"download-{job.id[:8]}",
        daemon=True,
    )
    thread.start()
    return job


def get_job(job_id: str) -> DownloadJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def list_jobs(limit: int = 20) -> list[DownloadJob]:
    with _jobs_lock:
        jobs = list(_jobs.values())
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return jobs[:limit]
