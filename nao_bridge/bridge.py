# -*- coding: utf-8 -*-
"""NAO-side bridge (Python 2.7, runs ON the robot, uses its preinstalled
naoqi module -- specs.md Sec12.1). Deliberately minimal: no lecture logic,
no knowledge of slides/questions/state machines. Every joint-touching
request is filtered through whitelist.py before it can reach ALMotion --
that check is the one thing standing between a bad request and a motor,
so nothing here bypasses it, ever.

Threaded on purpose (SocketServer.ThreadingMixIn): a single-threaded
server would queue /stop behind the /play it is meant to interrupt, and
Skip question would never work.

Run on the robot:  python bridge.py
"""
from __future__ import print_function

import json
import sys
import threading
import time
import traceback
from BaseHTTPServer import BaseHTTPRequestHandler
from SocketServer import ThreadingMixIn, TCPServer

from naoqi import ALProxy

import whitelist

NAOQI_IP = "127.0.0.1"
NAOQI_PORT = 9559
BRIDGE_PORT = 8765
AUDIO_DIR = "/home/nao/lecture_audio"
# Sec12.4.4 -- "slow interpolation... which is also the intended aesthetic".
# angleInterpolationWithSpeed's own maxSpeedFraction cap, applied to every
# /gesture and /gaze motion uniformly. Not config-driven here on purpose --
# the bridge has no config.yaml of its own (PC-only); Phase 7 tunes this by
# hand against the physical robot if it ever needs to change.
MOTION_SPEED = 0.15

def _to_str(value):
    """json.loads() on Python 2.7 always produces unicode for JSON strings,
    never native str -- and NAOqi's ALProxy bindings reject unicode with a
    cryptic "conversion failure from Value to Unknown" error (found live,
    2026-08-04, on the very first /posture call). Every string that came
    from JSON and is about to cross into an ALProxy call needs this. Plain
    .encode('utf-8') is safe here -- joint names, posture names, and
    filesystem paths in this project are always ASCII."""
    return value.encode("utf-8") if isinstance(value, unicode) else value


_LED_RGB = {
    "white": 0x00FFFFFF,
    "blue": 0x000000FF,
    "green": 0x0000FF00,
    "yellow": 0x00FFFF00,
    "off": 0x00000000,
}


class Proxies(object):
    """Persistent ALProxy connections, rebuilt on demand if a call fails --
    NAOqi connections can drop; nothing here should need an SSH restart to
    recover from that."""

    def __init__(self):
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        self.motion = ALProxy("ALMotion", NAOQI_IP, NAOQI_PORT)
        self.posture = ALProxy("ALRobotPosture", NAOQI_IP, NAOQI_PORT)
        self.audio_player = ALProxy("ALAudioPlayer", NAOQI_IP, NAOQI_PORT)
        self.audio_device = ALProxy("ALAudioDevice", NAOQI_IP, NAOQI_PORT)
        self.leds = ALProxy("ALLeds", NAOQI_IP, NAOQI_PORT)
        self.memory = ALProxy("ALMemory", NAOQI_IP, NAOQI_PORT)
        self.motion.setFallManagerEnabled(True)  # Sec12.4.4 -- never disabled
        # Sec12.4.4 -- permanently on. Seated, the thighs occupy space a
        # lowered arm wants to pass through.
        self.motion.setCollisionProtectionEnabled("Arms", True)
        # Found live, 2026-08-04: NAOqi's own Autonomous Life (background
        # behaviors -- ALBasicAwareness's face-tracking in particular) was
        # still moving NAO's head on its own, independent of anything this
        # bridge does. This project's whole design (gesture_library.py,
        # Scheduler, the "one gesture at a time" / no-concurrent-
        # angleInterpolation-on-the-same-joint guarantee) assumes NAO's
        # head/arms are *entirely* under this bridge's control -- a
        # background face-track fights our own gaze()/gesture() calls on
        # exactly the same joints. Disabled at startup, not per-request:
        # this must hold for the whole session, not just while gestures
        # are firing.
        try:
            life = ALProxy("ALAutonomousLife", NAOQI_IP, NAOQI_PORT)
            life.setState("disabled")
        except Exception:
            traceback.print_exc()  # non-fatal -- log and continue, per module docstring

    def reconnect(self):
        with self._lock:
            self._connect()


PROXIES = Proxies()


