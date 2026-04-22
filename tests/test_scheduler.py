"""Tests for app.services.scheduler.

The scheduler is pure-Python with a mock clock, so we can exercise the
real ``_run_forever`` loop, catch-up logic, and next-due math without
sleeping actual wall-clock seconds. The single hard-to-fake bit is
``threading.Condition.wait(timeout=...)``; we keep those timeouts short
(the loop caps each sleep at 60s anyway) and use ``join``/polling to
confirm behavior deterministically.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from app.services import scheduler


@pytest.fixture(autouse=True)
def _reset_scheduler():
    # Each test gets a clean job registry and real clock.
    scheduler._reset_for_tests()
    yield
    scheduler._reset_for_tests()


# --- Schedule math -----------------------------------------------------


def test_daily_next_after_today_future() -> None:
    now = datetime(2026, 4, 19, 4, 0, 0)  # 04:00
    spec = scheduler.daily("05:00")
    assert spec.next_after(now) == datetime(2026, 4, 19, 5, 0, 0)


def test_daily_next_after_today_already_past() -> None:
    now = datetime(2026, 4, 19, 5, 0, 1)  # 05:00:01
    spec = scheduler.daily("05:00")
    # Already past today; next fire is tomorrow.
    assert spec.next_after(now) == datetime(2026, 4, 20, 5, 0, 0)


def test_daily_next_after_exactly_at_time_rolls_forward() -> None:
    now = datetime(2026, 4, 19, 5, 0, 0)
    spec = scheduler.daily("05:00")
    # Exact match must roll to tomorrow or we'd loop forever firing the
    # same tick on re-entry.
    assert spec.next_after(now) == datetime(2026, 4, 20, 5, 0, 0)


def test_weekly_next_after() -> None:
    # 2026-04-19 is a Sunday (weekday=6).
    now = datetime(2026, 4, 19, 10, 0, 0)
    spec = scheduler.weekly("Mon", "09:30")
    # Next Monday 09:30.
    assert spec.next_after(now) == datetime(2026, 4, 20, 9, 30, 0)


def test_daily_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        scheduler.daily("25:00")
    with pytest.raises(ValueError):
        scheduler.daily("not-a-time")


def test_weekly_rejects_bad_day() -> None:
    with pytest.raises(ValueError):
        scheduler.weekly("Funday", "09:00")


# --- Registration + status --------------------------------------------


def test_register_adds_job_and_preserves_history_on_reregister() -> None:
    scheduler.register("x", scheduler.daily("05:00"), lambda: "ok")
    scheduler.run_now("x")

    status_before = scheduler.get_status()
    job_before = next(j for j in status_before["jobs"] if j["name"] == "x")
    assert job_before["run_count"] == 1
    assert job_before["last_error"] is None

    scheduler.register("x", scheduler.daily("06:00"), lambda: "ok2")
    status_after = scheduler.get_status()
    job_after = next(j for j in status_after["jobs"] if j["name"] == "x")
    # Re-registering keeps run history so observability doesn't reset
    # just because we changed the schedule.
    assert job_after["run_count"] == 1
    assert "06:00" in job_after["schedule"]


def test_run_now_executes_synchronously_and_records_result() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "done"

    scheduler.register("job", scheduler.daily("05:00"), fn)
    scheduler.run_now("job")

    assert calls == [1]
    jobs = scheduler.get_status()["jobs"]
    job = next(j for j in jobs if j["name"] == "job")
    assert job["last_result"] == "done"
    assert job["run_count"] == 1


def test_run_now_captures_exception_without_propagating() -> None:
    def boom() -> None:
        raise RuntimeError("kaboom")

    scheduler.register("job", scheduler.daily("05:00"), boom)
    # run_now swallows the exception so the scheduler loop wouldn't die
    # either; surface it via last_error.
    scheduler.run_now("job")
    jobs = scheduler.get_status()["jobs"]
    job = next(j for j in jobs if j["name"] == "job")
    assert job["last_error"] == "kaboom"
    assert job["run_count"] == 1


def test_run_now_missing_job_raises() -> None:
    with pytest.raises(KeyError):
        scheduler.run_now("nope")


# --- Mock-clock loop behavior -----------------------------------------


def test_scheduler_thread_fires_job_when_due() -> None:
    """Start the loop with a clock already past the daily fire time:
    the catch-up pass on loop entry should fire the job exactly once."""

    calls: list[int] = []

    def fn() -> None:
        calls.append(1)

    # Pin "now" to 05:01 so the 05:00 daily tick is in the past.
    scheduler.set_clock(lambda: datetime(2026, 4, 19, 5, 1, 0))
    scheduler.register("rotate", scheduler.daily("05:00"), fn)
    scheduler.start()

    # Poll briefly for the catch-up fire. The thread's only blocking
    # op is cond.wait(timeout=...), which returns quickly on notify.
    deadline = time.time() + 2.0
    while time.time() < deadline and not calls:
        time.sleep(0.02)
    scheduler.stop()

    assert calls == [1], "catch-up should fire exactly once"


def test_catch_up_does_not_double_fire_for_same_tick() -> None:
    """If last_run_at is already >= the most recent scheduled tick,
    startup catch-up must not fire again."""

    calls: list[int] = []

    def fn() -> None:
        calls.append(1)

    scheduler.set_clock(lambda: datetime(2026, 4, 19, 5, 1, 0))
    scheduler.register("rotate", scheduler.daily("05:00"), fn)

    # Simulate "already ran at 05:00:30 in a previous process".
    job = scheduler._jobs["rotate"]
    job.last_run_at = datetime(2026, 4, 19, 5, 0, 30)

    scheduler.start()
    time.sleep(0.25)  # let the loop hit its catch-up pass + settle
    scheduler.stop()

    assert calls == []


def test_catch_up_never_fires_more_than_once_per_job() -> None:
    """If the app was down for multiple days, we do not back-fire one
    rotation per missed day -- the user wants fresh images today, not
    a backlog."""

    calls: list[int] = []

    def fn() -> None:
        calls.append(1)

    # Clock pinned 3 days past a never-run job.
    scheduler.set_clock(lambda: datetime(2026, 4, 22, 5, 1, 0))
    scheduler.register("rotate", scheduler.daily("05:00"), fn)
    scheduler.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and len(calls) < 2:
        time.sleep(0.02)
    scheduler.stop()

    assert calls == [1], "catch-up must collapse backlogged ticks to one"


def test_get_status_reports_next_run_at() -> None:
    scheduler.set_clock(lambda: datetime(2026, 4, 19, 4, 0, 0))
    scheduler.register("rotate", scheduler.daily("05:00"), lambda: None)

    status = scheduler.get_status()
    job = next(j for j in status["jobs"] if j["name"] == "rotate")
    assert job["next_run_at"].startswith("2026-04-19T05:00:00")
