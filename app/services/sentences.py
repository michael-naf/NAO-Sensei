from __future__ import annotations

import re
from typing import AsyncIterable, AsyncIterator, Iterable, Iterator

from app.config import cfg

_BOUNDARY = re.compile(r"[.!?](?=\s)")


def split_stream(chunks: Iterable[str]) -> Iterator[str]:
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while True:
            cut = _next_cut(buffer)
            if cut is None:
                break
            sentence = buffer[:cut].strip()
            buffer = buffer[cut:].lstrip()
            if sentence:
                yield sentence

    remainder = buffer.strip()
    if remainder:
        yield remainder


async def split_stream_async(chunks: AsyncIterable[str]) -> AsyncIterator[str]:
    """Same boundary rules as split_stream, for an async source (§7.4's LLM
    token stream) instead of a sync one (§7.4's TTS-facing narration case)."""
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        while True:
            cut = _next_cut(buffer)
            if cut is None:
                break
            sentence = buffer[:cut].strip()
            buffer = buffer[cut:].lstrip()
            if sentence:
                yield sentence

    remainder = buffer.strip()
    if remainder:
        yield remainder


def _next_cut(buffer: str) -> int | None:
    for m in _BOUNDARY.finditer(buffer):
        end = m.end()
        if end >= cfg.audio.min_sentence_chars:
            return end
    if len(buffer) >= cfg.audio.max_sentence_chars:
        return cfg.audio.max_sentence_chars
    return None
