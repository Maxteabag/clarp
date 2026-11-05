# S4 remaining method round: combined survey

Pinned main: `74114894cf35458137937da1cf4f184ad0c8c60b`; integration: `9a51bdab3b158a7a73d9741723ac41465428229b`.

Lead 538 replaces six separate rounds with one combined eligible round. A refusal or finding defers only its mixin. The four facade-dependent methods listed in the approved design stay in TurnDispatchService; the remaining move map below is unchanged. No production files are edited by this prep.

All proposed method ASTs match e3b94c6e, the retained characterization coverage source. Existing coverage is reused only for unchanged methods. Every source class importer below keeps importing the facade; moved methods remain attributes of that class through its MRO.

## Source class importers

- `server/lib/agent_fork.py:289`
- `server/lib/agent_rings.py:1096`
- `server/lib/oracle_delegations.py:109`
- `server/runtime.py:26`
- `server/server.py:91`
- `server/server.py:5165`
- `tests/contract/test_core_shapes.py:389`
- `tests/contract/test_reference_client.py:81`
- `tests/integration/test_server_di.py:183`
- `tests/integration/test_server_di.py:345`
- `tests/integration/test_voice_events_endpoint.py:191`
- `tests/unit/test_background_wake_recovery.py:14`
- `tests/unit/test_callopen_lifecycle.py:21`
- `tests/unit/test_codex_connection_recovery.py:10`
- `tests/unit/test_delivery_enforcement.py:21`
- `tests/unit/test_delivery_fixup.py:15`
- `tests/unit/test_delivery_state.py:16`
- `tests/unit/test_drain_pause_parity.py:20`
- `tests/unit/test_graceful_release_drain.py:19`
- `tests/unit/test_janitor_autonomy.py:142`
- `tests/unit/test_queue_lane_recovery.py:21`
- `tests/unit/test_queued_recovery_backoff.py:16`
- `tests/unit/test_queued_recovery_invalid_thread.py:20`
- `tests/unit/test_runtime_bridge.py:23`
- `tests/unit/test_state_after_terminal.py:154`
- `tests/unit/test_tts_lanes.py:209`
- `tests/unit/test_turn_dispatch.py:10`

## Method patch sites

- `tests/integration/test_calls_oracle_g0_http_characterization.py:112`: `dispatch` (AdmissionMixin)
- `tests/integration/test_calls_oracle_g0_http_characterization.py:121`: `dispatch` (AdmissionMixin)
- `tests/integration/test_server_di.py:188`: `submit` (AdmissionMixin)
- `tests/integration/test_server_di.py:2137`: `dispatch` (AdmissionMixin)
- `tests/integration/test_server_di.py:3594`: `dispatch_queued` (ClaimMixin)
- `tests/unit/test_callopen_lifecycle.py:457`: `_record_failure_row` (RetryMixin)
- `tests/unit/test_callopen_lifecycle.py:532`: `_handle_failure` (RetryMixin)
- `tests/unit/test_callopen_lifecycle.py:728`: `_resume_and_spawn_claimed` (HandoffMixin)
- `tests/unit/test_callopen_lifecycle.py:1076`: `_broadcast_queue_state` (HandoffMixin)
- `tests/unit/test_callopen_lifecycle.py:1339`: `_resume_and_spawn_claimed` (HandoffMixin)
- `tests/unit/test_callopen_lifecycle.py:1354`: `_end_launched_opening` (AdmissionMixin)
- `tests/unit/test_drain_recovery_transfer.py:332`: `release_leaked_slots` (RecoveryMixin)
- `tests/unit/test_janitor_dispatch.py:96`: `_notify_herald` (AdmissionMixin)
- `tests/unit/test_janitor_dispatch.py:125`: `_notify_herald` (AdmissionMixin)
- `tests/unit/test_janitor_dispatch.py:316`: `_spawn_attempt` (LauncherMixin)
- `tests/unit/test_oracle_call_contract.py:37`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_call_contract.py:54`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_calls_stable.py:219`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_calls_stable.py:587`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_direct_contact.py:19`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_direct_contact.py:51`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_direct_session.py:159`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_gemini_live.py:73`: `dispatch` (AdmissionMixin)
- `tests/unit/test_oracle_stable_memory.py:18`: `dispatch` (AdmissionMixin)
- `tests/unit/test_turn_dispatch.py:2063`: `_enqueue_if_busy` (ClaimMixin)
- `tests/unit/test_turn_dispatch.py:3181`: `_enqueue_if_busy` (ClaimMixin)
- `tests/unit/test_turn_dispatch.py:3228`: `_mark_spawned` (HandoffMixin)
- `tests/unit/test_turn_dispatch.py:2465`: `_dispatch_owned` (AdmissionMixin)

Instance and class method patches retain their targets through inheritance. Module-global patching is audited by the generator separately; every refusal is recorded under its mixin. A prototype applies the already-approved DrainMixin first, then audits each proposed group against the complete tracked scan tree. This is source audit evidence, not generated gate evidence.

## RecoveryMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_recovery.py::RecoveryMixin`.

