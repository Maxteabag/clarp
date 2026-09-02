# Turn detection roadmap

How Clarp decides you've finished speaking, and where we want to take it.

## Today

Pure acoustic VAD. `SileroListener` runs Silero (FluidAudio/CoreML) over the
mic, and ends the turn after a fixed gap of silence:

```swift
// SileroListener.swift
private let segConfig = VadSegmentationConfig(minSilenceDuration: 0.6)
```

Silero has no idea whether your *sentence* is finished — it only sees silence.
So it fires on a mid-thought pause ("I need to think about that for a
moment…") and feels eager. The 0.6 s was tuned for push-to-talk snappiness.

Near-term tweak (separate, small): make `minSilenceDuration` a user setting
(slider in Settings, default ~1.8 s). That just moves the fixed number; it
doesn't make it smart. It also becomes the **ceiling** for the work below.

## Phase 1 — Smart Turn v3 (semantic endpointing) — chosen, stable path

Replace "fixed silence = done" with a model that judges, from the audio, whether
you actually sound finished. Smart Turn stays *alongside* Silero, not instead of
it.

- **Model:** [pipecat-ai/smart-turn-v3](https://huggingface.co/pipecat-ai/smart-turn-v3)
  — Whisper-tiny encoder + linear head, 8M params.
- **Input:** 16 kHz mono PCM, ≤ 8 s window. Pad with zeros at the *front* if
  shorter; truncate from the *front* if longer (real audio sits at the end).
- **Output:** turn-complete probability, threshold ~0.5.
- **Cost:** ~12 ms CPU inference. Files: 8 MB int8 ONNX / 32 MB fp32.
- **License:** BSD-2-Clause (commercial-safe).

**Runtime (Phase 1 decision): ONNX Runtime Mobile.** The model ships as ONNX
only. ONNX Runtime is the documented, low-risk path — bundle the 8 MB int8 model
and run it as-is. Adds a dependency to the iOS app, accepted for now in exchange
for getting it working reliably. The native CoreML route is Phase 2.

**How it slots into `SileroListener`:**

1. Keep Silero as the cheap always-on gate, but use a *short* silence trigger
   (~0.2–0.4 s) to mean "check now," not "end the turn."
2. On that trigger, run Smart Turn once over the current utterance buffer
   (`capture`, truncated to the last ~8 s).
3. `complete` → finish the utterance and send. `not complete` → it was a pause;
   keep capturing and re-check on the next silence. If more speech arrived
   mid-inference, re-run on the *full* turn (the model wants full context).
4. The configurable `minSilenceDuration` is the **hard ceiling** — force-send if
   Smart Turn keeps saying "not done" past it, so we can't hang forever.

Work involved: add ONNX Runtime to the app, bundle the int8 model, write a small
Swift inference wrapper (build the padded/truncated 8 s window → run → probability),
and rewrite the end-of-turn branch of `SileroListener.consume`. Plus the Settings
ceiling slider.

## Phase 2 — Convert Smart Turn to CoreML (future)

Drop the ONNX Runtime dependency and run Smart Turn natively on the Apple Neural
Engine, consistent with how Silero already runs via FluidAudio/CoreML.

- Convert the ONNX model with `coremltools` (Whisper-tiny encoder + linear head
  is a convertible shape).
- Swap the inference wrapper's backend from ONNX Runtime to CoreML; the
  `SileroListener` integration from Phase 1 stays the same.
- Verify parity against the ONNX outputs before removing the ONNX dep.

Upside: no third-party runtime, ANE acceleration, one less binary. Risk:
conversion fiddliness (quantization, op support), which is exactly why it's not
Phase 1.

## Phase 3 — Refinements (future, optional)

- **Adaptive ceiling:** learn the user's typical between-utterance pause and adapt
  the ceiling (LiveKit's "dynamic" endpointing does this with an EMA of pauses).
- **Text-based EOU as a second signal:** we already stream Apple's live partial.
  A transcript model like [livekit/turn-detector](https://huggingface.co/livekit/turn-detector)
  (Qwen2.5-0.5B fine-tune) could add semantic completeness on top of Smart Turn's
  prosody. Heavier on-device; verify its license before shipping commercially.
- **Research-grade low-latency EOT:** compact MFCC-student models (~1M params,
  ~36 ms) exist in recent papers if Smart Turn's window latency becomes a problem.

## References

- LiveKit — [turn detection: VAD, endpointing, model-based](https://livekit.com/blog/turn-detection-voice-agents-vad-endpointing-model-based-detection)
- AssemblyAI — [intelligent turn detection / endpointing](https://www.assemblyai.com/blog/turn-detection-endpointing-voice-agent)
- Deepgram — [evaluating end-of-turn models](https://deepgram.com/learn/evaluating-end-of-turn-detection-models)
- Pipecat Smart Turn — [GitHub](https://github.com/pipecat-ai/smart-turn) · [v3 model card](https://huggingface.co/pipecat-ai/smart-turn-v3)
