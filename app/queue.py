from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Student:
    student_id: str
    display_name: str | None = None

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        return f"Student {self.student_id[:6]}"


@dataclass
class QueueEntry:
    student: Student
    joined_at: float = field(default_factory=time.time)


class QuestionQueue:
    """Ordered per-student question queue (§6.3, §9.5). One position per
    student. Read and written only from the event loop thread — no locking
    (concurrency rule 1). Inspected only at checkpoints by the orchestrator;
    students may join or leave at any time, including mid-drain, and are
    served in the same drain (§6.3)."""

    def __init__(self) -> None:
        self._entries: dict[str, QueueEntry] = {}
        self.on_change: Callable[[], None] | None = None

    def join(self, student_id: str, display_name: str | None = None) -> QueueEntry:
        existing = self._entries.get(student_id)
        if existing is not None:
            return existing
        entry = QueueEntry(student=Student(student_id, display_name))
        self._entries[student_id] = entry
        self._notify()
        return entry

    def leave(self, student_id: str) -> None:
        if self._entries.pop(student_id, None) is not None:
            self._notify()

    def position(self, student_id: str) -> int | None:
        """1-based position, or None if the student isn't queued."""
        for i, sid in enumerate(self._entries):
            if sid == student_id:
                return i + 1
        return None

    def peek(self) -> QueueEntry | None:
        return next(iter(self._entries.values()), None)

    def positions(self) -> dict[str, int]:
        return {sid: i + 1 for i, sid in enumerate(self._entries)}

    def pop_next(self) -> QueueEntry | None:
        if not self._entries:
            return None
        student_id = next(iter(self._entries))
        entry = self._entries.pop(student_id)
        self._notify()
        return entry

    def clear(self) -> list[QueueEntry]:
        entries = list(self._entries.values())
        self._entries.clear()
        if entries:
            self._notify()
        return entries

    def is_empty(self) -> bool:
        return not self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()
