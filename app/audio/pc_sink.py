from __future__ import annotations

import threading
import wave

import numpy as np
import sounddevice as sd


_CHUNK_MS = 50


class PcAudioSink:
    """One OutputStream is held open across consecutive play() calls,
    reopened only if the format changes or stop() aborts it. Opening and
    closing a fresh stream per sentence produced an audible clip at the
    tail of every sentence — the device needs a moment to settle on open,
    and closing right after the last write() cut off before the hardware
    buffer had actually finished draining."""

    def __init__(self) -> None:
        self._stream: sd.OutputStream | None = None
        self._stream_format: tuple[int, int] | None = None
        self._stop_event: threading.Event | None = None
        self._lock = threading.Lock()

    def prepare(self, wav_path: str) -> str:
        return wav_path

    def play(self, token: str) -> None:
        data, sample_rate, channels = _read_wav(token)
        stop_event = threading.Event()

        stream = self._ensure_stream(sample_rate, channels)
        with self._lock:
            self._stop_event = stop_event

        # Write in small chunks and re-check the stop flag between them —
        # a single blocking write() of the whole clip does not reliably
        # abort promptly when stop() fires on another thread (abort() races
        # against however much is already queued for the one big write).
        chunk_size = max(1, sample_rate * _CHUNK_MS // 1000)
        try:
            for start in range(0, len(data), chunk_size):
                if stop_event.is_set():
                    break
                stream.write(data[start : start + chunk_size])
        except sd.PortAudioError:
            pass  # aborted from another thread via stop()
        finally:
            with self._lock:
                if self._stop_event is stop_event:
                    self._stop_event = None

    def stop(self) -> None:
        # Callable from another thread while play() is blocked in stream.write().
        with self._lock:
            stream = self._stream
            stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        if stream is not None:
            stream.abort(ignore_errors=True)
            # An aborted stream can't be written to again — drop it so the
            # next play() call reopens a fresh one via _ensure_stream().
            with self._lock:
                if self._stream is stream:
                    self._stream = None
                    self._stream_format = None

    def is_available(self) -> bool:
        try:
            sd.check_output_settings()
            return True
        except Exception:
            return False

    def _ensure_stream(self, sample_rate: int, channels: int) -> sd.OutputStream:
        with self._lock:
            fmt = (sample_rate, channels)
            if self._stream is not None and self._stream_format == fmt:
                return self._stream
            if self._stream is not None:
                self._stream.close(ignore_errors=True)
            # 'high' latency trades a bit of extra buffering for underrun
            # resistance — the default was too tight to absorb the Python
            # bookkeeping between sentences (callbacks, queue checks) that
            # runs on this same thread between one play() call and the
            # next, causing an audible glitch right at each sentence
            # boundary — worse at '.' (a full Utterance boundary with that
            # bookkeeping) than ',' (inside one tight write loop, no gap).
            stream = sd.OutputStream(
                samplerate=sample_rate, channels=channels, dtype="int16", latency="high"
            )
            stream.start()
            self._stream = stream
            self._stream_format = fmt
            return stream


def _read_wav(path: str) -> tuple[np.ndarray, int, int]:
    with wave.open(path, "rb") as f:
        channels = f.getnchannels()
        sample_rate = f.getframerate()
        frames = f.readframes(f.getnframes())
    data = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels)
    return data, sample_rate, channels
