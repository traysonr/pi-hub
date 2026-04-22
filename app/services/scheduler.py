"""Tiny purpose-built job scheduler for recurring Pi Hub tasks.

This intentionally does not pull in APScheduler. We only need a handful
of features and the dependency would add a moving part (job stores,
executors, persistence) we don't actually use:

- register jobs to run ``daily("HH:MM")`` or ``weekly("DAY HH:MM")`` in
  the Pi's local time zone,
- a single background thread that sleeps until the next-due job, fires
  it, logs the result, and re-computes,
- catch-up on a missed tick: if the app was restarted at 4:58 AM and
  comes up at 5:02 AM, a 5:00 AM job still fires once (and only once)
  on startup,
- a status snapshot (``get_status``) so the UI / API can show last/next
  runs and any last error, per job.

Jobs run sequentially on the scheduler thread. That's fine here: our
workload is "download ~40 images" or "send one email", not anything
CPU-bound or overlapping.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


# --- Schedule specs ----------------------------------------------------


class Schedule(Protocol):
    """Pluggable "when does this job next run after ``after``?" contract.

    Implementations must be pure: given the same ``after`` datetime, the
    same ``Schedule`` must always return the same next-fire datetime.
    That's what lets us compute missed-tick catch-up deterministically.
    """

    def describe(self) -> str: ...

    def next_after(self, after: datetime) -> datetime: ...


@dataclass(frozen=True)
class Daily:
    """Fire once a day at ``hour:minute`` in the local time zone."""

    hour: int
    minute: int = 0

    def describe(self) -> str:
        return f"daily {self.hour:02d}:{self.minute:02d}"

    def next_after(self, after: datetime) -> datetime:
        candidate = after.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0
        )
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate


@dataclass(frozen=True)
class Weekly:
    """Fire once a week on ``weekday`` (0=Mon) at ``hour:minute``."""

    weekday: int  # 0=Mon .. 6=Sun, matching datetime.weekday()
    hour: int
    minute: int = 0

    def describe(self) -> str:
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"weekly {names[self.weekday]} {self.hour:02d}:{self.minute:02d}"

    def next_after(self, after: datetime) -> datetime:
        candidate = after.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0
        )
        days_ahead = (self.weekday - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate


def daily(at: str) -> Daily:
    """Convenience: ``daily("05:00")`` -> ``Daily(5, 0)``."""
    hour, minute = _parse_hhmm(at)
    return Daily(hour=hour, minute=minute)


def weekly(day: str, at: str) -> Weekly:
    """Convenience: ``weekly("Mon", "09:30")``."""
    hour, minute = _parse_hhmm(at)
    names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    idx = names.get(day[:3].lower())
    if idx is None:
        raise ValueError(f"Unknown weekday: {day!r}")
    return Weekly(weekday=idx, hour=hour, minute=minute)


def _parse_hhmm(text: str) -> tuple[int, int]:
    try:
        hh, mm = text.split(":", 1)
        hour = int(hh)
        minute = int(mm)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid HH:MM string: {text!r}") from exc
    if not (0 <= hour < 24) or not (0 <= minute < 60):
        raise ValueError(f"Out-of-range time: {text!r}")
    return hour, minute


# --- Job state ---------------------------------------------------------


@dataclass
class Job:
    name: str
    schedule: Schedule
    func: Callable[[], Any]
    # The last time we decided this job should have fired. Used for
    # catch-up on startup: if we come up and ``last_due`` is older than
    # the most recent scheduled tick, fire once immediately.
    last_due: datetime | None = None
    last_run_at: datetime | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    last_result: str | None = None
    run_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schedule": self.schedule.describe(),
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "run_count": self.run_count,
        }


# --- Scheduler ---------------------------------------------------------


# Clock indirection so tests don't have to wait real wall time.
_now_fn: Callable[[], datetime] = lambda: datetime.now()  # noqa: E731


def set_clock(fn: Callable[[], datetime]) -> None:
    """Test hook: override the "what time is it" function globally."""
    global _now_fn
    _now_fn = fn


def _now() -> datetime:
    return _now_fn()


_lock = threading.Lock()
_cond = threading.Condition(_lock)
_jobs: dict[str, Job] = {}
_thread: threading.Thread | None = None
_stop = False


def register(name: str, schedule: Schedule, func: Callable[[], Any]) -> None:
    """Add or replace a job. Safe to call before or after ``start``.

    If a job with the same name already exists, we keep its run history
    (last_run_at etc.) so re-registering on a hot-reload doesn't wipe
    observability.
    """

    with _cond:
        existing = _jobs.get(name)
        if existing is None:
            _jobs[name] = Job(name=name, schedule=schedule, func=func)
            log.info("Scheduler: registered %s (%s)", name, schedule.describe())
        else:
            existing.schedule = schedule
            existing.func = func
            log.info(
                "Scheduler: re-registered %s (%s)", name, schedule.describe()
            )
        _cond.notify_all()


def start() -> None:
    """Start the background scheduler thread. Idempotent."""
    global _thread, _stop

    with _cond:
        if _thread is not None and _thread.is_alive():
            return
        _stop = False

    _thread = threading.Thread(
        target=_run_forever, name="pi-hub-scheduler", daemon=True
    )
    _thread.start()


def stop(timeout: float = 2.0) -> None:
    """Signal the scheduler to exit and wait up to ``timeout``."""
    global _stop
    with _cond:
        _stop = True
        _cond.notify_all()
    thread = _thread
    if thread is not None:
        thread.join(timeout=timeout)


def get_status() -> dict[str, Any]:
    """Snapshot for the API: all registered jobs, their schedules, and
    when they're next due (computed on the fly from "now")."""
    now = _now()
    with _cond:
        jobs = []
        for job in _jobs.values():
            entry = job.to_dict()
            entry["next_run_at"] = job.schedule.next_after(now).isoformat()
            jobs.append(entry)
    return {"jobs": jobs, "now": now.isoformat()}