Methods: `recover_queued`, `_rehydrate_once`, `_recover_queued_locked`, `_recovery_failed`, `clear_recovery_backoff`, `_retire_goal_queue`, `_release_superseded`, `release_leaked_slots`, `_start_ownerless_queues`, `_orphaned_spawn`, `repair_slot`, `_superseded`, `_preempt`.

G0: 194/226 = 85.8%; all methods AST-identical to the measured source.

**Included in this combined round.** Generator audit accepts the group.

## ClaimMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_claim.py::ClaimMixin`.

Methods: `dispatch_queued`, `_enqueue_if_busy`, `_hold_for_drain`, `_running_processes`, `_wait_for_process_exit`, `_preempt_for`, `_call_opening_claimed`, `_has_live_turn`, `_agent_has_live_turn`.

G0: 109/133 = 82.0%; all methods AST-identical to the measured source.

**Deferred to a follow-up round.** G0 is below the required 85%. Generator patch audit refuses the group:

```
group turn_claim: imported names its members read are patched through the server module; after the move the patch would not reach them:
  tests/unit/test_turn_dispatch.py:576: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:605: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:640: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:668: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:695: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:718: monkeypatch.setattr(_td, '_PROCESS_EXIT_POLL_S')
  tests/unit/test_turn_dispatch.py:895: monkeypatch.setattr(module, '_terminal_live')
  tests/unit/test_turn_dispatch.py:2381: monkeypatch.setattr(_td, '_terminal_live')
  tests/unit/test_turn_dispatch.py:2384: monkeypatch.setattr(_td, '_terminal_live')
  tests/unit/test_turn_dispatch.py:3326: monkeypatch.setattr(_td, '_terminal_live')
```

## HandoffMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_handoff.py::HandoffMixin`.

Methods: `_discard_fenced_janitor`, `_finish_turn`, `_finish_slot`, `_spawn_next`, `_resume_and_spawn`, `_requeue_failed_handoff`, `_retry_handoff`, `_hold_stopped_queue`, `_resume_and_spawn_claimed`, `_mark_spawned`, `_broadcast_queue_state`.

G0: 178/224 = 79.5%; all methods AST-identical to the measured source.

**Deferred to a follow-up round.** G0 is below the required 85%. Generator patch audit refuses the group:

```
group turn_handoff: imported names its members read are patched through the server module; after the move the patch would not reach them:
  tests/unit/test_stop_holds_queue.py:241: monkeypatch.setattr(td, '_stop_holds')
```

## LauncherMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_launcher.py::LauncherMixin`.

Methods: `_turn_in_flight`, `_bind_new_session`, `_spawn_attempt`, `_spawn_attempt_owned`, `_still_owns`, `_interrupt_unowned`, `_resume_after_account_switch`, `_admit_opening_launch`, `_pause_for_account`, `_sticky_session`, `_attempt_callbacks`.

G0: 142/182 = 78.0%; all methods AST-identical to the measured source.

**Deferred to a follow-up round.** G0 is below the required 85%. The generator accepts the group, but G0 is insufficient.

## RetryMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_retry.py::RetryMixin`.

Methods: `_handle_failure`, `_schedule_retry`, `_mark_interrupted`, `_end_opening_state`, `_record_failure_row`.

G0: 115/130 = 88.5%; all methods AST-identical to the measured source.

**Included in this combined round.** Generator audit accepts the group.

## AdmissionMixin

`lib/turn_dispatch.py::TurnDispatchService` -> `lib/turn_admission.py::AdmissionMixin`.

Methods: `dispatch`, `submit`, `_dispatch_owned_marked`, `_dispatch_owned`, `_end_launched_opening`, `_release_unsent_opening`, `_abandon_unlaunched`, `_retry_unlaunched_marker`, `_held_in_memory`, `_admitted_but_never_launched`, `_record_user_message`, `_notify_herald`.

G0: 288/324 = 88.9%; all methods AST-identical to the measured source.

**Deferred to a follow-up round.** Generator patch audit refuses the group:

```
group turn_admission: imported names its members read are patched through the server module; after the move the patch would not reach them:
  tests/unit/test_janitor_dispatch.py:107: monkeypatch.setattr(td, 'resolve_send_target')
```

## Combined candidate and boundaries

RecoveryMixin and RetryMixin advance together in one recipe update and one generated review/G1/G2 round after DrainMixin. Claim, handoff, launcher and admission remain in the source class pending their recorded follow-ups. No silent acknowledgements are added and no exception is bypassed. New G0 characterization for the three coverage gaps belongs in tests-only main commits before those follow-up moves.

The runtime-wide RPC lock remains in runtime_bridge.py (TP-02); this round does not change it. Queue lifecycle ownership stays with rf-contractc. Stage 1/2, TP-N3 and AUT-20b source changes continue to flow through the main-text generator; a changed measured method requires its G0 evidence refreshed. The three long-function extractions follow the moves as separately reviewed integration replay patches.

Raw audit and counts: `/var/tmp/rf-s4/remaining-six-survey.json` and `.log`. No G1/G2 or generated approval is claimed by this survey. Quiet-window cutoff: 17:30 Europe/Oslo.
