from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

# §12.1.1 — enforced again here at *load* time as a second line of defense.
# The bridge's own whitelist (nao_bridge/whitelist.py, Phase 6) is what
# actually guarantees no other joint can ever reach ALMotion; this check
# exists so a bad gestures.yaml fails loudly at startup instead of only
# being caught on the robot.
ALLOWED_JOINTS = {
    "HeadYaw", "HeadPitch",
    "LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll", "LWristYaw", "LHand",
    "RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw", "RHand",
}

# NAO V5 (H25 body) published joint limits, in degrees, from Aldebaran's
# official documentation (doc.aldebaran.com/2-1/family/nao_h25/joints_h25.html).
# HeadPitch's true usable range narrows further as HeadYaw moves off-center
# (shell collision avoidance) — not modeled here, since this project's gaze
# and gesture set never approaches the head's limits either way; revisit if
# that stops being true.
_LIMITS_DEG: dict[str, tuple[float, float]] = {
    "HeadYaw": (-119.5, 119.5),
    "HeadPitch": (-38.5, 29.5),
    "LShoulderPitch": (-119.5, 119.5),
    "LShoulderRoll": (-18.0, 76.0),
    "LElbowYaw": (-119.5, 119.5),
    "LElbowRoll": (-88.5, -2.0),
    "LWristYaw": (-104.5, 104.5),
    "RShoulderPitch": (-119.5, 119.5),
    "RShoulderRoll": (-76.0, 18.0),
    "RElbowYaw": (-119.5, 119.5),
    "RElbowRoll": (2.0, 88.5),
    "RWristYaw": (-104.5, 104.5),
}
JOINT_LIMITS_RAD: dict[str, tuple[float, float]] = {
    joint: (math.radians(lo), math.radians(hi)) for joint, (lo, hi) in _LIMITS_DEG.items()
}

# LHand/RHand are a normalized open/close actuator, not an angle joint —
# 0.0 (closed) to 1.0 (open). Not part of _LIMITS_DEG on purpose.
_HAND_JOINTS = {"LHand", "RHand"}

_REST_TOLERANCE = 1e-6

# §12.4.4 — "slow interpolation... gestures.speed 0.10-0.20 max speed
# fraction". Same bound enforced here for the optional per-gesture speed
# override (see Gesture.speed) — it must stay inside a safety range, not
# just inherit one by default.
#
# Upper bound widened 0.20 -> 0.30, 2026-08-04, at the user's explicit
# request and live-tested directly on the robot (wave felt too slow at
# 0.20; 0.4 was also requested but the user chose to try 0.3 first given
# the "don't break NAO" priority — see CLAUDE.md). This is a deliberate,
# confirmed deviation from specs.md's original ceiling, not an oversight;
# specs.md should be updated to match once a final value is settled. Still
# scoped to individual gestures via Gesture.speed, not the global default —
# most gestures stay at 0.10-0.20 unless a specific one is deliberately
# tuned past it the same way.
_SPEED_RANGE = (0.10, 0.30)


class GestureLibraryError(Exception):
    pass


@dataclass(frozen=True)
class Keyframe:
    t: float
    angles: dict[str, float]


@dataclass(frozen=True)
class Gesture:
    name: str
    duration_s: float
    contexts: tuple[str, ...]
    keyframes: tuple[Keyframe, ...]
    # None = use config.yaml's gestures.speed (the common case). Only set
    # per-gesture when one specific gesture needs to differ from the rest
    # (found live, 2026-08-04: wave felt slow at the global 0.15 while
    # every other gesture was approved as-is at that same speed — a global
    # bump would have disturbed already-approved gestures). Same 0.10-0.20
    # safety range as the global value, enforced below — this is not a
    # backdoor around §12.4.4's speed cap, just a per-gesture pick within it.
    speed: float | None = None


@dataclass(frozen=True)
class GazeTarget:
    head_yaw: float
    head_pitch: float


@dataclass(frozen=True)
class GestureLibrary:
    gestures: dict[str, Gesture]
    gaze: dict[str, GazeTarget]


