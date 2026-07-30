from __future__ import annotations

from typing import Protocol


class AudioSink(Protocol):
    """The swappable seam (NFR-5). Deliberately dumb."""

    def prepare(self, wav_path: str) -> str:
        """Make the audio available to the playback device.
        Returns an opaque play token."""
        ...

    def play(self, token: str) -> None:
        """Blocks until playback ends."""
        ...

    def stop(self) -> None:
        """Interrupt playback immediately.
        MUST be thread-safe and callable while play() is blocked."""
        ...

    def is_available(self) -> bool: ...
