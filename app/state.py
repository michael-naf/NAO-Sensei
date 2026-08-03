from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class LectureState(Enum):
    IDLE = auto()
    READY = auto()
    NARRATING = auto()
    CHECKPOINT = auto()
    ANSWERING = auto()
    PAUSED = auto()
    FINISHED = auto()


# §6.1. PAUSED is re-entrant from any active state (NARRATING, CHECKPOINT,
# ANSWERING) and returns to the exact state it interrupted — there is no
# RECOVERING state.
_VALID_TRANSITIONS: dict[LectureState, set[LectureState]] = {
    LectureState.IDLE: {LectureState.READY},
    LectureState.READY: {LectureState.NARRATING},
    # NARRATING -> FINISHED covers one edge §6.1's table implies but doesn't
    # spell out: when the *last* section's checkpoint has a non-empty queue,
    # ANSWERING drains it and returns to NARRATING (per the table's own "ANSWERING
    # -> NARRATING when the queue empties" row) with no section left to narrate.
    LectureState.NARRATING: {LectureState.CHECKPOINT, LectureState.PAUSED, LectureState.FINISHED},
    LectureState.CHECKPOINT: {
        LectureState.NARRATING,
        LectureState.ANSWERING,
        LectureState.FINISHED,
        LectureState.PAUSED,
    },
    LectureState.ANSWERING: {LectureState.NARRATING, LectureState.PAUSED},
    # Empty, not {NARRATING, CHECKPOINT, ANSWERING}: the only legitimate way
    # out of PAUSED is resume_from_pause() (uses _paused_from, bypasses this
    # table on purpose) or force_idle() (also bypasses it, for End lecture).
    # Those three targets used to sit here as leftover "resume" edges, but
    # nothing ever consumed them through transition() *deliberately* — they
    # only let a stray transition() call from elsewhere (e.g. _narrate()
    # walking forward through a checkpoint) succeed silently while actually
    # PAUSED, overwriting the pause without ever resuming playback. Found
    # live: pausing during the inter-section gap let the lecture "jump out
    # of pause" on its own. Now any such stray call raises loudly instead.
    LectureState.PAUSED: set(),
    LectureState.FINISHED: {LectureState.IDLE},
}


class InvalidTransition(Exception):
    pass


@dataclass
class LectureStateMachine:
    state: LectureState = LectureState.IDLE
    _paused_from: LectureState | None = None

    def transition(self, new_state: LectureState) -> None:
        if new_state not in _VALID_TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.name} -> {new_state.name} is not allowed")
        if new_state == LectureState.PAUSED:
            self._paused_from = self.state
        self.state = new_state

    def resume_from_pause(self) -> None:
        if self.state != LectureState.PAUSED:
            raise InvalidTransition("resume_from_pause() called while not PAUSED")
        assert self._paused_from is not None
        self.state = self._paused_from
        self._paused_from = None

    def force_idle(self) -> None:
        """Operator 'End lecture' (§10.2) must work from any active state,
        not just the single FINISHED -> IDLE edge §6.1's table names — there
        is no re-arm flow in this MVP (one process, one run() call), so this
        is a hard reset for shutdown, not a modeled transition. Deliberately
        bypasses _VALID_TRANSITIONS rather than adding an IDLE edge to every
        row, which would make the graph claim states can resume normally
        into IDLE when they can't."""
        self.state = LectureState.IDLE
        self._paused_from = None
