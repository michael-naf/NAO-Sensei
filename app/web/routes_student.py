from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.config import cfg
from app.services import stt

router = APIRouter(prefix="/api")


class JoinRequest(BaseModel):
    student_id: str
    display_name: str | None = None


class LeaveRequest(BaseModel):
    student_id: str


class SubmitRequest(BaseModel):
    student_id: str
    text: str = Field(min_length=1, max_length=cfg.timings.max_typed_chars)


class ChoiceRequest(BaseModel):
    student_id: str


@router.get("/status")
async def status(request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    return {"lecture_status": orchestrator.state.state.name}


@router.post("/join")
async def join(body: JoinRequest, request: Request) -> dict:
    queue = request.app.state.queue
    already_queued = queue.position(body.student_id) is not None
    if not already_queued and len(queue) >= cfg.server.max_students:
        raise HTTPException(status_code=409, detail="The question queue is full.")
    queue.join(body.student_id, body.display_name)
    return {"position": queue.position(body.student_id)}


@router.post("/leave")
async def leave(body: LeaveRequest, request: Request) -> dict:
    request.app.state.queue.leave(body.student_id)
    return {"ok": True}


@router.post("/submit")
async def submit(body: SubmitRequest, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    if not orchestrator.submit_question(body.student_id, body.text):
        raise HTTPException(status_code=409, detail="It isn't your turn to submit a question.")
    return {"ok": True}


@router.post("/reply")
async def reply(body: ChoiceRequest, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    if not orchestrator.choose_reply(body.student_id):
        raise HTTPException(status_code=409, detail="There is no pending Reply/Done choice for you.")
    return {"ok": True}


@router.post("/done")
async def done(body: ChoiceRequest, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    if not orchestrator.choose_done(body.student_id):
        raise HTTPException(status_code=409, detail="There is no pending Reply/Done choice for you.")
    return {"ok": True}


@router.post("/reply_text")
async def reply_text(body: SubmitRequest, request: Request) -> dict:
    orchestrator = request.app.state.orchestrator
    if not orchestrator.submit_reply_text(body.student_id, body.text):
        raise HTTPException(status_code=409, detail="You aren't in the reply composition window.")
    return {"ok": True}


@router.post("/submit_audio")
async def submit_audio(
    request: Request,
    student_id: str = Form(...),
    mode: str = Form(...),  # 'question' (initial) | 'reply' (follow-up)
    audio: UploadFile = File(...),
) -> dict:
    if mode not in ("question", "reply"):
        raise HTTPException(status_code=400, detail="Invalid mode.")

    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty recording.")

    loop = asyncio.get_running_loop()
    suffix = _suffix_for_content_type(audio.content_type)
    try:
        # STT failure must hold the student's queue position (§11) — we
        # simply never call submit_question/submit_reply_text below, so the
        # orchestrator's rendezvous stays open exactly as it was.
        wav_path = await loop.run_in_executor(None, stt.convert_to_wav, data, suffix)
        text = await loop.run_in_executor(None, stt.transcribe, wav_path)
    except stt.SttError as e:
        raise HTTPException(status_code=422, detail=f"Could not process that recording: {e}")

    if not text.strip():
        raise HTTPException(status_code=422, detail="Didn't catch anything in that recording — try again.")

    orchestrator = request.app.state.orchestrator
    if mode == "question":
        ok = orchestrator.submit_question(student_id, text, mode="voice")
    else:
        ok = orchestrator.submit_reply_text(student_id, text, mode="voice")

    if not ok:
        raise HTTPException(status_code=409, detail="It isn't your turn to submit right now.")

    return {"ok": True, "text": text}


def _suffix_for_content_type(content_type: str | None) -> str:
    if content_type and "webm" in content_type:
        return ".webm"
    if content_type and "mp4" in content_type:
        return ".mp4"
    return ".bin"
