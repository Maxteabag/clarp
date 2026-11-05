# S4 second combined method round

Lead 544 closes the five preconditions deferred in the first survey. This single source packet, main landing and generated round includes RecoveryMixin, ClaimMixin, HandoffMixin, LauncherMixin and AdmissionMixin. DrainMixin is already on integration; RetryMixin is approved and queued ahead of this round. No product changes are in the prep.

Pending main base: `4656d33862d90a37e8fc055fc645a85e39167a6f` (captain Retry/S9 stack). It must land first. Source turn_dispatch.py remains blob `19438ac1b34935f4efb0ae43fef49b140bf63d1d`, identical to measured e3b94c6e. The source importer and method patch inventory remains in s4-remaining-round.md; every proposed method remains AST-identical.

## Shared test prep

Existing files convert exactly twelve direct module patches to patch_shared: ten for Claim (_PROCESS_EXIT_POLL_S/_terminal_live), one for Handoff (_stop_holds) and one for Admission (resolve_send_target). New G0 files use patch_shared for module globals and ordinary patches for instance methods. No patch acknowledgement is added. The post-prep generator audit accepts every group.

The silent-except guard keeps its BASELINE and scanner unchanged. It first groups S9 definitions as before, then transfers counts only from exact recipe-listed methods in the recipe-listed output class to the named source class module (resolved by name through S9). Unlisted methods, other classes and module code stay physical. An extra swallow inside a declared method still grows source debt. Method sources that are S9 parts are covered too. No shared tests/patching.py API changes.

Guard RED reproduced the real Recovery physical debt and source shrink plus both method-family fixture failures (4 failed, 2 passed). Repaired generated Recovery guard: 6/6. Unsplit guard, three existing patched files and Claim G0: 235 passed, 2 skipped. Cancelled accidental pre-repair jobs are excluded from evidence (130/143); corrected runs are method-debt-green-r2.log and method-debt-unsplit-r2.log.

## G0 evidence

The retained full-suite JSON is coverage-f604e5d2.json. Helper rf-s4g0-7976 adds only three new test files (7c91b2a8, c7c8d539); focused buildbox coverage passed 18/18. Counts union executed lines against the identical source blob, within each moved method span including decorators. Denominators are unchanged; the parent independently recomputed all three unions. Raw focused JSON SHA256: `0c61dc38e665324f85bcaf9356cc306c933e45a1c7f7acb352dd460b01266935`.

## RecoveryMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_recovery.py::RecoveryMixin`.

Methods: `recover_queued`, `_rehydrate_once`, `_recover_queued_locked`, `_recovery_failed`, `clear_recovery_backoff`, `_retire_goal_queue`, `_release_superseded`, `release_leaked_slots`, `_start_ownerless_queues`, `_orphaned_spawn`, `repair_slot`, `_superseded`, `_preempt`.

G0: 194/226 = 85.8%.

The method-debt guard adaptation closes the existing _rehydrate_once.live count relocation. The silent exception body itself is unchanged.

## ClaimMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_claim.py::ClaimMixin`.

Methods: `dispatch_queued`, `_enqueue_if_busy`, `_hold_for_drain`, `_running_processes`, `_wait_for_process_exit`, `_preempt_for`, `_call_opening_claimed`, `_has_live_turn`, `_agent_has_live_turn`.

G0: 120/133 = 90.2%.

The new tests characterize durable queue-claim release after submission failure, transient goal refusal and runtime errors. Ten shared patch conversions close the generator audit.

## HandoffMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_handoff.py::HandoffMixin`.

Methods: `_discard_fenced_janitor`, `_finish_turn`, `_finish_slot`, `_spawn_next`, `_resume_and_spawn`, `_requeue_failed_handoff`, `_retry_handoff`, `_hold_stopped_queue`, `_resume_and_spawn_claimed`, `_mark_spawned`, `_broadcast_queue_state`.

G0: 194/224 = 86.6%.

The new tests characterize post-launch bookkeeping, ownership races, retryable handoff failure and transient goal refusal. One shared patch conversion closes the audit. _mark_spawned contains the suspected pre-spawn-check race seam identified by rf-harness; its move is byte-identical, and the harness owner was notified before this recipe. No race fix or normalization is included.

## LauncherMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_launcher.py::LauncherMixin`.

Methods: `_turn_in_flight`, `_bind_new_session`, `_spawn_attempt`, `_spawn_attempt_owned`, `_still_owns`, `_interrupt_unowned`, `_resume_after_account_switch`, `_admit_opening_launch`, `_pause_for_account`, `_sticky_session`, `_attempt_callbacks`.

G0: 157/182 = 86.3%.

The new tests characterize busy-state uncertainty, backend-session binding failure, superseded attempts, handle termination failure and init storage failure.

## AdmissionMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_admission.py::AdmissionMixin`.

Methods: `dispatch`, `submit`, `_dispatch_owned_marked`, `_dispatch_owned`, `_end_launched_opening`, `_release_unsent_opening`, `_abandon_unlaunched`, `_retry_unlaunched_marker`, `_held_in_memory`, `_admitted_but_never_launched`, `_record_user_message`, `_notify_herald`.

G0: 288/324 = 88.9%.

One shared resolve_send_target patch conversion closes the audit; existing characterization already exceeds 85%.

## Gates and remaining work

This is one combined five-group recipe update. Each stage-E group gets its own method AST identity check; the final generated tree gets one G1/G2 and exact CI plus A3 review. Source prep evidence is not generated approval. New real findings defer their affected group, without holding the others. The three long-function extractions follow the moves as integration replay patches, with their separate characterization/forward-port requirements. Queue lifecycle and TP-02 behavior remain untouched. Stop at 17:30 Europe/Oslo.

Proof receipts are retained under /var/tmp/rf-s4/ and /var/tmp/rf-s4g0/. Raw source audit: five-preconditions-audit.json/.log. Helper extra full-suite attempt was not requested by the parent; it was told to cancel while still at 3%, and its result is excluded from this packet. No successful full suite was repeated by S4.
