# NAO Lecturer — Implementation Plan

**Companion to:** `specs.md` v2.2 · **Rules:** `CLAUDE.md`
`specs.md` says *what* and *why*. This says *what to build next, and when to stop
and test*.

---

## How this works

The build is a **linear sequence of work, gated by checkpoints.** No schedules,
estimates, or sprints — only tasks and gates.

**A checkpoint is a hard stop.** Do not begin the next task until it passes.
Checkpoints exist only where a failure would otherwise be discovered late and
expensively — not after every task.

### Verification legend

Every checkpoint item is tagged:

| Tag | Meaning |
|---|---|
| **[AUTO]** | A command with a definite expected output. Claude Code runs it and reads the result. |
| **[HUMAN]** | Requires ears, eyes, a phone, a projector, or the robot. **Claude Code cannot verify this and must not claim it passed.** Stop and ask. |

If a checkpoint contains any **[HUMAN]** item, Claude Code stops there, reports
the **[AUTO]** results, and waits for the user.

### Task format

Each task names the file, the spec section, the interface to implement, and the
known pitfall. Build exactly that. Do not invent scope.

---

# PRE-FLIGHT — environment and content

No application code. Cross-reference: `specs.md` §3.2.

### P1 — Toolchain

- Python **3.11** (not 3.12+ — some wheels lag), venv created and activated.
- Ollama installed; pull a **text-only** Qwen3.5 9B Q4_K_M build (§3.1).
- ffmpeg and ffprobe on `PATH`.
- `pip install piper-tts soxr sounddevice numpy python-pptx pywin32 pyyaml fastapi "uvicorn[standard]" httpx "qrcode[pil]" faster-whisper`
- Microsoft PowerPoint installed and licensed.
- Two displays, Windows set to **Extend** (not Duplicate).

### P2 — Repo skeleton

- Directory tree per §15. Empty files are fine.
- `config.yaml` copied verbatim from §14, paths and IPs adjusted.
- `CLAUDE.md` in the repo root.
- `git init`, commit.

### P3 — Content

- `content/lecture.pptx` — 5 slides, dinosaurs, static only.
- Speaker notes on **every** slide, sections split by `---` on its own line, at
  least one `[NO-CHECKPOINT]` (§5.1).
- `content/qa_material.md` — under 3500 tokens (§7.3).
- **Write down one guardrail-test question**: absent from the material, but
  something the model would confidently answer from pretraining (§7.3). This is
  a deliverable, not a nicety — it is the only proof that grounding works.
- Note which side the projector is on, for the `gaze.slides` `HeadYaw` sign
  (§12.6.3).

---

## ✅ CHECKPOINT 1 — Environment is real

*Everything downstream assumes these. A failure here stays invisible until it is
expensive.*

```bash
python --version                      # [AUTO] expect 3.11.x
ffprobe -version                      # [AUTO] expect a version banner
ollama list                           # [AUTO] expect the model present
```

```bash
# [AUTO] run a prompt, then immediately:
ollama ps
```

Expect **100% GPU**. Any CPU split → use `llama3.1:8b-instruct-q4_K_M` instead
and record the change in `config.yaml`.

- **[HUMAN]** PowerPoint opens a real presentation and enters slideshow mode.
- **[HUMAN]** A window dragged to display 2 stays there.
- **[HUMAN]** The deck exists, every slide has notes, and the notes read sensibly
  aloud.
- **[HUMAN]** The guardrail-test question is written down.

---

# PHASE 1 — Audio pipeline

The foundation. Everything above assumes it.

### 1.1 — `app/config.py`

**Spec:** §14

```python
cfg: Config          # module-level, loaded at import
```

- `config.yaml` → typed object (dataclasses or Pydantic).
- **Missing key = raise.** No silent defaults; §14 says every timing lives in
  config, so absence is a bug.

### 1.2 — `app/services/tts.py`

**Spec:** §12.2, §4.3 (model lifetime)

```python
def synthesize(text: str) -> str: ...   # returns wav path
def warm() -> None: ...
```

- `PiperVoice` loaded **once** at module init, held for process lifetime.
- Pipeline: Piper → `soxr` resample 22050→16000 → 16-bit mono PCM →
  `runtime/audio/`.
