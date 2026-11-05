# S4 turn engine restructure: design note (rf-s4, draft)

Base: main `e3b94c6e`. Target: `server/lib/turn_dispatch.py` (4,062 lines;
`TurnDispatchService` is 2,817 of them). Integration only; moves only; every
moved definition ast-identical; G2 0/0/0.

## 1. Scope

| file | lines | longest function | S4 action |
|---|---|---|---|
| `lib/turn_dispatch.py` | 4,062 | `_dispatch_owned` 401 | split (this note) |
| `lib/turn_queue.py` | 868 | `terminalize_idle_agent_receipts` 65 | **none**: under every target; rf-contract's Phase 3 queue conversion owns it |
| `lib/turn_lifecycle.py` | 994 | under 150 | none (TP-10/TP-11 are fix-lane behaviour work) |
| `lib/turn_slots.py` | 580 | under 150 | none |

TP-02 (runtime-wide lock) lives in `lib/runtime_bridge.py` (`_admission_lock`),
not in turn_dispatch. S4 does not touch runtime_bridge. When TP-02 is worked it
stays there; the only S4 contact is that the backend IO it wants outside the lock
(interrupt, goal, spawn) is reached through `lib/turn_launcher.py` after the cut.

## 2. Mechanism (no new one)

Two existing tools, both regenerated from main's text at every main->integration
merge, so stage 1, stage 2, TP-N3 and AUT-20b on main flow through unedited:

1. **Module-level definitions**: `tools/refactor/split_apply.py` (S9), recipe
   `tools/refactor/splits/s4-turn-dispatch.toml`, facade = `turn_dispatch.py`,
   verified by `scripts/refactor/ast_identity.py BASE HEAD turn_dispatch.py=...`.
2. **`TurnDispatchService` methods**: S3's mixin group mover (`s3_apply.py`:
   cut members byte for byte into `class XMixin:` in a new module with the old
   module's narrowed imports; the old class gets the mixins as first bases),
   verified by ast_identity's existing method mode (`path.py::Class`). Today
   s3_apply hard-codes `server/server.py::Handler`. Tools-only change on main:
   let a group name its target (`module`, `class`), default server.py/Handler,
   so S3's recipes and outputs are byte-identical. Owner choice for the lead:
   (a) rf-routes makes the change in s3_apply, or (b) rf-s4 adds it, reviewer B.
   split_apply already imports s3_apply's machinery; this keeps one mechanism.

Mixins are behaviour-neutral here: no method of the class calls `super()`, the
class body has methods only (no class attributes), no two clusters share a
name, and `TurnDispatchService` keeps `__init__`. Instance and class patches
(`patch.object(service, ...)`, `monkeypatch.setattr(TurnDispatchService, ...)`)
keep working through the MRO.

## 3. Modules and move map

Module names follow the turn-pipeline report's seams (section 3), flat in
`server/lib/` because split parts must sit beside the old module.

Module-level (split_apply, cut 1):

