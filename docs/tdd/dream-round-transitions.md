# Monotonic dream round completion

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R38** `tests/unit/test_dreaming.py::test_fast_dream_completion_cannot_be_regressed_back_to_sent`: Synchronous completion is overwritten by mark_round_sent after dispatch returns. Use conditional state transitions and verify parent progress also stays completed.

## Implementation and verification

Make the post-dispatch sent transition conditional on the round still being queued, and do not regress completed parent progress. Verify inline and asynchronous completion, rejected dispatch and stale recovery. Do not delete a worktree merely because a final digest exists: preserve unmerged/dirty/active work and follow AGENTS.md closeout requirements.

## Qualified or excluded claims

- **R39** (rejected): Unconditional deletion on a final digest can destroy unmerged work or a running/restartable dependency. The fixture is only an empty directory, not a Git worktree. Reject this contract; retention/cleanup must obey repository closeout rules.
