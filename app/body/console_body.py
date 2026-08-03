from __future__ import annotations

import time

from app.body.gesture_library import GestureLibrary


class ConsoleBody:
    """Phase 3 Body implementation (§12.6.1): logs to the operator console
    instead of driving real motors. No previewer wiring — the user cut that
    (see CLAUDE.md's Current position); the console log is the only record
    of what NAO would be doing until the robot exists (Phase 6)."""

    def __init__(self, library: GestureLibrary) -> None:
        self._library = library
        self._gesture_until = 0.0
        self._current_gesture = "rest"
        self._current_gaze = "class"
        self._current_leds = "off"

    def gesture(self, name: str) -> None:
        gesture = self._library.gestures.get(name)
        duration = gesture.duration_s if gesture is not None else 0.0
        self._gesture_until = time.monotonic() + duration
        self._current_gesture = name
        self._log()

    def gaze(self, target: str) -> None:
        self._current_gaze = target
        self._log()

    def leds(self, pattern: str) -> None:
        self._current_leds = pattern
        self._log()

    def posture(self, name: str) -> None:
        print(f"[BODY] posture={name}")

    def stiffness(self, on: bool) -> None:
        print(f"[BODY] stiffness={'on' if on else 'off'}")

    def is_gesturing(self) -> bool:
        return time.monotonic() < self._gesture_until

    def is_available(self) -> bool:
        return True

    def _log(self) -> None:
        print(
            f"[BODY] gesture={self._current_gesture} "
            f"gaze={self._current_gaze} leds={self._current_leds}"
        )
