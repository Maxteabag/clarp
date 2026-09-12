# Queued audio ownership and durable held delivery

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R06** `tests/regression/test_audio_lifecycle_failures.py::test_old_tts_work_cannot_overwrite_a_newer_turns_trace`: Queued audio writes the agent-global trace and can steal a newer turn owner. Pass the queued trace through synthesis/clip recording without mutating live ownership.
- **R08** `tests/regression/test_audio_lifecycle_failures.py::test_server_held_herald_clip_has_a_durable_recovery_record`: Herald stores an off-focus reply only in memory, with neither held status nor a replay row. Restart loses the delivery reference. Persist held delivery before withholding broadcast.
- **R10** `tests/regression/test_audio_lifecycle_failures.py::test_terminal_playback_ack_cannot_move_back_to_queued`: A late queued ack overwrites play-ok and makes consumed audio recoverable. Fence stale transitions while preserving explicitly requested replay; no new per-device ledger is required for this fix.

## Implementation and verification

Pass immutable queue trace context through synthesis rather than modifying the current turn. Persist held audio and replay metadata before withholding delivery. Prevent stale acks from regressing a completed clip. Before crash deduplication, add explicit queue-to-clip identity: a trace can contain several utterances. Define bounded held-file retention and expire recovery references consistently.

## Qualified or excluded claims

- **R07** (needs-contract): The publication/completion crash window exists, but trace alone is not a queue identity: one turn can produce several utterances. The fixture also never creates the claimed MP3. Add explicit queue-to-clip linkage and real-file recovery tests before demanding no resynthesis.
- **R09** (needs-contract): Janitor ignores held state while recovery advertises held clips. Real mismatch, but retaining every held file forever is not safe. Define bounded held retention and remove expired recovery references together.
- **R44** (deferred): The same stopped TTSWorker instance cannot start again, but production builds a fresh instance for each server. No current same-instance restart caller was found. Do not expand lifecycle semantics solely to satisfy this unit test.
