from __future__ import annotations

import uuid
import wave
from pathlib import Path

import numpy as np
import soxr
from piper import PiperVoice, SynthesisConfig

from app.config import cfg

_VOICES_DIR = Path(__file__).resolve().parent.parent.parent / "voices"
_MODEL_PATH = _VOICES_DIR / f"{cfg.tts.voice}.onnx"

_OUTPUT_DIR = Path(cfg.audio.temp_dir)
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Loaded once at import, held for process lifetime — never shell out to
# piper.exe per call, that reloads the ONNX model every time.
_voice = PiperVoice.load(_MODEL_PATH)
_SYN_CONFIG = SynthesisConfig(length_scale=cfg.tts.length_scale)


def synthesize(text: str) -> str:
    chunks = list(_voice.synthesize(text, syn_config=_SYN_CONFIG))
    audio = np.concatenate([c.audio_int16_array for c in chunks])
    source_rate = chunks[0].sample_rate

    # Piper's rate is fixed by voice quality (22050 Hz for medium voices),
    # not by tts.sample_rate — that's a post-processing target, not a Piper
    # parameter. Resample here, explicitly, between Piper and disk.
    if source_rate != cfg.tts.sample_rate:
        audio = soxr.resample(audio, source_rate, cfg.tts.sample_rate).astype(np.int16)

    # PlaybackQueue plays clips back-to-back with no gap of its own (that's
    # deliberate — see §4.4) — so a natural inter-sentence pause has to be
    # part of the clip itself, or sentences run straight into each other.
    silence_samples = cfg.tts.sample_rate * cfg.tts.trailing_silence_ms // 1000
    if silence_samples > 0:
        silence = np.zeros(silence_samples, dtype=np.int16)
        audio = np.concatenate([audio, silence])

    wav_path = _OUTPUT_DIR / f"tts_{uuid.uuid4().hex[:12]}.wav"
    _write_wav(wav_path, audio, cfg.tts.sample_rate, cfg.tts.channels)
    return str(wav_path)


def warm() -> None:
    synthesize("Warming up.")


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int, channels: int) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)  # 16-bit PCM
        f.setframerate(sample_rate)
        f.writeframes(audio.tobytes())
