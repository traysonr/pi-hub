"""Physical pushbutton controls over Raspberry Pi GPIO.

Three momentary buttons are wired from BCM GPIO inputs to ground. Internal
pull-ups keep each line HIGH when idle and LOW while pressed:

  BCM 17 (header pin 11) — toggle music shuffle on/off
  BCM 27 (header pin 13) — toggle pause / play
  BCM 22 (header pin 15) — short press: hard-skip to next shuffle track;
                           hold ~2s: seek +15s immediately, then repeat
                           ~once per second while held

This module is intentionally tolerant: missing ``gpiod``, a missing
gpiochip, or permission errors are logged and ``init()`` becomes a no-op
so the web UI and the rest of the app keep working on non-Pi hosts and in
tests. Button handlers call the same service functions the HTTP remote
uses; they never go through the web layer.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

# BCM GPIO numbers (physical header pins 11 / 13 / 15).
GPIO_SHUFFLE = 17
GPIO_PAUSE = 27
GPIO_SKIP = 22

_DEFAULT_CHIP = "/dev/gpiochip0"
_POLL_INTERVAL_S = 0.02
_DEBOUNCE_S = 0.05
_LONG_PRESS_S = 2.0
_SEEK_REPEAT_S = 1.0
_SEEK_DELTA_S = 15.0

# pressed = line driven low (to ground) against pull-up.
_PRESSED = False
_RELEASED = True


def _env_enabled() -> bool:
    raw = os.environ.get("PI_HUB_GPIO_BUTTONS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _chip_path() -> str:
    return os.environ.get("PI_HUB_GPIO_CHIP", _DEFAULT_CHIP).strip() or _DEFAULT_CHIP


def _import_gpiod():
    """Import libgpiod's Python bindings, preferring the venv then the OS package."""

    try:
        import gpiod
        from gpiod.line import Bias, Direction, Value

        return gpiod, Bias, Direction, Value
    except ImportError:
        pass

    # Pi Hub's venv is isolated; Raspberry Pi OS ships python3-libgpiod
    # system-wide. Fall back to that dist-packages tree when present.
    for candidate in (
        "/usr/lib/python3/dist-packages",
        f"/usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/dist-packages",
    ):
        if candidate not in sys.path and os.path.isdir(candidate):
            sys.path.append(candidate)
    import gpiod
    from gpiod.line import Bias, Direction, Value

    return gpiod, Bias, Direction, Value


Action = Callable[[], None]


@dataclass
class _DebouncedLine:
    """Software-debounced digital input (True = released/HIGH, False = pressed/LOW)."""

    name: str
    stable: bool = _RELEASED
    raw: bool = _RELEASED
    since: float = 0.0


@dataclass
class _SkipState:
    pressed_at: float | None = None
    long_press: bool = False
    next_seek_at: float | None = None


@dataclass
class ButtonController:
    """Pure state machine for the three car-deck buttons.

    ``read_pressed`` returns a mapping of BCM offset -> pressed (True while
    held). ``now`` is injectable so unit tests can advance time without
    sleeping. Action callbacks are injectable so tests don't need mpv.
    """

    read_pressed: Callable[[], dict[int, bool]]
    toggle_shuffle: Action
    toggle_pause: Action
    next_track: Action
    seek_forward: Action
    now: Callable[[], float] = time.monotonic
    debounce_s: float = _DEBOUNCE_S
    long_press_s: float = _LONG_PRESS_S
    seek_repeat_s: float = _SEEK_REPEAT_S
    _lines: dict[int, _DebouncedLine] = field(init=False)
    _skip: _SkipState = field(init=False)

    def __post_init__(self) -> None:
        t0 = self.now()
        self._lines = {
            GPIO_SHUFFLE: _DebouncedLine("shuffle", since=t0),
            GPIO_PAUSE: _DebouncedLine("pause", since=t0),
            GPIO_SKIP: _DebouncedLine("skip", since=t0),
        }
        self._skip = _SkipState()

    def tick(self) -> None:
        samples = self.read_pressed()
        t = self.now()
        for bcm, line in self._lines.items():
            raw_pressed = bool(samples.get(bcm, False))
            raw_level = _PRESSED if raw_pressed else _RELEASED
            if raw_level != line.raw:
                line.raw = raw_level
                line.since = t
                continue
            if raw_level == line.stable:
                continue
            if (t - line.since) < self.debounce_s:
                continue
            previous = line.stable
            line.stable = raw_level
            self._on_stable_edge(bcm, previous=previous, new=raw_level, t=t)

        self._tick_skip_hold(t)

    def _on_stable_edge(
        self, bcm: int, *, previous: bool, new: bool, t: float
    ) -> None:
        pressed = new is _PRESSED
        released = new is _RELEASED and previous is _PRESSED

        if bcm == GPIO_SHUFFLE and pressed:
            self._safe("toggle shuffle", self.toggle_shuffle)
            return

        if bcm == GPIO_PAUSE and pressed:
            self._safe("toggle pause", self.toggle_pause)
            return

        if bcm != GPIO_SKIP:
            return

        if pressed:
            self._skip = _SkipState(pressed_at=t, long_press=False, next_seek_at=None)
            return

        if released:
            was_long = self._skip.long_press
            self._skip = _SkipState()
            if not was_long:
                self._safe("next track", self.next_track)

    def _tick_skip_hold(self, t: float) -> None:
        skip_line = self._lines[GPIO_SKIP]
        if skip_line.stable is not _PRESSED or self._skip.pressed_at is None:
            return

        if not self._skip.long_press:
            if (t - self._skip.pressed_at) < self.long_press_s:
                return
            self._skip.long_press = True
            self._safe("seek +15 (long-press start)", self.seek_forward)
            self._skip.next_seek_at = t + self.seek_repeat_s
            return

        if self._skip.next_seek_at is not None and t >= self._skip.next_seek_at:
            self._safe("seek +15 (repeat)", self.seek_forward)
            self._skip.next_seek_at = t + self.seek_repeat_s

    @staticmethod
    def _safe(label: str, action: Action) -> None:
        try:
            action()
        except Exception:
            log.exception("gpio buttons: %s failed", label)


