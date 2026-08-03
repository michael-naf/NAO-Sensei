from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


def _get(d: dict, key: str, path: str):
    if key not in d:
        raise ConfigError(f"Missing required config key: {path}.{key}")
    return d[key]


@dataclass(frozen=True)
class LectureConfig:
    pptx_path: str
    qa_material: str


@dataclass(frozen=True)
class LlmConfig:
    model: str
    host: str
    keep_alive: str
    num_ctx: int
    temperature: float
    presence_penalty: float
    num_predict: int
    max_answer_sentences: int
    qa_material_token_cap: int
    followup_turn_cap: int


@dataclass(frozen=True)
class SttConfig:
    model: str
    device: str
    compute_type: str


@dataclass(frozen=True)
class TtsConfig:
    voice: str
    sample_rate: int
    channels: int
    filler_variants: int
    length_scale: float
    trailing_silence_ms: int
    section_gap_ms: int


@dataclass(frozen=True)
class AudioConfig:
    temp_dir: str
    ready_queue_size: int
    min_sentence_chars: int
    max_sentence_chars: int


@dataclass(frozen=True)
class SlidesConfig:
    display: int
    com_timeout_s: float


@dataclass(frozen=True)
class TimingsConfig:
    reply_window_s: float
    compose_window_s: float
    max_recording_s: float
    max_typed_chars: int
    question_repeat_max_words: int
    disconnect_grace_s: float


@dataclass(frozen=True)
class ServerConfig:
    mode: str
    host: str
    port: int
    public_url: str | None
    operator_token: str
    max_students: int


@dataclass(frozen=True)
class PathsConfig:
    sessions_dir: str
    logs_dir: str


@dataclass(frozen=True)
class GesturesConfig:
    library: str
    enabled: bool
    interval_s: tuple[float, float]
    speed: float
    previewer: bool


@dataclass(frozen=True)
class NaoTimeouts:
    connect_s: float
    stop_s: float
    motion_s: float
    health_s: float
    upload_s: float
    posture_s: float
    play_margin_s: float


@dataclass(frozen=True)
class NaoConfig:
    bridge_url: str
    audio_dir: str
    volume: int
    timeouts: NaoTimeouts


@dataclass(frozen=True)
class Config:
    audio_output: str
    body: str
    lecture: LectureConfig
    llm: LlmConfig
    stt: SttConfig
    tts: TtsConfig
    audio: AudioConfig
    slides: SlidesConfig
    timings: TimingsConfig
    server: ServerConfig
    paths: PathsConfig
    gestures: GesturesConfig
    nao: NaoConfig


def _load(path: Path) -> Config:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    lecture = _get(raw, "lecture", "")
    llm = _get(raw, "llm", "")
    stt = _get(raw, "stt", "")
    tts = _get(raw, "tts", "")
    audio = _get(raw, "audio", "")
    slides = _get(raw, "slides", "")
    timings = _get(raw, "timings", "")
    server = _get(raw, "server", "")
    paths = _get(raw, "paths", "")
    gestures = _get(raw, "gestures", "")
    nao = _get(raw, "nao", "")
    nao_timeouts = _get(nao, "timeouts", "nao")

    return Config(
        audio_output=_get(raw, "audio_output", ""),
        body=_get(raw, "body", ""),
        lecture=LectureConfig(
            pptx_path=_get(lecture, "pptx_path", "lecture"),
            qa_material=_get(lecture, "qa_material", "lecture"),
        ),
        llm=LlmConfig(
            model=_get(llm, "model", "llm"),
            host=_get(llm, "host", "llm"),
            keep_alive=_get(llm, "keep_alive", "llm"),
            num_ctx=_get(llm, "num_ctx", "llm"),
            temperature=_get(llm, "temperature", "llm"),
            presence_penalty=_get(llm, "presence_penalty", "llm"),
            num_predict=_get(llm, "num_predict", "llm"),
            max_answer_sentences=_get(llm, "max_answer_sentences", "llm"),
            qa_material_token_cap=_get(llm, "qa_material_token_cap", "llm"),
            followup_turn_cap=_get(llm, "followup_turn_cap", "llm"),
        ),
        stt=SttConfig(
            model=_get(stt, "model", "stt"),
            device=_get(stt, "device", "stt"),
            compute_type=_get(stt, "compute_type", "stt"),
        ),
        tts=TtsConfig(
            voice=_get(tts, "voice", "tts"),
            sample_rate=_get(tts, "sample_rate", "tts"),
            channels=_get(tts, "channels", "tts"),
            filler_variants=_get(tts, "filler_variants", "tts"),
            length_scale=_get(tts, "length_scale", "tts"),
            trailing_silence_ms=_get(tts, "trailing_silence_ms", "tts"),
            section_gap_ms=_get(tts, "section_gap_ms", "tts"),
        ),
        audio=AudioConfig(
            temp_dir=_get(audio, "temp_dir", "audio"),
            ready_queue_size=_get(audio, "ready_queue_size", "audio"),
            min_sentence_chars=_get(audio, "min_sentence_chars", "audio"),
            max_sentence_chars=_get(audio, "max_sentence_chars", "audio"),
        ),
        slides=SlidesConfig(
            display=_get(slides, "display", "slides"),
            com_timeout_s=_get(slides, "com_timeout_s", "slides"),
        ),
        timings=TimingsConfig(
            reply_window_s=_get(timings, "reply_window_s", "timings"),
            compose_window_s=_get(timings, "compose_window_s", "timings"),
            max_recording_s=_get(timings, "max_recording_s", "timings"),
            max_typed_chars=_get(timings, "max_typed_chars", "timings"),
            question_repeat_max_words=_get(timings, "question_repeat_max_words", "timings"),
            disconnect_grace_s=_get(timings, "disconnect_grace_s", "timings"),
        ),
        server=ServerConfig(
            mode=_get(server, "mode", "server"),
            host=_get(server, "host", "server"),
            port=_get(server, "port", "server"),
            public_url=_get(server, "public_url", "server"),
            operator_token=_get(server, "operator_token", "server"),
            max_students=_get(server, "max_students", "server"),
        ),
        paths=PathsConfig(
            sessions_dir=_get(paths, "sessions_dir", "paths"),
            logs_dir=_get(paths, "logs_dir", "paths"),
        ),
        gestures=GesturesConfig(
            library=_get(gestures, "library", "gestures"),
            enabled=_get(gestures, "enabled", "gestures"),
            interval_s=tuple(_get(gestures, "interval_s", "gestures")),
            speed=_get(gestures, "speed", "gestures"),
            previewer=_get(gestures, "previewer", "gestures"),
        ),
        nao=NaoConfig(
            bridge_url=_get(nao, "bridge_url", "nao"),
            audio_dir=_get(nao, "audio_dir", "nao"),
            volume=_get(nao, "volume", "nao"),
            timeouts=NaoTimeouts(
                connect_s=_get(nao_timeouts, "connect_s", "nao.timeouts"),
                stop_s=_get(nao_timeouts, "stop_s", "nao.timeouts"),
                motion_s=_get(nao_timeouts, "motion_s", "nao.timeouts"),
                health_s=_get(nao_timeouts, "health_s", "nao.timeouts"),
                upload_s=_get(nao_timeouts, "upload_s", "nao.timeouts"),
                posture_s=_get(nao_timeouts, "posture_s", "nao.timeouts"),
                play_margin_s=_get(nao_timeouts, "play_margin_s", "nao.timeouts"),
            ),
        ),
    )


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
cfg: Config = _load(_CONFIG_PATH)
