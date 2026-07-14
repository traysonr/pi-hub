"""Self-updating for yt-dlp so downloads don't silently rot.

YouTube periodically rotates its player JS / streaming path. When the
installed ``yt-dlp`` falls behind those changes it starts returning
HTTP 403 or "no formats found", and the CLI also nags that the build is
"older than 90 days". The only durable fix is to keep yt-dlp current, so
this module upgrades it on a schedule (wired into ``app.services.scheduler``
from ``app.main``) and, as a safety net, on startup if the installed build
is already stale.

Why this is safe to do live: ``app.services.downloader`` shells out to
``.venv/bin/yt-dlp`` fresh on every job, so an in-place pip upgrade takes
effect on the *next* download with no service restart. The systemd unit's
``ReadWritePaths=/home/gilberto/pi-hub`` covers the venv, so the upgrade
can write even under ``ProtectSystem=full``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import date, datetime
from pathlib import Path

from app.config import PROJECT_ROOT

log = logging.getLogger(__name__)

# Packages we keep current. ``yt-dlp[default]`` pulls the recommended
# runtime extras (brotli, websockets, pycryptodomex, ...); ``yt-dlp-ejs``
# is the JS-challenge (nsig) solver bundle the downloader relies on.
_PACKAGES = ("yt-dlp[default]", "yt-dlp-ejs")

# If the installed build's date is older than this on startup, kick a
# one-off upgrade shortly after boot so a Pi that was powered off for a
# while self-heals without waiting for the next weekly tick.
_STARTUP_STALE_DAYS = 30

# pip + a fresh index resolution over the Pi's connection can be slow;
# give it room but don't hang the scheduler thread forever.
_UPDATE_TIMEOUT_S = 15 * 60


def _venv_bin(name: str) -> Path:
    return PROJECT_ROOT / ".venv" / "bin" / name


def _env_with_local_bin() -> dict[str, str]:
    """Copy the environment, ensuring ``~/.local/bin`` is on PATH.

    Mirrors the downloader so any post-install hooks behave the same as a
    real download would (harmless for pip, cheap to keep consistent).
    """

    env = os.environ.copy()
    home_local = Path.home() / ".local" / "bin"
    if home_local.is_dir():
        env["PATH"] = os.pathsep.join([str(home_local), env.get("PATH", "")])
    return env


def installed_version() -> str | None:
    """Return the venv yt-dlp version string (e.g. ``2026.7.4``) or None."""

    binary = _venv_bin("yt-dlp")
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        return None
    try:
        out = subprocess.run(
            [str(binary), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("yt-dlp version probe failed: %s", exc)
        return None
    version = (out.stdout or "").strip().splitlines()
    return version[0].strip() if version else None


def _version_to_date(version: str | None) -> date | None:
    """Parse yt-dlp's date-based version (``YYYY.M.D[.postN]``) to a date.

    Returns None if the string doesn't look like a dated release, so the
    caller can degrade gracefully (treat as "unknown age").
    """

    if not version:
        return None
    parts = version.split(".")
    if len(parts) < 3:
        return None
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


def installed_age_days() -> int | None:
    """Age (in days) of the installed build, or None if undeterminable."""

    build_date = _version_to_date(installed_version())
    if build_date is None:
        return None
    return (datetime.now().date() - build_date).days


def update() -> str:
    """Upgrade yt-dlp (+ extras) in the venv. Returns a short status string.

    Raises ``RuntimeError`` on failure so the scheduler records it in the
    job's ``last_error`` (and never crashes the loop).
    """

    pip = _venv_bin("pip")
    if not pip.is_file():
        raise RuntimeError(f"pip not found in venv at {pip}")

    before = installed_version()
    cmd = [str(pip), "install", "-U", *_PACKAGES]
    log.info("yt-dlp auto-update: running %s", " ".join(cmd))
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=_UPDATE_TIMEOUT_S,
            env=_env_with_local_bin(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"yt-dlp upgrade timed out after {_UPDATE_TIMEOUT_S}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"yt-dlp upgrade failed to start: {exc}") from exc

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(
            "pip upgrade exited "
            f"{completed.returncode}: {'; '.join(tail) or 'unknown error'}"
        )

    after = installed_version()
    if before and after and before != after:
        result = f"updated yt-dlp {before} -> {after}"
    elif after:
        result = f"yt-dlp already current ({after})"
    else:
        result = "yt-dlp upgrade ran (version unknown)"
    log.info("yt-dlp auto-update: %s", result)
    return result


def maybe_update_on_startup() -> None:
    """Fire a one-off upgrade if the installed build is stale.

    Runs synchronously on the caller; ``app.main`` invokes this on a
    daemon thread so boot is never blocked on the network. Exceptions are
    swallowed (logged) — a failed self-update must never stop the app from
    serving.
    """

    age = installed_age_days()
    if age is None:
        log.info(
            "yt-dlp auto-update: installed version/age unknown; skipping "
            "startup check (weekly job still applies)."
        )
        return
    if age < _STARTUP_STALE_DAYS:
        log.info(
            "yt-dlp auto-update: installed build is %d day(s) old; "
            "within %d-day freshness window, skipping startup update.",
            age, _STARTUP_STALE_DAYS,
        )
        return
    log.info(
        "yt-dlp auto-update: installed build is %d day(s) old (>= %d); "
        "running startup update.",
        age, _STARTUP_STALE_DAYS,
    )
    try:
        update()
    except Exception:  # noqa: BLE001
        log.exception("yt-dlp auto-update: startup update failed")
