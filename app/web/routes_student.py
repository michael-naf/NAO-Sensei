from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import cfg

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
