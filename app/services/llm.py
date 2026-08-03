from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import AsyncIterator, Iterator

import asyncio
import httpx

from app.config import cfg

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_SYSTEM_TEMPLATE = (_PROMPTS_DIR / "qa_system.txt").read_text(encoding="utf-8")
_QA_MATERIAL = Path(cfg.lecture.qa_material).read_text(encoding="utf-8")

# One persistent HTTP session for the process lifetime (§4.3 model lifetime)
# — Ollama's keep_alive keeps the model resident between requests on top of
# this; there's no per-question connection setup either way.
_client = httpx.Client(base_url=cfg.llm.host, timeout=httpx.Timeout(120.0, connect=5.0))

_OPTIONS = {
    # num_ctx is non-negotiable: the Ollama default truncates from the
    # *front* of the prompt — the grounding material — silently, and the
    # guardrail then fails invisibly (§3.1, §7.3).
    "num_ctx": cfg.llm.num_ctx,
    "temperature": cfg.llm.temperature,
    "presence_penalty": cfg.llm.presence_penalty,
    "num_predict": cfg.llm.num_predict,
}

# Only one Q&A turn is ever in flight (single lecture, single orchestrator) —
# a module-level flag checked between chunks is sufficient, and matches the
# "explicit cancel flag" pitfall exactly. Set from the event loop thread by
# cancel(); read from the executor thread inside _stream_chat, hence Event
# rather than a plain asyncio primitive.
_cancel_event = threading.Event()


class LlmError(Exception):
    pass


def qa_material_token_estimate() -> int:
    # No tokenizer is loaded for this model; ~4 characters/token is the
    # standard rough estimate for English text and is only used to catch
    # gross oversizing of qa_material.md before it silently gets truncated.
    return len(_QA_MATERIAL) // 4


def validate_qa_material() -> list[str]:
    estimate = qa_material_token_estimate()
    if estimate > cfg.llm.qa_material_token_cap:
        return [
            f"content/qa_material.md is ~{estimate} tokens, over the "
            f"{cfg.llm.qa_material_token_cap}-token cap (§7.3)."
        ]
    return []


def build_system_prompt(narration: str, position: str) -> str:
    return _SYSTEM_TEMPLATE.format(qa_material=_QA_MATERIAL, narration=narration, position=position)


def warm() -> None:
    """Throwaway inference during IDLE -> READY (§4.3) — without this the
    first real question pays a cold model load from disk."""
    for _ in _stream_chat([{"role": "user", "content": "Hello."}]):
        pass


def cancel() -> None:
    _cancel_event.set()


def is_reachable() -> bool:
    """Cheap liveness probe for the operator console's health row (§10.1)
    and the §11 'Ollama unreachable' failure path — no generation, just
    confirms the server answers. A short timeout of its own: this must never
    block the event loop thread for as long as _client's normal 120s chat
    timeout would."""
    try:
        resp = _client.get("/api/tags", timeout=httpx.Timeout(2.0, connect=1.0))
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def answer(question: str, history: list[dict], narration: str, position: str) -> AsyncIterator[str]:
    """Streams raw text chunks from the model as they arrive. The blocking
    HTTP stream runs in an executor thread; chunks cross back to the event
    loop via loop.call_soon_threadsafe onto an asyncio.Queue (§7.4)."""
    _cancel_event.clear()
    system_prompt = build_system_prompt(narration, position)
    messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": question}]

    loop = asyncio.get_running_loop()
    chunk_queue: asyncio.Queue[str | None] = asyncio.Queue()

    def _produce() -> None:
        try:
            for chunk in _stream_chat(messages):
                loop.call_soon_threadsafe(chunk_queue.put_nowait, chunk)
        except Exception as e:
            logger.error("LLM stream failed: %s", e)
        finally:
            loop.call_soon_threadsafe(chunk_queue.put_nowait, None)

    future = loop.run_in_executor(None, _produce)
    try:
        while True:
            chunk = await chunk_queue.get()
            if chunk is None:
                break
            yield chunk
    finally:
        _cancel_event.set()
        await future


def _stream_chat(messages: list[dict]) -> Iterator[str]:
    payload = {
        "model": cfg.llm.model,
        "messages": messages,
        "stream": True,
        "keep_alive": cfg.llm.keep_alive,
        "options": _OPTIONS,
    }
    with _client.stream("POST", "/api/chat", json=payload) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if _cancel_event.is_set():
                return
            if not line:
                continue
            data = json.loads(line)
            if data.get("done"):
                break
            content = data.get("message", {}).get("content", "")
            if content:
                yield content