def run_now(name: str) -> dict[str, Any]:
    """Fire a job immediately on the caller's thread. Used by tests and
    by the "Rotate now" UI button. Raises ``KeyError`` if the job name
    isn't registered."""

    with _cond:
        job = _jobs.get(name)
        if job is None:
            raise KeyError(f"No such job: {name}")
    _execute(job, due=_now())
    return job.to_dict()


# --- Internals ---------------------------------------------------------


def _run_forever() -> None:
    """Main scheduler loop. Wakes up at whichever job is next due,
    fires it, handles catch-up, then goes back to sleep."""

    # Startup catch-up: for each job, compute the most recent scheduled
    # fire time in the past. If we've never run it (or last_due is
    # older), fire once. This covers "app was down at 5 AM; it came up
    # at 5:05 AM" without firing a second time for the same day.
    _catch_up_startup()

    while True:
        with _cond:
            if _stop:
                return
            job, due = _next_due_locked()
            if job is None:
                # No jobs registered yet; wait until register() pokes us.
                _cond.wait(timeout=60.0)
                continue
            wait_s = max(0.0, (due - _now()).total_seconds())
            if wait_s > 0:
                # Cap each wait so clock changes / new registrations
                # get picked up reasonably quickly without busy-looping.
                _cond.wait(timeout=min(wait_s, 60.0))
                if _stop:
                    return
                # Re-check: a new job may have jumped to the head, or
                # the wait returned early and we're not actually due yet.
                if _now() < due:
                    continue

        _execute(job, due=due)


def _catch_up_startup() -> None:
    """On first entry to the scheduler loop, fire any job whose most
    recent scheduled tick is in the past and which hasn't been run
    yet today. Intentional semantics:

    - We fire at most one catch-up per job, regardless of how long the
      app was down. (If the app was down for 3 days, we don't fire 3
      daily rotations back-to-back -- the user wants fresh images
      *today*, not a backlog of rotations.)
    """

    now = _now()
    with _cond:
        jobs = list(_jobs.values())

    for job in jobs:
        # Most recent scheduled tick in the past = next_after(now -
        # <period>), which we approximate by walking back a day/week and
        # asking when the next fire after that point is. For Daily, the
        # "next after 24h ago" is today's tick if it's already past, or
        # yesterday's tick if not. Either way it's the right "most
        # recent" boundary.
        reference = now - timedelta(days=7)
        last_scheduled = job.schedule.next_after(reference)
        while True:
            candidate = job.schedule.next_after(last_scheduled)
            if candidate > now:
                break
            last_scheduled = candidate
        if last_scheduled > now:
            # No past tick yet (job registered before its very first
            # scheduled time). Nothing to catch up on.
            continue
        if job.last_run_at is not None and job.last_run_at >= last_scheduled:
            # Already ran for this tick (previous process).
            continue
        log.info(
            "Scheduler: catching up on missed tick for %s (due %s)",
            job.name, last_scheduled.isoformat(),
        )
        _execute(job, due=last_scheduled)


def _next_due_locked() -> tuple[Job | None, datetime]:
    """Return the job with the soonest next-fire time. Caller holds
    ``_cond``."""

    now = _now()
    best_job: Job | None = None
    best_due: datetime = now + timedelta(days=365)
    for job in _jobs.values():
        due = job.schedule.next_after(now)
        if due < best_due:
            best_due = due
            best_job = job
    return best_job, best_due


def _execute(job: Job, *, due: datetime) -> None:
    """Run ``job`` synchronously, recording result + timing. Exceptions
    are logged and stashed in ``job.last_error`` -- they never kill the
    scheduler loop."""

    started_wall = _now()
    started_perf = time.perf_counter()
    log.info("Scheduler: running %s (due %s)", job.name, due.isoformat())
    try:
        result = job.func()
    except Exception as exc:  # noqa: BLE001
        log.exception("Scheduler: job %s raised", job.name)
        with _cond:
            job.last_error = str(exc) or exc.__class__.__name__
            job.last_run_at = started_wall
            job.last_duration_ms = int((time.perf_counter() - started_perf) * 1000)
            job.last_due = due
            job.run_count += 1
        return

    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    with _cond:
        job.last_error = None
        job.last_result = str(result)[:500] if result is not None else "ok"
        job.last_run_at = started_wall
        job.last_duration_ms = duration_ms
        job.last_due = due
        job.run_count += 1
    log.info(
        "Scheduler: %s done in %dms (%s)", job.name, duration_ms, job.last_result
    )


# --- Test helpers ------------------------------------------------------


def _reset_for_tests() -> None:
    """Wipe all registered jobs and clock override. Used by pytest's
    fixture teardown -- not part of the public API."""
    global _jobs, _now_fn, _stop
    stop(timeout=0.5)
    with _cond:
        _jobs = {}
        _stop = False
    _now_fn = lambda: datetime.now()  # noqa: E731