- **Pitfall:** Piper medium voices emit **22050 Hz**. Without the `soxr` step
  everything works on the PC and NAO refuses the file in Phase 6.
- **Never** shell out to `piper.exe` — that reloads the ONNX model per call.

### 1.3 — `app/services/sentences.py`

**Spec:** §7.4

```python
def split_stream(chunks: Iterable[str]) -> Iterator[str]: ...
```

- Boundary = `[.!?]` + whitespace or end-of-stream.
- Minimum emit **20 chars**; shorter fragments merge forward.
- Force-split at **300 chars** if no terminator appeared.
- **Flush the remainder at stream end**, terminator or not.
- **Unit-test this** (`tests/test_sentences.py`): abbreviations, decimals, a
  400-char run-on, a stream ending without a terminator.

### 1.4 — `app/audio/sink.py` + `app/audio/pc_sink.py`

**Spec:** §4.4

```python
class AudioSink(Protocol):
    def prepare(self, wav_path: str) -> str: ...
    def play(self, token: str) -> None: ...      # blocks until playback ends
    def stop(self) -> None: ...                  # thread-safe, callable during play()
    def is_available(self) -> bool: ...
```

- `PcAudioSink.prepare()` returns the path unchanged.
- **Pitfall:** `stop()` must interrupt a blocked `play()` from another thread.
  Use an abortable stream (`sounddevice` with an explicit stream object). A
  fire-and-forget `playsound`-style call **cannot** be interrupted and forces a
  rewrite when Skip question is built.

### 1.5 — `app/audio/playback_queue.py`

**Spec:** §4.4

```python
@dataclass(frozen=True)
class Utterance:
    turn_id: int; seq: int; wav_path: str; kind: str

class PlaybackQueue:
    def enqueue(self, u: Utterance) -> None: ...
    def flush(self, turn_id: int | None = None) -> None: ...
    def stop_now(self) -> None: ...
    def stop_after_current(self) -> None: ...
    def resume(self) -> None: ...
    def is_playing(self) -> bool: ...
    on_utterance_start: Callable | None
    on_utterance_end:   Callable | None
    on_idle:            Callable | None
```

- Two threads, bounded ready queue `maxsize=2`.
- **Pitfall:** re-check `turn_id` **at dequeue**, not only at flush. An upload
  already in flight lands in the ready queue after the flush and plays one stale
  sentence.
- **Pitfall:** callbacks fire on the **play thread**. Consumers must marshal via
  `loop.call_soon_threadsafe` before touching state (rule 2).

### 1.6 — `tests/smoke_audio.py`

- Synthesize 6 sentences, enqueue all, play; call `stop_now()` partway through.

---

## ✅ CHECKPOINT 2 — Audio foundation

*A wrong WAV format is discovered on robot day. Non-gapless playback invalidates
the entire streaming design. Both are caught here or not at all.*

```bash
python -m tests.smoke_audio           # [AUTO] runs without exception
```

```bash
# [AUTO] format check — the one that matters most
ffprobe -v error -show_entries stream=sample_rate,channels,sample_fmt \
        -of csv=p=0 runtime/audio/<any>.wav
```

Expect exactly: **`16000,1,s16`**

```bash
python -m pytest tests/test_sentences.py    # [AUTO] all pass
```

- **[HUMAN]** The 6 sentences play back-to-back with **no audible gap**.
- **[HUMAN]** `stop_now()` cuts playback **mid-sentence**, not at the next
  boundary.
- **[HUMAN]** After a `turn_id` bump, no stale sentence is ever heard.

---

# PHASE 2 — Lecture delivery

### 2.1 — `app/script_parser.py`

**Spec:** §5.1–5.3

```python
def parse(pptx_path: str) -> LectureScript: ...
```

- Split notes on a line containing exactly `---`.
- **`[NO-CHECKPOINT]` suppresses the checkpoint *after* that section.** Every
  section has a checkpoint by default. This meaning is **inverted** from v1 of
  the spec — do not follow older intuition.
- Marker stripped before the text reaches TTS.
- Final section always has a checkpoint regardless of marker.
- Build `Section` and `LectureScript` including `full_narration` (§5.3).
- **Unit-test** (`tests/test_parser.py`) against a fixture deck.

