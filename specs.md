# NAO Lecturer — Technical Specification

**Version:** 2.2
**Status:** Approved for implementation
**Supersedes:** v2.1, v2.0, v1.0
**Scope:** Full project — PC-first build (Phases 0–5) and NAO integration (Phases 6–7).

> **What changed since v2.0.** Embodiment now has a seam of its own (`Body`,
> §12.6) built in Phase 3 rather than Phase 7, backed by a gesture library
> authored as data. Balance safety is enforced structurally by a joint whitelist
> in the bridge (§12.1.1) and a fixed seated posture (§12.4.4). The Windows
> Python 2.7 SDK proved unobtainable, so the bridge runs **on the robot** and
> SFTP is replaced by an HTTP upload (§4.2, §12.3). The LLM moves to Qwen3.5 9B
> (§3.1). Both filler stages now fire for typed questions too (§7.1). A full
> change log is in §17.

---

## 1. Overview

A NAO V5 humanoid robot delivers a prepared lecture to a class. It narrates a
PowerPoint deck section by section, advances its own slides, and answers student
questions at defined checkpoints. Students queue questions through a phone web
app. All intelligence runs on a Windows PC; NAO is an embodied output device.

The MVP target is a **five-slide lecture on dinosaurs** delivered to a small
class (five students), demonstrated for a course.

### 1.1 Core principle

**Brains on the PC, body in the robot.** NAO V5 (Intel Atom CPU) cannot run an
LLM, speech recognition, or any heavy processing. NAO receives only two kinds of
command: *play this audio file* and *perform this gesture*. Every design decision
below follows from this.

### 1.2 Content authority

The system performs authored content and does not improvise:

| Artifact | Authored by | Used for |
|---|---|---|
| `lecture.pptx` slides | Human | Visual display |
| Speaker notes (in the .pptx) | Human | Verbatim narration script |
| `qa_material.md` | Human | Grounding source for answers |

Narration is spoken **verbatim** from speaker notes — the LLM is never invoked
during narration. The LLM is invoked **only** to answer student questions, and
only from the grounding context defined in §7.3.

### 1.3 MVP scope and deliberate exclusions

Out of scope, by decision:

- Leg movement or locomotion. NAO stays in place.
- PowerPoint animations, transitions, embedded video/audio.
- NAO's onboard microphone (unused — all audio input is via student phones).
- NAO's built-in TTS (`ALTextToSpeech`) — replaced by Piper on the PC.
- Retrieval/embeddings for Q&A grounding (§7.3 uses whole-document context).
- Student authentication, persistence across lectures, multi-lecture management.
- **Automatic PowerPoint crash recovery.** A slide fault pauses the lecture and
  hands control to the operator (§6.4).
- **Slides with empty speaker notes.** Every slide in the deck must be narrated.
  Validation rejects a deck containing an un-narrated slide (§5.4).
- **Cloud AI services of any kind.** All inference is local (§2, NFR-1).
- A separate `flagged_questions.log`. The session transcript (§7.5) already
  records a grounding flag per exchange and is the single record.
- **Locomotion, weight shift, and any leg-joint motion.** NAO is seated for the
  entire lecture. Enforced structurally by the bridge joint whitelist (§12.1.1).
- **Formal HRI evaluation.** No questionnaire, no conditions, no participant
  study. Design rationale is documented in the writeup instead.

---

## 2. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | **All inference is local.** Speech recognition, language modelling, and synthesis run on the PC. No cloud AI service is called at any point. Network *transport* may traverse a tunnel when voice input is enabled (§9.1, Mode B); this carries bytes only. |
| NFR-2 | NAO begins speaking ≤ 1 s after question submission, and begins speaking the *answer* ≤ 6 s after submission (§8). |
| NFR-3 | A failure in Ollama or STT degrades Q&A but never terminates the lecture (§11). |
| NFR-4 | The system runs on one Windows PC with ~8 GB VRAM. |
| NFR-5 | Swapping PC audio output → NAO audio output requires implementing one interface (`AudioSink`, §4.4) and changing one config key. No other module changes. |
| NFR-6 | All generated audio is authored in NAO's expected WAV format from Phase 1 onward, even while the PC is the output device. |
| NFR-7 | Supports **5** concurrent student app connections. |
| NFR-8 | No mutable application state is shared across threads (§4.3). |
| NFR-9 | Swapping console embodiment → NAO embodiment requires implementing one interface (`Body`, §12.6) and changing one config key (`body: console → nao`). No other module changes. |
| NFR-10 | No command issued during lecture delivery can move a leg joint. Enforced in the bridge (§12.1.1), not by convention. |

---

## 3. Technology Stack

| Layer | Choice | Notes |
|---|---|---|
| Robot | NAO V5 | Speaker + gestures + LEDs. Kept on charger during lectures. |
| PC | Windows 10/11, ≥8 GB VRAM **NVIDIA** GPU | Runs all intelligence. See §3.2. |
| Main app | Python 3.11 | Orchestrator and all services. |
| LLM | **Qwen3.5 9B via Ollama, text-only Q4_K_M build** | ~5.6 GB, Apache 2.0. The official `qwen3.5:9b` tag is 6.6 GB **and multimodal** — its vision encoder holds ~1.4 GB of VRAM even for text-only prompts, which does not fit 8 GB alongside the KV cache. Fallback: `llama3.1:8b-instruct-q4_K_M`. Full GPU offload, `keep_alive` set, `num_ctx` pinned (§3.1). |
| STT | faster-whisper `small` | **CPU**, int8, to keep VRAM free for the LLM. |
| TTS | Piper (`piper-tts`, OHF-Voice/piper1-gpl) | Local, CPU. See §3.2 on licensing. |
| Resampling | `soxr` | Piper 22.05 kHz → 16 kHz (§12.2). |
| Audio decode | **ffmpeg** | WebM/MP4 → WAV for STT (§9.3). External binary. |
| Slides | PowerPoint via `pywin32` COM | Display + slide advance. Requires Microsoft PowerPoint installed. |
| Notes parsing | `python-pptx` | Reads speaker notes at load time. |
| Student app | Web app (HTML/CSS/JS), no framework | No native app, no install. |
| Web server | FastAPI + `uvicorn` | REST + WebSocket. |
| Tunnel (Mode B only) | `cloudflared` quick tunnel | HTTPS termination for microphone access (§9.1). |
| NAO bridge | Python 2.7 + `naoqi`, **preinstalled on NAO V5**, running **on the robot** | Threaded HTTP server on `<nao-ip>:8765`. Deployed by `scp`. No Windows SDK required (§3.2, §4.2). |
| WAV transfer | HTTP `POST /upload` to the bridge | Replaces SFTP/`paramiko` (§12.3). |
| Gesture authoring | Choregraphe 2.1.4.13 virtual robot | **Authoring tool only** — pose joints, read angles into `gestures.yaml`. Not a runtime target (§12.6.1). |

### 3.1 VRAM and context budget

Context is pinned, not left to default. Ollama's default context window (2048 in
older builds, 4096 in recent ones) is **too small** for this workload, and Ollama
truncates silently — dropping the front of the prompt, which is the grounding
material. That failure is invisible and defeats §7.3 entirely.

Token budget at steady state:

| Component | Tokens |
|---|---|
| `qa_material.md` (capped, §7.3) | ≤ 3500 |
| Full lecture narration (5 slides) | ~1000 |
| Guardrail + scaffolding | ~200 |
| Current slide/section marker | ~25 |
| 6 turns of follow-up history (cap, §7.2) | ~1000 |
| Current question | ~60 |
| Answer generation headroom | ~250 |
| **Worst case total** | **~6035** |

**`num_ctx: 8192`.** Realistic dinosaur-lecture usage is ~3000–4000 — roughly
half capacity.

VRAM at that setting:

- Qwen3.5 9B @ Q4_K_M, **text-only build**: ~5.6 GB
- KV cache @ 8192 (GQA, fp16): ~1 GB
- Runtime overhead: ~0.5 GB
- faster-whisper: 0 GB (CPU); Piper: 0 GB (CPU)
- **Total: ~7.1 GB.** Fits 8 GB with ~0.9 GB headroom — tighter than Llama 3.1
  8B was.

**Verify with `ollama ps` that the model reports 100% GPU.** Any CPU split means
fall back to `llama3.1:8b-instruct-q4_K_M`.

**Sampling parameters must be overridden.** Qwen3.5's registry defaults are
`temperature: 1` and `presence_penalty: 1.5` — both wrong for a grounding
guardrail. Set `temperature: 0.3` and `presence_penalty: 0` explicitly in
config; do not inherit.

