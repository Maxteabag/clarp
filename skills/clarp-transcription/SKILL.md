---
name: clarp-transcription
description: Inspect, remove, test, select, or diagnose Clarp server transcription models and custom adapters, and show or set the language and engines one agent is heard with. Use clarp-transcription-model-installation for managed model recommendations and installation.
---

# Clarp Transcription

Use the supported model manager:

```bash
clarp-admin transcription list
clarp-admin transcription use faster-whisper:small.en
clarp-admin transcription test
```

## How one agent is heard

On the Rust Host (`clarp-host`), an agent's own hearing language and engines are
shown and set with `clarp-admin transcription agent`, for yourself or, with
`--session`, for another agent of the same Host. Change another agent's hearing
only when the owner asks for it: a wrong language garbles what he says to that
agent, and nothing tells him why.

```bash
clarp-admin transcription agent                       # show your own
clarp-admin transcription agent --language no         # one code, or up to three: no,en
clarp-admin transcription agent --session SESSION --engines elevenlabs:scribe_v2,google:chirp_3
clarp-admin transcription agent --language default --engines default   # heard like every agent
```

Engines are tried in the order given; a set of languages needs Google Chirp or
ElevenLabs Scribe. It holds from the next clip. The Python Host has no such
command: there the owner sets it in the app's transcription settings.

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
