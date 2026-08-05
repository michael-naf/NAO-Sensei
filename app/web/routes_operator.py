from __future__ import annotations

import asyncio
import io

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from app.config import cfg
from app.services import llm

router = APIRouter(prefix="/api/operator")


def require_operator_token(token: str = Query(default="")) -> None:
    """§10.3 — a LAN convenience measure, not a security boundary: a fixed
    token from config.yaml, shown in the terminal at startup, passed as a
    query parameter so the console page itself can be a plain bookmarked
    URL (token baked in) rather than needing a login step."""
    if token != cfg.server.operator_token:
        raise HTTPException(status_code=403, detail="Invalid or missing operator token.")


@router.get("/state", dependencies=[Depends(require_operator_token)])
async def operator_state(request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    snapshot = orchestrator.snapshot()
    snapshot["health"] = await _health(request)
    snapshot["join_url"] = request.app.state.join_url
    snapshot["queue_size"] = len(snapshot["queue"])
    return snapshot


@router.get("/qr", dependencies=[Depends(require_operator_token)])
async def operator_qr(request: Request) -> Response:
    join_url = request.app.state.join_url
    if not join_url:
        raise HTTPException(status_code=404, detail="No public join URL (Mode A / LAN only right now).")
    img = qrcode.make(join_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@router.get("/transcript", dependencies=[Depends(require_operator_token)])
async def operator_transcript(request: Request) -> FileResponse:
    # The orchestrator's own current transcript — recreated fresh each time
    # a new lecture starts (§7.5 update, 2026-08-03), so this always serves
    # the most recently started/completed lecture, never a stale or
    # combined one.
    transcript = request.app.state.orchestrator.transcript
    if transcript is None or not transcript.path.exists():
        raise HTTPException(status_code=404, detail="No transcript written yet.")
    return FileResponse(transcript.path, media_type="text/markdown", filename=transcript.path.name)


@router.post("/start", dependencies=[Depends(require_operator_token)])
async def operator_start(request: Request) -> dict:
    if not request.app.state.orchestrator.start():
        raise HTTPException(status_code=409, detail="Not currently READY.")
    return {"ok": True}


@router.post("/pause", dependencies=[Depends(require_operator_token)])
async def operator_pause(request: Request) -> dict:
    if not request.app.state.orchestrator.pause():
        raise HTTPException(status_code=409, detail="Nothing to pause right now.")
    return {"ok": True}


@router.post("/resume", dependencies=[Depends(require_operator_token)])
async def operator_resume(request: Request) -> dict:
    if not request.app.state.orchestrator.resume():
        raise HTTPException(
            status_code=409,
            detail="Not currently paused (or a slide fault needs reopen_deck/resume_without_slides).",
        )
    return {"ok": True}


@router.post("/skip_section", dependencies=[Depends(require_operator_token)])
async def operator_skip_section(request: Request) -> dict:
    if not request.app.state.orchestrator.skip_section():
        raise HTTPException(status_code=409, detail="Can't skip section from the current state.")
    return {"ok": True}


@router.post("/skip_question", dependencies=[Depends(require_operator_token)])
async def operator_skip_question(request: Request) -> dict:
    if not request.app.state.orchestrator.skip_question():
        raise HTTPException(status_code=409, detail="No active question to skip.")
    return {"ok": True}


@router.post("/clear_queue", dependencies=[Depends(require_operator_token)])
async def operator_clear_queue(request: Request) -> dict:
    if not request.app.state.orchestrator.clear_queue():
        raise HTTPException(status_code=409, detail="Queue is already empty.")
    return {"ok": True}


@router.post("/reopen_deck", dependencies=[Depends(require_operator_token)])
async def operator_reopen_deck(request: Request) -> dict:
    if not request.app.state.orchestrator.reopen_deck():
        raise HTTPException(status_code=409, detail="No slide fault is currently being held.")
    return {"ok": True}


@router.post("/resume_without_slides", dependencies=[Depends(require_operator_token)])
async def operator_resume_without_slides(request: Request) -> dict:
    if not request.app.state.orchestrator.resume_without_slides():
        raise HTTPException(status_code=409, detail="No slide fault is currently being held.")
    return {"ok": True}


@router.post("/end_lecture", dependencies=[Depends(require_operator_token)])
async def operator_end_lecture(request: Request) -> dict:
    if not request.app.state.orchestrator.end_lecture():
        raise HTTPException(status_code=409, detail="Nothing running to end.")
    return {"ok": True}


@router.post("/exit", dependencies=[Depends(require_operator_token)])
async def operator_exit(request: Request) -> dict:
    # Closes the whole application (§10.2 update, 2026-08-03), not just the
    # current lecture — see Orchestrator.request_exit(). The response still
    # goes out normally; the process shuts down shortly after, once
    # main.py's existing teardown path runs.
    if not request.app.state.orchestrator.request_exit():
        raise HTTPException(status_code=409, detail="Nothing running to exit.")
    return {"ok": True}


async def _health(request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    loop = asyncio.get_running_loop()
    # llm.is_reachable() makes a real (short-timeout) network call — never
    # block the event loop thread with it directly (see "stuck" checklist
    # item 8 in CLAUDE.md).
    llm_ok = await loop.run_in_executor(None, llm.is_reachable)

    # TTS/STT models are loaded once at process startup (app/services/tts.py,
    # stt.py) and the process wouldn't be running at all if that had failed
    # — there's no ongoing liveness signal to poll beyond that for either,
    # so "ok" here means "loaded," matching §11's actual failure handling
    # for them (logged per-call, not tracked as a standing health state).
    powerpoint = "down" if orchestrator.fault_message else "ok"

    if cfg.body == "nao" or cfg.audio_output == "nao":
        # Phase 6 live bring-up (2026-08-05) — the bridge's /health is
        # real now (nao_bridge/bridge.py, deployed and running on the
        # robot); NaoBody.is_available() already calls it. This used to
        # hardcode "unknown" from back when the bridge didn't exist yet
        # — stale since Phase 6 landed, found live while reviewing the
        # operator console mid-session.
        body = orchestrator.body
        if body is None:
            nao = "unknown"
        else:
            nao_ok = await loop.run_in_executor(None, body.is_available)
            nao = "ok" if nao_ok else "down"
    else:
        nao = "n/a"

    return {
        "llm": "ok" if llm_ok else "down",
        "stt": "ok",
        "tts": "ok",
        "powerpoint": powerpoint,
        "nao": nao,
    }