### 3.2 External dependencies and acquisition risks

Resolve all of these in Phase 0, before any application code is written.

| Item | Status / Risk | Action |
|---|---|---|
| Choregraphe 2.1.4.13 | **Resolved.** Installed, licensed, virtual robot connects. | None. Used for gesture authoring in Phase 3 (§12.6.2). |
| Windows `pynaoqi` (Python 2.7 32-bit) | **Unavailable, and no longer required.** United Robotics Group hosts Python SDK downloads for NAOqi 2.1.4.13 in Linux32/Linux64 only; the Windows row is empty. | Bridge runs **on the robot** using NAO's own preinstalled Python 2.7 and `naoqi`. Running Python on the robot is standard, documented practice. |
| Piper package | The original `rhasspy/piper` (MIT) was archived read-only in Oct 2025; active development moved to `OHF-Voice/piper1-gpl` (**GPL-3.0**). | Pin `piper-tts` from OHF-Voice. Note the GPL-3.0 licence in course submission materials. |
| ffmpeg | Not a Python package. Must be on `PATH`. | Install and verify `ffmpeg -version`. |
| Microsoft PowerPoint | COM automation requires real PowerPoint. LibreOffice will not work. | Verify installed and licensed. |
| Two displays | §6.4 assumes slides full-screen on display 2, console on display 1. | Verify two outputs, set Windows to **Extend**, not Duplicate. |
| GPU vendor | Ollama on Windows targets NVIDIA/CUDA. AMD support on Windows is limited. | Confirm NVIDIA. |

> **Residual risk.** The bridge cannot be exercised against the Choregraphe
> virtual robot, because doing so would require the Windows SDK on the PC. First
> execution of `bridge.py` therefore happens on the day the robot arrives.
> Mitigated by keeping the bridge trivial (§12.1) — no lecture logic, ~150 lines —
> and by the staged bring-up order in Phase 6 (§13).

---

## 4. Architecture

### 4.1 Processes

```
┌──────────────────────── Windows PC ─────────────────────────┐
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Main App (Python 3.11)                             │    │
│  │                                                     │    │
│  │   Orchestrator (asyncio task — the lecture loop)    │    │
│  │      ├── LectureScript   (parsed .pptx notes)       │    │
│  │      ├── QuestionQueue                              │    │
│  │      ├── SlideController ──── COM thread ───────────┼────┼──> POWERPNT.EXE
│  │      ├── STTService      (faster-whisper, CPU)      │    │
│  │      ├── LLMService      (Ollama HTTP)  ────────────┼────┼──> ollama serve
│  │      ├── TTSService      (Piper + soxr)             │    │
│  │      ├── PlaybackQueue   (prepare thread +          │    │
│  │      │      │             play thread)              │    │
│  │      │      └── AudioSink ◄── swappable interface   │    │
│  │      │             ├── PcAudioSink   (Phases 1–5)   │    │
│  │      │             └── NaoAudioSink  (Phase 6+) ────┼────┼──┐
│  │      ├── Body ◄──────── swappable interface         │    │  │
│  │      │      ├── ConsoleBody  (Phases 3–5)           │    │  │
│  │      │      └── NaoBody      (Phase 6+) ────────────┼────┼──┤
│  │      ├── GestureScheduler (on_utterance_start)      │    │  │
│  │      └── WebServer (FastAPI, same event loop)       │    │  │
│  │             ├── /student  (student web app)         │    │  │
│  │             └── /operator (instructor console)      │    │  │
│  └─────────────────────────────────────────────────────┘    │  │
│                                                             │  │
│  ┌─────────────────────────────────────────────────────┐    │  │
│  │  cloudflared (Mode B only) ─────────────────────────┼────┼──┼─> Cloudflare
│  └─────────────────────────────────────────────────────┘    │  │
└─────────────────────────────────────────────────────────────┘  │
                                                    HTTP over LAN │
              ┌──────────────── NAO V5 ─────────────────────────◄─┘
              │  NAO Bridge (Python 2.7 + naoqi, ON THE ROBOT)    │
              │  Threaded HTTP server on <nao-ip>:8765            │
              │     └── ALProxy(module, "127.0.0.1", 9559)        │
              │  Speaker · Joints (arms + head only) · LEDs       │
              └──────────────────────────────────────────────────┘

                    ┌──────────────────┐
                    │ Student phones   │
                    │ (browser)        │
                    └──────────────────┘
```

### 4.2 The Python 2/3 split

`naoqi` for NAO V5 is Python 2.7 only. It is quarantined **on the robot itself**:
the Python 2.7 bridge process runs on NAO, and the Python 3 application talks to
it over HTTP across the LAN.

**The main Python 3 app never imports `naoqi`, and neither does the PC.** The
legacy runtime does not exist anywhere on the development machine.

This is stronger than the original design, where both interpreters lived on the
same Windows box. The boundary is now a network boundary and cannot be crossed by
accident. It is the single most important structural boundary in the project.

The trade-off is that the bridge cannot be tested before the robot exists
(§3.2). The bridge is kept correspondingly trivial.

### 4.3 Concurrency model

The application is **one asyncio event loop plus four threads and a small
executor pool**. The orchestrator is a coroutine, not a thread — it is a
sequential script that waits on things, which is what coroutines are for. The
consequence is that lecture state, the question queue, and student sessions all
live on a single thread, so **no locks are required anywhere.**

| Thread | Owns | Notes |
|---|---|---|
| **MainThread** (asyncio event loop, uvicorn) | All mutable application state: lecture state, `QuestionQueue`, student sessions, timers. Orchestrator task. All HTTP + WebSocket I/O. | The only thread permitted to mutate state. |
| **COM thread** | `SlideController` and the PowerPoint COM object. | `CoInitializeEx(COINIT_APARTMENTTHREADED)`. Commands in via `queue.Queue`, results out via `concurrent.futures.Future`. |
| **Prepare thread** | `PlaybackQueue` stage 1 — calls `AudioSink.prepare()`. | §4.4 |
| **Play thread** | `PlaybackQueue` stage 2 — calls `AudioSink.play()`. | §4.4 |
| **ThreadPoolExecutor(3)** | STT, LLM streaming, TTS synthesis. | Invoked via `await loop.run_in_executor(...)`. faster-whisper (ctranslate2) and Piper (onnxruntime) release the GIL in native code, so these parallelise genuinely. |

#### The four rules

These are invariants. They belong verbatim in `CLAUDE.md`.

1. **All mutable application state is read and written only from the event loop
   thread.** No exceptions.
2. **Worker threads never touch state.** They return values, or post back with
   `loop.call_soon_threadsafe`.
3. **The COM thread executes `SlideController` commands and nothing else.**
4. **Only `PlaybackQueue`'s threads call `AudioSink` methods** — except
   `AudioSink.stop()`, which is explicitly cross-thread (§4.4).

#### COM call timeouts

Every future the orchestrator awaits from the COM thread carries a **5-second
timeout**. A dead PowerPoint does not always raise — `GotoSlide` can block
indefinitely, which would hang the orchestrator with no fault ever detected.
**Timeout counts as a fault** and triggers §6.4.

Do not subscribe to PowerPoint COM events in the MVP: event delivery requires a
message pump, and a thread blocked on `queue.get()` is not pumping.

#### Model lifetime

`PiperVoice`, `WhisperModel`, and the Ollama HTTP session are created **once at
startup** and held for the process lifetime. Never shell out to `piper.exe`
per sentence — that reloads the ONNX model every time.

All three are **warmed** during `IDLE → READY` with a throwaway inference.
Without this, the first question of the lecture pays a ~5 GB cold model load
from disk (10+ seconds).

### 4.4 The audio seam: `AudioSink` + `PlaybackQueue`

Two layers. The swappable part is deliberately tiny; all the logic is shared.

```python
class AudioSink(Protocol):
    """The swappable seam (NFR-5). Deliberately dumb."""

    def prepare(self, wav_path: str) -> str:
        """Make the audio available to the playback device.
        Returns an opaque play token.
        PcAudioSink : no-op, returns wav_path.
        NaoAudioSink: HTTP POST /upload, returns the remote path."""

    def play(self, token: str) -> None:
        """Blocks until playback ends."""

    def stop(self) -> None:
        """Interrupt playback immediately.
        MUST be thread-safe and callable while play() is blocked."""

    def is_available(self) -> bool: ...
```

