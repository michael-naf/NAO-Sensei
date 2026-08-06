from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.audio.playback_queue import PlaybackQueue, Utterance
from app.body.body import Body
from app.body.scheduler import Scheduler
from app.config import cfg
from app.queue import QueueEntry, QuestionQueue
from app.script_parser import LectureScript, Section
from app.services import llm, moderation, sentences, stt, tts
from app.slides import SlideController, SlideControllerError
from app.state import LectureState, LectureStateMachine
from app.transcript import Transcript, TranscriptEntry


class Notifier(Protocol):
    """What the web layer exposes for pushing state to students (§9.4).
    Structural — orchestrator.py has no import-time dependency on FastAPI."""

    async def send(self, student_id: str, message: dict) -> None: ...
    async def broadcast(self, message: dict) -> None: ...
    def is_connected(self, student_id: str) -> bool: ...


# §12.4.3 — eye LED pattern per orchestrator state.
_LED_PATTERNS: dict[LectureState, str] = {
    LectureState.IDLE: "off",
    LectureState.READY: "off",
    LectureState.NARRATING: "white",
    LectureState.CHECKPOINT: "green",
    LectureState.ANSWERING: "blue",
    LectureState.PAUSED: "yellow",
    LectureState.FINISHED: "off",
}


class Orchestrator:
    """The lecture loop — a coroutine, not a thread (§4.3). Position (which
    section we're on) is owned here and never read back from PowerPoint."""

    def __init__(
        self,
        script: LectureScript,
        slides: SlideController,
        playback: PlaybackQueue,
        queue: QuestionQueue,
        notifier: Notifier | None = None,
        body: Body | None = None,
        scheduler: Scheduler | None = None,
    ) -> None:
        self._script = script
        self._slides = slides
        self._playback = playback
        self._queue = queue
        # Created fresh in _wait_for_start() each time a lecture actually
        # begins, not injected — a restart (§10.2 End lecture, or a natural
        # finish looping back) gets its own transcript file rather than
        # appending to the previous lecture's. None until the first Start.
        self._transcript: Transcript | None = None
        self._notifier = notifier
        self._body = body
        self._scheduler = scheduler
        self._filler_bank = _FillerBank()
        self.state = LectureStateMachine()
        self.position = 0  # index into script.sections — the source of truth
        self.fault_message: str | None = None
        self.qa_disabled = False  # §11 — set at checkpoint time when Ollama is unreachable

        # §10.1 console display — best-effort snapshot, not a source of truth
        # for anything else.
        self.current_section_text: str | None = None
        self.current_question: str | None = None
        self.current_answer: str | None = None
        self.questions_answered = 0

        self._last_slide_index: int | None = None
        self._slides_broken = False  # set by resume_without_slides() — see _handle_slide_fault
        self._pptx_path: str | None = None
        self._turn_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._own_task: asyncio.Task | None = None
        self._exit_requested = False  # operator "Exit" — see request_exit()
        # Set the instant end_lecture()/request_exit() first cancels
        # run()'s task, cleared once the next lecture actually starts (or
        # never, on Exit, since the process exits). Guards every other
        # operator control — including a second End lecture/Exit click —
        # against firing while _end_lecture_shutdown() is still mid-flight.
        # Without this, a second cancel() lands on the SAME task while it's
        # already inside run()'s except-CancelledError handler (e.g. during
        # the ~7s goodbye wave+speech), raising a second CancelledError
        # that nothing catches — it propagates out of run() uncaught and
        # crashes the whole process. Found live 2026-08-06: End lecture
        # clicked a second time while the first was still tearing down.
        self._shutdown_in_progress = False
        self._idle_event: asyncio.Event | None = None
        self._start_event: asyncio.Event | None = None
        self._skip_event: asyncio.Event | None = None  # operator "Skip question" — see skip_question()
        # Set whenever not paused, cleared by pause()/_on_fault(), set again
        # by resume()/_handle_slide_fault()'s resolution. _narrate() and
        # _drain_queue() await this at every point that isn't already
        # gated by playback (the section-gap sleep, between Q&A turns) so a
        # pause landing there actually holds instead of being silently run
        # through — see the _VALID_TRANSITIONS[PAUSED] comment in state.py
        # for the bug this replaces.
        self._paused_event: asyncio.Event | None = None
        self._fault_future: asyncio.Future | None = None  # operator's slide-fault resolution — see _handle_slide_fault

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
        self._own_task = asyncio.current_task()
        self._pptx_path = pptx_path
        self._idle_event = asyncio.Event()
        self._finish_event = asyncio.Event()
        self._skip_event = asyncio.Event()
        self._paused_event = asyncio.Event()
        self._paused_event.set()
        self._playback.on_idle = self._handle_idle
        self._playback.on_utterance_end = self._handle_utterance_end

        # Models are warmed exactly once per process, ever — not per lecture
        # run. See "Model lifetime" in CLAUDE.md's Gotchas: re-warming would
        # pay the ~10s cold-load cost again on every restart.
        await self._warm_up_models()

        # End lecture (§10.2) *and* a natural finish both return to IDLE and
        # must genuinely be able to Start again without relaunching the
        # process — state.py's IDLE -> READY edge already supports it; this
        # loop is what actually uses it. Exit (below) uses the exact same
        # cancellation path as End lecture but breaks out of the loop
        # instead of re-arming, so it's the only way run() itself returns
        # during normal operation — everything else (a natural finish, an
        # operator End lecture) loops back for another Start.
        while True:
            self._reset_for_new_lecture()
            try:
                await self._wait_for_start()
                await self._narrate(pptx_path)
                # _narrate() already set FINISHED and tore down the
                # body/scheduler inline. Symmetric with
                # _end_lecture_shutdown() below: close PowerPoint and drop
                # back to IDLE so the loop can wait for another Start,
                # rather than main.py treating this as "the process is over"
                # the way it used to.
                await self._finish_and_rearm()
            except asyncio.CancelledError:
                # Operator "End lecture" (§10.2) *and* "Exit" (§10.2 update,
                # 2026-08-03) both cancel this task directly — the simplest
                # way to unblock whichever of the many awaits above we
                # happened to be sitting on (a rendezvous future, on_idle,
                # an executor future, a slide-fault hold, asyncio.sleep, or
                # _wait_for_start()'s own wait for the next Start) without
                # threading a checked flag through every one of them
                # individually. The normal FINISHED completion path never
                # raises this and does its own equivalent teardown via
                # _finish_and_rearm() instead, so _end_lecture_shutdown()
                # must only run here, not in a blanket `finally`.
                await self._end_lecture_shutdown()
                if self._exit_requested:
                    return  # lets main.py's existing shutdown path run
                # Otherwise loop back to _wait_for_start() — cancel() can be
                # called again later without any special re-arming: catching
                # CancelledError here without re-raising leaves this task in
                # a normal (not cancelled) state as far as asyncio is
                # concerned.

    async def _finish_and_rearm(self) -> None:
        """Runs once, right after _narrate() completes a lecture normally.
        Mirrors _end_lecture_shutdown()'s slides-close + back-to-IDLE, just
        without the queue-clear/stiffness-off steps _narrate()'s own
        FINISHED handling and _drain_queue()'s drain-to-empty already
        cover."""
        assert self._loop is not None
        try:
            await self._loop.run_in_executor(None, self._slides.close)
        except SlideControllerError:
            pass
        self.state.transition(LectureState.IDLE)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

    async def _warm_up_models(self) -> None:
        assert self._loop is not None
        # §6.1: IDLE -> READY happens "on successful load + model warm-up".
        # All three models (Piper, Whisper, Ollama) are warmed unconditionally
        # — Mode A/B can differ at runtime without an app restart (§9.1), so
        # STT must already be resident whenever voice first gets used.
        await self._loop.run_in_executor(None, tts.warm)
        await self._loop.run_in_executor(None, llm.warm)
        await self._loop.run_in_executor(None, stt.warm)
        await self._filler_bank.warm(self._loop)

    def _reset_for_new_lecture(self) -> None:
        """Runs immediately after the previous lecture ends (shutdown or
        rearm), before waiting for the next Start — clears the "what's
        currently happening" display fields so READY doesn't show stale
        narration/Q&A. Deliberately leaves questions_answered and the
        transcript alone: those describe the *completed* lecture and stay
        valid (and downloadable) right up until the operator actually
        starts a new one — see _wait_for_start()."""
        self.position = 0
        self._last_slide_index = None
        self._slides_broken = False
        self.current_section_text = None
        self.current_question = None
        self.current_answer = None
        self.fault_message = None
        self.qa_disabled = False
        self._shutdown_in_progress = False
        assert self._paused_event is not None
        self._paused_event.set()

    async def _wait_for_start(self) -> None:
        assert self._loop is not None
        self._start_event = asyncio.Event()

        self.state.transition(LectureState.READY)
        self._set_body_state(LectureState.READY)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

        # READY is "not started" until an operator start (§6.1) — students
        # can connect and see live status while this waits.
        await self._start_event.wait()

        # Only now — the instant a new lecture actually begins, not right
        # after the previous one ended — does the question count reset and
        # a fresh transcript file start. Until this point the console and
        # the /transcript download both still reflect the lecture that just
        # finished.
        self._transcript = Transcript(cfg.paths.sessions_dir)
        self.questions_answered = 0
        print(f"Transcript: {self._transcript.path}")

        self.state.transition(LectureState.NARRATING)
        self._set_body_state(LectureState.NARRATING)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})
        await self._body_lecture_start()

    async def _narrate(self, pptx_path: str) -> None:
        assert self._loop is not None
        self._slides_broken = False

        try:
            await self._loop.run_in_executor(None, self._slides.open, pptx_path)
        except SlideControllerError as e:
            resolution = await self._handle_slide_fault(str(e))
            self._slides_broken = resolution == "resume_without_slides"

        while self.position < len(self._script.sections):
            # Not gated by playback (unlike the rest of this loop, which
            # blocks on _speak()'s idle_event) — a pause landing right here,
            # or during the section-gap sleep below, used to be silently run
            # through: PAUSED -> CHECKPOINT/NARRATING was a "valid" table
            # edge nothing legitimately used, so the loop just kept going
            # without ever going through resume(). See state.py.
            assert self._paused_event is not None
            await self._paused_event.wait()

            section = self._script.sections[self.position]

            if not self._slides_broken and section.slide_index != self._last_slide_index:
                # The very first slide of the lecture already gets its
                # own gaze-at-slides moment from point_slide's one-shot
                # intro gesture (this loop's position==0 check below) --
                # firing the glance here too would be redundant on top of
                # that gesture's own head keyframes.
                is_first_slide = self._last_slide_index is None
                try:
                    await self._loop.run_in_executor(None, self._slides.goto, section.slide_index)
                    self._last_slide_index = section.slide_index
                    if not is_first_slide:
                        # Brief glance at the new slide (user-tuned
                        # 2026-08-05, gestures.slide_glance_hold_s),
                        # concurrent with narration below — hold_s keeps
                        # it from being reconciled away the instant the
                        # new section's first utterance starts (narration's
                        # own gaze context is "class" by default now —
                        # scheduler.py's _CONTEXTS), without delaying
                        # narration itself. Not fired from the except
                        # branch — a fault isn't a real slide change.
                        self._set_gaze("slides", hold_s=cfg.gestures.slide_glance_hold_s)
                except SlideControllerError as e:
                    # _last_slide_index is set to the target *before* the
                    # fault resolves — it's what "Reopen deck" goes back to
                    # (§6.4: "issues goto() for the remembered slide").
                    self._last_slide_index = section.slide_index
                    resolution = await self._handle_slide_fault(str(e))
                    self._slides_broken = resolution == "resume_without_slides"

            self.current_section_text = section.text
            if self.position == 0 and self._body is not None:
                # One-shot intro cue (user-requested 2026-08-05): point at
                # the slides during the lecture's very first line,
                # introducing the topic. point_slide is already in the
                # scheduler's own narration-context pool (scheduler.py's
                # _CONTEXTS), so without this it only had a 1-in-3 random
                # chance of landing on the opening line by luck. Fired
                # directly, same non-blocking pattern as the hello wave in
                # _body_lecture_start() — is_gesturing() is already true by
                # the time this same utterance's on_utterance_start reaches
                # the scheduler's own _fire_gesture(), so its random pick
                # naturally no-ops instead of colliding (§12.4.1's "one
                # gesture at a time" gate, not a new mechanism).
                self._body.gesture("point_slide")
            await self._speak(section.text)

            is_last_section = self.position + 1 >= len(self._script.sections)
            if not is_last_section:
                # Extra pause on top of the last sentence's own trailing
                # silence — makes a section break read as a distinct beat
                # rather than just another inter-sentence gap.
                await asyncio.sleep(cfg.tts.section_gap_ms / 1000)

            assert self._paused_event is not None
            await self._paused_event.wait()

            if section.checkpoint:
                self.state.transition(LectureState.CHECKPOINT)
                self._set_body_state(LectureState.CHECKPOINT)
                if not self._queue.is_empty():
                    # §11 — Ollama unreachable means Q&A is disabled and the
                    # queue stays frozen (nothing drained, nothing dropped)
                    # rather than silently producing an empty, falsely-
                    # "grounded" answer, which is what happened before: a
                    # dead connection made llm.answer() yield zero chunks
                    # with no error surfaced anywhere. Narration is
                    # unaffected either way — it needs no LLM.
                    llm_ok = await self._loop.run_in_executor(None, llm.is_reachable)
                    self.qa_disabled = not llm_ok
                    if llm_ok:
                        await self._drain_queue(section)  # always ends back in NARRATING
                        if is_last_section:
                            # _body_lecture_end() first, FINISHED after —
                            # FINISHED's LED pattern is "off" (_LED_PATTERNS),
                            # and _body_lecture_end() runs the real goodbye
                            # wave+speech for several more seconds. Setting
                            # FINISHED first went dark mid-farewell — found
                            # live 2026-08-05 ("LEDs went off too soon,
                            # before he finished").
                            await self._body_lecture_end()
                            self.state.transition(LectureState.FINISHED)
                            self._set_body_state(LectureState.FINISHED)
                            self.position += 1
                            return
                    else:
                        await self._notify_all({"type": "qa_unavailable"})
                        if not is_last_section:
                            self.state.transition(LectureState.NARRATING)
                            self._set_body_state(LectureState.NARRATING)
                elif not is_last_section:
                    self.state.transition(LectureState.NARRATING)
                    self._set_body_state(LectureState.NARRATING)
                # else: last section, empty queue — stay in CHECKPOINT; the
                # loop ends below and FINISHED is entered from there.

            self.position += 1

        # Same ordering fix as the is_last_section branch above: run the
        # real goodbye wave+speech before flipping LEDs to FINISHED's
        # "off" pattern, not before.
        await self._body_lecture_end()
        self.state.transition(LectureState.FINISHED)
        self._set_body_state(LectureState.FINISHED)

    async def _handle_slide_fault(self, message: str) -> str:
        """A COM fault from either open() or goto() (§6.4) — the orchestrator
        genuinely holds here, in PAUSED, until the operator resolves it via
        reopen_deck() or resume_without_slides() (end_lecture() instead
        cancels run() outright, which unwinds this await like any other).
        Returns the resolution string; the caller decides what to do with
        "resume_without_slides" (skip further slide control), while
        "reopen" is fully handled here — the deck is reopened and re-goto()'d
        to the remembered slide before this returns, so callers never retry
        the failed call themselves."""
        self._on_fault(message)
        await self._notify_all({"type": "fault", "message": message})

        assert self._loop is not None
        self._fault_future = self._loop.create_future()
        try:
            resolution = await self._fault_future
        finally:
            self._fault_future = None

        if resolution == "reopen":
            try:
                await self._loop.run_in_executor(None, self._slides.open, self._pptx_path)
                if self._last_slide_index is not None:
                    await self._loop.run_in_executor(None, self._slides.goto, self._last_slide_index)
            except SlideControllerError as e:
                # Reopening itself failed — hold again with a fresh fault,
                # same three options, rather than pretending it succeeded.
                return await self._handle_slide_fault(str(e))

        self.fault_message = None
        self.state.resume_from_pause()
        self._playback.resume()
        self._set_body_state(self.state.state)
        if self._paused_event is not None:
            self._paused_event.set()
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})
        return resolution

    async def _end_lecture_shutdown(self) -> None:
        """Runs once, from run()'s CancelledError handler, when the operator
        calls end_lecture() (§10.2) mid-run. The normal FINISHED completion
        path already does its own equivalent teardown inline and never
        raises CancelledError, so this never double-runs against it."""
        assert self._loop is not None
        self._playback.stop_now()
        # If this shutdown was triggered from PAUSED, pause() had cleared
        # PlaybackQueue's resume_event (stop_after_current()) and nothing
        # since has set it — _body_lecture_end()'s goodbye line would enqueue
        # onto a queue whose play thread is permanently parked on
        # resume_event.wait(), so _wait_until_played() never returns and
        # this hangs forever (found live: End lecture/Exit called while
        # PAUSED never completed, force-killed after 20+s with the server
        # otherwise still responsive). stop_now() first (flushes/stops while
        # the play thread is still parked, so nothing stale from before the
        # pause gets a chance to play), *then* resume(), so the goodbye
        # utterance enqueued below actually gets picked up.
        self._playback.resume()
        await self._body_lecture_end()
        try:
            await self._loop.run_in_executor(None, self._slides.close)
        except SlideControllerError:
            pass
        for entry in self._queue.clear():
            await self._notify(entry.student.student_id, {"type": "queue_cleared"})
        self.state.force_idle()
        self._set_body_state(LectureState.IDLE)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

    async def _speak(self, text: str) -> None:
        assert self._loop is not None and self._idle_event is not None
        self._idle_event.clear()
        # Captured once, before synthesis starts — not re-read at enqueue
        # time. Synthesis below can take real wall-clock time (a whole
        # section, before anything is enqueued), and skip_section() bumps
        # self._turn_id + flushes the moment it's clicked. Reading
        # self._turn_id at enqueue time would stamp these utterances with
        # whatever turn_id skip_section() had already bumped to — which its
        # own flush() had just whitelisted — silently defeating the skip
        # (found live: skip section appeared to do nothing until the
        # section finished on its own). Same fix as _answer_question()'s
        # my_turn_id, applied here for the same reason.
        my_turn_id = self._turn_id

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
                Utterance(turn_id=my_turn_id, seq=seq, wav_path=wav_path, kind="narration")
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
        self._set_body_state(LectureState.PAUSED)
        if self._paused_event is not None:
            self._paused_event.clear()
        self.fault_message = message
        print(f"[FAULT] {message}")  # operator console alert lands in Phase 5

    def _set_gaze(self, target: str, hold_s: float = 0.0) -> None:
        """Every gaze() call in this class goes through here, not
        self._body.gaze() directly — the scheduler is the single owner of
        gaze so a change requested while a head-moving gesture is in
        flight is postponed and retried rather than silently colliding
        with it (see Scheduler.set_gaze()). Falls back to a direct call
        only if there's genuinely no scheduler (body without gestures
        enabled) — main.py always builds them as a pair otherwise.
        hold_s: see Scheduler.set_gaze(); ignored in the no-scheduler
        fallback since holding only means anything against the
        scheduler's own reconciliation."""
        if self._scheduler is not None:
            self._scheduler.set_gaze(target, hold_s=hold_s)
        elif self._body is not None:
            self._body.gaze(target)

    def _set_body_state(self, state: LectureState) -> None:
        if self._body is None:
            return
        self._body.leds(_LED_PATTERNS.get(state, "off"))
        if state in (LectureState.CHECKPOINT, LectureState.PAUSED):
            # §12.4.2 — no utterance is playing in these states, so nothing
            # drives gaze via the scheduler's on_utterance_start hook. Routed
            # through the scheduler (not self._body.gaze() directly) so a
            # gesture still in flight from just before the transition
            # (point_slide/thinking/acknowledge — all carry their own head
            # keyframe) postpones this instead of colliding with it; the
            # scheduler's own interval tick retries it even though nothing
            # is playing here to trigger a retry otherwise.
            self._set_gaze("class")

    async def _body_lecture_start(self) -> None:
        assert self._loop is not None
        if self._body is not None:
            # Unlike gesture()/gaze() (explicitly non-blocking by the Body
            # protocol's own contract), posture()/stiffness() carry no such
            # promise — ConsoleBody's versions are instant prints, but
            # NaoBody's are real HTTP calls that block for real seconds
            # (up to nao.timeouts.posture_s = 15s). Calling either bare on
            # the event loop thread would freeze the whole app — WebSocket
            # pushes, the HTTP server, everything — for that long. Routed
            # through the executor like every other genuinely-blocking call
            # in this file (TTS synthesis, slide COM, LLM reachability).
            # §12.5 lecture-start sequence: volume, then posture, then
            # stiffness. volume() is non-blocking (Body protocol contract,
            # like gesture/gaze/leds) so it isn't run_in_executor'd.
            self._body.volume(cfg.nao.volume)
            await self._loop.run_in_executor(None, self._body.posture, "Sit")
            await self._loop.run_in_executor(None, self._body.stiffness, True)
            # Fired *before* scheduler.start(), so the scheduler's
            # on_utterance_start hook is still inert (its own self._loop is
            # None until start() sets it) — the greeting utterance below
            # can't collide with a random gesture pick, no is_gesturing()
            # race to worry about. gesture() itself is non-blocking; the
            # wave plays out concurrently with (not before) the spoken line.
            self._set_gaze("class")
            self._body.gesture("wave")
        # Spoken regardless of whether a Body is configured — the greeting
        # is a narration feature; the wave above is just its (optional)
        # visual accompaniment.
        await self._say(self._filler_bank.hello_text)
        if self._scheduler is not None:
            self._scheduler.start()

    async def _body_lecture_end(self) -> None:
        """Shared by both ways a lecture can end — a natural finish
        (_narrate()) and an operator End lecture/Exit (_end_lecture_
        shutdown()) — so the goodbye wave+line only needs writing once."""
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._body is not None:
            # A narration gesture (point_slide/explain_open/beat/thinking/
            # acknowledge) fired moments before the lecture ended can still
            # be physically in flight — scheduler.stop() only stops
            # *future* picks, not one already underway. Wait on
            # is_gesturing() directly (the same general "is anything
            # currently in flight" flag _fire_gesture() itself checks
            # before firing anything new), not the narrower
            # head_owned_by_gesture() (which also requires the in-flight
            # gesture to touch the head — that's the right check for
            # postponing a *gaze* change, the wrong one for "is it safe to
            # fire wave on top of this"). Found live 2026-08-06: NAO fired
            # the goodbye wave while a narration gesture was still
            # physically executing — two moves at once. is_gesturing()'s
            # own duration estimate can itself run short (already confirmed
            # for wave — see its duration_s override in gestures.yaml — and
            # not yet re-measured on hardware for the others), so this
            # wait is best-effort insurance, not a guarantee; capped so a
            # stuck estimate can never hang shutdown indefinitely.
            elapsed = 0.0
            while self._body.is_gesturing() and elapsed < 5.0:
                await asyncio.sleep(0.1)
                elapsed += 0.1
            self._set_gaze("class")
            self._body.gesture("wave")
        await self._say(self._filler_bank.goodbye_text)
        if self._body is not None:
            # gesture() doesn't block, and unlike hello there's nothing
            # else left to narrate — wait for the wave to actually finish
            # so stiffness(False) doesn't cut it off mid-motion. Capped so
            # a stuck is_gesturing() can never hang shutdown indefinitely.
            elapsed = 0.0
            while self._body.is_gesturing() and elapsed < 5.0:
                await asyncio.sleep(0.1)
                elapsed += 0.1
            assert self._loop is not None
            # NAOqi's own rest() (Body.sleep()) was tried in its place
            # 2026-08-05 and reverted the same day: confirmed live twice
            # (once standalone, once in this exact flow) that from a
            # seated posture it reaches the identical released-stiffness
            # end state as this call, just ~5s slower — no visible
            # crouch/repositioning benefit on this robot to justify the
            # extra wait.
            await self._loop.run_in_executor(None, self._body.stiffness, False)

    # ---- Q&A (§6.3, §7) ---------------------------------------------

    async def _drain_queue(self, section: Section) -> None:
        self.state.transition(LectureState.ANSWERING)
        self._set_body_state(LectureState.ANSWERING)
        self.qa_disabled = False  # reaching here means llm.is_reachable() just confirmed OK
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})
        await self._notify_all({"type": "qa_available"})
        await self._say(self._filler_bank.qa_start_text)

        while not self._queue.is_empty():
            # Same class of gap as _narrate()'s guards — between two turns
            # there's no playback in flight yet, so a pause landing exactly
            # here needs an explicit check too.
            assert self._paused_event is not None
            await self._paused_event.wait()

            entry = self._queue.pop_next()
            await self._broadcast_queue_positions()
            await self._run_turn(entry, section)

        self.current_question = None
        self.current_answer = None
        await self._say(self._filler_bank.qa_end_text)
        self.state.transition(LectureState.NARRATING)
        self._set_body_state(LectureState.NARRATING)
        await self._notify_all({"type": "lecture_status", "state": self.state.state.name})

    async def _run_turn(self, entry: QueueEntry, section: Section) -> None:
        assert self._skip_event is not None
        student = entry.student
        history: list[dict] = []
        follow_up_depth = 0
        self.current_question = None
        self.current_answer = None

        prompted = await self._prompt_and_await_question(student.student_id)
        if prompted is None:
            return  # abandoned (force_skip) before ever asking anything
        question, input_mode = prompted
        self.current_question = question

        while True:
            self._turn_id += 1
            self._skip_event.clear()
            answer_text, grounded, was_skipped = await self._answer_question(question, history, section)

            if was_skipped:
                # Operator Skip question (§10.2) — move to the next student;
                # there's no completed answer to offer Reply/Done on.
                self._record_transcript(
                    section, student, question, answer_text, grounded, "skipped", follow_up_depth, input_mode
                )
                break

            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": answer_text})

            allow_reply = (follow_up_depth + 1) < cfg.llm.followup_turn_cap
            resolution, next_question, next_mode = await self._offer_reply(
                student.student_id, allow_reply, answer_text
            )

            self._record_transcript(
                section, student, question, answer_text, grounded, resolution, follow_up_depth, input_mode
            )

            if resolution != "replied":
                break
            question = next_question
            self.current_question = question
            input_mode = next_mode
            follow_up_depth += 1

    async def _answer_question(
        self, question: str, history: list[dict], section: Section
    ) -> tuple[str, bool, bool]:
        """Returns (answer_text, grounded, was_skipped)."""
        assert self._loop is not None and self._skip_event is not None
        # Captured once: skip_question() may bump self._turn_id from outside
        # partway through this call (to invalidate anything *else* queued
        # after this point) — every enqueue and the final wait below must
        # stay pinned to the turn this call actually started, not whatever
        # self._turn_id has become by the time it returns.
        my_turn_id = self._turn_id
        seq = 0

        filler_wav = self._filler_bank.next_stage1()
        self._playback.enqueue(Utterance(turn_id=my_turn_id, seq=seq, wav_path=filler_wav, kind="filler"))
        seq += 1

        # Checked before stage-2, which would otherwise speak the question
        # back verbatim with no guardrail of its own — the LLM's guardrail
        # (§7.3) only governs what NAO *answers*, not what it *repeats*.
        blocked = moderation.is_inappropriate(question)

        if not blocked and len(question.split()) <= cfg.timings.question_repeat_max_words:
            stage2_text = self._filler_bank.stage2(question)
            stage2_wav = await self._loop.run_in_executor(None, tts.synthesize, stage2_text)
            self._playback.enqueue(
                Utterance(turn_id=my_turn_id, seq=seq, wav_path=stage2_wav, kind="filler")
            )
            seq += 1

        if blocked:
            # No LLM round-trip either — a flagged question gets a fixed,
            # scripted decline, not a repeat and not a model-generated reply.
            answer_text = self._filler_bank.decline_text
            wav_path = await self._loop.run_in_executor(None, tts.synthesize, answer_text)
            self._playback.enqueue(Utterance(turn_id=my_turn_id, seq=seq, wav_path=wav_path, kind="answer"))
            seq += 1
            grounded = True
        else:
            position = self._position_marker(section)
            answer_chunks: list[str] = []
            async for sentence in sentences.split_stream_async(
                llm.answer(question, history, self._script.full_narration, position)
            ):
                if self._skip_event.is_set():
                    break  # operator Skip question — stop feeding the queue, unwind below
                answer_chunks.append(sentence)
                wav_path = await self._loop.run_in_executor(None, tts.synthesize, sentence)
                self._playback.enqueue(Utterance(turn_id=my_turn_id, seq=seq, wav_path=wav_path, kind="answer"))
                seq += 1

            answer_text = " ".join(answer_chunks)
            lowered = answer_text.lower()
            grounded = "isn't covered" not in lowered and "not covered" not in lowered

        self.current_answer = answer_text

        if self._skip_event.is_set():
            return answer_text, grounded, True

        # Wait for the specific last utterance we enqueued to actually
        # finish playing — not PlaybackQueue.on_idle, which can fire while
        # this coroutine is still producing more (see _wait_until_played) —
        # but race it against a skip landing in the instant right after the
        # check above: stop_now() drops anything still queued without ever
        # firing on_utterance_end for the dropped items, so waiting
        # unconditionally on `seq - 1` here could otherwise hang forever.
        wait_task = asyncio.ensure_future(self._wait_until_played(my_turn_id, seq - 1))
        skip_task = asyncio.ensure_future(self._skip_event.wait())
        done, pending = await asyncio.wait({wait_task, skip_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        return answer_text, grounded, skip_task in done

    async def _offer_reply(
        self, student_id: str, allow_reply: bool, answer_text: str
    ) -> tuple[str, str | None, str | None]:
        if not allow_reply:
            await self._notify(student_id, {"type": "answered", "answer": answer_text, "reply_allowed": False})
            await self._notify(student_id, {"type": "turn_ended"})
            return "done", None, None

        await self._notify(
            student_id,
            {
                "type": "answered",
                "answer": answer_text,
                "reply_allowed": True,
                "window_s": cfg.timings.reply_window_s,
            },
        )
        choice = await self._await_choice(student_id, cfg.timings.reply_window_s)
        if choice != "reply":
            # The 5 s/30 s windows are enforced here, server-side — the
            # client's own countdown is cosmetic. Either way the client
            # needs an explicit push once the window closes, or it's left
            # showing Reply/Done (or a reply box) with no way out.
            await self._notify(student_id, {"type": "turn_ended"})
            return ("timeout" if choice is None else "done"), None, None

        await self._notify(student_id, {"type": "reply_window", "window_s": cfg.timings.compose_window_s})
        reply = await self._await_reply_text(student_id, cfg.timings.compose_window_s)
        if reply is None:
            await self._notify(student_id, {"type": "turn_ended"})
            return "timeout", None, None
        reply_text, reply_mode = reply
        return "replied", reply_text, reply_mode

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
        input_mode: str,
    ) -> None:
        if follow_up_depth == 0:
            self.questions_answered += 1
        entry = TranscriptEntry(
            timestamp=datetime.now(),
            slide_index=section.slide_index,
            section_index=section.section_index + 1,
            student_label=student.label,
            input_mode=input_mode,
            question_text=question,
            answer_text=answer_text,
            grounded=grounded,
            resolution=resolution,
            follow_up_depth=follow_up_depth,
        )
        assert self._transcript is not None  # _wait_for_start() creates it before any turn can run
        self._transcript.record(entry)

    # ---- cross-request rendezvous ------------------------------------

    async def _prompt_and_await_question(self, student_id: str) -> tuple[str, str] | None:
        await self._notify(student_id, {"type": "your_turn"})
        if self._loop is not None and self._notifier is not None and not self._notifier.is_connected(student_id):
            # Already gone before their turn even started (left minutes
            # ago) — no fresh disconnect event will ever fire for this one,
            # so start the same grace timer directly instead of waiting on
            # notify_disconnect().
            self._loop.create_task(self._disconnect_grace_timer(student_id))
        result = await self._await("question", student_id)
        if result is None:
            return None  # force_skip() abandoned this turn
        await self._notify(student_id, {"type": "submitted"})
        return result["text"], result.get("mode", "typed")

    async def _await_choice(self, student_id: str, timeout: float) -> str | None:
        try:
            return await self._await("choice", student_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def _await_reply_text(self, student_id: str, timeout: float) -> tuple[str, str] | None:
        try:
            result = await self._await("reply", student_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if result is None:  # force_skip() abandoned this reply
            return None
        return result["text"], result.get("mode", "typed")

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

    # ---- operator console (§10.1) --------------------------------------

    @property
    def transcript(self) -> Transcript | None:
        """The current lecture's transcript (§10.1 download link) — None
        only in the brief window before the very first Start of the
        process. A fresh one replaces it each time _wait_for_start()
        actually begins a new lecture."""
        return self._transcript

    @property
    def body(self) -> Body | None:
        """The configured Body (§10.1 component health) — None unless
        content/gestures.yaml is real (main.py). Read-only access for
        routes_operator.py's health check; nothing else should reach
        into this from outside the class."""
        return self._body

    def snapshot(self) -> dict:
        """Everything the operator console's display needs in one shot —
        a plain REST poll (§10.1 doesn't require a push channel of its own,
        and the existing student WebSocket is keyed by student_id, not
        worth repurposing for a single console client)."""
        section = None
        if 0 <= self.position < len(self._script.sections):
            section = self._script.sections[self.position]
        return {
            "state": self.state.state.name,
            "slide_index": section.slide_index if section is not None else None,
            # section.index, not section.section_index — the latter resets
            # to 0 at the start of every slide (it's "the Nth section of
            # *this* slide", used by _position_marker() for the LLM). Using
            # it here against a deck-wide total_sections made the console's
            # counter jump backward at every slide boundary. section.index
            # is the deck-wide position that's actually comparable to
            # total_sections — and it's already equal to self.position by
            # construction, so this and slide_index now advance in lockstep.
            "section_index": (section.index + 1) if section is not None else None,
            "total_sections": len(self._script.sections),
            "total_slides": self._script.slide_count,
            "current_section_text": self.current_section_text,
            "fault_message": self.fault_message,
            "fault_pending": self._fault_future is not None,
            "qa_disabled": self.qa_disabled,
            "current_question": self.current_question,
            "current_answer": self.current_answer,
            "questions_answered": self.questions_answered,
            "queue": self._queue.list_entries(),
        }

    # ---- called by web routes, on the event loop thread (§4.3) --------

    def start(self) -> bool:
        """READY -> NARRATING (§6.1). False if not currently waiting to start."""
        if self.state.state != LectureState.READY or self._start_event is None:
            return False
        self._start_event.set()
        return True

    def pause(self) -> bool:
        """Operator Pause (§10.2) — distinct from a slide fault: fault_message
        stays None, so resume() (not reopen_deck()/resume_without_slides())
        is the correct way back. stop_after_current() leaves everything
        already queued untouched, so resume() picks up exactly where it
        left off with no re-synthesis or re-goto needed."""
        if self._shutdown_in_progress:
            return False
        if self.state.state not in (LectureState.NARRATING, LectureState.CHECKPOINT, LectureState.ANSWERING):
            return False
        self._playback.stop_after_current()
        self.state.transition(LectureState.PAUSED)
        self._set_body_state(LectureState.PAUSED)
        if self._paused_event is not None:
            self._paused_event.clear()
        self._notify_all_soon({"type": "lecture_status", "state": self.state.state.name})
        return True

    def resume(self) -> bool:
        if self._shutdown_in_progress:
            return False
        if self.state.state != LectureState.PAUSED or self.fault_message is not None:
            return False
        self.state.resume_from_pause()
        self._playback.resume()
        self._set_body_state(self.state.state)
        if self._paused_event is not None:
            self._paused_event.set()
        self._notify_all_soon({"type": "lecture_status", "state": self.state.state.name})
        return True

    def skip_section(self) -> bool:
        """Operator Skip section (§10.2) — abandons whatever's left of the
        current section and advances. Also clears the queue: the questions
        in it were about material that's now being skipped (§10.2).
        Restricted to NARRATING/CHECKPOINT — mid Q&A (ANSWERING), use Skip
        question first; abandoning a section while a specific student is
        actively being answered has no single sensible meaning.

        Relies on PlaybackQueue's own mechanics rather than touching the
        main narration loop directly: flush+stop_now empties both of its
        internal queues, which fires on_idle, which is exactly what
        _speak() is awaiting — the loop then falls through to the
        checkpoint/advance logic on its own, with the queue already empty."""
        if self._shutdown_in_progress:
            return False
        if self.state.state not in (LectureState.NARRATING, LectureState.CHECKPOINT):
            return False
        self._turn_id += 1
        self._playback.flush(self._turn_id)
        self._playback.stop_now()
        self._clear_queue_and_notify()
        return True

    def skip_question(self) -> bool:
        """Operator Skip question (§10.2) — abandons the active turn and
        moves to the next student. Two cases: we're waiting on the student
        themselves (their turn to submit, or the Reply/Done/compose
        windows) — force_skip() already resolves that as an abandonment.
        Or an answer is actively being generated/played — cut the LLM
        stream and the audio, and let _answer_question's own skip-awareness
        unwind cleanly (see there) rather than reaching into _run_turn from
        the outside."""
        if self._shutdown_in_progress:
            return False
        if self.state.state != LectureState.ANSWERING:
            return False
        if self._awaiting_future is not None and not self._awaiting_future.done():
            return self.force_skip()
        if self._skip_event is None:
            return False
        self._turn_id += 1
        self._playback.flush(self._turn_id)
        self._playback.stop_now()
        llm.cancel()
        self._skip_event.set()
        return True

    def clear_queue(self) -> bool:
        """Operator Clear queue (§10.2) — drops all *waiting* students, who
        are notified. The active turn (already popped off the queue)
        completes normally; use Skip question to end that one instead."""
        if self._shutdown_in_progress:
            return False
        if self._queue.is_empty():
            return False
        self._clear_queue_and_notify()
        return True

    def _clear_queue_and_notify(self) -> None:
        for entry in self._queue.clear():
            if self._loop is not None:
                self._loop.create_task(self._notify(entry.student.student_id, {"type": "queue_cleared"}))

    def reopen_deck(self) -> bool:
        """Operator's first option after a slide fault (§6.4) — reopens
        lecture.pptx and re-goto()'s the remembered slide. False if no
        fault is currently being held."""
        return self._resolve_fault("reopen")

    def resume_without_slides(self) -> bool:
        """Operator's second option after a slide fault (§6.4) — narration
        and Q&A continue; slide control simply stops being attempted until
        a later reopen_deck() succeeds. False if no fault is currently held."""
        return self._resolve_fault("resume_without_slides")

    def _resolve_fault(self, resolution: str) -> bool:
        if self._shutdown_in_progress:
            return False
        if self._fault_future is None or self._fault_future.done():
            return False
        self._fault_future.set_result(resolution)
        return True

    def end_lecture(self) -> bool:
        """Operator End lecture (§10.2) — works from any active state.
        Cancels run()'s own task, which unblocks whichever await it's
        currently sitting on (see run()'s CancelledError handler for why
        this is simpler than threading a checked flag through every await
        site individually). llm.cancel() is a best-effort head start on the
        one case — an in-flight LLM stream — that cancellation alone
        wouldn't interrupt promptly, since it's driven from a blocking
        executor call that only notices cancellation between chunks.

        Guarded against a second call landing while the first is still
        tearing down (self._shutdown_in_progress) — _end_lecture_shutdown()
        takes several real seconds (the goodbye wave+speech), and state
        doesn't reach IDLE until it's done, so the state-based guard above
        alone doesn't catch a double-click during that window. A second
        .cancel() on the same task there raises a second CancelledError
        that nothing catches, crashing the whole process — found live
        2026-08-06 from a real double End-lecture click."""
        if self._own_task is None or self.state.state in (LectureState.IDLE, LectureState.FINISHED):
            return False
        if self._shutdown_in_progress:
            return False
        self._shutdown_in_progress = True
        llm.cancel()
        self._own_task.cancel()
        return True

    def request_exit(self) -> bool:
        """Operator Exit (§10.2 update, 2026-08-03) — closes the whole
        application, not just the current lecture. Shares End lecture's
        cancellation mechanism (same teardown: audio stopped, PowerPoint
        closed, queue cleared) but sets _exit_requested first so run()'s
        CancelledError handler breaks out of its loop instead of re-arming
        for another Start; run() then genuinely returns, which lets
        main.py's existing shutdown path stop the web server and exit the
        process. Unlike end_lecture(), this works from *any* state
        including IDLE/READY — run() is always blocked somewhere (waiting
        for Start, narrating, or paused) once it has started, so there's
        always something to cancel.

        Guarded against a second call (from Exit or End lecture) landing
        while a shutdown is already in progress — see end_lecture()'s own
        docstring for why a double-cancel on the same task crashes the
        process. If End lecture was already clicked, Exit is rejected too
        until that re-arms back to READY; click Exit again from there."""
        if self._own_task is None:
            return False
        if self._shutdown_in_progress:
            return False
        self._shutdown_in_progress = True
        self._exit_requested = True
        llm.cancel()
        self._own_task.cancel()
        return True

    def _notify_all_soon(self, message: dict) -> None:
        """Fire-and-forget broadcast from a synchronous operator method
        (called directly from a route handler on the event loop thread,
        same pattern as force_skip()'s own notifications)."""
        if self._loop is not None:
            self._loop.create_task(self._notify_all(message))

    def submit_question(self, student_id: str, text: str, mode: str = "typed") -> bool:
        return self._resolve("question", student_id, {"text": text, "mode": mode})

    def choose_reply(self, student_id: str) -> bool:
        return self._resolve("choice", student_id, "reply")

    def choose_done(self, student_id: str) -> bool:
        return self._resolve("choice", student_id, "done")

    def submit_reply_text(self, student_id: str, text: str, mode: str = "typed") -> bool:
        return self._resolve("reply", student_id, {"text": text, "mode": mode})

    def force_skip(self) -> bool:
        """Emergency unblock for a turn stuck waiting on a student who's
        gone (closed browser, disconnected) — resolves whatever is
        currently pending as an abandonment. Minimal stand-in for the real
        Phase 5 'Skip question' control (§10.2): no auth, no UI, just this.
        False if nothing is currently pending."""
        if self._awaiting_future is None or self._awaiting_future.done():
            return False
        stale_student = self._awaiting_student
        if self._awaiting_kind == "choice":
            self._awaiting_future.set_result("done")
        else:  # "question" or "reply" — both treat None as abandonment
            self._awaiting_future.set_result(None)
        if self._loop is not None and stale_student is not None:
            # In case that browser reconnects later — reset it to idle
            # instead of leaving it showing a turn that's already over.
            self._loop.create_task(self._notify(stale_student, {"type": "turn_ended"}))
        return True

    def notify_disconnect(self, student_id: str) -> None:
        """Called from the WebSocket layer the moment a student's browser
        drops. Found live: closing the browser mid-turn left the queue
        stuck forever, since the initial question wait has no timeout by
        design. Only actually starts a grace timer if we're waiting on
        *this specific* student right now — a disconnect while just queued,
        or after their turn is already over, needs no action."""
        if self._awaiting_student != student_id or self._loop is None:
            return
        self._loop.create_task(self._disconnect_grace_timer(student_id))

    async def _disconnect_grace_timer(self, student_id: str) -> None:
        await asyncio.sleep(cfg.timings.disconnect_grace_s)
        # Re-check at expiry, not just at start: they may have reconnected
        # and completed the action already, or the orchestrator may have
        # moved on to a different student entirely by now.
        if (
            self._awaiting_student == student_id
            and self._awaiting_future is not None
            and not self._awaiting_future.done()
        ):
            self.force_skip()

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
        self.hello_text = sections.get("hello", [""])[0]
        self.goodbye_text = sections.get("goodbye", [""])[0]
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