| new module | content |
|---|---|
| `lib/turn_dispatch_state.py` | constants, `_TURN_LOCK`, `_SLOTS` and its views, `_FAILOVERS` and pool helpers, recovery backoff, request/janitor lock maps, held-order and hold-headline helpers, `live_work`, `provider_turns`, `runtime_status`, `reset_for_tests` (one home for TP-26's globals) |
| `lib/turn_command.py` | `DispatchResult`, `DispatchError` family, `_TurnSpec`, `DispatchCommand`, `_with_*` context helpers, `_park*`, `_stop_holds`, admission helpers |
| `lib/turn_stop.py` | result helpers and stop/cancel functions that read no facade-only name |

TurnDispatchService mixins (s3 group mover, cuts 2-8; line counts at e3b94c6e):

| new module / class | methods | lines |
|---|---|---|
| `lib/turn_drain.py` DrainMixin | admission fence, held work, `persist_held`, `transfer_recovery_parks` | 241 |
| `lib/turn_recovery.py` RecoveryMixin | `recover_queued`, rehydrate, recovery failure/backoff, leaked/orphaned slot repair, `_superseded`, `_preempt` | 371 |
| `lib/turn_claim.py` ClaimMixin | `dispatch_queued`, `_enqueue_if_busy`, drain hold, process wait, `_preempt_for`, live-turn checks | 210 |
| `lib/turn_handoff.py` HandoffMixin | finish slot, spawn next, resume-and-spawn, failed handoff, stopped queue, `_mark_spawned`, queue broadcast | 345 |
| `lib/turn_launcher.py` LauncherMixin | spawn attempt (owned), session bind, account switch, opening launch, `_attempt_callbacks` (TP-12's future home) | 284 |
| `lib/turn_retry.py` RetryMixin | `_handle_failure`, `_schedule_retry`, `_mark_interrupted`, opening end, failure row | 275 |
| `lib/turn_admission.py` AdmissionMixin | `dispatch`, `submit`, `_dispatch_owned*`, unlaunched/opening abandonment, user-message record, herald | 616 |

## 4. What stays in `turn_dispatch.py` (facade)

Two globals are rebound with `global` (`_RUNTIME_CLIENT` in
`configure_runtime_client`, `_DELIVERY_STREAM` in `TurnDispatchService.__init__`).
Moving them would leave the facade's binding stale, so they, their readers and
everything that reads those readers stay: `configure_runtime_client`,
`_slot_is_spawning`, `_send_admitting`, `free_stale_slot`, `owns_inflight_trace`,
`_announce_delivery(_locked)`, `_fence_launch_receipts`, `fence_user_stop`,
`clear_for_agent`, `cancel_janitor_run`, `_cancel_fenced_janitor_run`,
`complete_stop`, `finish_stop`, `drain_after_terminal`, and in the class
`__init__`, `_slot_snapshot`, `_claim_or_queue`, `_steer_if_supported`,
`_spawn_attempt_claimed` (391 lines). Expected facade: about 1,000 lines.
split_apply refuses any other facade read (a cycle), so the generator, not
judgement, enforces this list at every merge.

## 5. Fixes on main get a home without forward-ports

The recipe lists names; whatever main adds and the recipe does not list stays in
the facade (module level) or the core class (methods), so a new name never
breaks a merge. Measured on the current fix branches against main:

- stage 1 (`f1-backends06-s1-r12`) and stage 2 (`f1-backends06b`) change
  `_park`, `_enqueue_if_busy`, `_claim_or_queue`, `_running_processes`,
  `_preempt_for`, `_spawn_attempt_owned`, `_spawn_attempt_claimed`,
  `_handle_failure`, `_mark_interrupted`, `release_leaked_slots`, `begin_stop`,
  and add `_alive`, `_session_holds`, `_session_held`.
- TP-N3 adds `_dispatch_owned`, `clear_for_agent`, `restore_stop_state`,
  `_start_pending`, and `turn_queue.keep_start_parked`/`repark`.

Changed bodies move with the next regeneration. New names stay in the facade
until a recipe update lists them (a reviewed recipe change). A failure mode the
generator catches: a moved method starts reading a name main added to the
facade (a cycle); the merge stops and the recipe is updated. Cut order puts the
clusters these fixes touch last (claim, launcher, retry, admission) so they
move only after stage 2 and TP-N3 are on integration.

## 6. Functions over 150 lines

Moves cannot shorten `_dispatch_owned` (401), `_spawn_attempt_claimed` (167) or
`transfer_recovery_parks` (161). Proposal: after the moves, and after stage 2,
TP-N3 and AUT-20b are on integration, extract helpers in a separate
behaviour-preserving step (not ast-identical, so outside the move commits),
each with a G0 table test and G2 0/0/0, reviewer A3, forward-ported by the
fix-lane watch (rf-merge's fwdport-check) rather than regenerated. Lead decides
whether that is S4's or later.

## 7. Test prep on main (tests-only)

Module-level names tests patch through `lib.turn_dispatch` that moved code
reads switch to `tests/patching.py:patch_shared` before the cut that moves the
reader: `log`, `_TURN_LOCK`, `_park`, `_stop_holds`, `_terminal_live`,
`_validate_janitor_target`, `_default_retry_scheduler` (also assigned in
`tests/conftest.py`), `_PROCESS_EXIT_POLL_S`, `_REQUEST_LOCKS`,
`provider_turns`, `runtime_status`, `resolve_send_target`. Patches of
`_RUNTIME_CLIENT` (15 files), `_DELIVERY_STREAM`, `_announce_delivery`,
`_FENCE_RETRY_DELAYS`, `begin_stop`, `complete_stop`, `drain_after_terminal`,
`snapshot_stop_state` (`tests/replay/host.py`) need no change: their readers
stay in the facade. split_apply refuses any patch it cannot see reaching the
moved code, so the list is checked mechanically per cut.

## 8. G0

Coverage of the code each cut moves, from one full-suite run with pytest-cov
on heavy-remote: (pending, section filled from coverage.json).

## 9. Cut order

0. Tools: s3 group mover gets a target class (main, tools-only).
   Tests: G0 characterization for gaps, patch_shared prep (main, tests-only).
1. `turn_dispatch_state`, `turn_command` (split_apply).
2. `turn_stop` remainder (split_apply).
3. DrainMixin, 4. RecoveryMixin, 5. HandoffMixin,
6. ClaimMixin, 7. LauncherMixin + RetryMixin, 8. AdmissionMixin
   (6-8 after stage 2 and TP-N3 reach integration, or regenerated over them).
Each cut: claim from the lead, recipe reviewed by A3, s3/split merge on
integration, ast_identity, G1, G2.