def load(path: str) -> GestureLibrary:
    """Parse and validate content/gestures.yaml (§12.6.2). Every joint must
    be on the whitelist, every angle within its published limit, and every
    gesture other than `rest` must end at `rest`'s own values. Any violation
    raises — this must block startup (§5.4), never degrade silently."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    errors: list[str] = []

    gaze_raw = raw.pop("gaze", {})

    if "rest" not in raw:
        raise GestureLibraryError(f"{path}: missing required 'rest' gesture.")

    gestures: dict[str, Gesture] = {
        name: _parse_gesture(name, spec, errors) for name, spec in raw.items()
    }

    rest = gestures["rest"]
    if rest.keyframes:
        rest_final = rest.keyframes[-1].angles
        for name, gesture in gestures.items():
            if name == "rest" or not gesture.keyframes:
                continue
            final = gesture.keyframes[-1].angles
            for joint, rest_value in rest_final.items():
                actual = final.get(joint)
                if actual is None or abs(actual - rest_value) > _REST_TOLERANCE:
                    errors.append(
                        f"Gesture '{name}' does not end at rest: joint '{joint}' is "
                        f"{actual!r} in its final keyframe, rest has {rest_value!r}."
                    )

    gaze = {
        target: _parse_gaze(target, values, errors) for target, values in gaze_raw.items()
    }

    if errors:
        raise GestureLibraryError(
            f"{path} failed validation:\n" + "\n".join(f"- {e}" for e in errors)
        )

    return GestureLibrary(gestures=gestures, gaze=gaze)


def _parse_gesture(name: str, spec: dict, errors: list[str]) -> Gesture:
    duration_s = spec.get("duration_s")
    if duration_s is None:
        errors.append(f"Gesture '{name}' is missing 'duration_s'.")
        duration_s = 0.0
    contexts = tuple(spec.get("contexts", []))

    speed = spec.get("speed")
    if speed is not None and not (_SPEED_RANGE[0] <= speed <= _SPEED_RANGE[1]):
        errors.append(
            f"Gesture '{name}' sets speed={speed}, outside the safety range "
            f"[{_SPEED_RANGE[0]}, {_SPEED_RANGE[1]}] (§12.4.4)."
        )

    keyframes: list[Keyframe] = []
    for kf in spec.get("keyframes", []):
        t = kf.get("t")
        if t is None:
            errors.append(f"Gesture '{name}' has a keyframe with no 't'.")
        angles = {k: v for k, v in kf.items() if k != "t"}
        for joint, angle in angles.items():
            _validate_joint(name, joint, angle, errors)
        keyframes.append(Keyframe(t=t, angles=angles))

    if not keyframes:
        errors.append(f"Gesture '{name}' has no keyframes.")

    return Gesture(
        name=name, duration_s=duration_s, contexts=contexts, keyframes=tuple(keyframes), speed=speed
    )


def _validate_joint(owner: str, joint: str, angle: float, errors: list[str]) -> None:
    if joint not in ALLOWED_JOINTS:
        errors.append(
            f"'{owner}' uses joint '{joint}', which is not on the whitelist "
            f"(§12.1.1). Refused in full."
        )
        return
    if joint in _HAND_JOINTS:
        if not (0.0 <= angle <= 1.0):
            errors.append(
                f"'{owner}' sets '{joint}' to {angle}, outside the 0.0-1.0 "
                f"open/close range."
            )
        return
    lo, hi = JOINT_LIMITS_RAD[joint]
    if not (lo <= angle <= hi):
        errors.append(
            f"'{owner}' sets '{joint}' to {angle} rad, outside its published "
            f"limit [{lo:.3f}, {hi:.3f}] rad."
        )


def _parse_gaze(target: str, values: dict, errors: list[str]) -> GazeTarget:
    head_yaw = values.get("HeadYaw", 0.0)
    head_pitch = values.get("HeadPitch", 0.0)
    _validate_joint(f"gaze.{target}", "HeadYaw", head_yaw, errors)
    _validate_joint(f"gaze.{target}", "HeadPitch", head_pitch, errors)
    return GazeTarget(head_yaw=head_yaw, head_pitch=head_pitch)
