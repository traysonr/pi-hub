"""HDMI-CEC control via the v4l-utils `cec-ctl` binary.

Exposes two operations: `wake()` to power on the TV and switch it to the Pi's
HDMI input (the same behavior you get when you turn on a Nintendo Switch),
and `standby()` to send the TV back to sleep.

This module is intentionally tolerant: it logs failures and never raises out
to callers, so a missing/unplugged TV can never crash the web app.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading

log = logging.getLogger(__name__)

# Friendly name shown on the TV's input list / device menu.
_OSD_NAME = os.environ.get("PI_HUB_CEC_OSD_NAME", "Pi Hub")

# CEC logical address of the TV is always 0 in the spec.
_TV_LOGICAL_ADDR = "0"

# All cec-ctl invocations get a short timeout; the bus is local and any
# command should complete in well under a second when the TV is reachable.
_CEC_TIMEOUT = 5.0

_lock = threading.Lock()
_phys_addr: str | None = None
_claimed = False


def _cec_path() -> str | None:
    return shutil.which("cec-ctl")


def _run(args: list[str], *, timeout: float = _CEC_TIMEOUT) -> subprocess.CompletedProcess[str]:
    binary = _cec_path()
    if binary is None:
        raise FileNotFoundError("cec-ctl is not installed (apt install v4l-utils)")
    return subprocess.run(
        [binary, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


_PHYS_ADDR_RE = re.compile(r"Physical Address\s*:\s*([0-9a-fA-F.]+)")


def _claim_locked() -> tuple[bool, str]:
    """Register as a Playback device and cache our physical address.

    Returns (ok, message). Safe to call repeatedly; only does real work the
    first time it succeeds.
    """

    global _claimed, _phys_addr
    if _claimed and _phys_addr:
        return True, "already claimed"

    try:
        result = _run(["--playback", "--osd-name", _OSD_NAME])
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "cec-ctl claim timed out"
    except OSError as exc:
        return False, f"cec-ctl failed to start: {exc}"

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        return False, "; ".join(tail) or "cec-ctl claim failed"

    match = _PHYS_ADDR_RE.search(result.stdout or "")
    if match:
        _phys_addr = match.group(1)
    _claimed = True
    log.info("CEC claimed (osd=%s, phys_addr=%s)", _OSD_NAME, _phys_addr)
    return True, "claimed"


def _ensure_claimed() -> tuple[bool, str]:
    with _lock:
        return _claim_locked()


def wake() -> tuple[bool, str]:
    """Power the TV on and switch it to our HDMI input.

    Sends Image-View-On (powers a sleeping TV) followed by Active-Source
    (tells the TV to switch to our input). Returns (ok, message).
    """

    ok, msg = _ensure_claimed()
    if not ok:
        log.warning("CEC wake skipped: %s", msg)
        return False, msg

    try:
        view = _run(["--to", _TV_LOGICAL_ADDR, "--image-view-on"])
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        log.warning("CEC image-view-on timed out (TV may still wake)")
        view = None
    except OSError as exc:
        return False, f"cec-ctl failed: {exc}"

    if view is not None and view.returncode != 0:
        # Some TVs don't ACK Image-View-On when fully off, but still wake.
        # Log and continue to Active-Source.
        log.info(
            "image-view-on returned rc=%s (often expected when TV is off)",
            view.returncode,
        )

    active_args = ["--playback", "--active-source"]
    if _phys_addr:
        active_args[-1] = f"--active-source=phys-addr={_phys_addr}"
    try:
        active = _run(active_args)
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        log.warning("CEC active-source timed out")
        return True, "wake sent (active-source timed out)"
    except OSError as exc:
        return False, f"cec-ctl failed: {exc}"

    if active.returncode != 0:
        tail = (active.stderr or active.stdout or "").strip().splitlines()[-3:]
        msg = "; ".join(tail) or f"active-source rc={active.returncode}"
        log.warning("CEC active-source failed: %s", msg)
        # Still report success — the TV likely woke from Image-View-On.
        return True, f"wake sent (active-source warning: {msg})"

    log.info("CEC wake sent (phys_addr=%s)", _phys_addr)
    return True, "wake sent"


def standby() -> tuple[bool, str]:
    """Send the TV to standby."""

    ok, msg = _ensure_claimed()
    if not ok:
        log.warning("CEC standby skipped: %s", msg)
        return False, msg

    try:
        result = _run(["--to", _TV_LOGICAL_ADDR, "--standby"])
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        log.warning("CEC standby timed out (TV may still sleep)")
        return True, "standby sent (timed out)"
    except OSError as exc:
        return False, f"cec-ctl failed: {exc}"

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        message = "; ".join(tail) or f"standby rc={result.returncode}"
        log.warning("CEC standby failed: %s", message)
        return False, message

    log.info("CEC standby sent")
    return True, "standby sent"


def wake_async() -> None:
    """Fire-and-forget wake for use from request handlers (e.g. before mpv).

    Spawns a daemon thread so the HTTP request returns immediately and the
    user never waits on CEC bus latency.
    """

    def _worker() -> None:
        try:
            wake()
        except Exception:  # noqa: BLE001 — never crash the worker
            log.exception("CEC wake_async worker failed")

    threading.Thread(
        target=_worker, name="cec-wake", daemon=True
    ).start()