#### Why two threads

`prepare()` and `play()` are both blocking. On NAO, `prepare()` is an HTTP
upload to the bridge (~0.3–0.5 s, §12.3) and `play()` is the length of the
sentence. A single thread doing
both in sequence produces a silent gap before **every** sentence.

So `PlaybackQueue` runs two threads with a bounded buffer between them:

```
enqueue()  →  [pending]  →  PREPARE thread  →  [ready, maxsize=2]  →  PLAY thread
                            sink.prepare()                            sink.play()
```

While sentence *n* plays, sentence *n+1* is already uploading. Playback is
gapless.

`maxsize=2` is deliberate: enough to guarantee the next item is always ready,
small enough that a cancelled turn has not wasted many uploads.

**On PC this costs nothing.** `prepare()` returns instantly, the prepare thread
idles, and the pipeline degenerates to a plain ordered queue. Identical code
both modes — which is what makes NFR-5 real.

#### Interface

```python
@dataclass(frozen=True)
class Utterance:
    turn_id: int      # bumped on cancel; stale items are discarded
    seq: int          # order within the turn
    wav_path: str
    kind: str         # 'narration' | 'filler' | 'answer'

class PlaybackQueue:
    def enqueue(self, u: Utterance) -> None: ...
    def flush(self, turn_id: int | None = None) -> None:
        """Drop pending items. None = drop all."""
    def stop_now(self) -> None:
        """Interrupt the current utterance and drop all pending."""
    def stop_after_current(self) -> None:
        """Let the current utterance finish, then hold. (Operator pause.)"""
    def resume(self) -> None: ...
    def is_playing(self) -> bool: ...

    # Callbacks. FIRE ON THE PLAY THREAD — must marshal via
    # loop.call_soon_threadsafe before touching any state (rule 2).
    on_utterance_start: Callable[[Utterance], None] | None
    on_utterance_end:   Callable[[Utterance], None] | None
    on_idle:            Callable[[], None] | None   # queue emptied
```

#### Cancellation

Every utterance carries a `turn_id`. Cancelling a turn bumps the counter and
calls `flush()`.

**The play thread re-checks `turn_id` at dequeue, not only at flush.** An upload
may already be in flight in the prepare thread when the flush happens; it will
land in the ready queue afterwards. Without the dequeue check, "skip question"
occasionally plays one stale sentence.

#### What this buys

| Requirement | How it falls out |
|---|---|
| Sentence streaming (§7.4) | TTS pushes WAVs in as the LLM produces them; the queue drains in order, gaplessly. |
| Filler → answer transition (§7.1) | Filler enqueued first, answer sentences behind it. If a sentence is ready early it simply waits its turn. No gap, no overlap, no special-casing. |
| Reply/Done window trigger (§6.3) | `on_idle` fires the moment the answer finishes playing — identical on PC and NAO, because both sinks block in `play()`. |
| Operator pause (§10.2) | `stop_after_current()`. |
| Skip question (§10.2) | Bump `turn_id`; `stop_now()`. |
| Gestures (§12.4, Phase 3) | A scheduler subscribes to `on_utterance_start` and fires every 8–12 s while `is_playing()`. Never touches the audio path — so "gesture failure never interrupts speech" is structural, not a promise. Works identically against `ConsoleBody` and `NaoBody`. |

#### Temporary audio files

Generated WAVs are written to `runtime/audio/`, cleared at lecture start and at
lecture end. NAO's remote directory is cleared on the same schedule (§12.3).

---

## 5. Lecture Content Format

### 5.1 Single source of truth

`lecture.pptx` contains both slides and narration. Speaker notes hold the
narration text, split into sections by a `---` delimiter on its own line.

**Every section ends with a checkpoint by default.** The author writes a marker
only in the exception case — where stopping would interrupt a point
mid-argument.

**`[NO-CHECKPOINT]` means: do not stop for questions after this section ends.**

**Example — notes field of slide 3:**

```
Sauropods were the largest animals ever to walk on land.
---
[NO-CHECKPOINT]
Their long necks let them reach vegetation nothing else could.
---
And that feeding strategy is what made the size possible in the first place.
```

Parses to three sections. NAO pauses for questions after section 1 and after
section 3. The marker on section 2 suppresses the pause that would otherwise
fall between sections 2 and 3, so those two are delivered as one continuous
stretch.

### 5.2 Parsing rules

| Rule | Behaviour |
|---|---|
| Section split | A line containing exactly `---` |
| Checkpoint | Every section ends with a checkpoint **by default** — the author writes nothing |
| Suppressing a checkpoint | A section containing the marker `[NO-CHECKPOINT]` has **no checkpoint after it**; it runs straight into the next section |
| Marker placement | Anywhere in the section. Convention: its own first line |
| Marker stripping | `[NO-CHECKPOINT]` is removed before the text reaches TTS |
| Final section | Always has a checkpoint, regardless of marker — the queue drains before `FINISHED` |
| Empty notes | **Validation error** (§5.4). Out of MVP scope (§1.3) |
| Whitespace | Trimmed; blank sections discarded |

### 5.3 Parsed model

```python
@dataclass(frozen=True)
class Section:
    index: int              # global, 0-based — the orchestrator's position
    slide_index: int        # 1-based, matches PowerPoint
    section_index: int      # 0-based within the slide
    text: str
    checkpoint: bool

@dataclass(frozen=True)
class LectureScript:
    sections: list[Section]   # flat, ordered; the orchestrator's playlist
    slide_count: int
    full_narration: str       # all section text joined — used in §7.3 grounding
```

### 5.4 Validation at load

The deck is validated **before** the lecture starts. Failures are reported in the
operator console and block the start:

- File exists, opens, and has ≥1 slide.
- **Every slide has at least one non-empty section.** An un-narrated slide is a
  fatal error (§1.3).
- No section exceeds 1500 characters (TTS-latency guard; warning, not fatal).
- `qa_material.md` exists, is non-empty, and is under the token cap (§7.3).
- Ollama is reachable and the configured model is present.
- Piper voice model loads.
- ffmpeg is on `PATH` (Mode B only).

---

## 6. Lecture Delivery

### 6.1 States

| State | Meaning | Transitions to |
|---|---|---|
| `IDLE` | Nothing loaded | `READY` on successful load + model warm-up |
| `READY` | Deck validated, models warm, not started | `NARRATING` on operator start |
| `NARRATING` | Speaking a section | `CHECKPOINT` at section end; `PAUSED` on operator pause or slide fault |
| `CHECKPOINT` | Inspecting the queue | `NARRATING` if empty; `ANSWERING` if not; `FINISHED` if no sections remain |
| `ANSWERING` | Draining the question queue | `NARRATING` when the queue empties |
| `PAUSED` | Held by the operator, or a slide fault | back to the state it was paused from |
| `FINISHED` | Lecture complete | `IDLE` on operator reset |

`PAUSED` is re-entrant from any active state and returns to the exact section it
interrupted.

> There is no `RECOVERING` state. Automatic PowerPoint recovery was removed
> (§1.3, §6.4).

### 6.2 Main loop

1. Display the slide for the current section (issue `goto()` only when
   `slide_index` changes from the previous section).
2. Synthesize and speak the section text.
3. If `section.checkpoint` is false → next section.
4. If the queue is empty → next section.
5. If the queue is non-empty → enter `ANSWERING`, drain the queue (§7), then
   resume at the next section.
6. No sections remain → `FINISHED`.

### 6.3 Checkpoint semantics

- The queue is **only** inspected at checkpoints. Questions arriving mid-section
  wait.
- Students who queue *during* an answering phase are appended and served in the
  same drain. Accepted as unbounded for the MVP; the operator's **Clear queue**
  control (§10.2) is the escape hatch.
- Students see live queue position throughout.

#### Turn resolution

After each answer finishes playing (`on_idle`), the student is offered **Reply**
or **Done**. The lecture never waits on an unresponsive student:

| Event | Window | On expiry |
|---|---|---|
| Choosing Reply or Done | **5 s** from `on_idle` | Auto-resolved as **Done**; the queue advances |
| Composing a reply after choosing Reply | **30 s** | Turn abandoned as **Done**; the queue advances |

The 5 s window requires only that the student *starts* — tapping Reply, pressing
record, or typing the first character cancels it immediately and starts the 30 s
composition window. Both windows show a visible countdown.

Both timers are `asyncio.Task`s on the event loop, started from `on_idle` and
cancelled by student activity arriving over WebSocket. No locking is involved.

