# Reliability audit after the separate runtime

Baseline: `eeecc72c3d6f6ca34f3929f63c8d44d87eba733f` (#34). This is the
current assessment; it supersedes the older audit's blanket “confirmed bug”
wording. PR #25 remains a draft umbrella and must not be merged with red tests.

## Results and relevance

All **53 original failing cases**, plus the existing archive guard, were run.
The unadapted branch produced **53 failures and 1 pass**. After correcting the
runtime boundary and stale fixtures, the equivalent inventory produces
**51 failures and 3 passes**. A failed assertion is not automatically an
accepted defect.

- **32 confirmed defects** remain for focused TDD follow-up.
- **2 confirmed defects have fixes in PRs:** [#35](https://github.com/Maxteabag/clarp/pull/35)
  (limited file access) and [#36](https://github.com/Maxteabag/clarp/pull/36)
  (owned process groups). They are not merged, so the audit baseline stays red.
- **9 cases need a clearer contract or a more realistic reproduction** before a fix.
- **7 are deferred improvements or unsupported usage**, rather than demonstrated production defects.
- **1 is rejected as unsafe:** unconditional dream-worktree deletion.
- **1 is resolved by #34**, **1 legacy test is superseded by a runtime guard**,
  and **1 was already a passing archive guard**.

The important rejected assumptions are: arbitrary 32-thread limits; immediate
same-timestamp heartbeat retry; restarting a stopped TTS worker object that
production never reuses; inferring a permanently stuck recorder from a missing
onerror callback without exercising its existing onstop handler; and deleting
unmerged or actively used worktrees on receipt of a final dream digest.

## Runtime boundary and fixture corrections

An HTTP restart with a runtime client neither marks work interrupted nor sends
synthetic continuity prompts. Existing #34 integration tests cover a live
runtime callback completing after the HTTP listener is replaced. A runtime
crash calls `runtime_startup.recover_runtime`; the audit now exercises that
production function with real interruption and reconciliation database logic.
The boot-order regression is green. Runtime crash ledger closure still fails.
The legacy heartbeat-latch test was replaced with a runtime startup guard
showing that one failed continuity dispatch does not block healthy agents.
Retrying that failed prompt requires durable per-agent admission/backoff, not
calling all of recovery again.

Other fixture corrections: doctor stubs the new runtime service probe; the
process test invokes the actual Claude runner; the SSE overlap test reads to a
live sentinel so success can terminate; and reconnect timer mocks implement
cancellation instead of treating every clearTimeout as a no-op. No cases were
hidden with xfail or skip.

## Priority fixes and proof

| PR | Concrete result | Evidence |
|---|---|---|
| [#35](https://github.com/Maxteabag/clarp/pull/35) | Limited credentials cannot browse `/agent-file` or `/agent-files`; full and admin access is preserved | Before: 6 red scope cases, 12 allowed reads. After: 129 integration/files/pairing tests passed; clean structured autoreview. |
| [#36](https://github.com/Maxteabag/clarp/pull/36) | Five CLI runners own separate POSIX process groups and stop signals reach descendants | Before: all five real-runner probes leaked their child. After: 115 runner/registry/runtime-bridge tests passed; clean structured autoreview. An initial existing callback-drain assertion raced and passed unchanged on rerun. |

Limited scope remains an API permission boundary, not an agent sandbox: `/send`
can ask host-powered agents to do work. Process-group signaling does not claim
containment of deliberately detached daemons or add a new escalation policy.
Both exact PR heads also passed GitHub Docker integration: [#35 CI](https://github.com/Maxteabag/clarp/actions/runs/33922370034) and [#36 CI](https://github.com/Maxteabag/clarp/actions/runs/33922371564).
Neither PR changes HTTP-restart survival. No live service was deployed or
restarted, and no PR has been merged as part of this work.

## Focused TDD slices

These are **draft reproductions plus implementation contracts**, not completed
fixes. Each carries only accepted red cases for its subject and documents the
claims that still need qualification. Implement and add positive controls
before marking a slice ready; never merge it solely to land a red test.

- [#37](https://github.com/Maxteabag/clarp/pull/37) — Turn admission, ledger closure, and stale hook ownership (R14, R22, R24, R45, R46).
- [#38](https://github.com/Maxteabag/clarp/pull/38) — SSE replay handoff, reconnect ownership, and contact health (R01, R48, R52, R53).
- [#39](https://github.com/Maxteabag/clarp/pull/39) — HTTP authentication availability and error diagnostics (R04, R05, R13).
- [#40](https://github.com/Maxteabag/clarp/pull/40) — Durable scheduled occurrence admission (R30, R31, R32, R33, R35).
- [#41](https://github.com/Maxteabag/clarp/pull/41) — Queued audio ownership and durable held delivery (R06, R08, R10).
- [#42](https://github.com/Maxteabag/clarp/pull/42) — Archive bootstrap, staging cleanup, and update identity (R15, R16, R19, R36, R40).
- [#43](https://github.com/Maxteabag/clarp/pull/43) — Mic admission and plain-TTS capability boundaries (R11, R37, R47, R49).
- [#44](https://github.com/Maxteabag/clarp/pull/44) — Leader tick consolidation and automation admission (R41).
- [#45](https://github.com/Maxteabag/clarp/pull/45) — Monotonic dream round completion (R38).
- [#46](https://github.com/Maxteabag/clarp/pull/46) — Model-install monitor ownership (R43).

Structured review command for the adaptations: `skills/autoreview/scripts/autoreview --mode local --no-web-search --prompt-file /tmp/clarp-audit-review-context.md`. The final review reported no actionable findings. The context explicitly identified this as an intentionally red audit, not a product implementation.

## Run the exact inventory

After `uv sync --frozen --group dev` and `npm ci`:

```sh
python scripts/run_reliability_audit.py --output /tmp/clarp-reliability-audit
```

Exit 1 is expected while reproduced assertions remain red. The runner selects
47 Python cases and 7 JavaScript cases explicitly, preserves raw reports, and
writes `results.json` with each outcome and judgment. Runner errors or missing
cases never become successful results. The committed
[results](reliability-audit-runtime-results.json) exclude raw logs and contain
only stable identifiers, results and rationales. The machine-readable
[inventory](reliability-audit-cases.json) is the selection source of truth.

## Per-case assessment

“Fixed in PR” is unmerged branch evidence. “Needs contract” means the observed
behavior deserves investigation, but the current assertion does not yet define
a safe implementation. “Deferred” is not a claim that the behavior is ideal.

| Case | Reproduction | Baseline result | Judgment and next action |
|---|---|---|---|
| R01 | [sse event seen during replay is not delivered again as live](../tests/integration/test_server_di.py) | failed | **confirmed** — SSE replay/live overlap duplicates a persisted event. Adapted the test to finish on a live sentinel, so a correct fix can pass without waiting for a nonexistent duplicate. |
| R02 | [slow clients cannot create unbounded request threads](../tests/integration/test_server_di.py) | failed | **deferred** — Forty-eight concurrent subscribers do not prove exhaustion, and 32 is an invented ceiling. Define measured admission, SSE reservations, overload responses and recovery before imposing a cap. |
| R03 | [limited device cannot read arbitrary host files](../tests/integration/test_server_di.py) | failed | **fixed-in-pr** — Direct limited-token file read is an API privilege leak. PR #35 denies both file endpoints for all credential transports; limited send remains host-powered, not sandboxed. |
| R04 | [access log redacts query credentials](../tests/integration/test_server_di.py) | failed | **confirmed** — Access logging interpolates raw request URLs, so query credentials enter logs. Redact credential values before logging; preserve useful route diagnostics. |
| R05 | [transcription provider errors remain valid json](../tests/integration/test_server_di.py) | failed | **confirmed** — A provider error containing quotes/newlines produces invalid application/json. Serialize the response; keep the HTTP status and original error text. |
| R06 | [old tts work cannot overwrite a newer turns trace](../tests/regression/test_audio_lifecycle_failures.py) | failed | **confirmed** — Queued audio writes the agent-global trace and can steal a newer turn owner. Pass the queued trace through synthesis/clip recording without mutating live ownership. |
| R07 | [restart does not requeue tts after its clip was already broadcast](../tests/regression/test_audio_lifecycle_failures.py) | failed | **needs-contract** — The publication/completion crash window exists, but trace alone is not a queue identity: one turn can produce several utterances. The fixture also never creates the claimed MP3. Add explicit queue-to-clip linkage and real-file recovery tests before demanding no resynthesis. |
| R08 | [server held herald clip has a durable recovery record](../tests/regression/test_audio_lifecycle_failures.py) | failed | **confirmed** — Herald stores an off-focus reply only in memory, with neither held status nor a replay row. Restart loses the delivery reference. Persist held delivery before withholding broadcast. |
| R09 | [audio janitor preserves a durably held clip](../tests/regression/test_audio_lifecycle_failures.py) | failed | **needs-contract** — Janitor ignores held state while recovery advertises held clips. Real mismatch, but retaining every held file forever is not safe. Define bounded held retention and remove expired recovery references together. |
| R10 | [terminal playback ack cannot move back to queued](../tests/regression/test_audio_lifecycle_failures.py) | failed | **confirmed** — A late queued ack overwrites play-ok and makes consumed audio recoverable. Fence stale transitions while preserving explicitly requested replay; no new per-device ledger is required for this fix. |
| R11 | [tts auth error does not claim an elevenlabs quota failure](../tests/regression/test_audio_lifecycle_failures.py) | failed | **confirmed** — Cartesia authentication failure is labeled ElevenLabs quota. Provider and error category must determine the message; keep raw details in diagnostics. |
| R12 | [pull requests run the fast python and javascript suites](../tests/regression/test_ci_failures.py) | failed | **deferred** — Missing fast-suite CI is a useful coverage improvement, not a runtime defect. Grepping workflow text cannot prove pull_request triggers or required branch checks. Replace with workflow/job validation; branch protection is separate. |
| R13 | [paired device authentication remains readable during writer contention](../tests/regression/test_database_availability_failures.py) | failed | **confirmed** — Valid paired authentication requires an unrelated last-seen write and fails under a WAL writer lock. Make telemetry best-effort without hiding revoked tokens or unrelated database corruption. |
| R14 | [stale stop hook cannot mark a newer turn done](../tests/regression/test_hook_lifecycle_failures.py) | failed | **confirmed** — The stop hook has no turn-owner fence and can mark a newer turn done. The test proposes a currently unsupported environment variable; the fix must propagate an actual runner identity and preserve interactive fallback. |
| R15 | [quick start removes its downloaded source tree](../tests/regression/test_installer_failures.py) | failed | **confirmed** — exec replaces get.sh, discarding its EXIT cleanup trap. Temporary source and persisted source metadata must be handled together; do not merely delete a path an updater still needs. |
| R16 | [advertised pipe install preserves interactive terminal input](../tests/regression/test_installer_failures.py) | failed | **confirmed** — The advertised pipe hands non-TTY stdin to setup. The failure is real; the fix test needs a controlling terminal, not only openpty file descriptors, before testing /dev/tty handoff. |
| R17 | [archive install uses the supplied release identity](../tests/regression/test_installer_failures.py) | failed | **needs-contract** — Archives currently report unknown-dirty, but CLARP_VERSION is not an install.sh input contract. Test an explicit supplied archive identity wired from the downloader instead of assuming Docker build args apply to host installs. |
| R18 | [nonmanaged archive install does not require toolchain lock](../tests/regression/test_installer_failures.py) | passed | **guard** — Existing non-managed archive lockfile behavior passes. This is the extra guard, not one of the 53 original failures. |
| R19 | [failure before activation removes the incomplete release directory](../tests/regression/test_installer_failures.py) | failed | **confirmed** — Environment setup can fail after moving staging into releases and before installing rollback cleanup. Incomplete directories accumulate; retain active/previous releases and remove only the failed staged release. |
| R20 | [interrupt terminates the backend process tree](../tests/regression/test_process_tree_failures.py) | failed | **fixed-in-pr** — User stop reaches only the CLI parent. The audit now invokes a real runner; PR #36 verifies all five runners kill cooperative descendants without touching another process. Detached daemons and escalation policy are separate. |
| R21 | [production boot order does not erase interrupted turn evidence](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | passed | **resolved** — The old test manually replayed legacy HTTP ordering. Production recover_runtime now records crash evidence before reconciliation. The adapted real-database runtime test passes; runtime-backed HTTP replacement already has continuity integration coverage. |
| R22 | [restart recovery closes the orphaned durable turn](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — Runtime crash recovery records an interruption but leaves the durable turn open. Closing must target the dead trace, never a new runtime-owned turn. |
| R23 | [restart finalizes running subagent cells](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **needs-contract** — Historical transcript cells retain running status. Presentation should distinguish dead-runtime work, but blindly rewriting all cells could falsify independently surviving subagents and be undone by re-import. Define a projection/liveness rule first. |
| R24 | [one trace cannot create two durable turn rows](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — Dispatch and Claude UserPromptSubmit can insert duplicate ledger rows for one trace. Make nonempty logical identities idempotent while retaining distinct turns and empty-trace compatibility. |
| R25 | [failed periodic heartbeat is retryable without waiting a full interval](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **deferred** — Two retries at the exact same timestamp is not a sound requirement. Existing cadence throttles repeated failures. Track attempted versus accepted time and choose bounded retry/backoff before changing it; never hot-loop unavailable providers. |
| R26 | [heartbeat dispatch failure is visible in subsystem health](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **deferred** — A heartbeat health entry is an observability enhancement; the failure is already logged. An exact subsystem key is not evidence of broken delivery. Add it with the retry/admission contract if useful. |
| R27 | [heartbeat callback must confirm that dispatch was accepted](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **needs-contract** — Production heartbeat callback returns None both on success and on busy/missing-agent skip. Counting skips is a real admission ambiguity, but a mocked False return does not reproduce the actual HTTP/runtime boundary. Test that callback and define its result first. |
| R28 | [non user interruption is not mistaken for an explicit stop](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **needs-contract** — reason=interrupted is treated as explicit stop regardless of source. Some of these are deliberate preemption, so blindly auto-resuming is unsafe. Require actual producer provenance and test real stop, preemption and crash separately. |
| R29 | [runtime crash recovery isolates continuity dispatch failure](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | passed | **superseded** — The old once-per-HTTP-process restart latch is outside normal runtime startup. Replaced with a passing test that one failed runtime-continuity dispatch does not block healthy agents. Durable retry for the failed prompt remains a design decision, not fixed by replaying recovery wholesale. |
| R30 | [failed scheduled dispatch remains due for retry](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — Schedule advance commits before dispatch; a known synchronous failure loses the occurrence. Preserve a retryable occurrence with a stable identity, and handle ambiguous runtime acknowledgement without duplicate execution. |
| R31 | [two scheduler workers cannot dispatch the same due run twice](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — Read then advance is not an atomic occurrence claim. Overlapping workers can both dispatch the same row. Use a durable conditional claim, not a process-local lock or a transaction held across backend I/O. |
| R32 | [schedule disabled after due read cannot still dispatch](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — A row disabled after the due snapshot is still dispatched. Revalidate the same occurrence and configuration when claiming; define the race boundary once admission has already happened. |
| R33 | [deleted agent cannot leave an enabled schedule behind](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — Soft deletion leaves recurring schedule rows enabled. These rows continue to be selected, and a reused session may receive stale work. Disable by stable agent identity at deletion. |
| R34 | [deleted agent cannot leave active background work behind](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **needs-contract** — Agent deletion currently leaves background-job rows. Automatically killing every job is not yet a valid contract: jobs can belong to a Computer or a restartable service. Test exact owner/generation/worker identity and define cancel versus detach before implementing. |
| R35 | [leap day schedule searches far enough to find its next run](../tests/regression/test_restart_heartbeat_scheduler_failures.py) | failed | **confirmed** — A valid February 29 schedule cannot find 2028 from March 2025 because the search stops at 366 days. Extend a bounded calendar search without changing existing day-of-week semantics accidentally. |
| R36 | [doctor checks the backend selected at setup](../tests/unit/test_clarp_admin.py) | failed | **confirmed** — Doctor accepts any installed Claude/Codex binary even when setup chose the missing one. Adapted the new runtime service mock so the test now reaches the intended configured-backend assertion. |
| R37 | [ssml capability rejects truthy non boolean values](../tests/unit/test_custom_tts_adapters.py) | failed | **confirmed** — bool("false") advertises SSML support and leaks markup to a plain adapter. Validate the capability as a JSON boolean or default conservatively; do not rely on truthiness. |
| R38 | [fast dream completion cannot be regressed back to sent](../tests/unit/test_dreaming.py) | failed | **confirmed** — Synchronous completion is overwritten by mark_round_sent after dispatch returns. Use conditional state transitions and verify parent progress also stays completed. |
| R39 | [completed dream removes its disposable worktree](../tests/unit/test_dreaming.py) | failed | **rejected** — Unconditional deletion on a final digest can destroy unmerged work or a running/restartable dependency. The fixture is only an empty directory, not a Git worktree. Reject this contract; retention/cleanup must obey repository closeout rules. |
| R40 | [in app update remote has the same canonical fallback as admin](../tests/unit/test_server_update.py) | failed | **confirmed** — The in-app updater and admin updater resolve absent remote metadata differently. Use the same canonical fallback while preserving explicit persisted/source overrides. |
| R41 | [one leader gets one consolidated tick for multiple teams](../tests/unit/test_team_leader.py) | failed | **confirmed** — One precomputed tick per team can preempt the same leader repeatedly with identical team-wide prompts. Consolidate by leader identity before dispatch, preserving all team context. |
| R42 | [failed leader tick is not reported as delivered](../tests/unit/test_team_leader.py) | failed | **deferred** — run_once returns eligible count after failed sends. Logging already records the failure and the loop ignores this count; correct metrics if consuming the return value, but do not treat it as a delivery outage. |
| R43 | [repeated active install requests share one monitor thread](../tests/unit/test_transcription_models.py) | failed | **confirmed** — Each repeated install request starts another monitor thread for the same running job. Test actual thread creation and generation ownership; mocking _start_monitor would reject a valid deduplication inside that helper. |
| R44 | [worker can be started again after it was stopped](../tests/unit/test_tts_worker.py) | failed | **deferred** — The same stopped TTSWorker instance cannot start again, but production builds a fresh instance for each server. No current same-instance restart caller was found. Do not expand lifecycle semantics solely to satisfy this unit test. |
| R45 | [clean completion closes the durable turn row](../tests/unit/test_turn_dispatch.py) | failed | **confirmed** — A successful result never closes the corresponding turn row. Fix by trace/turn identity while retaining queued audio policy and fencing old callbacks. |
| R46 | [idempotent retry respawns when first attempt never started](../tests/unit/test_turn_dispatch.py) | failed | **confirmed** — A user row persists before synchronous spawn failure; retry of that client_msg_id deduplicates as success without dispatch. User-message existence cannot be the durable admission receipt. |
| R47 | [plain tts strips ssml outside clarps internal markup vocabulary](../tests/unit/test_voice_markup.py) | failed | **confirmed** — The plain-provider boundary promises to strip SSML but leaves standard prosody/emphasis/say-as tags. Strip the supported SSML vocabulary without deleting literal comparison/code text. |
| R48 | [keeps counting error responses as contact after an earlier success](../tests/state/client-health.test.js) | failed | **confirmed** — Continuous HTTP errors are still server contact. Health currently ages the last success and falsely reports network silence. Preserve authentication/readiness distinctions. |
| R49 | [a second tap cancels a start that is still awaiting mic permission](../tests/state/mic.test.js) | failed | **confirmed** — Two taps before permission resolves create two getUserMedia requests and can start capture after cancel intent. Coalesce acquisition and fence late grants; release stale tracks. |
| R50 | [recovers and records diagnostics when MediaRecorder errors mid-capture](../tests/state/mic.test.js) | failed | **needs-contract** — The test requires onerror and invokes it alone, while onstop already clears capturing. It does not prove a permanent stuck recorder. Model error/data/stop ordering and establish whether partial audio should be uploaded; diagnostic absence is separate. |
| R51 | [records a diagnostic when transcription returns an HTTP error](../tests/state/mic.test.js) | failed | **deferred** — HTTP transcription failures already flash a user-visible error. Missing durable diagnostics is useful observability work, not proof of lost recorder state. Avoid logging unlimited provider text or transcript content. |
| R52 | [carries the last durable event id into a replacement EventSource](../tests/state/regression-sse-reconnect.test.js) | failed | **confirmed** — Replacing EventSource loses its native cursor because no lastEventId is carried. Explicit reconnects need a server-scoped cursor; a global cursor across different servers is unsafe. |
| R53 | [coalesces repeated reconnect requests into one replacement stream](../tests/state/regression-sse-reconnect.test.js) | failed | **confirmed** — Repeated signals schedule multiple EventSources and leak ownership. Coalesce timers, close superseded sources and ignore callbacks from stale connections. |
| R54 | [stops retrying while the server is known to reject authentication](../tests/state/regression-sse-reconnect.test.js) | failed | **needs-contract** — Known rejection should avoid retry noise, but permanently stopping reconnect may prevent recovery after cookie/token replacement. Add a credential-change recovery test before a circuit breaker. |
