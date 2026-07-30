from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.audio.playback_queue import PlaybackQueue, Utterance
from app.config import cfg
from app.queue import QueueEntry, QuestionQueue
from app.script_parser import LectureScript, Section
from app.services import llm, moderation, sentences, tts
from app.slides import SlideController, SlideControllerError
from app.state import LectureState, LectureStateMachine
from app.transcript import Transcript, TranscriptEntry


class Notifier(Protocol):
    """What the web layer exposes for pushing state to students (§9.4).
    Structural — orchestrator.py has no import-time dependency on FastAPI."""

    async def send(self, student_id: str, message: dict) -> None: ...
    async def broadcast(self, message: dict) -> None: ...


class Orchestrator:
    """The lecture loop — a coroutine, not a thread (§4.3). Position (which
    section we're on) is owned here and never read back from PowerPoint."""

    def __init__(
        self,
        script: LectureScript,
        slides: SlideController,
        playback: PlaybackQueue,
        queue: QuestionQueue,
        transcript: Transcript,
        notifier: Notifier | None = None,
    ) -> None:
        self._script = script
        self._slides = slides
        self._playback = playback
        self._queue = queue
        self._transcript = transcript
        self._notifier = notifier
        self._filler_bank = _FillerBank()
        self.state = LectureStateMachine()
        self.position = 0  # index into script.sections — the source of truth
        self.fault_message: str | None = None

        self._last_slide_index: int | None = None
        self._turn_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle_event: asyncio.Event | None = None
        self._start_event: asyncio.Event | None = None

        # Per-utterance completion tracking (§4.4's on_utterance_end), used
        # instead of on_idle wherever utterances are enqueued incrementally
        # while still being produced (Q&A answers) — on_idle fires whenever
        # the queue *momentarily* drains, which happens mid-production too
        # (e.g. the gap between a filler finishing and the first LLM
        # sentence being ready), so it can signal "done" before the last
        # sentence has actually played. Tracking the specific (turn_id, seq)
        # that must finish avoids that race entirely.
        self._last_finished_seq: dict[int, int] = {}
        self._finish_event: asyncio.Event | None = None

        # Cross-request rendezvous (§6.3 turn resolution, §7.1 turn sequence).
        # A web route (running on this same event loop, §4.3) resolves the
        # matching future; there is only ever one turn in flight, so a single
        # slot is sufficient and requires no locking (rule 1).
        self._awaiting_kind: str | None = None
        self._awaiting_student: str | None = None
        self._awaiting_future: asyncio.Future | None = None

        self._queue.on_change = self._on_queue_change

    # ---- main loop -------------------------------------------------

    async def run(self, pptx_path: str) -> None:
        self._loop = asyncio.get_running_loop()
        self._idle_event = asyncio.Event()
        self._start_event = asyncio.Event()
        self._finish_event = asyncio.Event()
        self._playback.on_idle = self._handle_idle
        self._playback.on_utterance_end = self._handle_utterance_end

        # §6.1: IDLE -> READY happens "on successful load + model warm-up".
        await self._loop.run_in_executor(None, tts.warm)
        await self._loop.run_in_executor(None, llm.warm)
        await self._filler_bank.warm(self._loop)

        self.state.transition(LectureState.READY)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

        # READY is "not started" until an operator start (§6.1) — students
        # can connect and see live status while this waits. The real Start
        # control belongs to the Phase 5 operator console; start() below is
        # the minimal piece of that wiring needed now, with no console, no
        # auth, and none of the other six controls pulled forward.
        await self._start_event.wait()

        self.state.transition(LectureState.NARRATING)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

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
                if not self._queue.is_empty():
                    await self._drain_queue(section)  # always ends back in NARRATING
                    if is_last_section:
                        self.state.transition(LectureState.FINISHED)
                        self.position += 1
                        return
                elif not is_last_section:
                    self.state.transition(LectureState.NARRATING)
                # else: last section, empty queue — stay in CHECKPOINT; the
                # loop ends below and FINISHED is entered from there.

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

    def _handle_utterance_end(self, u: Utterance) -> None:
        # Also fires on the play thread (concurrency rule 2).
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._on_utterance_end_main, u)

    def _on_utterance_end_main(self, u: Utterance) -> None:
        assert self._finish_event is not None
        prev = self._last_finished_seq.get(u.turn_id, -1)
        if u.seq > prev:
            self._last_finished_seq[u.turn_id] = u.seq
        self._finish_event.set()

    async def _wait_until_played(self, turn_id: int, seq: int) -> None:
        assert self._finish_event is not None
        while self._last_finished_seq.get(turn_id, -1) < seq:
            self._finish_event.clear()
            # Re-check after clearing: an update between the check above and
            # the clear() would otherwise be lost, and this utterance may
            # already have finished before we ever started waiting.
            if self._last_finished_seq.get(turn_id, -1) >= seq:
                return
            await self._finish_event.wait()

    async def _say(self, text: str) -> None:
        """Speak one standalone line (Q&A start/end announcements) and wait
        for it to actually finish playing — not LLM-generated, not a filler."""
        assert self._loop is not None
        self._turn_id += 1
        wav_path = await self._loop.run_in_executor(None, tts.synthesize, text)
        self._playback.enqueue(Utterance(turn_id=self._turn_id, seq=0, wav_path=wav_path, kind="narration"))
        await self._wait_until_played(self._turn_id, 0)

    def _on_fault(self, message: str) -> None:
        self._playback.stop_after_current()
        self.state.transition(LectureState.PAUSED)
        self.fault_message = message
        print(f"[FAULT] {message}")  # operator console alert lands in Phase 5

    # ---- Q&A (§6.3, §7) ---------------------------------------------

    async def _drain_queue(self, section: Section) -> None:
        self.state.transition(LectureState.ANSWERING)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})
        await self._say(self._filler_bank.qa_start_text)

        while not self._queue.is_empty():
            entry = self._queue.pop_next()
            await self._broadcast_queue_positions()
            await self._run_turn(entry, section)

        await self._say(self._filler_bank.qa_end_text)
        self.state.transition(LectureState.NARRATING)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

    async def _run_turn(self, entry: QueueEntry, section: Section) -> None:
        student = entry.student
        history: list[dict] = []
        follow_up_depth = 0

        question = await self._prompt_and_await_question(student.student_id)

        while True:
            self._turn_id += 1
            answer_text, grounded = await self._answer_question(question, history, section)
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer_text})

            allow_reply = (follow_up_depth + 1) < cfg.llm.followup_turn_cap
            resolution, next_question = await self._offer_reply(student.student_id, allow_reply)

            self._record_transcript(
                section, student, question, answer_text, grounded, resolution, follow_up_depth
            )

            if resolution != "replied":
                break
            question = next_question
            follow_up_depth += 1

    async def _answer_question(
        self, question: str, history: list[dict], section: Section
    ) -> tuple[str, bool]:
        assert self._loop is not None
        seq = 0

        filler_wav = self._filler_bank.next_stage1()
        self._playback.enqueue(Utterance(turn_id=self._turn_id, seq=seq, wav_path=filler_wav, kind="filler"))
        seq += 1

        # Checked before stage-2, which would otherwise speak the question
        # back verbatim with no guardrail of its own — the LLM's guardrail
        # (§7.3) only governs what NAO *answers*, not what it *repeats*.
        blocked = moderation.is_inappropriate(question)

        if not blocked and len(question.split()) <= cfg.timings.question_repeat_max_words:
            stage2_text = self._filler_bank.stage2(question)
            stage2_wav = await self._loop.run_in_executor(None, tts.synthesize, stage2_text)
            self._playback.enqueue(
                Utterance(turn_id=self._turn_id, seq=seq, wav_path=stage2_wav, kind="filler")
            )
            seq += 1

        if blocked:
            # No LLM round-trip either — a flagged question gets a fixed,
            # scripted decline, not a repeat and not a model-generated reply.
            answer_text = self._filler_bank.decline_text
            wav_path = await self._loop.run_in_executor(None, tts.synthesize, answer_text)
            self._playback.enqueue(Utterance(turn_id=self._turn_id, seq=seq, wav_path=wav_path, kind="answer"))
            seq += 1
            grounded = True
        else:
            position = self._position_marker(section)
            answer_chunks: list[str] = []
            async for sentence in sentences.split_stream_async(
                llm.answer(question, history, self._script.full_narration, position)
            ):
                answer_chunks.append(sentence)
                wav_path = await self._loop.run_in_executor(None, tts.synthesize, sentence)
                self._playback.enqueue(Utterance(turn_id=self._turn_id, seq=seq, wav_path=wav_path, kind="answer"))
                seq += 1

            answer_text = " ".join(answer_chunks)
            lowered = answer_text.lower()
            grounded = "isn't covered" not in lowered and "not covered" not in lowered

        # Wait for the specific last utterance we enqueued to actually
        # finish playing — not PlaybackQueue.on_idle, which can fire while
        # this coroutine is still producing more (see _wait_until_played).
        await self._wait_until_played(self._turn_id, seq - 1)
        return answer_text, grounded

    async def _offer_reply(self, student_id: str, allow_reply: bool) -> tuple[str, str | None]:
        if not allow_reply:
            await self._notify(student_id, {"type": "answered", "reply_allowed": False})
            await self._notify(student_id, {"type": "turn_ended"})
            return "done", None

        await self._notify(student_id, {"type": "answered", "reply_allowed": True, "window_s": cfg.timings.reply_window_s})
        choice = await self._await_choice(student_id, cfg.timings.reply_window_s)
        if choice != "reply":
            # The 5 s/30 s windows are enforced here, server-side — the
            # client's own countdown is cosmetic. Either way the client
            # needs an explicit push once the window closes, or it's left
            # showing Reply/Done (or a reply box) with no way out.
            await self._notify(student_id, {"type": "turn_ended"})
            return ("timeout" if choice is None else "done"), None

        await self._notify(student_id, {"type": "reply_window", "window_s": cfg.timings.compose_window_s})
        reply_text = await self._await_reply_text(student_id, cfg.timings.compose_window_s)
        if reply_text is None:
            await self._notify(student_id, {"type": "turn_ended"})
            return "timeout", None
        return "replied", reply_text

    def _position_marker(self, section: Section) -> str:
        return (
            f"The lecture is currently on slide {section.slide_index}, "
            f"section {section.section_index + 1}."
        )

    def _record_transcript(
        self,
        section: Section,
        student,
        question: str,
        answer_text: str,
        grounded: bool,
        resolution: str,
        follow_up_depth: int,
    ) -> None:
        entry = TranscriptEntry(
            timestamp=datetime.now(),
            slide_index=section.slide_index,
            section_index=section.section_index + 1,
            student_label=student.label,
            input_mode="typed",
            question_text=question,
            answer_text=answer_text,
            grounded=grounded,
            resolution=resolution,
            follow_up_depth=follow_up_depth,
        )
        self._transcript.record(entry)

    # ---- cross-request rendezvous ------------------------------------

    async def _prompt_and_await_question(self, student_id: str) -> str:
        await self._notify(student_id, {"type": "your_turn"})
        result = await self._await("question", student_id)
        await self._notify(student_id, {"type": "submitted"})
        return result["text"]

    async def _await_choice(self, student_id: str, timeout: float) -> str | None:
        try:
            return await self._await("choice", student_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _await_reply_text(self, student_id: str, timeout: float) -> str | None:
        try:
            result = await self._await("reply", student_id, timeout=timeout)
            return result["text"]
        except asyncio.TimeoutError:
            return None

    async def _await(self, kind: str, student_id: str, timeout: float | None = None):
        assert self._loop is not None
        fut = self._loop.create_future()
        self._awaiting_kind = kind
        self._awaiting_student = student_id
        self._awaiting_future = fut
        try:
            if timeout is not None:
                return await asyncio.wait_for(fut, timeout)
            return await fut
        finally:
            self._awaiting_kind = None
            self._awaiting_student = None
            self._awaiting_future = None

    # ---- called by web routes, on the event loop thread (§4.3) --------

    def start(self) -> bool:
        """READY -> NARRATING (§6.1). False if not currently waiting to start."""
        if self.state.state != LectureState.READY or self._start_event is None:
            return False
        self._start_event.set()
        return True

    def submit_question(self, student_id: str, text: str) -> bool:
        return self._resolve("question", student_id, {"text": text})

    def choose_reply(self, student_id: str) -> bool:
        return self._resolve("choice", student_id, "reply")

    def choose_done(self, student_id: str) -> bool:
        return self._resolve("choice", student_id, "done")

    def submit_reply_text(self, student_id: str, text: str) -> bool:
        return self._resolve("reply", student_id, {"text": text})

    def _resolve(self, kind: str, student_id: str, value) -> bool:
        if (
            self._awaiting_kind == kind
            and self._awaiting_student == student_id
            and self._awaiting_future is not None
            and not self._awaiting_future.done()
        ):
            self._awaiting_future.set_result(value)
            return True
        return False

    # ---- notification plumbing ---------------------------------------

    async def _notify(self, student_id: str, message: dict) -> None:
        if self._notifier is not None:
            await self._notifier.send(student_id, message)

    async def _notify_all(self, message: dict) -> None:
        if self._notifier is not None:
            await self._notifier.broadcast(message)

    async def _broadcast_queue_positions(self) -> None:
        if self._notifier is None:
            return
        for student_id, position in self._queue.positions().items():
            await self._notifier.send(student_id, {"type": "queue_position", "position": position})

    def _on_queue_change(self) -> None:
        # QuestionQueue.on_change fires synchronously from the event loop
        # thread (join/leave are called from async route handlers, or from
        # this class itself) — safe to schedule a task from here.
        if self._loop is not None:
            self._loop.create_task(self._broadcast_queue_positions())


class _FillerBank:
    """Stage-1 clips are pre-synthesized once at startup and rotated; stage-2
    is a template filled in per-question, since the text varies (§7.1)."""

    def __init__(self) -> None:
        path = Path(__file__).resolve().parent / "prompts" / "fillers.txt"
        sections = _parse_fillers(path.read_text(encoding="utf-8"))
        self._stage1_texts = sections.get("stage1", [])
        self._stage2_template = sections.get("stage2", [""])[0]
        self.decline_text = sections.get("decline", [""])[0]
        self.qa_start_text = sections.get("qa_start", [""])[0]
        self.qa_end_text = sections.get("qa_end", [""])[0]
        self._stage1_wavs: list[str] = []
        self._rotation = 0

    async def warm(self, loop: asyncio.AbstractEventLoop) -> None:
        for text in self._stage1_texts[: cfg.tts.filler_variants] or self._stage1_texts:
            wav = await loop.run_in_executor(None, tts.synthesize, text)
            self._stage1_wavs.append(wav)

    def next_stage1(self) -> str:
        wav = self._stage1_wavs[self._rotation % len(self._stage1_wavs)]
        self._rotation += 1
        return wav

    def stage2(self, question: str) -> str:
        return self._stage2_template.format(question=question)


def _parse_fillers(raw: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
            continue
        if not line or current is None:
            continue
        sections[current].append(line)
    return sections
