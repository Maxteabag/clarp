# Model-install monitor ownership

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R43** `tests/unit/test_transcription_models.py::test_repeated_active_install_requests_share_one_monitor_thread`: Each repeated install request starts another monitor thread for the same running job. Test actual thread creation and generation ownership; mocking _start_monitor would reject a valid deduplication inside that helper.

## Implementation and verification

Deduplicate actual monitor threads per active job/generation, including concurrent install requests and a replacement HTTP server. Verify old monitor completion cannot overwrite a new generation; preserve activation callbacks. The audit monkeypatches _start_monitor, so replace that implementation-specific assertion with thread/worker behavior tests. Agent-deletion cancellation requires a separate exact-owner/generation policy; never kill arbitrary or Computer-owned jobs.

## Qualified or excluded claims

- **R34** (needs-contract): Agent deletion currently leaves background-job rows. Automatically killing every job is not yet a valid contract: jobs can belong to a Computer or a restartable service. Test exact owner/generation/worker identity and define cancel versus detach before implementing.
