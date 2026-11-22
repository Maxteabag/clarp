---
name: clarp-voice-adapters
description: Inspect, validate, install, test, select, replace, or remove custom Clarp server voice adapters. Use when adding a TTS engine or diagnosing a custom provider and its voices or previews.
---

# Clarp Voice Adapters

Use the supported manager; do not hand-edit Clarp's generated release:

```bash
clarp-admin tts adapters list
clarp-admin tts adapters validate /path/to/adapter
clarp-admin tts adapters install /path/to/adapter
clarp-admin tts adapters test custom.example
clarp-admin tts use custom.example --fallback none
```

An adapter package is trusted executable code on the user's Computer. Inspect
its manifest and executable source before proposing installation. Validate it
before installation and test its mandatory voice catalogue, preview, and
synthesis operations afterward. Never print credentials or put them in an
adapter manifest.

Installing, replacing, selecting, or removing an adapter changes server state.
Obtain explicit user authorization for the exact mutation. Prefer a normal
install; use `--replace` only when the user asked to update that adapter and the
adapter ID matches. Do not remove an active primary or fallback adapter until a
working replacement has been selected.

The Host's voice pool hands new agents voices for Cartesia, ElevenLabs and
Gemini only. An agent with no stored voice for a custom adapter speaks with
the adapter's default voice, which every such agent shares. If agents should
sound different, give the manifest a default voice and set a voice per agent
(`POST /agent-voice` with `provider`). Check the result in each agent's
`voice_profile.speech` in `GET /agents/snapshot`. See `clarp-agent-admin` for
how the pool picks a voice.

For authoring or diagnosing the versioned executable protocol, read
[references/protocol.md](references/protocol.md).