Auto-resolved turns are recorded in the transcript (§7.5) as `timeout`.

### 6.4 Slide control (PowerPoint COM)

`SlideController` owns a **dedicated thread** that initializes COM and is the
only thread permitted to touch the PowerPoint object. All other code posts
requests to it via a queue and awaits a future **with a 5-second timeout**
(§4.3).

```python
class SlideController:
    def open(self, pptx_path: str) -> None     # opens + starts slideshow
    def goto(self, slide_index: int) -> None   # 1-based
    def close(self) -> None
```

- `Presentation.SlideShowSettings.Run()` starts the show;
  `SlideShowWindow.View.GotoSlide(n)` moves.
- Presented full-screen on display 2 (projector); the operator console stays on
  display 1.

#### Position is owned by the orchestrator

The orchestrator's position — section index and state — is held **in the
orchestrator and never read back from PowerPoint**. PowerPoint is a display
device that is told where to be; it is never the source of truth. This is what
makes Skip section, Pause, and manual reopen all work.

#### On fault

If a COM call raises or times out:

1. The orchestrator finishes the sentence currently being spoken
   (`stop_after_current()`), then holds.
2. State moves to `PAUSED`. The operator console raises a prominent alert
   naming the fault.
3. The operator may: **Reopen deck** (reopens `lecture.pptx`, restarts the
   slideshow, and issues `goto()` for the remembered slide), **Resume without
   slides**, or **End lecture**.

There is no automatic retry, no backoff, and no fallback chain. The system does
not decide to carry on by itself.

The question queue keeps running throughout — students are not blocked by a
display fault.

---

## 7. Question & Answer

### 7.1 Turn sequence

1. The student at the head of the queue is prompted to submit — **typed**
   always, **voice** when Mode B is active (§9.1).
2. **Stage-1 filler fires immediately, for every question, regardless of input
   mode.** A short pre-synthesized clip: *"Question received — analysing."*
   Cached to disk at startup and (Phase 6+) pre-uploaded to NAO at lecture start,
   so it costs ~0 ms. 3–4 variants, rotated, to avoid grating.
   *For voice questions this is the only thing that can mask STT latency, which
   is otherwise fully exposed.*
3. Voice submissions are transcribed with faster-whisper. Typed submissions skip
   STT entirely.
4. **Stage-2 filler, also for every question regardless of input mode:**
   *"You asked: [question text]."* Masks the LLM, and — the primary reason it
   applies to typed questions too — lets the whole room hear the question.
   **Skipped when the question exceeds `question_repeat_max_words` (25).**
   `max_typed_chars` is 500; read aloud that is ~25 seconds of recitation.

> **Consequence for typed questions (Mode A, the primary target):** ~4–5 s of
> preamble before the answer. Accepted deliberately — the repeat is a
> classroom-audibility feature, not only a latency mask.
5. Concurrently, the LLM generates an answer grounded per §7.3.
6. The answer is synthesized sentence-by-sentence and enqueued as it is produced
   (§7.4). The `PlaybackQueue` handles ordering; no gap appears between the
   filler and the answer.
7. On `on_idle`, the student is offered **Reply** or **Done** (§6.3).
8. Repeat until the queue is empty.

### 7.2 Follow-ups

Per-student conversation history is held in the LLM context for the duration of
that student's turn. On **Done**, the context is cleared for the next student —
but the exchange is never lost: it is written to the session transcript (§7.5)
first.

**LLM context cap: 6 turns** within a single student's turn. A student who
exceeds it is told to re-queue. Every exchange is still recorded in full.

### 7.3 Grounding

The system prompt is assembled fresh per question from four parts:

1. The guardrail instruction.
2. The whole of `qa_material.md`.
3. **The full lecture narration** (`LectureScript.full_narration`). This lets
   NAO answer *"what did you just say about sauropods?"* — the most natural
   student question, and unanswerable without it.
4. **The current position marker**, e.g. `The lecture is currently on Slide 3,
   section 2: Sauropods.` ~25 tokens, and it sharply improves grounding on
   pronoun-heavy questions ("how big did *they* get?").

Retrieval/embeddings are **not** implemented (§1.3).

- **Cap:** `qa_material.md` ≤ 3500 tokens. Exceeding it fails validation at load.
- **Context:** `num_ctx: 8192` (§3.1). Non-negotiable — the default truncates
  silently and the guardrail fails invisibly.
- **Guardrail prompt:** answer *only* from the supplied material and narration;
  if the answer is not present, say so plainly. The exchange is marked in the
  transcript with a grounding flag.
- **Length:** 2–4 sentences, with `num_predict` as a hard backstop — the prompt
  instruction alone is not reliably honoured by an 8B model.
- **Temperature:** 0.3.

> **Testing note.** Any 8–9B model knows a great deal about dinosaurs from
> pretraining and will answer correctly from parametric memory even when
> grounding is broken. To verify the guardrail actually works, the demo script
> must include a question that is (a) absent from the material and (b) something
> the model would otherwise confidently answer.

### 7.4 Sentence streaming

The LLM response is consumed as a token stream in an executor thread. Sentences
are handed back to the event loop via `loop.call_soon_threadsafe` onto an
`asyncio.Queue`, synthesized, and enqueued to `PlaybackQueue`.

**Boundary rules** (specified, because a naive splitter produces fragments):

- A boundary is `[.!?]` followed by whitespace or end-of-stream.
- Minimum emit length **20 characters** — shorter fragments merge forward into
  the next sentence.
- Force-split at **300 characters** if no terminator has appeared.
- **Flush the remainder at stream end**, terminator or not.

**Cancellation:** the executor thread checks a cancel flag between chunks and
closes the connection. Without an explicit flag, "skip question" stops the audio
but leaves the GPU generating into the void.

TTS runs sequentially in one executor slot, so completion order equals emission
order. Piper is fast enough that parallelising gains nothing.

### 7.5 Session transcript

Every question and answer is written to a transcript file so the lecturer can
review, correct, and distribute it afterwards.

**Written at:** `sessions/lecture_YYYYMMDD_HHMMSS.md`
**When:** appended after each answer completes — never buffered. A crash
mid-lecture loses nothing.

**Recorded per exchange:** timestamp; slide and section index; student display
name (falling back to a short form of the anonymous ID); question text; input
mode (`voice` | `typed`); answer text as spoken; follow-up depth (`0` for the
initial question, incrementing for replies); grounding flag; resolution (`done` |
`replied` | `timeout`).

Follow-ups are nested under their parent question so the thread reads in order.

**Format** — Markdown, structured for direct editing:

```markdown
# Lecture Q&A — 2026-07-26 14:30

## Slide 3, Section 2

### Q1 — Dana (typed) — 14:38:12
How long were sauropod necks?

**Answer:**
The longest known sauropod necks reached about fifteen metres...

#### Q1.1 — Dana (typed) — 14:39:40 *(follow-up)*
Could they lift them straight up?

**Answer:**
The material covers posture only briefly...

### Q2 — Yoav (voice) — 14:41:05 ⚠ NOT IN MATERIAL
What colour were they?

**Answer:**
That isn't covered in the material for this lecture.
```

**Delivery:** the operator console offers a download link at any time; the file
remains on disk regardless.

**On write failure:** logged and surfaced in the console; the lecture continues,
and the exchange is written to the main log as a fallback.

---

## 8. Latency Budget

Target: NAO starts speaking almost immediately, and reaches the answer within
~6 s (NFR-2).

**Voice questions:**

| Stage | Elapsed | Notes |
|---|---|---|
| Stage-1 filler starts | **~0.3 s** | Pre-synthesized and cached. Nothing to generate. |
| Stage-1 filler plays | 0.3 → ~2.3 s | Masks upload + STT |
| Upload + STT | ~0.5–3 s | Concurrent with stage-1 filler |
| Stage-2 filler (question repeat) | ~2.5 → ~6 s | Masks the LLM |
| LLM first sentence ready | ~4–5 s | Concurrent with stage-2 filler |
| Answer playback begins | **~6 s** | Follows the filler seamlessly via `PlaybackQueue` |

**Typed questions** (no STT — both fillers still play, §7.1):

| Stage | Elapsed | Notes |
|---|---|---|
| Stage-1 filler starts | **~0.3 s** | Pre-synthesized and cached |
| Stage-2 filler (question repeat) | ~2.3 → ~5 s | Masks the LLM |
| Answer playback begins | **~5 s** | |

