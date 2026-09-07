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
