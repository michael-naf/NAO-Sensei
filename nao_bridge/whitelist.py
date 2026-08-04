# -*- coding: utf-8 -*-
"""Joint whitelist enforcement (specs.md Sec12.1.1).

This is the *only* structural guarantee that a non-whitelisted joint --
above all, any leg joint -- can never reach ALMotion, regardless of what
gestures.yaml or any client sends. It lives here, not on the PC side,
because this is the only code with a connection to ALMotion at all: the
PC-side gesture_library.py validates the same whitelist at load time, but
that is a second line of defense, not the guarantee itself.

A request naming ANY joint not on this list is refused in full -- never
partially applied. Never import naoqi here; this module has no NAOqi
dependency on purpose, so it can be unit-tested off the robot.
"""

ALLOWED_JOINTS = frozenset([
    "HeadYaw", "HeadPitch",
    "LShoulderPitch", "LShoulderRoll", "LElbowYaw", "LElbowRoll", "LWristYaw", "LHand",
    "RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw", "RHand",
])


def first_disallowed_joint(joint_names):
    """joint_names: any iterable of joint name strings. Returns the first
    name not on the whitelist, or None if every one of them is allowed."""
    for name in joint_names:
        if name not in ALLOWED_JOINTS:
            return name
    return None


def check_gesture_request(body):
    """body: parsed JSON {"name": str, "keyframes": [{"t": float, <joint>: <angle>, ...}, ...], "speed": float}.
    Every joint key in every keyframe (everything except "t") must be
    whitelisted. Returns the first disallowed joint name, or None if the
    whole request is clean."""
    for keyframe in body.get("keyframes", []):
        joint_names = [k for k in keyframe.keys() if k != "t"]
        bad = first_disallowed_joint(joint_names)
        if bad is not None:
            return bad
    return None


def check_flat_joint_request(body):
    """body: parsed JSON mapping joint name -> angle directly (used by
    /gaze, which sends {"HeadYaw": ..., "HeadPitch": ...} with no
    keyframe/time wrapping). Returns the first disallowed joint name, or
    None if clean."""
    return first_disallowed_joint(body.keys())
