from __future__ import annotations

# A deliberately small, local, deterministic blocklist — not general content
# moderation (that's a hypothetical future requirement this MVP doesn't need).
# Its one job: never let a flagged question reach stage-2 ("You asked: ...")
# or the LLM, since the repeat-back is a live TTS pass with no guardrail of
# its own — the LLM's own guardrail (§7.3) only covers what NAO *answers*,
# not what it *repeats*.
_BLOCKED_TERMS = [
    "fuck", "shit", "dick", "cock", "pussy", "cunt", "asshole", "bitch",
    "suck my", "blowjob", "blow job", "nigger", "faggot", "retard",
    "kill yourself", "rape",
]


def is_inappropriate(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _BLOCKED_TERMS)
