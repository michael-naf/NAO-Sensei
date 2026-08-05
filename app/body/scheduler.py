from __future__ import annotations

import asyncio
import random
import time

from app.audio.playback_queue import PlaybackQueue, Utterance
from app.body.body import Body
from app.body.gesture_library import GestureLibrary
from app.config import cfg

# §12.4.2 — gaze target and candidate gesture set per kind of utterance
# currently playing. Utterance.kind ('narration' | 'filler' | 'answer') is
# all any PlaybackQueue callback ever exposes, and it's enough: narration
# maps to NARRATING, filler to "ANSWERING - filler playing", answer to
# "ANSWERING - answer playing". No separate orchestrator-state wiring needed
# for gaze/gestures specifically (§4.4's own design already promises this).
#
# narration's gaze target was "slides" until 2026-08-05 -- user-requested
# change: NAO should face the class most of the time, with a brief
# deliberate glance at the slides specifically when the slide changes
# (Orchestrator._narrate(), right after goto()), not as the default
# baseline gaze for the entire time it's talking. point_slide dropped
# from narration's gesture pool the same day -- it's now a guaranteed
# one-shot at the lecture's opening line (_narrate()'s position==0
# check), not a random pick; leaving it in this pool risked it recurring
# again later by chance, which read as redundant when it happened right
# before the goodbye.
_CONTEXTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "narration": ("class", ("explain_open", "beat")),
    "filler": ("class", ("thinking",)),
    "answer": ("class", ("explain_open", "beat", "acknowledge")),
}

# Casual class-gaze scanning (2026-08-05): cycled on the same periodic
# tick that already drives gesture picks (_loop_body()) rather than a
# second timer. Small side-to-side variation so "looking at the class"
# doesn't read as a single fixed stare -- see their angles' own comment
# in gestures.yaml.
_CLASS_GAZE_VARIANTS = ("class", "class_left", "class_right")


