"""Tests for app.services.gpio_buttons button state machine.

Exercises debounce, short-press vs long-press skip, and seek repeat without
touching real GPIO hardware.
"""

from __future__ import annotations

from app.services.gpio_buttons import (
    GPIO_PAUSE,
    GPIO_SHUFFLE,
    GPIO_SKIP,
    ButtonController,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Harness:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.levels = {
            GPIO_SHUFFLE: False,
            GPIO_PAUSE: False,
            GPIO_SKIP: False,
        }
        self.events: list[str] = []
        self.ctrl = ButtonController(
            read_pressed=lambda: dict(self.levels),
            toggle_shuffle=lambda: self.events.append("shuffle"),
            toggle_pause=lambda: self.events.append("pause"),
            next_track=lambda: self.events.append("next"),
            seek_forward=lambda: self.events.append("seek"),
            now=self.clock,
            debounce_s=0.05,
            long_press_s=2.0,
            seek_repeat_s=1.0,
        )

    def set(self, bcm: int, pressed: bool) -> None:
        self.levels[bcm] = pressed

    def tick(self, dt: float = 0.02) -> None:
        self.clock.advance(dt)
        self.ctrl.tick()

    def hold_stable(self, seconds: float) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.tick(0.02)


def test_shuffle_toggles_on_debounced_press() -> None:
    h = _Harness()
    h.set(GPIO_SHUFFLE, True)
    h.tick()
    assert h.events == []  # still inside debounce window
    h.hold_stable(0.06)
    assert h.events == ["shuffle"]
    # Holding must not re-fire.
    h.hold_stable(0.5)
    assert h.events == ["shuffle"]
    h.set(GPIO_SHUFFLE, False)
    h.hold_stable(0.06)
    assert h.events == ["shuffle"]


def test_pause_toggles_on_debounced_press() -> None:
    h = _Harness()
    h.set(GPIO_PAUSE, True)
    h.hold_stable(0.1)
    assert h.events == ["pause"]


def test_short_skip_fires_next_on_release_only() -> None:
    h = _Harness()
    h.set(GPIO_SKIP, True)
    h.hold_stable(0.1)
    assert h.events == []
    h.hold_stable(0.4)  # short press, well under 2s
    assert h.events == []
    h.set(GPIO_SKIP, False)
    h.hold_stable(0.1)
    assert h.events == ["next"]


def test_long_skip_seeks_and_does_not_next_on_release() -> None:
    h = _Harness()
    h.set(GPIO_SKIP, True)
    h.hold_stable(0.06)
    assert h.events == []

    # Just under long-press threshold: still quiet.
    h.hold_stable(1.9)
    assert h.events == []

    # Cross 2.0s from press (press became stable at ~0.06).
    h.hold_stable(0.2)
    assert h.events == ["seek"]

    # Repeat approximately once per second while held.
    h.hold_stable(1.0)
    assert h.events == ["seek", "seek"]
    h.hold_stable(1.0)
    assert h.events == ["seek", "seek", "seek"]

    h.set(GPIO_SKIP, False)
    h.hold_stable(0.06)
    assert h.events == ["seek", "seek", "seek"]


def test_bounce_does_not_double_fire_shuffle() -> None:
    h = _Harness()
    # Chatter shorter than debounce window.
    h.set(GPIO_SHUFFLE, True)
    h.tick(0.02)
    h.set(GPIO_SHUFFLE, False)
    h.tick(0.02)
    h.set(GPIO_SHUFFLE, True)
    h.tick(0.02)
    assert h.events == []
    h.hold_stable(0.06)
    assert h.events == ["shuffle"]


def test_action_exceptions_are_swallowed() -> None:
    clock = _Clock()
    levels = {GPIO_SHUFFLE: False, GPIO_PAUSE: False, GPIO_SKIP: False}

    def boom() -> None:
        raise RuntimeError("shuffle not active")

    ctrl = ButtonController(
        read_pressed=lambda: dict(levels),
        toggle_shuffle=boom,
        toggle_pause=lambda: None,
        next_track=lambda: None,
        seek_forward=lambda: None,
        now=clock,
        debounce_s=0.05,
    )
    levels[GPIO_SHUFFLE] = True
    clock.t = 0.0
    ctrl.tick()
    clock.t = 0.06
    ctrl.tick()  # must not raise
