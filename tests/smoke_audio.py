from __future__ import annotations

import time

from app.audio.pc_sink import PcAudioSink
from app.audio.playback_queue import PlaybackQueue, Utterance
from app.services import tts

SENTENCES = [
    "Dinosaurs lived a very long time ago, long before there were any people.",
    "Sauropods were the largest animals that have ever walked on land.",
    "Tyrannosaurus rex had the strongest bite of any land animal known.",
    "Velociraptor was about the size of a turkey and was covered in feathers.",
    "An asteroid struck the Earth about sixty six million years ago.",
    "Birds are the living descendants of the dinosaurs.",
]


def main() -> None:
    sink = PcAudioSink()
    if not sink.is_available():
        raise RuntimeError("No audio output device available")

    pq = PlaybackQueue(sink)
    events: list[str] = []
    pq.on_utterance_start = lambda u: events.append(f"start seq={u.seq}")
    pq.on_utterance_end = lambda u: events.append(f"end   seq={u.seq}")
    pq.on_idle = lambda: events.append("idle")

    print(f"synthesizing {len(SENTENCES)} sentences...")
    for seq, text in enumerate(SENTENCES):
        wav_path = tts.synthesize(text)
        pq.enqueue(Utterance(turn_id=1, seq=seq, wav_path=wav_path, kind="narration"))
        print(f"  enqueued seq={seq}: {wav_path}")

    print("playing — listen for gapless back-to-back playback...")
    deadline = time.monotonic() + 5.0
    while not pq.is_playing() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not pq.is_playing():
        raise RuntimeError("playback never started")

    time.sleep(1.0)  # let it settle solidly mid-sentence, not right at a boundary
    print("cancelling turn 1 (bump watermark, then interrupt) — like 'skip question'...")
    # flush(new_turn_id) first: advances the staleness watermark so anything
    # from turn 1 still in flight in the prepare thread is discarded when it
    # later surfaces, instead of sneaking into the ready queue and playing.
    pq.flush(2)
    pq.stop_now()

    time.sleep(0.5)
    if pq.is_playing():
        raise RuntimeError(
            "playback continued after cancelling the turn — a stale turn-1 "
            "sentence was played instead of being discarded"
        )

    print(f"event log: {events}")
    print("SMOKE TEST PASSED (ran without exception)")
    print("[HUMAN] verify: gapless playback before the interrupt, the cut landed")
    print("[HUMAN] mid-sentence rather than at a boundary, and nothing played after.")


if __name__ == "__main__":
    main()