The two-stage filler means **there is no silence anywhere in the chain**, and
NFR-2's first clause is met with enormous margin. It also makes the budget
largely insensitive to faster-whisper's actual speed — which resolves the model-size
question in §16 by making it low-stakes.

Supporting settings: Ollama `keep_alive` keeps the model resident; all three
models warmed at startup (§4.3).

---

## 9. Student Web App

### 9.1 Two network modes

Microphone access requires a **secure context** (HTTPS or `localhost`) —
`navigator.mediaDevices` is *undefined* over plain HTTP to a LAN IP, so
recording fails hard. No public CA will issue a certificate for a private IP, so
there is no purely-local way to satisfy this at classroom scale.

The system therefore ships two modes, selected by `server.mode` in config:

**Mode A — `lan` (default, typed questions only)**

- Served over plain HTTP on the LAN: `http://<pc-ip>:8000`.
- No certificate, no tunnel, no third party. Fully offline (NFR-1 as written).
- Typed input needs no secure context, so everything works.
- **This is the primary MVP target and the fallback if the network fails.**

**Mode B — `tunnel` (adds voice questions)**

- `cloudflared tunnel --url http://localhost:8000` produces a public HTTPS URL
  (`https://<random>.trycloudflare.com`) with a certificate every phone already
  trusts. No account, no domain, no per-phone install.
- Page, REST, WebSocket, and audio upload all originate from **one HTTPS
  origin** — so no mixed-content block and no CORS.
- Cloudflare terminates TLS and relays plaintext down an outbound tunnel to
  `localhost:8000`. No port forwarding, no firewall change.
- The tunnel URL is parsed from `cloudflared` stdout at startup and fed to the
  QR generator; `server.public_url` overrides it manually.

> **Privacy note.** Because Cloudflare terminates TLS, it sees plaintext —
> including voice recordings and question text. Accepted for this demo: content
> is dinosaur questions from anonymous IDs, and no real personal information is
> collected. Students should be told in one sentence before the lecture starts.
> Do not collect real names (§9.2).

**If the network fails in Mode B, fall back to Mode A and continue typed-only.**

### 9.2 Access and identity

Students join by scanning a QR code on the operator console, or by typing the
URL. No install, no login, no account.

A random ID is generated on first visit and stored in `localStorage`. A student
may set a **display name** — nickname only; do not prompt for real names.
Identity survives a page refresh and reconnection.

### 9.3 States

| State | UI |
|---|---|
| Idle | "Ask a question" button. Live lecture status. |
| Queued | Queue position. "Leave queue" button. |
| Your turn | Text input always; record button in Mode B only. |
| Submitted | "Submitted — NAO is answering." |
| Answered | Answer text. "Reply" and "Done" with a visible **5 s** countdown; expiry auto-resolves as Done. |
| Reply | Type/record again, same context, **30 s** countdown. |
| Lecture paused | Shown during operator pause or slide fault. Queue position retained; submission disabled. |

### 9.4 Transport and audio capture

- **WebSocket** for state pushed to students: queue position, turn notification,
  lecture status, answer text.
- **REST** for actions: join queue, leave queue, submit text, submit audio,
  reply, done.

**Audio capture (Mode B).** Container support differs by browser and must be
negotiated, not assumed:

- Chromium writes `audio/webm` (Opus). **Safari writes `audio/mp4` (AAC) and
  does not write WebM at all** — so every iPhone in the room produces MP4.
- Probe with `MediaRecorder.isTypeSupported()` in preference order and record
  the resulting MIME type alongside the blob.
- Server-side, convert **either** container to 16 kHz mono WAV with **ffmpeg**
  before handing to faster-whisper.

### 9.5 Constraints

- Max recording length: **30 seconds** (hard stop). Reduced from 45 s — it
  shortens STT time and keeps the stage-2 repeat viable.
- Max typed question: 500 characters.
- A student may hold only one queue position at a time.
- Reconnection restores queue position via the stored ID.

---

## 10. Instructor Console

A separate web page (`/operator`), on the lecturer's own device or display 1.

### 10.1 Display

- Current state, slide index, section index.
- Text of the section currently being spoken.
- Live queue: ordered student list with names.
- Current question and generated answer text.
- Component health: LLM, STT, TTS, PowerPoint, NAO — OK / degraded / down.
- QR code and join URL.
- Questions-answered count and a **download transcript** link.

### 10.2 Controls (MVP set)

| Control | Effect |
|---|---|
| Start | `READY` → `NARRATING` |
| Pause / Resume | Pause finishes the current sentence (`stop_after_current`), then holds |
| Skip section | Abandon the current section, advance to the next. **Also clears the queue** — questions were asked about material now being skipped. Confirmation prompt shows the queue size |
| Skip question | Abandon the active answer (bump `turn_id`, `stop_now`), move to the next student |
| Clear queue | Drop all **waiting** students, who are notified. The active turn completes normally — use Skip question to end that |
| End lecture | Return to `IDLE` |
| Reopen deck | Only shown after a slide fault (§6.4) |

Deferred beyond MVP: Force checkpoint, Freeze queue, Repeat section, Go to
slide, Mute.

### 10.3 Access control

Served on a separate path guarded by a token in the config file, shown in the
terminal at startup. A LAN convenience measure, not a security boundary.

---

## 11. Failure Handling

**Implemented for MVP:**

| Failure | Behaviour |
|---|---|
| Ollama unreachable | Q&A disabled; queue frozen; students told questions are unavailable. Narration continues — it needs no LLM. Health shown as *down* in the console. |
| STT fails on a submission | Student is asked to retype or re-record. Their queue position is held. |