### 2.2 — Validation

**Spec:** §5.4

- All checks from §5.4. **Empty speaker notes on any slide is fatal.**
- Report all failures, not just the first.

### 2.3 — `app/slides.py`

**Spec:** §6.4, §4.3

```python
class SlideController:
    def open(self, pptx_path: str) -> None: ...
    def goto(self, slide_index: int) -> None: ...   # 1-based
    def close(self) -> None: ...
```

- Dedicated thread, `CoInitializeEx(COINIT_APARTMENTTHREADED)`. Commands in via
  `queue.Queue`, results out via `Future`.
- **5-second timeout on every awaited future. Timeout counts as a fault.**
- **Pitfall:** a dead PowerPoint does not always raise — `GotoSlide` can block
  forever, hanging the orchestrator with no fault ever detected.
- **Do not subscribe to COM events.** They need a message pump; a thread blocked
  on `queue.get()` is not pumping.

### 2.4 — `app/state.py` + `app/orchestrator.py`

**Spec:** §6.1, §6.2, §6.4

- States and transitions per §6.1. There is no `RECOVERING`.
- Main loop per §6.2. **`goto()` only when `slide_index` changes** between
  sections.
- **Position is owned by the orchestrator and never read back from PowerPoint.**
- On COM fault: `stop_after_current()`, → `PAUSED`, console alert.
- Checkpoints are no-ops for now — Q&A does not exist yet.

---

## ✅ CHECKPOINT 3 — Lecture delivers

*Proves parser, COM thread, and orchestrator together. The fault path is tested
here because it cannot be tested safely during a live demo.*

```bash
python -m pytest tests/test_parser.py       # [AUTO] all pass
```

```bash
# [AUTO] validation rejects a bad deck
python -m app.main --validate content/broken_deck.pptx
```

Expect a clear error naming the un-narrated slide.

- **[HUMAN]** The 5-slide deck narrates end to end on the projector, slides
  changing at the right moments.
- **[HUMAN]** No flicker or redundant slide change across multi-section slides.
- **[HUMAN]** Kill `POWERPNT.EXE` mid-lecture → the current sentence finishes,
  state goes `PAUSED`, an alert appears. **It does not hang.**

---

# PHASE 3A — Server, queue, student app

### 3A.1 — `app/web/server.py`

**Spec:** §9.4, §4.3

- FastAPI + uvicorn on the **same event loop** as the orchestrator.
- WebSocket for pushed state; REST for actions.
- Mode A only: plain HTTP on the LAN.

### 3A.2 — `app/queue.py`

**Spec:** §6.3

- Ordered queue, one position per student, join/leave.
- Inspected **only at checkpoints**.
- Students queueing during a drain are appended to the same drain.

### 3A.3 — `app/web/static/student.html`

**Spec:** §9.2, §9.3, §9.5

- The seven states from §9.3. **Typed input only** at this stage.
- Random ID in `localStorage`; optional display name; **never prompt for a real
  name**.
- Live queue position over WebSocket; reconnection restores position.
- Max typed question 500 chars.

---

# PHASE 3B — Grounded Q&A

### 3B.1 — `app/prompts/qa_system.txt`

**Spec:** §7.3

- Four parts assembled fresh per question: guardrail, whole `qa_material.md`,
  `full_narration`, current position marker.
- Guardrail: answer **only** from supplied material; if absent, say so plainly.
- 2–4 sentences.

### 3B.2 — `app/services/llm.py`

**Spec:** §3.1, §7.4

```python
async def answer(question: str, history: list, position: str) -> AsyncIterator[str]: ...
def warm() -> None: ...
def cancel() -> None: ...
```

- Persistent HTTP session. `num_ctx: 8192`, `temperature: 0.3`,
  `presence_penalty: 0`, `keep_alive`, `num_predict` backstop.
- **Pitfall:** the `num_ctx` default truncates from the *front* of the prompt —
  the grounding material — **silently**. The guardrail then fails invisibly.
- Stream in an executor thread; hand sentences back via
  `loop.call_soon_threadsafe` onto an `asyncio.Queue`.
- **Explicit cancel flag** checked between chunks, or "skip question" stops the
  audio while the GPU keeps generating.