def _run_gesture(keyframes, speed):
    """Worker thread body for /gesture -- one angleInterpolationWithSpeed
    call per keyframe, in order. Each call blocks until that waypoint is
    reached, so the loop naturally sequences the whole gesture. "t" is
    ordering only (see specs.md Sec12.4.1's note) -- the PC-recorded times
    came from Choregraphe's own simulator and have no relationship to
    this speed cap, so they are never used as literal timing here --
    *except* for the one case below.

    A same-pose "hold" keyframe (an authored pause) was silently a no-op:
    angleInterpolationWithSpeed sees zero distance to travel and returns
    almost instantly, so the pause never actually happened no matter what
    "t" gap the content author gave it. That is the one place "t" is read
    as a literal duration: a same-pose keyframe sleeps for the recorded
    gap instead of calling into ALMotion at all.

    2026-08-04 note: an apparent "no motion at all" regression from this
    exact change turned out to be unrelated -- NAO's stiffness had
    independently dropped to 0 (most likely physical contact with the
    chest button during hands-on testing), which no motion command of any
    kind could work around. Confirmed via ALMotion.getStiffnesses(): the
    unmodified original code produced the identical no-motion symptom
    under stiffness=0, and both versions moved normally again once
    stiffness was restored. This logic itself was never the problem."""
    try:
        prev_t = 0.0
        prev_pose = None
        for kf in keyframes:
            t = kf.get("t", 0.0)
            pose = dict((k, v) for k, v in kf.items() if k != "t")
            if not pose:
                prev_t, prev_pose = t, pose
                continue
            if pose == prev_pose:
                time.sleep(max(0.0, t - prev_t))
            else:
                joints = [_to_str(j) for j in pose.keys()]
                angles = [pose[j] for j in pose.keys()]
                PROXIES.motion.angleInterpolationWithSpeed(joints, angles, speed)
            prev_t, prev_pose = t, pose
    except Exception:
        traceback.print_exc()


def _run_gaze(joints, angles, speed):
    try:
        PROXIES.motion.angleInterpolationWithSpeed(joints, angles, speed)
    except Exception:
        traceback.print_exc()


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, payload):
        body = json.dumps(payload)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_raw(self):
        length = int(self.headers.getheader("Content-Length", "0"))
        return self.rfile.read(length) if length else ""

    def _read_json(self):
        raw = self._read_raw()
        return json.loads(raw) if raw else {}

    def do_GET(self):
        if self.path == "/health":
            return self._health()
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        routes = {
            "/upload": self._upload, "/play": self._play, "/stop": self._stop,
            "/volume": self._volume, "/posture": self._posture,
            "/gesture": self._gesture, "/gaze": self._gaze,
            "/leds": self._leds, "/stiffness": self._stiffness,
        }
        handler = routes.get(self.path)
        if handler is None:
            return self._send_json(404, {"error": "not found"})
        try:
            handler()
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _health(self):
        try:
            battery = PROXIES.memory.getData("BatteryChargeChanged")
        except Exception:
            battery = -1
        self._send_json(200, {
            "connected": True, "battery": battery,
            "volume": PROXIES.audio_device.getOutputVolume(), "temps_ok": True,
        })

    def _upload(self):
        filename = self.headers.getheader("X-Filename", "clip.wav")
        remote_path = AUDIO_DIR + "/" + filename
        with open(remote_path, "wb") as f:
            f.write(self._read_raw())
        self._send_json(200, {"remote_path": remote_path})

    def _play(self):
        # Blocks until playback finishes -- matches the PC client's own
        # (WAV duration + margin) timeout, never an infinite wait.
        PROXIES.audio_player.playFile(_to_str(self._read_json()["remote_path"]))
        self._send_json(200, {"ok": True})

    def _stop(self):
        PROXIES.audio_player.stopAll()
        self._send_json(200, {"ok": True})

    def _volume(self):
        PROXIES.audio_device.setOutputVolume(int(self._read_json()["level"]))
        self._send_json(200, {"ok": True})

    def _posture(self):
        name = _to_str(self._read_json()["name"])
        PROXIES.posture.goToPosture(name, 0.4)
        self._send_json(200, {"ok": True})

    def _stiffness(self):
        on = bool(self._read_json()["on"])
        PROXIES.motion.setStiffnesses("Body", 1.0 if on else 0.0)
        self._send_json(200, {"ok": True})

    def _leds(self):
        pattern = self._read_json().get("pattern", "off")
        rgb = _LED_RGB.get(pattern, _LED_RGB["off"])
        PROXIES.leds.fadeRGB("FaceLeds", rgb, 0.1)
        self._send_json(200, {"ok": True})

    def _gesture(self):
        body = self._read_json()
        bad_joint = whitelist.check_gesture_request(body)
        if bad_joint is not None:
            return self._send_json(
                403, {"error": "joint not whitelisted: " + bad_joint})
        keyframes = body.get("keyframes", [])
        speed = float(body.get("speed", MOTION_SPEED))
        t = threading.Thread(target=_run_gesture, args=(keyframes, speed))
        t.daemon = True
        t.start()
        self._send_json(200, {"ok": True})  # returns immediately -- worker thread runs the motion

    def _gaze(self):
        body = self._read_json()
        bad_joint = whitelist.check_flat_joint_request(body)
        if bad_joint is not None:
            return self._send_json(
                403, {"error": "joint not whitelisted: " + bad_joint})
        joints = [_to_str(j) for j in body.keys()]
        angles = [body[j] for j in body.keys()]
        t = threading.Thread(target=_run_gaze, args=(joints, angles, MOTION_SPEED))
        t.daemon = True
        t.start()
        self._send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        sys.stdout.write("[bridge] " + (fmt % args) + "\n")


class ThreadedServer(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    server = ThreadedServer(("0.0.0.0", BRIDGE_PORT), Handler)
    print("[bridge] listening on :%d" % BRIDGE_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
