from __future__ import annotations

import threading
import time

import httpx

from app.body.gesture_library import GestureLibrary
from app.config import cfg

# Real motion on the bridge runs speed-capped (ALMotion.angleInterpolation
# WithSpeed at nao.gestures.speed — see nao_bridge/bridge.py), not at the
# keyframe times recorded in gestures.yaml (those came from Choregraphe's
# own simulator and have no relationship to that speed cap). A speed-capped
# move is generally *slower* than how it looked authored, so is_gesturing()
# pads duration_s rather than trusting it exactly — better to have the
# scheduler wait slightly too long than fire a new gesture while the real
# robot is still finishing the last one. Exact once real timing can be
# watched and tuned (Phase 7); there is no live "still moving" query to
# ask the bridge instead (see is_gesturing()'s own note).
_DURATION_SAFETY_MARGIN = 1.5


class NaoBody:
    """Body implementation talking to nao_bridge over HTTP (specs.md
    Sec12.6.1, Sec12.1). Swap point for NFR-5/9 — Orchestrator/Scheduler
    never know this isn't ConsoleBody."""

    def __init__(self, library: GestureLibrary) -> None:
        self._library = library
        self._client = httpx.Client(base_url=cfg.nao.bridge_url)
        self._gesture_until = 0.0

    def gesture(self, name: str) -> None:
        # Non-blocking per the Body protocol's own contract — fired from a
        # background thread; a failure here must never touch the audio
        # path (§12.4.1: "gesture failure is non-fatal by construction").
        gesture = self._library.gestures.get(name)
        if gesture is None:
            return
        self._gesture_until = time.monotonic() + gesture.duration_s * _DURATION_SAFETY_MARGIN
        keyframes = [{"t": kf.t, **kf.angles} for kf in gesture.keyframes]
        body = {"name": name, "keyframes": keyframes, "speed": cfg.gestures.speed}
        self._fire(f"gesture {name!r}", "/gesture", body, cfg.nao.timeouts.motion_s)

    def gaze(self, target: str) -> None:
        gaze_target = self._library.gaze.get(target)
        if gaze_target is None:
            return
        body = {"HeadYaw": gaze_target.head_yaw, "HeadPitch": gaze_target.head_pitch}
        self._fire(f"gaze {target!r}", "/gaze", body, cfg.nao.timeouts.motion_s)

    def leds(self, pattern: str) -> None:
        self._fire(f"leds {pattern!r}", "/leds", {"pattern": pattern}, cfg.nao.timeouts.motion_s)

    def posture(self, name: str) -> None:
        # Genuinely blocking (real motion, up to nao.timeouts.posture_s) —
        # deliberately NOT fire-and-forget like gesture()/gaze()/leds()
        # above, because lecture start/end needs the posture change to
        # actually finish before anything else proceeds (e.g. arm gestures
        # assume the seated envelope — §12.4.4). Orchestrator._body_lecture
        # _start() calls this through run_in_executor(), same as every
        # other genuinely-blocking call in that file — never call this
        # directly from the event loop thread.
        #
        # Also deliberately NOT swallowed, unlike gesture()/gaze()/leds():
        # those are cosmetic and non-fatal by construction (§12.4.1); a
        # posture/stiffness call that genuinely fails on real hardware is
        # not — "Sit" not landing, or stiffness not actually engaging,
        # means the safety story the rest of this design leans on (§12.4.4)
        # no longer holds. Fail loudly (raise_for_status() propagates)
        # rather than degrade silently.
        self._post_sync("/posture", {"name": name}, cfg.nao.timeouts.posture_s)

    def stiffness(self, on: bool) -> None:
        self._post_sync("/stiffness", {"on": on}, cfg.nao.timeouts.posture_s)

    def is_gesturing(self) -> bool:
        # No live query exists for "is a gesture still physically running"
        # — the bridge's /gesture returns immediately once the worker
        # thread starts (§12.1), and polling /health on every call (this
        # is checked frequently, e.g. by Scheduler._fire_gesture()) would
        # mean a blocking HTTP round trip on the event loop thread for
        # every check. A local timer estimate, same approach as
        # ConsoleBody, padded per _DURATION_SAFETY_MARGIN above.
        return time.monotonic() < self._gesture_until

    def is_available(self) -> bool:
        try:
            resp = self._client.get(
                "/health",
                timeout=httpx.Timeout(cfg.nao.timeouts.health_s, connect=cfg.nao.timeouts.connect_s),
            )
            return resp.status_code == 200 and bool(resp.json().get("connected"))
        except httpx.HTTPError:
            return False

    def _fire(self, label: str, path: str, body: dict, timeout_s: float) -> None:
        t = threading.Thread(target=self._post_swallowed, args=(label, path, body, timeout_s), daemon=True)
        t.start()

    def _post_swallowed(self, label: str, path: str, body: dict, timeout_s: float) -> None:
        # Motion/LED failures are logged and swallowed (implementationPlan.md
        # 6.2) — never raise back into the (background) thread that has no
        # caller waiting to handle it, and never let a bridge hiccup ripple
        # into narration.
        try:
            self._post_sync(path, body, timeout_s)
        except httpx.HTTPError as e:
            print(f"[NAO] {label} failed: {e!r}")

    def _post_sync(self, path: str, body: dict, timeout_s: float) -> None:
        resp = self._client.post(
            path, json=body,
            timeout=httpx.Timeout(timeout_s, connect=cfg.nao.timeouts.connect_s),
        )
        resp.raise_for_status()