### 3B.3 — Fillers

**Spec:** §7.1

- 3–4 stage-1 variants pre-synthesized at startup, cached to disk.
- **Both stages fire for typed and voice alike.**
- Stage-2 skipped above `question_repeat_max_words` (25).

### 3B.4 — Turn loop

**Spec:** §7.1, §6.3, §7.2

- Filler enqueued first, answer sentences behind it — `PlaybackQueue` handles
  ordering, no special-casing.
- Reply/Done offered on `on_idle`.
- **5 s** resolution window, **30 s** composition window, both `asyncio.Task`s
  cancelled by student activity.
- 6-turn follow-up cap; over it, tell the student to re-queue.

### 3B.5 — `app/transcript.py`

**Spec:** §7.5

- Appended after **each** answer, never buffered.
- All §7.5 fields; follow-ups nested under their parent.
- Write failure logged, non-fatal.

---

## ✅ CHECKPOINT 4 — The guardrail holds

*The most important correctness gate in the project. A broken guardrail means a
robot confidently inventing facts in front of a class — and it looks completely
normal until it happens.*

```bash
# [AUTO] confirm the context setting actually took
curl http://localhost:11434/api/show -d '{"name":"<model>"}'
```

- **[HUMAN]** Ask the **guardrail-test question from P3**. NAO must decline —
  *"that isn't covered in the material"* — not answer it.
  **If it answers, grounding is broken. Check `num_ctx` first.**