**Logged and surfaced in the console, but not otherwise handled:** Piper
synthesis failure, PowerPoint fault (beyond §6.4's pause-and-alert), NAO bridge
unreachable, WAV upload failure, student disconnect, transcript write failure.

All events are logged to `logs/lecture_YYYYMMDD_HHMMSS.log`.

---

## 12. NAO Integration and Embodiment

### 12.1 Bridge API

Python 2.7 process running **on the robot**, using NAO V5's preinstalled `naoqi`
module. Threaded HTTP server on `<nao-ip>:8765`. It connects to NAOqi locally
with `ALProxy(module, "127.0.0.1", 9559)`. Deliberately minimal — the bridge
contains no lecture logic.

| Endpoint | Method | Body | Effect |
|---|---|---|---|
| `/health` | GET | — | `{"connected": bool, "battery": int, "volume": int, "temps_ok": bool}` |
| `/upload` | POST | raw WAV bytes, `X-Filename` header | Writes to `nao.audio_dir`; returns `{"remote_path": str}` |
| `/play` | POST | `{"remote_path": str}` | `ALAudioPlayer.playFile()`; blocks until done |
| `/stop` | POST | — | `ALAudioPlayer.stopAll()` immediately |
| `/volume` | POST | `{"level": int}` | `ALAudioDevice.setOutputVolume()`, 0–100 |
| `/posture` | POST | `{"name": "Sit"}` | `ALRobotPosture.goToPosture(name, 0.4)` |
| `/gesture` | POST | `{"name": str, "keyframes": [...], "speed": float}` | Runs `ALMotion.angleInterpolation` in a worker thread; returns immediately |
| `/gaze` | POST | `{"target": str}` | `"slides"` \| `"class"` — head joints only |
| `/leds` | POST | `{"pattern": str}` | Set an LED pattern |
| `/stiffness` | POST | `{"on": bool}` | Whole-body stiffness at lecture start/end |

**The HTTP server must be threaded** (`SocketServer.ThreadingMixIn`). A
single-threaded server queues `/stop` behind the blocking `/play` it is trying
to interrupt, so Skip question would never work.

The bridge holds a persistent `ALProxy` connection and reconnects on failure.

#### 12.1.1 Joint whitelist — enforced in the bridge

Every request touching joints (`/gesture`, `/gaze`) is filtered against a
whitelist **before** reaching `ALMotion`. A request naming any joint not on the
list is refused **in full** — never partially applied — and logged.

**Permitted:**

```
HeadYaw, HeadPitch
LShoulderPitch, LShoulderRoll, LElbowYaw, LElbowRoll, LWristYaw, LHand
RShoulderPitch, RShoulderRoll, RElbowYaw, RElbowRoll, RWristYaw, RHand
```

**Refused, unconditionally:**

```
LHipYawPitch, RHipYawPitch
LHipRoll, LHipPitch, LKneePitch, LAnklePitch, LAnkleRoll
RHipRoll, RHipPitch, RKneePitch, RAnklePitch, RAnkleRoll
```

This lives in the bridge, not in the gesture loader, because the bridge is the
only code with a connection to `ALMotion`. Nothing can reach the motors without
passing through it, so the guarantee holds regardless of what `gestures.yaml`
contains. Same principle as "gesture failure never interrupts speech" —
structural, not a promise. This is NFR-10.

`/posture` is the sole exception: it calls `ALRobotPosture`, which necessarily
moves legs. It is invoked exactly twice per lecture, at start and end, and never
during delivery.

#### 12.1.2 HTTP client timeouts

Connect timeout **1 s** on every endpoint. Read timeouts:

| Endpoint | Read timeout |
|---|---|
| `/play` | WAV duration + 5 s, computed per call. **Never infinite.** |
| `/stop` | 1.5 s |
| `/gesture`, `/gaze`, `/leds`, `/volume` | 1.5 s |
| `/health` | 2 s |
| `/upload` | 10 s |
| `/posture`, `/stiffness` | 15 s (real motion, start/end only) |

**Invariant: the orchestrator is never blocked by the robot.** `turn_id` is
bumped in application state *before* the `/stop` request is issued, so
cancellation is already true locally whether or not the robot complies. Robot
compliance is best-effort.

Failures on `/gesture`, `/gaze`, `/leds`, and `/volume` are logged and swallowed.
They never propagate into the audio path or the orchestrator.

### 12.2 Audio format

WAVs are authored to **16 kHz, mono, 16-bit PCM** from Phase 1 (NFR-6).

**Piper does not output this.** Voice quality determines the rate, and it is
fixed by the model: medium and high voices are **22050 Hz**, low voices 16000 Hz.
The configured `en_US-lessac-medium` therefore produces 22.05 kHz, and an
explicit **resample step (`soxr`) sits between Piper and disk**. `tts.sample_rate`
in config is a post-processing target, not a Piper parameter.

Phase 1's acceptance test (`ffprobe` confirms the format) is what catches this.

### 12.3 File transfer

The WAV is generated on the PC and must reach NAO's filesystem before
`ALAudioPlayer.playFile()` can play it. Since the bridge runs on the robot, this
is an **HTTP upload, not SFTP**.

`NaoAudioSink.prepare()` POSTs the WAV bytes to `/upload`; the bridge writes the
file locally and returns the path, which becomes the opaque play token consumed
by `play()`.

Benefits over the SFTP design: one transport instead of two, no persistent SFTP
session to manage, no SSH credentials in config, and one fewer Python dependency
(`paramiko` is not used).

`prepare()` remains a blocking call on the prepare thread, pipelined behind
playback exactly as specified in §4.4. Expected duration ~0.3–0.5 s, comparable
to the SFTP upload it replaces.

`nao.audio_dir` is cleared by the bridge at lecture start and at lecture end.
Prefer **wired Ethernet** — NAO is stationary on its charger and its onboard
Wi-Fi is slow.

### 12.4 Gestures, gaze, and LEDs

Because PC TTS bypasses NAO's own TTS, NAO loses its automatic talking animation
and must be animated explicitly.

#### 12.4.1 Scheduler

- Subscribes to `PlaybackQueue.on_utterance_start`.
- Fires one gesture at utterance start, then every `gestures.interval_s`
  (8–12 s, jittered) while `is_playing()`.
- **One gesture at a time.** Guarded by `Body.is_gesturing()`. Two overlapping
  `angleInterpolation` calls on the same joint produce visible jerk.
- Never touches the audio path. Gesture failure is non-fatal by construction.
- Always returns to the `rest` pose at the end of each gesture, so arm position
  cannot drift over the course of a lecture.
- Callbacks fire on the play thread and **must** marshal via
  `loop.call_soon_threadsafe` before touching state (§4.3, rule 2).

#### 12.4.2 Context sets

| Orchestrator state | Gaze | Gesture set |
|---|---|---|
| `NARRATING` | `slides` | `explain_open`, `point_slide`, `beat` |
| `ANSWERING` — filler playing | `class` | `thinking` |
| `ANSWERING` — answer playing | `class` | `explain_open`, `beat`, `acknowledge` |
| `CHECKPOINT`, `PAUSED` | `class` | `rest` only |

Gestures are chosen at random from the applicable set, without immediate repeats.

**Gaze is the highest-value cue and the cheapest to implement.** Head toward the
projector while narrating, toward the class while answering. Two head joints, one
interpolation. It reads as social attention far more strongly than arm motion
does.

#### 12.4.3 LED states

Eye LEDs signal orchestrator state: white (`NARRATING`), blue (`ANSWERING`),
green (`CHECKPOINT` — queue open), yellow (`PAUSED`), off (`IDLE` / `FINISHED`).

#### 12.4.4 Balance safety

Motion safety is not a matter of careful authoring. It is enforced at three
levels:

1. **Joint whitelist in the bridge** (§12.1.1). No leg joint is reachable during
   delivery.
2. **Posture is `Sit`. There is no standing option.** `goToPosture("Sit", 0.4)`
   at lecture start, stiffness held at 1.0 throughout. Rationale: sustained
   standing heats NAO's leg joints, and NAOqi's thermal protection *reduces
   stiffness* in response — over a 60–90 minute lecture a standing NAO can sag
   and fall with no gesture involved at all. Seated, the failure mode does not
   exist.
3. **Slow interpolation.** `gestures.speed` 0.10–0.20 max speed fraction — which
   is also the intended aesthetic.

Additional rules:

- **Use `Sit`, not `SitRelax`.** `SitRelax` drops stiffness, leaving the arms
  unable to gesture.
- **Enable arm collision protection.** Seated, the thighs occupy space a lowered
  arm wants to pass through.
  `ALMotion.setCollisionProtectionEnabled("Arms", True)` at bridge startup,
  permanently on.
- **Author every keyframe with the virtual robot already seated.** The reachable
  arm envelope differs from standing; the `rest` pose in particular needs arms
  further forward and outward than a standing rest.
- **Never invoke stock animation-library behaviors** (`animations/Stand/Gestures/*`).
  Many involve torso lean or weight shift. All gestures are authored in-project.
- **Leave the fall manager enabled.** `setFallManagerEnabled(True)` is the
  default; it is never disabled.

### 12.5 Physical setup

- NAO remains **on its charger** for the entire lecture. Verify the cable has
  slack and cannot tug.
- Seated posture set at lecture start (§12.4.4).
- Stiffness enabled at start, released at end.
- Output volume set explicitly at lecture start (`nao.volume`) — never inherited
  from whatever the previous user left it at.
- NAO and the PC on the same LAN; the robot's address is set in
  `nao.bridge_url`. Prefer wired Ethernet.
- NAO on the floor or a low, wide surface — never a narrow table at head height.

### 12.6 The body seam: `Body`

The same pattern as §4.4's `AudioSink`, applied to embodiment. It exists so that
gestures, gaze, and LEDs are designed, wired, and running from **Phase 3**, with
or without a robot present.

```python
class Body(Protocol):
    """The embodiment seam (NFR-9). Mirrors AudioSink: deliberately dumb,
    all logic lives in the scheduler above it."""

    def gesture(self, name: str) -> None:
        """Fire a named gesture. Non-blocking. Never raises."""

    def gaze(self, target: str) -> None:
        """'slides' | 'class'. Head joints only."""

    def leds(self, pattern: str) -> None: ...

    def posture(self, name: str) -> None:
        """Lecture start/end only."""

    def stiffness(self, on: bool) -> None: ...

    def is_gesturing(self) -> bool: ...
    def is_available(self) -> bool: ...
```

#### 12.6.1 Implementations

| Implementation | Phase | Behaviour |
|---|---|---|
| `ConsoleBody` | 3 | Logs to the operator console: `[BODY] gesture=explain_open gaze=slides leds=white`. Drives the browser previewer (§12.6.4). `is_gesturing()` simulated from gesture duration. |
| `NaoBody` | 6 | HTTP to the bridge at `nao.bridge_url`, with §12.1.2 timeouts. |

> **Choregraphe is an authoring tool, not a runtime target.** Driving the
> virtual robot from application code would require the Windows Python SDK,
> which is unavailable (§3.2). Choregraphe is used to pose joints and read angle
> values into `gestures.yaml` (§12.6.2). The gesture *data* is validated against
> the simulator; the gesture *code path* is first executed on the physical robot
> in Phase 6.

#### 12.6.2 Gesture library format

`content/gestures.yaml`. Angles in radians, times in seconds from gesture start.

```yaml
rest:
  duration_s: 1.2
  keyframes:
    - t: 1.2
      LShoulderPitch: 1.40
      LShoulderRoll:  0.15
      LElbowYaw:     -1.20
      LElbowRoll:    -0.50
      RShoulderPitch: 1.40
      RShoulderRoll: -0.15
      RElbowYaw:      1.20
      RElbowRoll:     0.50

explain_open:
  contexts: [narrating, answering]
  duration_s: 3.4
  keyframes:
    - t: 1.4
      LShoulderPitch: 1.10
      LShoulderRoll:  0.45
      LElbowRoll:    -0.35
      RShoulderPitch: 1.10
      RShoulderRoll: -0.45
      RElbowRoll:     0.35
    - t: 3.4
      # returns to rest
```

| Gesture | Contexts | Description |
|---|---|---|
| `rest` | all | Neutral. Arms low and slightly forward, elbows soft. |
| `explain_open` | narrating, answering | Both arms open outward, palms upward. |
| `point_slide` | narrating | One arm rises ~45° toward the screen; head follows. |
| `beat` | narrating, answering | Small forward emphasis beat. |
| `thinking` | filler | One hand toward chin height, slight head tilt. |
| `acknowledge` | answering | Small nod plus a low arm movement. |

> **All angle values above are starting points.** Tune them against the
> Choregraphe virtual robot in Phase 3 — seated — and validate against NAO V5's
> published joint limits before the first run on hardware.

**Loader validation:** every joint name in the file must be on the §12.1.1
whitelist, every angle within the published limit for that joint, and every
gesture must end at `rest`. Failures block startup (§5.4).

#### 12.6.3 Gaze targets

```yaml
gaze:
  slides: { HeadYaw: -0.45, HeadPitch: -0.15 }   # toward projector
  class:  { HeadYaw:  0.00, HeadPitch:  0.00 }   # level, forward
```

`HeadYaw` sign depends on which side the projector is on — set during Phase 0
room setup, config-driven.

#### 12.6.4 Browser previewer

The operator console renders a front-view stick figure driven by the same
keyframes: shoulder pitch/roll, elbow roll, head yaw/pitch. Animated live as
`ConsoleBody` fires.

Built in Phase 3 and **kept after the robot arrives** — during a live lecture it
shows the operator what NAO is about to do. Deliberately crude; it proves cadence
and timing, not appearance.

## 13. Build Order

Sequenced so no more than one hard thing is being debugged at a time.

### Phase 0 — Environment and content
Resolve every item in §3.2. Author the dinosaur deck: 5 slides, speaker notes
with `---` delimiters and at least one `[NO-CHECKPOINT]`, plus `qa_material.md`.
Set the gaze `HeadYaw` sign for the room (§12.6.3).
**Done when:** Choregraphe connects to a virtual robot,
ffmpeg/PowerPoint/two-displays/NVIDIA verified, and the deck + material exist and
read sensibly aloud.

### Phase 1 — Audio pipeline
Piper → `soxr` → WAV in NAO's exact format. `PcAudioSink` and `PlaybackQueue`
(both threads, bounded buffer, turn_id cancellation). Establish the seam now.
**Done when:** a list of sentences plays back-to-back with no gaps, `ffprobe`
confirms 16 kHz mono 16-bit, and `stop_now()` cuts playback mid-sentence.

### Phase 2 — Lecture delivery
Parse `.pptx` → `LectureScript`. `SlideController` on its COM thread with
timeouts. Orchestrator coroutine walks sections with synced slide advance. No Q&A.
**Done when:** the 5-slide deck narrates end to end with correct slide changes, no
redundant `goto()` across multi-section slides, and killing `POWERPNT.EXE`
mid-lecture pauses cleanly with an operator alert (not a hang).

### Phase 3 — Q&A (typed) and embodiment scaffolding
FastAPI server, student web app, queue, grounded LLM, two-stage filler, sentence
streaming, reply/done loop, session transcript. LAN HTTP, no tunnel, no STT.

**Plus the embodiment layer:** `Body` protocol, `ConsoleBody`,
`content/gestures.yaml` authored in full against the seated virtual robot in
Choregraphe, gesture scheduler wired to `on_utterance_start`, gaze and LED state
mapping, browser previewer.

**Done when:** two students on real phones queue, ask typed questions, receive
grounded answers, follow up, the lecture resumes — the transcript contains both
exchanges with follow-ups correctly nested — **and** the console log plus
previewer show gestures firing at the right cadence, gaze switching at
narrating↔answering transitions, and LEDs tracking state.

### Phase 4 — Voice input (Mode B)
`cloudflared` tunnel, QR from tunnel URL, `MediaRecorder` with container
negotiation, ffmpeg conversion, faster-whisper.
**Done when:** a question recorded on an iPhone **and** one on an Android phone
both transcribe and answer correctly, and pulling the tunnel falls back to Mode A
without restarting the app.

### Phase 5 — Instructor console and hardening
Operator page and the §10.2 control set. §11's two implemented failures. Latency
tuning against NFR-2.
**Done when:** every control works mid-lecture, Ollama-down and STT-failure are
manually exercised, and both turn-resolution timeouts are verified by leaving a
student unresponsive at each stage.

### Phase 6 — NAO integration
Deploy the bridge to the robot:

```
scp nao_bridge/*.py nao@<nao-ip>:/home/nao/bridge/
ssh nao@<nao-ip>
python /home/nao/bridge/bridge.py
```

Implement `NaoAudioSink` (`prepare` = POST `/upload`, `play` = POST `/play`) and
`NaoBody`. Set `audio_output: nao`, `body: nao`, `nao.bridge_url` to the robot.

**Bring-up order on the day** — one hard thing at a time:

1. `curl http://<nao-ip>:8765/health` returns battery and connection state.
2. `/volume`, then `/posture` with `"Sit"`, then `/stiffness`.
3. `/upload` + `/play` with a single hand-made WAV. **Audio before motion.**
4. **Whitelist test:** send a gesture containing `LKneePitch`; confirm the bridge
   refuses it and `ALMotion` is never called.
5. `/gaze`, then one gesture, then the scheduler.
6. Full Phase-5 system with `audio_output: nao`, `body: nao`.

**Done when:** the full Phase-5 system runs identically with audio from NAO's
speakers and gestures on the robot, sentence-to-sentence gaps are imperceptible,
the whitelist refusal is observed, and **no code outside `NaoAudioSink` and
`NaoBody` changed** (validates NFR-5 and NFR-9).

### Phase 7 — Embodiment tuning on hardware
No new components. Tune keyframe angles, gesture speed, and interval against the
physical robot. Run a full-length rehearsal and check joint temperatures.
**Done when:** NAO looks alive throughout narration and answering, with no audio
interruption and no balance incident across a full-length run.

---

## 14. Configuration

Single `config.yaml` at the project root. **Every timing and limit lives here** —
none are hardcoded.

```yaml
audio_output: pc            # pc | nao
body: console               # console | nao          — §12.6

lecture:
  pptx_path: ./content/lecture.pptx
  qa_material: ./content/qa_material.md

llm:
  model: qwen3.5:9b-text-q4_K_M   # text-only build — §3.1
  host: http://localhost:11434
  keep_alive: 30m
  num_ctx: 8192             # CRITICAL — default truncates silently (§3.1)
  temperature: 0.3
  presence_penalty: 0       # override registry default of 1.5 — §3.1
  num_predict: 200          # hard backstop on answer length
  max_answer_sentences: 4
  qa_material_token_cap: 3500
  followup_turn_cap: 6

stt:
  model: small
  device: cpu
  compute_type: int8

tts:
  voice: en_US-lessac-medium
  sample_rate: 16000        # resample target (Piper emits 22050) — §12.2
  channels: 1
  filler_variants: 4        # stage-1 clips pre-synthesized at startup

audio:
  temp_dir: ./runtime/audio
  ready_queue_size: 2       # §4.4 double-buffer depth
  min_sentence_chars: 20
  max_sentence_chars: 300

slides:
  display: 2                # projector monitor index
  com_timeout_s: 5

timings:
  reply_window_s: 5
  compose_window_s: 30
  max_recording_s: 30
  max_typed_chars: 500
  question_repeat_max_words: 25

server:
  mode: lan                 # lan | tunnel   — §9.1
  host: 0.0.0.0
  port: 8000
  public_url:               # optional manual override of the tunnel URL
  operator_token: change-me
  max_students: 5

paths:
  sessions_dir: ./sessions
  logs_dir: ./logs

gestures:
  library: ./content/gestures.yaml
  enabled: true
  interval_s: [8, 12]       # jittered range between gestures — §12.4.1
  speed: 0.15               # ALMotion max-speed fraction, 0.10–0.20 — §12.4.4
  previewer: true           # stick-figure preview on operator console

nao:
  bridge_url: http://192.168.1.50:8765   # the ROBOT's address, not localhost
  audio_dir: /home/nao/lecture_audio
  volume: 70                # 0–100, set explicitly at lecture start — §12.5
  timeouts:                 # §12.1.2
    connect_s: 1.0
    stop_s: 1.5
    motion_s: 1.5
    health_s: 2.0
    upload_s: 10.0
    posture_s: 15.0
    play_margin_s: 5.0      # added to WAV duration
```

> `nao.ip` / `nao.port` are absent by design: only the bridge speaks NAOqi, and
> it does so on `127.0.0.1:9559` from inside the robot. There is no `nao.posture`
> key — `Sit` is not a choice (§12.4.4).

---

## 15. Repository Layout

```
nao-lecturer/
├── config.yaml
├── CLAUDE.md
├── content/
│   ├── lecture.pptx
│   ├── qa_material.md
│   └── gestures.yaml        # §12.6.2
├── sessions/                # generated Q&A transcripts (§7.5)
├── logs/
├── runtime/
│   └── audio/               # generated WAVs, cleared per lecture
├── app/
│   ├── main.py
│   ├── config.py
│   ├── orchestrator.py
│   ├── state.py
│   ├── script_parser.py
│   ├── queue.py
│   ├── slides.py            # SlideController + COM thread
│   ├── transcript.py
│   ├── audio/
│   │   ├── sink.py          # AudioSink protocol
│   │   ├── pc_sink.py
│   │   ├── nao_sink.py      # prepare = POST /upload (§12.3)
│   │   └── playback_queue.py   # prepare + play threads (§4.4)
│   ├── body/                # §12.6
│   │   ├── body.py          # Body protocol
│   │   ├── console_body.py
│   │   ├── nao_body.py
│   │   ├── gesture_library.py  # gestures.yaml loader + validation
│   │   └── scheduler.py     # on_utterance_start subscriber
│   ├── services/
│   │   ├── stt.py
│   │   ├── llm.py
│   │   ├── tts.py           # Piper + soxr
│   │   └── sentences.py     # boundary detector (§7.4)
│   ├── web/
│   │   ├── server.py
│   │   ├── routes_student.py
│   │   ├── routes_operator.py
│   │   ├── tunnel.py        # cloudflared launch + URL parse
│   │   └── static/
│   │       ├── student.html
│   │       ├── operator.html
│   │       └── previewer.js    # §12.6.4
│   └── prompts/
│       ├── qa_system.txt
│       └── fillers.txt      # stage-1 filler variants
├── nao_bridge/              # Python 2.7 — deployed to /home/nao/bridge/
│   ├── bridge.py
│   ├── whitelist.py         # §12.1.1
│   └── deploy.md            # scp + ssh + autostart notes
└── tests/
```

---

## 16. Open Decisions

- **faster-whisper model size:** spec assumes `small` on CPU. Benchmark against
  `base` in Phase 4. Low stakes — the two-stage filler absorbs STT latency (§8).
- **Piper voice selection:** pick in Phase 1 by listening to candidates. Note the
  rate implication in §12.2.
- **Bridge autostart on the robot:** run manually over SSH, or via
  `autoload.ini`. Manual is fine for a single demo. Decide in Phase 6.

Closed since v2.0:

- ~~LLM choice~~ → Qwen3.5 9B text-only, `llama3.1:8b` fallback (§3.1).
- ~~Gesture library contents~~ → authored in Phase 3 (§12.6.2); Phase 7 tunes
  values only.
- ~~Bridge deployment location~~ → on the robot. Forced by SDK availability
  (§3.2).
- ~~Posture for the live demo~~ → `Sit`, permanently (§12.4.4).

---

## 17. Change Log

### v2.2 — from v2.0

| § | Change |
|---|---|
| 1.3 | Leg motion and formal HRI evaluation added to explicit exclusions |
| 2 | NFR-9 (body seam) and NFR-10 (no leg motion) added |
| 3 | LLM → Qwen3.5 9B text-only; bridge row rewritten (robot-hosted); `paramiko`/SFTP replaced by HTTP upload; Choregraphe added as authoring tool |
| 3.1 | VRAM recomputed (~7.1 GB); `ollama ps` check; sampling-parameter override made explicit |
| 3.2 | Choregraphe resolved; Windows `pynaoqi` marked unavailable **and unnecessary**; residual bridge-testing risk stated |
| 4.1 | Bridge moved from the PC box to the NAO box; `Body` and `GestureScheduler` added |
| 4.2 | Python 2/3 split is now a **network** boundary — no Python 2 on the PC at all |
| 4.4 | Gestures row moved from Phase 7 to Phase 3 |
| 7.1 | **Both** filler stages now fire for typed questions as well as voice |
| 8 | Latency budget split into voice and typed tables |
| 12 | Section retitled "NAO Integration and Embodiment" |
| 12.1 | Bridge binds `<nao-ip>:8765`; `/upload`, `/volume`, `/posture`, `/gaze`, `/stiffness` added; joint whitelist (§12.1.1) and client timeouts (§12.1.2) added |
| 12.3 | SFTP **replaced** by HTTP `/upload` — the file still transfers, the transport changed |
| 12.4 | Rewritten — scheduler, context sets, gaze, LED states, three-level balance safety |
| 12.5 | Seated posture and explicit output volume added |
| 12.6 | **New** — `Body` protocol, implementations, gesture format, gaze targets, browser previewer |
| 13 | Gestures moved from Phase 7 into Phase 3; Phase 6 rewritten as `scp` deploy with a six-step bring-up order; Phase 7 becomes hardware tuning only |
| 14 | `body` and `gestures:` blocks added; `nao:` rewritten; `nao.ip`, `nao.port`, `nao.posture` removed |
| 15 | `app/body/`, `content/gestures.yaml`, `nao_bridge/whitelist.py`, `previewer.js` added; `gestures.py` removed |
| 16 | Four decisions closed; bridge autostart opened |

### v2.0 — from v1.0

| § | Change |
|---|---|
| 1.3 | Auto-recovery, empty-notes slides, and `flagged_questions.log` moved out of scope |
| 2 | NFR-1 reworded (local inference, transport may tunnel); NFR-2 restated as two clauses; NFR-7 40 → 5; NFR-8 added |
| 3.1 | `num_ctx: 8192` pinned; full token budget stated; VRAM recomputed |
| 3.2 | New — external dependency and acquisition-risk table |
| 4.3 | New — full concurrency model, four rules, COM timeouts, model lifetime and warm-up |
| 4.4 | Rewritten — `AudioSink` (`prepare`/`play` split) + two-thread `PlaybackQueue` with bounded buffer and turn_id cancellation |
| 5.1–5.2 | `[NO-CHECKPOINT]` meaning **inverted** to "no checkpoint after this section"; example rewritten; empty notes now fatal |
| 5.3 | `Section.index` and `LectureScript.full_narration` added |
| 6.1 | `RECOVERING` state removed |
| 6.4 | Auto-recovery replaced with pause + operator alert + manual reopen |
| 7.1 | Two-stage filler; 25-word cap on the question repeat |
| 7.3 | Narration and current-position marker added to grounding; `num_predict` backstop; testing note |
| 7.4 | Sentence boundary rules and LLM cancellation specified |
| 7.5 | Renumbered into sequence |
| 8 | Rebuilt around the two-stage filler |
| 9.1 | New — Mode A (LAN, typed) / Mode B (tunnel, voice), with privacy note |
| 9.4 | Safari MP4 vs Chromium WebM negotiation; ffmpeg named |
| 9.5 | Max recording 45 s → 30 s |
| 10.2 | Control set trimmed to MVP; Clear-queue vs Skip-question semantics defined |
| 11 | Trimmed to two implemented failures; rest logged |
| 12.1 | Bridge must be threaded |
| 12.2 | Piper 22050 → `soxr` resample step made explicit |
| 12.3 | Persistent SFTP session; pipelined via `prepare()` |
| 12.4 | Gestures rehomed onto `on_utterance_start` |
| 13 | Restructured to Phases 0–7; typed-Q&A and voice split |
| 14 | All magic numbers hoisted into config |
