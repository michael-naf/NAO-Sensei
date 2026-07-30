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
    LectureState.NARRATING: {LectureState.CHECKPOINT, LectureState.PAUSED},
    LectureState.CHECKPOINT: {
        LectureState.NARRATING,
        LectureState.ANSWERING,
        LectureState.FINISHED,
        LectureState.PAUSED,
    },
    LectureState.ANSWERING: {LectureState.NARRATING, LectureState.PAUSED},
    LectureState.PAUSED: {LectureState.NARRATING, LectureState.CHECKPOINT, LectureState.ANSWERING},
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
