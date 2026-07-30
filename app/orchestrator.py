from __future__ import annotations

import asyncio

from app.audio.playback_queue import PlaybackQueue, Utterance
from app.config import cfg
from app.script_parser import LectureScript
from app.services import sentences, tts
from app.slides import SlideController, SlideControllerError
from app.state import LectureState, LectureStateMachine


class Orchestrator:
    """The lecture loop — a coroutine, not a thread (§4.3). Position (which
    section we're on) is owned here and never read back from PowerPoint."""

    def __init__(self, script: LectureScript, slides: SlideController, playback: PlaybackQueue) -> None:
        self._script = script
        self._slides = slides
        self._playback = playback
        self.state = LectureStateMachine()
        self.position = 0  # index into script.sections — the source of truth
        self.fault_message: str | None = None

        self._last_slide_index: int | None = None
        self._turn_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle_event: asyncio.Event | None = None

    async def run(self, pptx_path: str) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle_event = asyncio.Event()
        self._playback.on_idle = self._handle_idle

        self.state.transition(LectureState.READY)
        self.state.transition(LectureState.NARRATING)

        try:
            await self._loop.run_in_executor(None, self._slides.open, pptx_path)
        except SlideControllerError as e:
            self._on_fault(str(e))
            return

        while self.position < len(self._script.sections):
            section = self._script.sections[self.position]

            if section.slide_index != self._last_slide_index:
                try:
                    await self._loop.run_in_executor(None, self._slides.goto, section.slide_index)
                except SlideControllerError as e:
                    self._on_fault(str(e))
                    return
                self._last_slide_index = section.slide_index

            await self._speak(section.text)

            is_last_section = self.position + 1 >= len(self._script.sections)
            if not is_last_section:
                # Extra pause on top of the last sentence's own trailing
                # silence — makes a section break read as a distinct beat
                # rather than just another inter-sentence gap.
                await asyncio.sleep(cfg.tts.section_gap_ms / 1000)

            if section.checkpoint:
                self.state.transition(LectureState.CHECKPOINT)
                # Checkpoints are no-ops for now — Q&A does not exist yet
                # (Phase 3B), so the queue is always empty: go straight back
                # to narrating, or finish if this was the final section.
                if not is_last_section:
                    self.state.transition(LectureState.NARRATING)

            self.position += 1

        self.state.transition(LectureState.FINISHED)

    async def _speak(self, text: str) -> None:
        assert self._loop is not None and self._idle_event is not None
        self._idle_event.clear()

        # Synthesize the whole section before enqueuing any of it. Piper
        # synthesis is CPU-bound; interleaving it with playback (as before)
        # let a sentence's synthesis run concurrently with the *previous*
        # sentence's tail end, and CPU contention between the two produced
        # an audible glitch right at that boundary — worse there than at a
        # mid-sentence comma, where nothing else is being computed.
        wav_paths = []
        for sentence in sentences.split_stream([text]):
            wav_path = await self._loop.run_in_executor(None, tts.synthesize, sentence)
            wav_paths.append(wav_path)

        for seq, wav_path in enumerate(wav_paths):
            self._playback.enqueue(
                Utterance(turn_id=self._turn_id, seq=seq, wav_path=wav_path, kind="narration")
            )

        await self._idle_event.wait()

    def _handle_idle(self) -> None:
        # Fires on the play thread — must marshal via call_soon_threadsafe
        # before touching any state (concurrency rule 2).
        assert self._loop is not None and self._idle_event is not None
        self._loop.call_soon_threadsafe(self._idle_event.set)

    def _on_fault(self, message: str) -> None:
        self._playback.stop_after_current()
        self.state.transition(LectureState.PAUSED)
        self.fault_message = message
        print(f"[FAULT] {message}")  # operator console alert lands in Phase 5
