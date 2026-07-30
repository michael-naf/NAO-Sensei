from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Callable

from app.audio.sink import AudioSink
from app.config import cfg


@dataclass(frozen=True)
class Utterance:
    turn_id: int  # bumped on cancel; stale items are discarded
    seq: int  # order within the turn
    wav_path: str
    kind: str  # 'narration' | 'filler' | 'answer'


class PlaybackQueue:
    def __init__(self, sink: AudioSink) -> None:
        self._sink = sink
        self._pending: queue.Queue[Utterance] = queue.Queue()
        self._ready: queue.Queue[tuple[Utterance, str]] = queue.Queue(
            maxsize=cfg.audio.ready_queue_size
        )

        self._turn_lock = threading.Lock()
        self._valid_turn_id = 0

        self._resume_event = threading.Event()
        self._resume_event.set()  # not paused by default
        self._playing_event = threading.Event()

        self.on_utterance_start: Callable[[Utterance], None] | None = None
        self.on_utterance_end: Callable[[Utterance], None] | None = None
        self.on_idle: Callable[[], None] | None = None

        threading.Thread(target=self._prepare_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    def enqueue(self, u: Utterance) -> None:
        with self._turn_lock:
            if u.turn_id > self._valid_turn_id:
                self._valid_turn_id = u.turn_id
        self._pending.put(u)

    def flush(self, turn_id: int | None = None) -> None:
        """Drop pending items. None = drop all.

        Passing turn_id advances the staleness watermark immediately, before
        anything already in flight in the prepare thread can land in the
        ready queue. That ordering is what a genuine cancellation needs:
        call flush(new_turn_id) first, then stop_now() to cut the utterance
        currently playing. stop_now() alone only drops a snapshot of what's
        queued *right now* — it does not by itself stop same-turn items
        that are still in flight from continuing to play afterward.
        """
        if turn_id is not None:
            with self._turn_lock:
                if turn_id > self._valid_turn_id:
                    self._valid_turn_id = turn_id
        _drain(self._pending)
        _drain(self._ready)

    def stop_now(self) -> None:
        """Interrupt the current utterance and drop all pending."""
        self.flush()
        self._sink.stop()

    def stop_after_current(self) -> None:
        """Let the current utterance finish, then hold. (Operator pause.)"""
        self._resume_event.clear()

    def resume(self) -> None:
        self._resume_event.set()

    def is_playing(self) -> bool:
        return self._playing_event.is_set()

    def _prepare_loop(self) -> None:
        while True:
            u = self._pending.get()
            token = self._sink.prepare(u.wav_path)
            self._ready.put((u, token))

    def _play_loop(self) -> None:
        while True:
            self._resume_event.wait()
            u, token = self._ready.get()

            with self._turn_lock:
                stale = u.turn_id < self._valid_turn_id
            if stale:
                continue

            self._playing_event.set()
            if self.on_utterance_start is not None:
                self.on_utterance_start(u)

            self._sink.play(token)  # returns on natural completion OR stop_now()

            self._playing_event.clear()
            if self.on_utterance_end is not None:
                self.on_utterance_end(u)

            if self._pending.empty() and self._ready.empty():
                if self.on_idle is not None:
                    self.on_idle()


def _drain(q: queue.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break
