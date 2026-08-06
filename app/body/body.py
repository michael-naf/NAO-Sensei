from __future__ import annotations

from typing import Protocol


class Body(Protocol):
    """The embodiment seam (NFR-9). Mirrors AudioSink (§4.4): deliberately
    dumb — all logic lives in the scheduler above it."""

    def gesture(self, name: str) -> None:
        """Fire a named gesture. Non-blocking. Never raises."""
        ...

    def gaze(self, target: str) -> None:
        """target: 'slides' | 'class'. Head joints only."""
        ...

    def leds(self, pattern: str) -> None: ...

    def posture(self, name: str) -> None:
        """Lecture start/end only."""
        ...

    def stiffness(self, on: bool) -> None: ...

    def volume(self, level: int) -> None:
        """Output volume, 0-100. Lecture start only (§12.5). Non-blocking,
        failures logged and swallowed — same contract as gesture/gaze/leds,
        not posture/stiffness (specs.md §12.1.2's failure table)."""
        ...

    def is_gesturing(self) -> bool: ...

    def is_available(self) -> bool: ...
