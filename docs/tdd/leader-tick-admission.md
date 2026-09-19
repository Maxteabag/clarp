# Leader tick consolidation and automation admission

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R41** `tests/unit/test_team_leader.py::test_one_leader_gets_one_consolidated_tick_for_multiple_teams`: One precomputed tick per team can preempt the same leader repeatedly with identical team-wide prompts. Consolidate by leader identity before dispatch, preserving all team context.

## Implementation and verification

Consolidate eligible team work by leader identity so one tick cannot preempt the same leader with duplicate prompts. Preserve context for all teams. Heartbeat admission needs an actual accepted/skipped result at the production callback boundary before changing cadence. Do not adopt same-timestamp retries or auto-resume explicit user stops.

## Qualified or excluded claims

- **R25** (deferred): Two retries at the exact same timestamp is not a sound requirement. Existing cadence throttles repeated failures. Track attempted versus accepted time and choose bounded retry/backoff before changing it; never hot-loop unavailable providers.
- **R26** (deferred): A heartbeat health entry is an observability enhancement; the failure is already logged. An exact subsystem key is not evidence of broken delivery. Add it with the retry/admission contract if useful.
- **R27** (needs-contract): Production heartbeat callback returns None both on success and on busy/missing-agent skip. Counting skips is a real admission ambiguity, but a mocked False return does not reproduce the actual HTTP/runtime boundary. Test that callback and define its result first.
- **R28** (needs-contract): reason=interrupted is treated as explicit stop regardless of source. Some of these are deliberate preemption, so blindly auto-resuming is unsafe. Require actual producer provenance and test real stop, preemption and crash separately.
- **R42** (deferred): run_once returns eligible count after failed sends. Logging already records the failure and the loop ignores this count; correct metrics if consuming the return value, but do not treat it as a delivery outage.
