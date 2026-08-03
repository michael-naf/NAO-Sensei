from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import sys
from pathlib import Path

# Must happen before any window/monitor query (slides.py's monitor
# positioning depends on it) — a DPI-unaware process sees Windows-scaled
# ("virtualized") coordinates instead of real physical pixels, which don't
# match what PowerPoint (itself DPI-aware) expects for window bounds.
# Confirmed on this dev machine: 150% scaling reports 1707x1067 instead of
# the real 2560x1600, which is exactly why full-screen slides rendered
# oversized/not screen-fitted.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except OSError:
    pass  # already set by something else, or not supported — non-fatal

import uvicorn

from app.audio.pc_sink import PcAudioSink
from app.audio.playback_queue import PlaybackQueue
from app.body.console_body import ConsoleBody
from app.body.gesture_library import GestureLibraryError
from app.body.gesture_library import load as load_gesture_library
from app.body.scheduler import Scheduler
from app.config import cfg
from app.orchestrator import Orchestrator
from app.queue import QuestionQueue
from app.script_parser import parse, validate
from app.services import llm
from app.slides import SlideController, SlideControllerError
from app.state import LectureState
from app.transcript import Transcript
from app.web.server import ConnectionManager, create_app
from app.web.tunnel import Tunnel, print_qr


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.main")
    parser.add_argument(
        "--validate", metavar="PPTX_PATH", help="Validate a lecture deck and exit."
    )
    args = parser.parse_args()

    if args.validate:
        return _run_validate(args.validate)

    return asyncio.run(_run_lecture())


def _run_validate(pptx_path: str) -> int:
    result = validate(pptx_path)

    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}")

    if result.is_valid:
        print(f"'{pptx_path}' is valid.")
        return 0

    print(f"'{pptx_path}' failed validation ({len(result.errors)} error(s)).")
    return 1


def _build_body(playback: PlaybackQueue) -> tuple[ConsoleBody | None, Scheduler | None]:
    """content/gestures.yaml is still being authored (Phase 3C, in
    Choregraphe) — only load it once it's a real file, not the original
    0-byte placeholder. Once it's real, a validation failure must block
    startup (§12.6.2) same as any other config problem; this is not the
    "gesture failure is non-fatal" rule, which is about runtime gesture
    calls, not the load-time safety check."""
    if cfg.body != "console" or not cfg.gestures.enabled:
        return None, None

    path = Path(cfg.gestures.library)
    if not path.exists() or path.stat().st_size == 0:
        print(f"NOTE: {path} not yet authored — running without embodiment (Phase 3C in progress).")
        return None, None

    # Once the file is real, a validation failure must block startup
    # (§12.6.2) like any other config problem — left to propagate to
    # _run_lecture()'s caller rather than caught here.
    library = load_gesture_library(str(path))
    body = ConsoleBody(library)
    scheduler = Scheduler(playback, body, library)
    return body, scheduler


async def _setup_public_url(loop: asyncio.AbstractEventLoop) -> tuple[str | None, Tunnel | None]:
    """Mode B (§9.1): a public HTTPS URL is what makes voice work at all —
    navigator.mediaDevices is undefined outside a secure context. Returns
    (url, tunnel) — tunnel is None whenever nothing needs stopping later
    (manual override, Mode A, or the tunnel never came up)."""
    if cfg.server.public_url:
        return cfg.server.public_url, None

    if cfg.server.mode != "tunnel":
        return None, None

    tunnel = Tunnel(f"http://localhost:{cfg.server.port}")
    tunnel.start()
    url = await loop.run_in_executor(None, tunnel.wait_for_url, 20.0)
    if url is None:
        print("WARNING: cloudflared didn't produce a tunnel URL in time — "
              "falling back to Mode A (typed-only, LAN). No restart needed; "
              "students on the LAN can still connect.", file=sys.stderr)
        tunnel.stop()
        return None, None
    return url, tunnel


async def _run_lecture() -> int:
    result = validate(cfg.lecture.pptx_path)
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    if not result.is_valid:
        for error in result.errors:
            print(f"ERROR: {error}")
        return 1

    qa_errors = llm.validate_qa_material()
    for error in qa_errors:
        print(f"ERROR: {error}")
    if qa_errors:
        return 1

    script = parse(cfg.lecture.pptx_path)

    sink = PcAudioSink()
    if not sink.is_available():
        print("ERROR: no audio output device available", file=sys.stderr)
        return 1

    playback = PlaybackQueue(sink)
    slides = SlideController()
    question_queue = QuestionQueue()
    transcript = Transcript(cfg.paths.sessions_dir)
    connections = ConnectionManager()

    try:
        body, scheduler = _build_body(playback)
    except GestureLibraryError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    orchestrator = Orchestrator(
        script, slides, playback, question_queue, transcript, connections,
        body=body, scheduler=scheduler,
    )

    app = create_app(orchestrator, question_queue, connections)
    uvicorn_config = uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    server = uvicorn.Server(uvicorn_config)

    lecture_task = asyncio.create_task(orchestrator.run(cfg.lecture.pptx_path))
    server_task = asyncio.create_task(server.serve())

    loop = asyncio.get_running_loop()
    public_url, tunnel = await _setup_public_url(loop)

    print(f"Narrating {cfg.lecture.pptx_path} ({script.slide_count} slides, "
          f"{len(script.sections)} sections)...")
    print(f"Transcript: {transcript.path}")
    if public_url:
        print(f"Student app (voice enabled): {public_url}")
        print_qr(public_url)
    else:
        print(f"Student app (typed only): http://{cfg.server.host}:{cfg.server.port}  "
              f"(use the PC's LAN IP instead of {cfg.server.host} from a phone)")

    done, _ = await asyncio.wait({lecture_task, server_task}, return_when=asyncio.FIRST_COMPLETED)

    server_failed = server_task in done and server_task.exception() is not None
    # Previously unchecked: if orchestrator.run() itself raised (e.g. a
    # model warm-up failure), lecture_task completed-with-exception while
    # server_task kept running — server_failed stayed False, state never
    # reached PAUSED (the crash was before READY), and execution fell all
    # the way through to a false "Lecture FINISHED." with exit 0. Silent
    # success on an actual crash — exactly what "never degrade silently"
    # forbids.
    lecture_failed = lecture_task in done and lecture_task.exception() is not None

    if server_failed:
        print(f"ERROR: web server stopped unexpectedly: {server_task.exception()}", file=sys.stderr)
        lecture_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lecture_task
    elif lecture_failed:
        print(f"ERROR: lecture crashed: {lecture_task.exception()!r}", file=sys.stderr)

    server.should_exit = True
    if not server_task.done():
        await server_task

    if tunnel is not None:
        tunnel.stop()

    # This one-shot CLI run has no operator console to issue "End lecture" —
    # close PowerPoint ourselves so the process doesn't linger after the
    # script exits. If PowerPoint is already gone (e.g. the fault we're
    # cleaning up after was it dying), close() will fault too — expected.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, slides.close)
    except SlideControllerError:
        pass

    if server_failed or lecture_failed:
        return 1

    if orchestrator.state.state == LectureState.PAUSED:
        print(f"Lecture PAUSED — {orchestrator.fault_message}")
        return 1

    print("Lecture FINISHED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