_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_request = None  # gpiod LineRequest | None
_last_error: str | None = None


def last_error() -> str | None:
    return _last_error


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _default_toggle_shuffle() -> None:
    from app.services import shuffle

    if shuffle.is_active():
        shuffle.stop()
        log.info("gpio: shuffle stopped")
    else:
        shuffle.start()
        log.info("gpio: shuffle started (%s)", shuffle.current_filename())


def _default_toggle_pause() -> None:
    from app.services import player

    paused = player.toggle_pause()
    log.info("gpio: pause -> %s", paused)


def _default_next_track() -> None:
    from app.services import shuffle

    result = shuffle.next_track()
    log.info("gpio: next track -> %s", result.get("current"))


def _default_seek_forward() -> None:
    from app.services import player

    player.seek(_SEEK_DELTA_S)
    log.info("gpio: seek +%.0fs", _SEEK_DELTA_S)


def _open_request(gpiod, Bias, Direction, Value):
    chip = _chip_path()
    settings = gpiod.LineSettings(
        direction=Direction.INPUT,
        bias=Bias.PULL_UP,
    )
    offsets = (GPIO_SHUFFLE, GPIO_PAUSE, GPIO_SKIP)
    request = gpiod.request_lines(
        chip,
        consumer="pi-hub-buttons",
        config={offset: settings for offset in offsets},
    )
    # Confirm lines are readable (also surfaces permission errors early).
    _ = {offset: request.get_value(offset) for offset in offsets}
    return request, Value


def _make_reader(request, Value) -> Callable[[], dict[int, bool]]:
    def read_pressed() -> dict[int, bool]:
        # Pull-up idle = ACTIVE/HIGH = not pressed. Pressed to GND = INACTIVE.
        return {
            offset: request.get_value(offset) == Value.INACTIVE
            for offset in (GPIO_SHUFFLE, GPIO_PAUSE, GPIO_SKIP)
        }

    return read_pressed


def _loop(controller: ButtonController) -> None:
    log.info(
        "gpio buttons: listening on %s (shuffle=BCM%d pause=BCM%d skip=BCM%d)",
        _chip_path(),
        GPIO_SHUFFLE,
        GPIO_PAUSE,
        GPIO_SKIP,
    )
    while not _stop.is_set():
        try:
            controller.tick()
        except Exception:
            log.exception("gpio buttons: tick failed")
        _stop.wait(_POLL_INTERVAL_S)


def init() -> None:
    """Start the GPIO listener thread, or no-op if hardware/libs unavailable."""

    global _thread, _request, _last_error

    with _lock:
        if _thread is not None and _thread.is_alive():
            return

        _last_error = None
        if not _env_enabled():
            log.info("gpio buttons: disabled via PI_HUB_GPIO_BUTTONS")
            return

        try:
            gpiod, Bias, Direction, Value = _import_gpiod()
        except Exception as exc:
            _last_error = f"gpiod unavailable: {exc}"
            log.warning("gpio buttons: %s — hardware controls disabled", _last_error)
            return

        try:
            request, Value = _open_request(gpiod, Bias, Direction, Value)
        except Exception as exc:
            _last_error = f"could not claim GPIOs on {_chip_path()}: {exc}"
            log.warning("gpio buttons: %s — hardware controls disabled", _last_error)
            return

        controller = ButtonController(
            read_pressed=_make_reader(request, Value),
            toggle_shuffle=_default_toggle_shuffle,
            toggle_pause=_default_toggle_pause,
            next_track=_default_next_track,
            seek_forward=_default_seek_forward,
        )

        _request = request
        _stop.clear()
        _thread = threading.Thread(
            target=_loop,
            args=(controller,),
            name="gpio-buttons",
            daemon=True,
        )
        _thread.start()


def shutdown() -> None:
    """Stop the listener and release GPIO lines. Safe to call repeatedly."""

    global _thread, _request

    with _lock:
        _stop.set()
        thread = _thread
        request = _request
        _thread = None
        _request = None

    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)

    if request is not None:
        try:
            request.release()
        except Exception:
            log.exception("gpio buttons: failed to release line request")


def _reset_for_tests() -> None:
    """Test helper: force a clean module state."""

    shutdown()
    global _last_error
    _last_error = None
    _stop.clear()
