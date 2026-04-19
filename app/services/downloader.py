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

from app.config import PROJECT_ROOT, VIDEO_DIR

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
) -> str:
    """Map yt-dlp stderr to a user-facing string (last few lines only)."""

    stderr_tail = (stderr or "").strip().splitlines()[-5:]
    joined = "; ".join(stderr_tail).lower()
    if "requested format is not available" in joined:
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


@dataclass
class DownloadJob:
    id: str
    url: str
    status: JobStatus = "queued"
    message: str = ""
    filename: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
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
    return shutil.which("yt-dlp")


def _run_download(job: DownloadJob) -> None:
    binary = _yt_dlp_path()
    if binary is None:
        log.error("yt-dlp binary not found on PATH")
        _set_status(job, "error", "yt-dlp is not installed on the server")
        return

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
        "-o", output_template,
    ]

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

    cmd.append(job.url)

    _set_status(job, "downloading", "Starting download")
    log.info("Download starting: id=%s url=%s", job.id, job.url)

    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=60 * 60,
        )
    except subprocess.TimeoutExpired:
        log.exception("Download timed out: id=%s", job.id)
        _set_status(job, "error", "Download timed out")
        return
    except OSError as exc:
        log.exception("Download failed to start: id=%s", job.id)
        _set_status(job, "error", f"Failed to start yt-dlp: {exc}")
        return

    if completed.returncode != 0:
        message = _yt_dlp_failure_user_message(
            completed.stderr or "",
            cookies_path=_COOKIES_PATH,
            cookies_present=_cookies_file() is not None,
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
    _set_status(job, "success", "Download complete", filename=filename)


def start_download(url: str) -> DownloadJob:
    """Create a job and start the download in a background thread."""

    job = DownloadJob(id=uuid.uuid4().hex, url=url)
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
