from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptEntry:
    timestamp: datetime
    slide_index: int
    section_index: int
    student_label: str
    input_mode: str  # 'typed' | 'voice'
    question_text: str
    answer_text: str
    grounded: bool
    resolution: str  # 'done' | 'replied' | 'timeout'
    follow_up_depth: int = 0  # 0 for the initial question, incrementing for replies


class Transcript:
    """Appended after each answer completes — never buffered (§7.5). A crash
    mid-lecture loses nothing already recorded."""

    def __init__(self, sessions_dir: str, started_at: datetime | None = None) -> None:
        self._started_at = started_at or datetime.now()
        Path(sessions_dir).mkdir(parents=True, exist_ok=True)
        self._path = Path(sessions_dir) / f"lecture_{self._started_at:%Y%m%d_%H%M%S}.md"
        self._question_number = 0
        self._current_root_number: int | None = None
        self._last_section: tuple[int, int] | None = None
        self._header_written = False

    @property
    def path(self) -> Path:
        return self._path

    def record(self, entry: TranscriptEntry) -> None:
        try:
            self._write(entry)
        except OSError as e:
            # Non-fatal per §7.5: the lecture continues, but the exchange
            # must not vanish, so it goes to the main log as a fallback.
            logger.error("Transcript write failed (%s): %s", self._path, e)
            logger.info("TRANSCRIPT FALLBACK: %s", entry)

    def _write(self, entry: TranscriptEntry) -> None:
        lines: list[str] = []

        if not self._header_written:
            lines.append(f"# Lecture Q&A — {self._started_at:%Y-%m-%d %H:%M}\n")
            self._header_written = True

        section_key = (entry.slide_index, entry.section_index)
        if section_key != self._last_section:
            lines.append(f"\n## Slide {entry.slide_index}, Section {entry.section_index}\n")
            self._last_section = section_key

        if entry.follow_up_depth == 0:
            self._question_number += 1
            self._current_root_number = self._question_number
            number = str(self._current_root_number)
            level = "###"
            follow_up_note = ""
        else:
            number = f"{self._current_root_number}.{entry.follow_up_depth}"
            level = "####"
            follow_up_note = " *(follow-up)*"

        grounding_note = "" if entry.grounded else " ⚠ NOT IN MATERIAL"

        lines.append(
            f"\n{level} Q{number} — {entry.student_label} ({entry.input_mode}) — "
            f"{entry.timestamp:%H:%M:%S}{follow_up_note}{grounding_note}\n"
        )
        lines.append(f"{entry.question_text}\n")
        lines.append("\n**Answer:**\n")
        lines.append(f"{entry.answer_text}\n")
        if entry.resolution == "timeout":
            lines.append("\n*(auto-resolved after timeout)*\n")

        with self._path.open("a", encoding="utf-8") as f:
            f.writelines(lines)
