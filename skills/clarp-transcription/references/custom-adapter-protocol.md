# Custom transcription adapter protocol

A portable package contains `manifest.json` and a relative executable:

```json
{
  "schema_version": 1,
  "id": "custom.example",
  "name": "Example STT",
  "description": "Private transcription service",
  "executable": "./adapter",
  "operations": ["models", "transcribe"],
  "default_model": "general",
  "timeout_seconds": 120
}
```

Adapter IDs must begin with `custom.` and contain 2–64 lowercase letters,
digits, dots, underscores, or hyphens. Local model IDs contain 1–128 letters,
digits, dots, underscores, or hyphens and are namespaced by Clarp as
`<adapter-id>:<model-id>`. `timeout_seconds` defaults to 120 and must be between
5 and 600.

The executable reads one JSON request from stdin and writes one JSON response
to stdout. Diagnostics belong on stderr. Every response must explicitly contain
`"ok": true` or `"ok": false`.

Clarp supplies a minimal environment plus:

- `CLARP_ADAPTER_PROTOCOL=stt`
- `CLARP_ADAPTER_ID`, the manifest adapter ID
- `CLARP_ADAPTER_ROOT`, the installed package directory

Use the adapter root to locate adapter-owned configuration. Do not expect Clarp
or phone credentials in the environment.

`models` receives `{"schema_version":1,"operation":"models"}` and returns:

```json
{"ok":true,"models":[{"id":"general","name":"General","weight":"remote","languages":["en"]}]}
```

`default_model` is required and must match a returned local model ID.

`transcribe` receives `model_id`, `audio_path`, `content_type`, and
`vocabulary_prompt`. The audio path is a temporary server-owned file. Return:

```json
{"ok":true,"text":"Hello world.","ends_terminal":true,"duration_seconds":0.42}
```

`ends_terminal` is an optional Boolean indicating whether the trimmed transcript
ends with `.`, `!`, or `?`. `duration_seconds` is optional adapter processing time,
not audio duration, and must be a finite number between 0 and 3600.

Clarp bounds request and response sizes, uses a minimal environment, enforces
timeouts on the entire process group, limits transcript size, rejects symlinks
and absolute executables, and tests both operations transactionally before
installation.