- **[HUMAN]** Ask something answerable only from the *narration* (e.g. *"what did
  you just say about sauropods?"*). It must answer correctly.
- **[HUMAN]** Ask a pronoun-heavy question (*"how big did they get?"*) mid-slide.
  The position marker should disambiguate it.
- **[HUMAN]** Answers are 2–4 sentences, not paragraphs.

---

## ✅ CHECKPOINT 5 — Full typed loop on real phones

*Turn-taking, timing windows, and the transcript can only be tested with real
devices and real people.*

- **[HUMAN]** Two phones join over the LAN, queue, and see live positions.
- **[HUMAN]** The first student's question is answered; **Reply** continues the
  same context; **Done** advances to the second student.
- **[HUMAN]** Leaving the Reply/Done prompt untouched auto-resolves after **5 s**
  and the queue advances.
- **[HUMAN]** Choosing Reply then going silent abandons the turn after **30 s**.
- **[HUMAN]** The lecture resumes at the correct section once the queue drains.
- **[AUTO]** `sessions/lecture_*.md` contains both exchanges with follow-ups
  correctly nested and the grounding flag present.

---

# PHASE 3C — Embodiment

### 3C.1 — `content/gestures.yaml`

**Spec:** §12.6.2, §12.6.3

- Authored in Choregraphe: virtual robot → **Sit** → pose arms → read slider
  values.
- `rest` first and correct; everything returns to it.
- Then `explain_open`, `point_slide`, `beat`, `thinking`, `acknowledge`, plus
  gaze targets.
- **Pitfall:** author these **seated**. The reachable arm envelope differs from
  standing, and `rest` needs arms further forward and outward.

### 3C.2 — `app/body/`

**Spec:** §12.6, §12.4

```python
class Body(Protocol):
    def gesture(self, name: str) -> None: ...
    def gaze(self, target: str) -> None: ...
    def leds(self, pattern: str) -> None: ...
    def posture(self, name: str) -> None: ...
    def stiffness(self, on: bool) -> None: ...
    def is_gesturing(self) -> bool: ...
    def is_available(self) -> bool: ...
```

- `gesture_library.py` validates on load: every joint on the §12.1.1 whitelist,
  every angle within limits, every gesture ends at `rest`. **Failures block
  startup.**
- `console_body.py` logs and emits previewer events.
- `scheduler.py` subscribes to `on_utterance_start`, fires every 8–12 s jittered
  while `is_playing()`, **one gesture at a time**, context sets per §12.4.2.

### 3C.3 — `app/web/static/previewer.js`

**Spec:** §12.6.4

- Front-view stick figure: shoulder pitch/roll, elbow roll, head yaw/pitch.
- Animated live from `ConsoleBody` events. Crude is fine.

---

## ✅ CHECKPOINT 6 — Embodiment design is right, before hardware exists

*The gesture design is fixed here. Phase 7 tunes numbers — it does not redesign.
If the cadence is wrong now, it will be wrong on the robot with no time left.*

```bash
python -m pytest tests/test_gesture_library.py   # [AUTO] whitelist + limits
```

Include a fixture containing `LKneePitch` — loading it must **fail**.

- **[HUMAN]** During narration the previewer shows gestures every 8–12 s, never
  overlapping.
- **[HUMAN]** Gaze switches to `slides` when narrating, `class` when answering.
- **[HUMAN]** LEDs track state: white / blue / green / yellow.
- **[HUMAN]** Every gesture visibly returns to `rest`; arms do not drift over a
  full run.
- **[HUMAN]** The cadence looks deliberate, not twitchy.

---

# PHASE 4 — Voice input (Mode B)

### 4.1 — `app/web/tunnel.py`

**Spec:** §9.1

- Launch `cloudflared tunnel --url http://localhost:8000`, parse the URL from
  stdout, feed the QR generator. `server.public_url` overrides.
- **Falling back to Mode A must not require an app restart.**

### 4.2 — Browser recording

**Spec:** §9.4, §9.5

- `MediaRecorder` with `isTypeSupported()` probing in preference order.
- Record the resulting MIME type alongside the blob.
- **Pitfall:** Safari writes `audio/mp4` (AAC) and never WebM — every iPhone in
  the room produces MP4. Chromium writes WebM.
- 30-second hard stop.

### 4.3 — `app/services/stt.py`

**Spec:** §3, §11

- ffmpeg converts either container → 16 kHz mono WAV.
- `WhisperModel` loaded once, CPU, int8, `warm()` at startup.
- STT failure asks the student to retry and **holds their queue position**.

---

## ✅ CHECKPOINT 7 — Voice works on both phone families

*The Safari/Chromium container divergence is invisible until tested on real
hardware of each kind.*

- **[HUMAN]** A question recorded on an **iPhone** transcribes and is answered.
- **[HUMAN]** A question recorded on an **Android** transcribes and is answered.
- **[HUMAN]** Killing the tunnel mid-session falls back to typed-only **without
  restarting the app**.
- **[HUMAN]** The stage-1 filler starts near-instantly, masking STT.

---

# PHASE 5 — Console and hardening

### 5.1 — `app/web/static/operator.html`

**Spec:** §10.1

- Everything in §10.1, including component health and the QR code.
- Embed the previewer from 3C.3.

### 5.2 — Controls

**Spec:** §10.2, §10.3

- The seven MVP controls. **Skip section also clears the queue**, with a
  confirmation showing queue size.
- Token guard on `/operator`.

### 5.3 — Failure handling

**Spec:** §11

- Ollama unreachable → Q&A disabled, queue frozen, **narration continues**.
- Everything else logged and surfaced only.

---

## ✅ CHECKPOINT 8 — Demo-ready

*This is the presentable project. Everything after this is upside.*

- **[HUMAN]** Every control works **mid-lecture**: Start, Pause/Resume, Skip
  section, Skip question, Clear queue, End lecture.
- **[HUMAN]** Kill Ollama mid-lecture → narration continues, Q&A disabled, health
  shows *down*, students are told.
- **[HUMAN]** A failed STT submission holds the student's queue position.
- **[HUMAN]** A full 5-slide run with two students, start to finish, no
  intervention.
- **[AUTO]** Latency measured against §8; NFR-2's two clauses hold.

> **Stop here if time is short.** The PC version is a complete, presentable
> project. Phases 6–7 are upside, not the deliverable.

Tag this commit — Checkpoint 10 diffs against it.

---

# PHASE 6 — NAO integration

**Do not start any of this before the robot is physically present.** The bridge
cannot be tested without it (§3.2), and code written weeks early rots.

### 6.1 — `nao_bridge/`

**Spec:** §12.1, §12.1.1

- `bridge.py` — Python 2.7, `SocketServer.ThreadingMixIn`, endpoints per §12.1.
- **Pitfall:** a single-threaded server queues `/stop` behind the blocking
  `/play` it is meant to interrupt. Skip question would never work.
- `whitelist.py` — refuse the **whole request** on any non-whitelisted joint.
- Persistent `ALProxy`, reconnect on failure.
- `setCollisionProtectionEnabled("Arms", True)` at startup.
- Keep it under ~150 lines. No lecture logic.

### 6.2 — `app/audio/nao_sink.py` + `app/body/nao_body.py`

**Spec:** §12.3, §12.1.2

- `prepare()` = POST `/upload`; `play()` = POST `/play`.
- All timeouts from §12.1.2; connect timeout 1 s everywhere.
- **`turn_id` is bumped before `/stop` is sent.** The orchestrator never waits on
  the robot to agree.
- Motion and LED failures logged and swallowed.

### 6.3 — Bring-up order

One hard thing at a time. **Audio before motion.**

1. `curl http://<nao-ip>:8765/health` → battery and connection state.
2. `/volume`, then `/posture` `"Sit"`, then `/stiffness`.
3. `/upload` + `/play` with one hand-made WAV.
4. Whitelist test (Checkpoint 9).
5. `/gaze`, then one gesture, then the scheduler.
6. Full system with `audio_output: nao`, `body: nao`.

---

## ✅ CHECKPOINT 9 — Safety, before any motion

*Run this before the first gesture ever reaches the robot. It is the only proof
that NAO cannot be commanded into a fall.*

```bash
# [AUTO] a leg joint must be refused
curl -X POST http://<nao-ip>:8765/gesture \
     -d '{"name":"bad","keyframes":[{"t":1.0,"LKneePitch":0.5}],"speed":0.15}'
```

Expect a **refusal**, and the bridge log must show `ALMotion` was never called.

```bash
# [AUTO] a mixed request must be refused in full, not partially applied
curl -X POST http://<nao-ip>:8765/gesture \
     -d '{"name":"mixed","keyframes":[{"t":1.0,"LShoulderPitch":1.1,"LKneePitch":0.5}],"speed":0.15}'
```

Expect refusal. **[HUMAN]** Confirm the shoulder did **not** move.

- **[HUMAN]** NAO is seated, stable, charger cable has slack.
- **[HUMAN]** `/posture` `"Sit"` (not `SitRelax`) — arms remain stiff enough to
  hold a pose.

---

## ✅ CHECKPOINT 10 — The swap is clean

*Validates NFR-5 and NFR-9 — the entire justification for the two-seam design.*

```bash
# [AUTO] only the sink and body implementations changed
git diff --stat <checkpoint-8-tag>..HEAD -- app/ \
    ':!app/audio/nao_sink.py' ':!app/body/nao_body.py'
```

Expect **no changes** outside those two files.

- **[HUMAN]** The full system runs with all audio from NAO's speakers.
- **[HUMAN]** Sentence-to-sentence gaps are imperceptible.
- **[HUMAN]** Gestures fire during narration and answering without interrupting
  speech.
- **[HUMAN]** Skip question cuts NAO mid-sentence.

---

# PHASE 7 — Tuning on hardware

No new components. Numbers only.

- Tune keyframe angles against the physical robot.
- Tune `gestures.speed` and `interval_s` by eye.
- Set NAO's volume for the actual room.
- Full-length rehearsal; check joint temperatures afterwards.

**[HUMAN]** NAO looks alive throughout, no audio interruption, no balance
incident across a full-length run.

---

## If time runs short

Cut in this order. Each cut leaves a working demo.

1. **Phase 7** — ship with untuned gestures.
2. **Phase 6** — ship the PC version. The seams mean this costs nothing
   architecturally, and the previewer still demonstrates the embodiment design.
3. **Phase 4** — typed questions only. Mode A was always the primary target.
4. **Phase 5.2** — trim the console to Start / Pause / Skip question / Clear
   queue.

**Never cut:** the guardrail test (Checkpoint 4), the transcript (3B.5), or the
joint whitelist (Checkpoint 9).

---

## Not pip-installable

Install separately, verify on `PATH`: **ffmpeg/ffprobe**, **Ollama**,
**cloudflared** (Phase 4 only), **Microsoft PowerPoint**.

Deliberately absent: `paramiko` (SFTP replaced by HTTP upload, §12.3) and
`pynaoqi` (the bridge uses NAO's own preinstalled `naoqi`, §4.2).
