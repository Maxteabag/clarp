# Turn admission, ledger closure, and stale hook ownership

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R14** `tests/regression/test_hook_lifecycle_failures.py::test_stale_stop_hook_cannot_mark_a_newer_turn_done`: The stop hook has no turn-owner fence and can mark a newer turn done. The test proposes a currently unsupported environment variable; the fix must propagate an actual runner identity and preserve interactive fallback.
- **R22** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_restart_recovery_closes_the_orphaned_durable_turn`: Runtime crash recovery records an interruption but leaves the durable turn open. Closing must target the dead trace, never a new runtime-owned turn.
- **R24** `tests/regression/test_restart_heartbeat_scheduler_failures.py::test_one_trace_cannot_create_two_durable_turn_rows`: Dispatch and Claude UserPromptSubmit can insert duplicate ledger rows for one trace. Make nonempty logical identities idempotent while retaining distinct turns and empty-trace compatibility.
- **R45** `tests/unit/test_turn_dispatch.py::test_clean_completion_closes_the_durable_turn_row`: A successful result never closes the corresponding turn row. Fix by trace/turn identity while retaining queued audio policy and fencing old callbacks.
- **R46** `tests/unit/test_turn_dispatch.py::test_idempotent_retry_respawns_when_first_attempt_never_started`: A user row persists before synchronous spawn failure; retry of that client_msg_id deduplicates as success without dispatch. User-message existence cannot be the durable admission receipt.

## Implementation and verification

Use durable admission identity distinct from the visible user row. Close only the owning trace on success/failure/crash; make nonempty logical turn opens idempotent. Propagate a real hook trace contract and preserve terminal-session fallback. Verify concurrent retries, preemption, queued audio policy, and runtime socket acknowledgement loss before shipping.

## Qualified or excluded claims

- **R23** (needs-contract): Historical transcript cells retain running status. Presentation should distinguish dead-runtime work, but blindly rewriting all cells could falsify independently surviving subagents and be undone by re-import. Define a projection/liveness rule first.
