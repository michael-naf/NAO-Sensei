from __future__ import annotations

import subprocess
import uuid
import wave
from pathlib import Path

from faster_whisper import WhisperModel

from app.config import cfg

_TEMP_DIR = Path(cfg.audio.temp_dir)
_TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Loaded once at import, held for process lifetime (§4.3 model lifetime) —
# same reasoning as tts.py's PiperVoice: reloading per question pays a
# multi-second cold load from disk.
_model = WhisperModel(cfg.stt.model, device=cfg.stt.device, compute_type=cfg.stt.compute_type)


class SttError(Exception):
    pass


def warm() -> None:
    """Throwaway inference during IDLE -> READY (§4.3) — without this the
    first voice question pays a cold model load."""
    silence_path = _TEMP_DIR / f"warm_{uuid.uuid4().hex[:8]}.wav"
    _write_silence_wav(silence_path)
    try:
        transcribe(str(silence_path))
    finally:
        silence_path.unlink(missing_ok=True)


def convert_to_wav(input_bytes: bytes, suffix: str) -> str:
    """Converts an uploaded recording (webm from Chromium, mp4 from Safari —
    §9.4) to 16 kHz mono WAV on disk via ffmpeg, returning the path. Raises
    SttError on failure; a corrupt/empty upload must not crash the request
    handler, and the caller is responsible for holding the student's queue
    position when that happens (§11)."""
    input_path = _TEMP_DIR / f"stt_in_{uuid.uuid4().hex[:12]}{suffix}"
    output_path = _TEMP_DIR / f"stt_{uuid.uuid4().hex[:12]}.wav"
    input_path.write_bytes(input_bytes)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(input_path), "-ar", "16000", "-ac", "1", str(output_path)],
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")[:500]
            raise SttError(f"ffmpeg could not convert the recording: {stderr}")
        return str(output_path)
    finally:
        input_path.unlink(missing_ok=True)


def transcribe(wav_path: str) -> str:
    """wav_path must already be 16 kHz mono WAV (via convert_to_wav) — this
    does not attempt format detection itself."""
    segments, _info = _model.transcribe(wav_path, language="en")
    return " ".join(segment.text.strip() for segment in segments).strip()


def _write_silence_wav(path: Path, duration_s: float = 0.5, sample_rate: int = 16000) -> None:
    n_samples = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(b"\x00\x00" * n_samples)
