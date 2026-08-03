from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.orchestrator import Orchestrator
from app.queue import QuestionQueue

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class ConnectionManager:
    """Structurally implements orchestrator.Notifier. Lives entirely on the
    event loop thread — the same one uvicorn and the orchestrator share
    (§4.3), so no locking is needed around `_sockets`."""

    def __init__(self) -> None:
        self._sockets: dict[str, WebSocket] = {}

    async def connect(self, student_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets[student_id] = ws

    def disconnect(self, student_id: str) -> None:
        self._sockets.pop(student_id, None)

    def is_connected(self, student_id: str) -> bool:
        return student_id in self._sockets

    async def send(self, student_id: str, message: dict) -> None:
        ws = self._sockets.get(student_id)
        if ws is None:
            return
        try:
            await ws.send_json(message)
        except Exception:
            self.disconnect(student_id)

    async def broadcast(self, message: dict) -> None:
        for student_id in list(self._sockets):
            await self.send(student_id, message)


def create_app(orchestrator: Orchestrator, queue: QuestionQueue, connections: ConnectionManager) -> FastAPI:
    app = FastAPI()
    app.state.orchestrator = orchestrator
    app.state.queue = queue
    app.state.connections = connections

    from app.web.routes_student import router as student_router  # deferred: avoids import cycle at module load

    app.include_router(student_router)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "student.html")

    @app.post("/api/start")
    async def start_lecture() -> dict:
        # Bare minimum stand-in for the Phase 5 operator console's Start
        # control (§10.2) — no token guard, no UI, none of the other six
        # controls. Just the one piece of §6.1's READY -> NARRATING wiring
        # needed to test with students already connected.
        if not orchestrator.start():
            raise HTTPException(status_code=409, detail="Not currently READY.")
        return {"ok": True}

    @app.post("/api/force_skip")
    async def force_skip() -> dict:
        # Same category as /api/start above — a minimal stand-in for one
        # Phase 5 operator control ("Skip question", §10.2), added because
        # a turn stuck waiting on a student who's gone (closed browser,
        # disconnected) currently has no other way to unblock.
        if not orchestrator.force_skip():
            raise HTTPException(status_code=409, detail="Nothing is currently pending.")
        return {"ok": True}

    @app.websocket("/ws/{student_id}")
    async def ws_endpoint(websocket: WebSocket, student_id: str) -> None:
        await connections.connect(student_id, websocket)
        await connections.send(
            student_id,
            {
                "type": "sync",
                "lecture_status": orchestrator.state.state.name,
                "queue_position": queue.position(student_id),
            },
        )
        try:
            while True:
                # The student app pushes actions over REST (§9.4); this
                # socket is receive-only from the client's side. Waiting on
                # it here is just how we detect disconnects.
                await websocket.receive_text()
        except WebSocketDisconnect:
            connections.disconnect(student_id)
            orchestrator.notify_disconnect(student_id)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app
