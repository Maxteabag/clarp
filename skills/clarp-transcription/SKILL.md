---
name: clarp-transcription
description: Inspect, remove, test, select, or diagnose Clarp server transcription models and custom adapters. Use clarp-transcription-model-installation for managed model recommendations and installation.
---

# Clarp Transcription

Use the supported model manager:

```bash
clarp-admin transcription list
clarp-admin transcription use faster-whisper:small.en
clarp-admin transcription test
```

For managed model recommendations and downloads, use the
`clarp-transcription-model-installation` skill.

Only validated local artifacts are advertised as installed. Never download a
model implicitly during transcription.

Custom speech-to-text adapters use the same supported manager:

```bash
clarp-admin transcription adapters list
clarp-admin transcription adapters validate /path/to/adapter
clarp-admin transcription adapters install /path/to/adapter
clarp-admin transcription adapters test custom.example
clarp-admin transcription use custom.example:model-id
```

Adapters are trusted executable code. Inspect their source, validate before
installation, and test both model discovery and transcription afterward.
Installing, replacing, selecting, or removing one requires explicit user
authorization. Never remove the active adapter; select another model first.
Credentials belong in adapter-owned configuration, never its manifest.

Read [references/custom-adapter-protocol.md](references/custom-adapter-protocol.md)
when authoring or diagnosing a custom adapter.

## Benchmarking providers on real retained audio

`~/.cache/clarp/heard/` keeps real user clips (WAV plus JSON sidecar) whose filename is the
`trace_id`, joinable to `vocab_runs` in `state.sqlite` for the model, prompt and latency
recorded during the live interaction. That is the corpus for offline comparison; no synthetic
audio is needed.

```bash
skills/clarp-transcription/scripts/benchmark_providers.py
```

It makes **real, paid API calls** to ElevenLabs and Cartesia, so run it deliberately. It
compares batch POST against realtime WebSocket for both vendors and writes raw JSON results.

Four traps that make such a benchmark wrong or misleading:

1. **Report post-speech latency, not total.** For a streaming engine the only number a user
   feels is commit to final text. Comparing that against a batch engine's whole request is the
   honest comparison; comparing total stream time is not.
2. **Pushing chunks faster than realtime is not a phone.** Streaming 50 ms of audio every
   10 ms is 5x realtime and gives the provider less idle time than a real device would. State
   the multiplier alongside any latency claim.
3. **Batch figures exclude the phone-to-Host upload**, which is the cost streaming exists to
   avoid. Host-to-provider timings flatter batch, so say so.
4. **A hand-written reference is not ground truth.** Word-error rates against it mostly measure
   disfluency and punctuation policy: an engine that faithfully transcribes "um" scores worse
   than one that silently drops it. Read the per-clip diffs before believing a percentage.

Measured 2026-09-07 over four clips (3.8 to 21.6 seconds), mean post-speech latency: Cartesia
Ink-Whisper batch 0.297 s, ElevenLabs Scribe Live 0.405 s, ElevenLabs Scribe v2 batch 1.202 s,
Cartesia Ink-2 WebSocket 1.461 s. The durable finding is that live latency is flat in utterance
length (0.364 to 0.456 s across all four clips) while batch grows with it — not that live is
fastest, because Ink-Whisper won outright on short clips. Ink-2 also clipped the first word of
utterances, which is a real streaming boundary defect rather than a reference artifact.