class Scheduler:
    """Fires one gesture at each utterance's start, then re-fires every
    gestures.interval_s (jittered) while playback continues (§12.4.1).
    Never touches the audio path — Body.gesture() never raises, so a
    gesture failure here is structurally non-fatal, not just a promise."""

    def __init__(self, playback: PlaybackQueue, body: Body, library: GestureLibrary) -> None:
        self._playback = playback
        self._body = body
        self._library = library
        self._enabled = cfg.gestures.enabled
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._current_gestures: tuple[str, ...] = ("rest",)
        self._last_gesture: str | None = None
        # What gaze should be right now, vs. what was actually last sent —
        # see set_gaze()/_reconcile_gaze(). Gaze carries real communicative
        # meaning (where NAO is "looking"), so a collision with an in-flight
        # gesture postpones it to the next reconciliation point instead of
        # dropping it outright.
        self._desired_gaze: str | None = None
        self._current_gaze: str | None = None
        # A held glance (e.g. the slide-change cue) that must survive
        # narration's own per-utterance gaze reconciliation without
        # blocking playback — see set_gaze()'s hold_s parameter.
        self._gaze_hold_until: float = 0.0

        playback.on_utterance_start = self._handle_utterance_start

    def start(self) -> None:
        if not self._enabled:
            return
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._loop_body())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        # Also deactivates _handle_utterance_start's guard, not just the
        # periodic interval loop above — found live, 2026-08-04: without
        # this, an utterance played *after* stop() (e.g. the goodbye line
        # enqueued by _body_lecture_end(), right after it calls
        # scheduler.stop()) still ran _on_utterance_start_main, which
        # unconditionally overwrites gaze even though is_gesturing()
        # correctly skipped picking a new gesture — NAO's gaze snapped to
        # "slides" mid-farewell instead of staying on the class. Matches
        # the same None-until-start() state _loop already has before the
        # first start() ever runs.
        self._loop = None

    def _handle_utterance_start(self, u: Utterance) -> None:
        # Fires on the play thread — marshal via call_soon_threadsafe before
        # touching any state (concurrency rule 2).
        if self._enabled and self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_utterance_start_main, u)

    def _on_utterance_start_main(self, u: Utterance) -> None:
        gaze, gestures = _CONTEXTS.get(u.kind, ("class", ("rest",)))
        self._current_gestures = gestures
        self.set_gaze(gaze)
        self._fire_gesture()

    def set_gaze(self, target: str, hold_s: float = 0.0) -> None:
        """Request a gaze change (2026-08-04 redesign). Gaze carries real
        meaning — where NAO is "looking" — so a collision with an
        in-flight gesture postpones it instead of dropping it: remembered
        in _desired_gaze, applied the moment _reconcile_gaze() next gets a
        chance to run (the next utterance, or the next interval tick —
        see _loop_body()) and the colliding gesture has finished. Also the
        single owner of gaze for Orchestrator._set_body_state() and
        _body_lecture_start()/_body_lecture_end(), not just this class's
        own on_utterance_start hook — those call sites collide with an
        in-flight point_slide/thinking/acknowledge just as easily.

        hold_s (2026-08-05, user-requested): a deliberate glance (e.g. the
        slide-change cue) that must survive narration's own per-utterance
        reconciliation for hold_s seconds, without blocking narration
        itself — narration keeps playing normally underneath. A hold_s=0
        call (the common case — narration/filler/answer context switches,
        _set_body_state()) is silently ignored while a previous hold is
        still active, rather than cutting the held glance short; the next
        such call after the hold expires goes through normally, which in
        practice is the following utterance's own on_utterance_start."""
        now = time.monotonic()
        if hold_s > 0:
            self._desired_gaze = target
            self._gaze_hold_until = now + hold_s
            self._reconcile_gaze()
            return
        if now < self._gaze_hold_until:
            return
        self._desired_gaze = target
        self._reconcile_gaze()

    def _reconcile_gaze(self) -> None:
        if self._desired_gaze is None or self._desired_gaze == self._current_gaze:
            return
        if self.head_owned_by_gesture():
            return  # still postponed — retried on the next tick
        self._body.gaze(self._desired_gaze)
        self._current_gaze = self._desired_gaze

    def head_owned_by_gesture(self) -> bool:
        """True while a gesture that carries its own HeadYaw/HeadPitch
        keyframe is actively in flight. _fire_gesture()'s own
        is_gesturing() guard already refuses to pick anything new for as
        long as this is true, which is also what makes a pending gaze
        change implicitly take priority over a competing gesture pick —
        neither can proceed until the same is_gesturing() gate clears, so
        there's nothing extra to arbitrate between them."""
        return self._body.is_gesturing() and self._touches_head(self._last_gesture)

    def _touches_head(self, gesture_name: str | None) -> bool:
        if gesture_name is None:
            return False
        gesture = self._library.gestures.get(gesture_name)
        if gesture is None:
            return False
        return any(
            "HeadYaw" in kf.angles or "HeadPitch" in kf.angles for kf in gesture.keyframes
        )

    async def _loop_body(self) -> None:
        while True:
            interval = random.uniform(*cfg.gestures.interval_s)
            await asyncio.sleep(interval)
            # Catches up a postponed gaze even when nothing new is playing
            # (e.g. sitting in CHECKPOINT/PAUSED, no utterance-start events
            # to trigger it otherwise).
            self._reconcile_gaze()
            self._maybe_scan_class()
            if self._playback.is_playing():
                self._fire_gesture()

    def _maybe_scan_class(self) -> None:
        # Only nudges when we're already *supposed* to be looking at the
        # class in some form -- never overrides a deliberate "slides"
        # glance (the slide-change cue, or an in-flight point_slide/
        # explain_open gesture carrying its own head keyframe -- that
        # case is already excluded since a gesture in flight means
        # _desired_gaze wasn't reconciled to a class variant yet, or the
        # gesture's own end will trigger the next reconciliation).
        if self._desired_gaze not in _CLASS_GAZE_VARIANTS:
            return
        choices = [g for g in _CLASS_GAZE_VARIANTS if g != self._desired_gaze]
        self.set_gaze(random.choice(choices))

    def _fire_gesture(self) -> None:
        if self._body.is_gesturing():
            return  # one gesture at a time (§12.4.1)
        choices = [g for g in self._current_gestures if g != self._last_gesture]
        if not choices:
            choices = list(self._current_gestures)
        name = random.choice(choices)
        self._last_gesture = name
        self._body.gesture(name)
