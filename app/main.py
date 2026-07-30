from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import sys

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
from app.config import cfg
from app.orchestrator import Orchestrator
from app.queue import QuestionQueue
from app.script_parser import parse, validate
from app.services import llm
from app.slides import SlideController, SlideControllerError
from app.state import LectureState
from app.transcript import Transcript
from app.web.server import ConnectionManager, create_app


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
    orchestrator = Orchestrator(script, slides, playback, question_queue, transcript, connections)

    app = create_app(orchestrator, question_queue, connections)
    uvicorn_config = uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    server = uvicorn.Server(uvicorn_config)

    print(f"Narrating {cfg.lecture.pptx_path} ({script.slide_count} slides, "
          f"{len(script.sections)} sections)...")
    print(f"Student app: http://{cfg.server.host}:{cfg.server.port}  "
          f"(use the PC's LAN IP instead of {cfg.server.host} from a phone)")
    print(f"Transcript: {transcript.path}")

    lecture_task = asyncio.create_task(orchestrator.run(cfg.lecture.pptx_path))
    server_task = asyncio.create_task(server.serve())

    done, _ = await asyncio.wait({lecture_task, server_task}, return_when=asyncio.FIRST_COMPLETED)

    server_failed = server_task in done and server_task.exception() is not None
    if server_failed:
        print(f"ERROR: web server stopped unexpectedly: {server_task.exception()}", file=sys.stderr)
        lecture_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lecture_task

    server.should_exit = True
    if not server_task.done():
        await server_task

    # This one-shot CLI run has no operator console to issue "End lecture" —
    # close PowerPoint ourselves so the process doesn't linger after the
    # script exits. If PowerPoint is already gone (e.g. the fault we're
    # cleaning up after was it dying), close() will fault too — expected.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, slides.close)
    except SlideControllerError:
        pass

    if server_failed:
        return 1

    if orchestrator.state.state == LectureState.PAUSED:
        print(f"Lecture PAUSED — {orchestrator.fault_message}")
        return 1

    print("Lecture FINISHED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
