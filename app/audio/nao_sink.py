from __future__ import annotations

import wave
from pathlib import Path

import httpx

from app.config import cfg


class NaoAudioSink:
    """AudioSink implementation talking to nao_bridge over HTTP (specs.md
    Sec12.3, Sec12.1.2). Swap point for NFR-5/9 — Orchestrator/PlaybackQueue
    never know this isn't PcAudioSink."""

    def __init__(self) -> None:
        self._client = httpx.Client(base_url=cfg.nao.bridge_url)
        # prepare() knows the WAV's real duration (reads it locally before
        # upload); play() needs it too, to size /play's read timeout
        # (duration + play_margin_s — "never infinite", Sec12.1.2), but the
        # AudioSink protocol's play(token) only takes the token. Stashed
        # here instead of threading a second value through the interface.
        self._durations: dict[str, float] = {}

    def prepare(self, wav_path: str) -> str:
        duration_s = _wav_duration(wav_path)
        data = Path(wav_path).read_bytes()
        resp = self._client.post(
            "/upload",
            content=data,
            headers={
                "X-Filename": Path(wav_path).name,
                "Content-Type": "application/octet-stream",
            },
            timeout=httpx.Timeout(cfg.nao.timeouts.upload_s, connect=cfg.nao.timeouts.connect_s),
        )
        resp.raise_for_status()
        remote_path = resp.json()["remote_path"]
        self._durations[remote_path] = duration_s
        return remote_path

    def play(self, token: str) -> None:
        # Sec12.1.2 — "WAV duration + 5s, computed per call. Never
        # infinite." Falls back to a generous fixed ceiling only if
        # somehow asked to play a token this sink never prepared (should
        # not happen in practice — PlaybackQueue always prepare()s before
        # play()s the same Utterance).
        duration_s = self._durations.get(token, 30.0)
        read_timeout = duration_s + cfg.nao.timeouts.play_margin_s
        resp = self._client.post(
            "/play",
            json={"remote_path": token},
            timeout=httpx.Timeout(read_timeout, connect=cfg.nao.timeouts.connect_s),
        )
        resp.raise_for_status()

    def stop(self) -> None:
        # Must be thread-safe and callable while play() is blocked in
        # another thread (§4.4) — a fresh short-lived request over this
        # sink's own httpx.Client is safe for that; nothing here is
        # shared mutable state play() also touches. Best-effort: if the
        # bridge is already unreachable there is nothing left to stop.
        try:
            self._client.post(
                "/stop",
                timeout=httpx.Timeout(cfg.nao.timeouts.stop_s, connect=cfg.nao.timeouts.connect_s),
            )
        except httpx.HTTPError:
            pass

    def is_available(self) -> bool:
        try:
            resp = self._client.get(
                "/health",
                timeout=httpx.Timeout(cfg.nao.timeouts.health_s, connect=cfg.nao.timeouts.connect_s),
            )
            return resp.status_code == 200 and bool(resp.json().get("connected"))
        except httpx.HTTPError:
            return False


def _wav_duration(path: str) -> float:
    with wave.open(path, "rb") as f:
        return f.getnframes() / float(f.getframerate())
