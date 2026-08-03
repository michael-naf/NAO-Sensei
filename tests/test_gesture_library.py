from pathlib import Path

import pytest

from app.body.gesture_library import GestureLibraryError, load

_REST = """
rest:
  duration_s: 1.2
  keyframes:
    - t: 1.2
      LShoulderPitch: 1.40
      LElbowRoll: -0.50
"""


def _write(tmp_path: Path, content: str) -> str:
    path = tmp_path / "gestures.yaml"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_valid_library_loads(tmp_path):
    content = _REST + """
explain_open:
  contexts: [narrating, answering]
  duration_s: 3.4
  keyframes:
    - t: 1.4
      LShoulderPitch: 1.10
      LElbowRoll: -0.35
    - t: 3.4
      LShoulderPitch: 1.40
      LElbowRoll: -0.50

gaze:
  slides: { HeadYaw: 0.45, HeadPitch: -0.15 }
  class:  { HeadYaw: 0.00, HeadPitch: 0.00 }
"""
    library = load(_write(tmp_path, content))

    assert set(library.gestures) == {"rest", "explain_open"}
    assert library.gestures["explain_open"].contexts == ("narrating", "answering")
    assert library.gaze["slides"].head_yaw == 0.45


def test_missing_rest_fails(tmp_path):
    content = """
explain_open:
  duration_s: 1.0
  keyframes:
    - t: 1.0
      LShoulderPitch: 1.10
"""
    with pytest.raises(GestureLibraryError, match="rest"):
        load(_write(tmp_path, content))


def test_leg_joint_fails_to_load(tmp_path):
    # The exact Checkpoint 6 fixture: a leg joint must never load, whatever
    # else is in the file.
    content = _REST + """
bad:
  duration_s: 1.0
  keyframes:
    - t: 1.0
      LKneePitch: 0.5
      LShoulderPitch: 1.40
      LElbowRoll: -0.50
"""
    with pytest.raises(GestureLibraryError, match="LKneePitch"):
        load(_write(tmp_path, content))


def test_angle_outside_published_limit_fails(tmp_path):
    content = _REST + """
overextended:
  duration_s: 1.0
  keyframes:
    - t: 1.0
      LShoulderRoll: 3.0
    - t: 2.0
      LShoulderPitch: 1.40
      LElbowRoll: -0.50
"""
    with pytest.raises(GestureLibraryError, match="LShoulderRoll"):
        load(_write(tmp_path, content))


def test_gesture_not_returning_to_rest_fails(tmp_path):
    content = _REST + """
drifts:
  duration_s: 1.0
  keyframes:
    - t: 1.0
      LShoulderPitch: 0.90
      LElbowRoll: -0.50
"""
    with pytest.raises(GestureLibraryError, match="does not end at rest"):
        load(_write(tmp_path, content))


def test_hand_joint_uses_open_close_range(tmp_path):
    content = _REST + """
grasp:
  duration_s: 1.0
  keyframes:
    - t: 1.0
      LHand: 1.5
      LShoulderPitch: 1.40
      LElbowRoll: -0.50
"""
    with pytest.raises(GestureLibraryError, match="LHand"):
        load(_write(tmp_path, content))


def test_gaze_target_outside_limit_fails(tmp_path):
    content = _REST + """
gaze:
  slides: { HeadYaw: 5.0, HeadPitch: -0.15 }
"""
    with pytest.raises(GestureLibraryError, match="HeadYaw"):
        load(_write(tmp_path, content))
