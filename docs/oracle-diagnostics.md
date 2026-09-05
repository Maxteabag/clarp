# Oracle diagnostics

Oracle diagnostics are private conversation data, separate from general Host
logs. Enable explicitly in the Host TOML before restarting through the normal
installation workflow:

```toml
[openai]
oracle_diagnostics = true
realtime_transcription_model = "gpt-4o-mini-transcribe"
```

This adds input transcription to the Host-owned Realtime session configuration.
Transcription uses an additional API model and may incur additional usage. Set
`realtime_transcription_model = ""` to retain event/output diagnostics without
input ASR. The voice model and tool policy remain unchanged. Provider dashboard
tracing is not enabled by this change.

Private JSONL files live in the platform data directory under `oracle-diagnostics`
(`~/.local/share/clarp/oracle-diagnostics` on Linux). Files are mode 0600; retain
at most 20 sessions, bounded to 8 MiB each. At the size limit, recording stops and
an `oracleDiagnosticsWriteFail` event is emitted; voice continues. Delete files
there to remove retained history. The data includes spoken text and tool content;
audio payloads and transport authorization headers are excluded. Disabling
capture does not delete old files.

```sh
python3 scripts/inspect_oracle.py
python3 scripts/inspect_oracle.py --session SESSION_UUID --include-text
```

The first command emits event counts only. The second explicitly reveals private
conversation/tool data. Correlate native and Host timelines using provider
session/item/response/call IDs. Host `session.close` includes approximate streamed
byte totals, not proof that audio played on the phone. Native playback/drain and
ACK records supply the other half. Missing records are unknown, never success.

The journal records the effective sanitized session contract and its SHA256,
input transcription events, output text, tool calls/output, response status/usage,
errors and connection lifetime. Input ASR is asynchronous and is not exactly what
the speech model understood; match item IDs rather than event arrival order.

Tests run without a model or real agent: `python -m pytest
 tests/unit/test_oracle_diagnostics.py tests/unit/test_oracle_realtime.py
 tests/unit/test_oracle_delegations.py -q` in the repo environment. They cover
recording boundaries, bounded-file failure, Host-only ASR configuration and the
existing durable delegation invariants.
