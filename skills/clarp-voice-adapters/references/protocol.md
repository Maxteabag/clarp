# Custom TTS adapter protocol

Place a portable package in a directory containing `manifest.json` and its
relative executable. Schema version 1 requires all three operations:

```json
{
  "schema_version": 1,
  "id": "custom.example",
  "name": "Example Voice",
  "description": "Example local TTS adapter",
  "executable": "./adapter",
  "operations": ["voices", "preview", "synthesize"],
  "audio_format": "audio/mpeg",
  "default_voice": "voice-one",
  "can_fallback": true,
  "timeout_seconds": 120
}
```

`default_voice` is required and must exactly match an ID returned by the
`voices` operation. The manager verifies this before installation so heralds
and Contacts without an explicit provider-specific choice always have a voice.

The executable reads one JSON object from stdin and writes one JSON object to
stdout. Diagnostics belong on stderr. It receives a minimal environment plus
`CLARP_TTS_ADAPTER_ID` and `CLARP_TTS_ADAPTER_ROOT`.

## Operations

`voices` receives:

```json
{"schema_version":1,"operation":"voices"}
```

It must return a non-empty catalogue:

```json
{"ok":true,"voices":[{"id":"voice-one","name":"Voice One","description":"Warm"}]}
```

`preview` and `synthesize` receive `text`, `voice_id`, `output_path`, and
`audio_format`. They must write a regular, non-empty file at the exact requested
path and return `{"ok":true}`. Version 1 accepts `audio/mpeg` and `audio/wav`;
Clarp normalizes WAV to MP3. Preview is mandatory so every provider shown in
Edit Contact can be auditioned.

The manager rejects reserved IDs, absolute executable paths in installable
packages, symlinks, oversized packages, invalid catalogues, excessive output,
unsupported audio types, and operations that exceed their timeout.
