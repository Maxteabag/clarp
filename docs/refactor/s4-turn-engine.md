# S4 turn engine restructure: design note (rf-s4c)

Base: main `e3b94c6e`. Target: `server/lib/turn_dispatch.py` (4,062 lines;
`TurnDispatchService` is 2,817 of them). Integration only; moves only; every
moved definition ast-identical; G2 0/0/0.

## 1. Scope

| file | lines | longest function | S4 action |
|---|---|---|---|
| `lib/turn_dispatch.py` | 4,062 | `_dispatch_owned` 401 | split (this note) |
| `lib/turn_queue.py` | 868 | `terminalize_idle_agent_receipts` 65 | **none** (lead, 5 Nov 07:22): under every target; rf-contract converts it under its Phase 3 claim |
| `lib/turn_lifecycle.py` | 994 | under 150 | none (TP-10/TP-11 are fix-lane behaviour work) |
| `lib/turn_slots.py` | 580 | under 150 | none |

## 1a. Boundary with rf-contract

rf-contract (5 Nov 07:23): its queue conversion edits only `turn_queue.py` (and
the kit on main) and keeps every public turn_queue name, signature and return
value (pinned by `tests/unit/test_queue_receipt_matrix.py`). The turn_dispatch
callers (`claim`, `release_claim`, `cancel`, `mark_started`,
`reset_stale_claims`, `settle_receipt` and the others) need no change and move
freely. The turns conversion (lifecycle-conversion-plan section 4) is designed
with rf-contract on the seams below once the layout settles; any queue slice
that needs a turn_dispatch change asks S4 first.

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
   so S3's recipes and outputs are byte-identical. Lead decision, 5 Nov:
   S4 owns this tools-only change; reviewer B reviews it. Recipe-level
   `source_module` / `source_class` select the source, defaulting to
   `server.py` / `Handler`; existing group `module` / `class` still name the
   output. S6 agreed this interface (5 Nov), for
   `lib/codex_app_client.py::_Client` after its module split. Apply method
   moves after split_apply's module parts exist. Verify S3 recipes 1-10 give
   byte-identical results before and after the tool change.
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
`_spawn_attempt_claimed`. Expected residual class: about 391 lines;
expected facade file: about 1,000 lines.
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
- TP-N3 (`f1-tpn3`) also changes `_dispatch_owned`, `clear_for_agent`,
  `restore_stop_state`, adds `_start_pending`, and adds
  `turn_queue.keep_start_parked`/`repark` (turn_queue is not split, so those
  need nothing from S4).
- AUT-20b has no branch yet (registered 4 Nov 05:13, rf-fix after stage 2).

Homes in the move map: changed definitions are already listed and travel with
regeneration (`_park`, `_park_at_head`, `begin_stop`, `restore_stop_state` ->
`turn_command`/`turn_stop`; `_enqueue_if_busy`, `_running_processes`,
`_preempt_for` -> ClaimMixin; `_spawn_attempt_owned`, `_still_owns`,
`_interrupt_unowned` -> LauncherMixin; `_handle_failure`, `_mark_interrupted`
-> RetryMixin; `release_leaked_slots` -> RecoveryMixin; `_dispatch_owned` ->
AdmissionMixin; `_claim_or_queue`, `_spawn_attempt_claimed`, `clear_for_agent`
stay in the facade). New names: `_alive` (module level) and the methods
`_session_holds`, `_session_held`, `_start_pending` stay where main puts them
until the stage lands on main; then the recipe of the next cut lists them
beside their callers (`_alive`, `_session_hold*` -> ClaimMixin with
`_running_processes`; `_start_pending` -> AdmissionMixin). AUT-20b's symbols
get the same treatment when its branch exists. If one of them reads a
facade-only name it stays in the facade; split_apply decides, not judgement.

Changed bodies move with the next regeneration. New names stay in the facade
until a recipe update lists them (a reviewed recipe change). A failure mode the
generator catches: a moved method starts reading a name main added to the
facade (a cycle); the merge stops and the recipe is updated. Cut order puts the
clusters these fixes touch last (claim, launcher, retry, admission) so they
move only after stage 2 and TP-N3 are on integration.

## 6. Functions over 150 lines: a separate extraction step

Moves cannot shorten `_dispatch_owned` (401), `_spawn_attempt_claimed` (167) or
`transfer_recovery_parks` (161). Proposal (b):

- A separate step, never mixed into a move commit, after the last S4 move and
  after stage 2, TP-N3 and AUT-20b are on integration (all three touch the first
  two functions).
- One commit per function. Each extracts contiguous blocks into private helpers
  of the same module (the function keeps its name, signature and call order;
  locals the blocks read become parameters, locals they write become return
  values; no reordering, no new branches, no exception-handling change).
- Its own proof: a G0 table test per function written first against the
  unextracted code (each branch the coverage run reports, the three are at
  90.6%, 83.8% and 91.3% now, so raised to 100% branch coverage of the function
  first), G1, G2 0/0/0, and A3's review of the diff as behaviour-preserving
  (not ast-identical, so ast_identity does not apply).
- Not regenerated: these are hand edits on integration, so each goes on
  rf-merge's forward-port watch (logs/fwdport-watch.txt) like S7 step 6, and
  any later main fix to these functions stops the merge until forward-ported.
  Lead approved the integration extraction path on 5 Nov: each function enters
  rf-merge's forward-port watch, and restructuring ships with the big release.
  Extraction does not land on main or ship in a live window.

## 7. Test prep on main (tests-only)

Module-level names tests patch through `lib.turn_dispatch` that moved code
reads switch to `tests/patching.py:patch_shared` before the cut that moves the
reader: `log`, `_TURN_LOCK`, `_park`, `_stop_holds`, `_terminal_live`,
`_validate_janitor_target`, `_default_retry_scheduler` (also assigned in
`tests/conftest.py`), `_PROCESS_EXIT_POLL_S`, `_REQUEST_LOCKS`,
`provider_turns`, `runtime_status`, `resolve_send_target`. Patches of
`_RUNTIME_CLIENT` (15 files), `_DELIVERY_STREAM`, `_announce_delivery`,
`_FENCE_RETRY_DELAYS`, `begin_stop`, `complete_stop`, `drain_after_terminal`,
`snapshot_stop_state` (`tests/replay/host.py`) need no change for cut 1.
For `snapshot_stop_state`, the only production reader at this base is
`server/server.py:5241`, through the facade; neither `begin_stop` nor any
moved helper reads it. The replay assignment therefore still intercepts
that caller even though the definition moves to `turn_stop`. Harness owner independently confirmed this on main and trial
`c831cad3`. It owns a future-proof family wrapper on
`refactor/harness-stop-family` at `b566c741` (one observation even through a
delegating facade). Harness owner clarified that this is optional hardening,
not a cut-1 prerequisite: the current facade assignment already reaches the
only reader, and split_apply correctly permits it. Cut 1 does not wait for
`b566c741`. S4 does not edit `tests/replay/`. For the other names, readers stay in the facade. split_apply refuses any patch it cannot see reaching the
moved code, so the list is checked mechanically per cut.

## 8. G0

One full-suite run with pytest-cov on heavy-remote at f604e5d2 (= main
e3b94c6e plus this note; log /var/tmp/rf-s4/g0cov-full.log, exit 0, json
/var/tmp/rf-s4/coverage-f604e5d2.json). turn_dispatch.py 87% of lines.

| what moves | coverage |
|---|---|
| module level (cut 1: state, command, stop) | 91.9% (476/518) |
| DrainMixin | 92.8% (142/153) |
| RecoveryMixin | 85.8% (194/226) |
| AdmissionMixin | 88.9% (288/324) |
| RetryMixin | 88.5% (115/130) |
| ClaimMixin | 82.0% (gap: dispatch_queued error paths, _preempt_for) |
| HandoffMixin | 79.5% (gap: _resume_and_spawn_claimed, _hold_stopped_queue) |
| LauncherMixin | 78.0% (gap: _attempt_callbacks superseded paths, account switch) |
| stays in facade | 89.3% |

Recomputed by rf-s4c from the retained JSON on 5 Nov: numerator = executed
lines, denominator = executed plus missing lines within each moved definition's
span, including decorators. This conservative accounting slightly changes the
predecessor's cut-1/drain/launcher percentages; no cut crosses the 85% threshold.
G0 log read back: `exit=0`, no `FAILED` lines; whole file 1,973/2,255 (87.49%).

Cuts 1-4 and admission/retry pass G0 now. Claim, handoff and launcher get
characterization tests before their cuts, written after stage 1 lands on main
(stage 1 changes exactly those methods; tests written now would churn).
Missed lines per method: /var/tmp/rf-s4/g0-gaps.txt.

## 9. Cut order

0. Tools: split_apply's comprehension false refusal fixed (rf-fix3 asked);
   s3 group mover gets a target module/class (S4, main, tools-only; reviewer B).
1. Cut 1, one split_apply recipe (`tools/refactor/splits/s4-turn-dispatch.toml`,
   refactor/s4 c9484372): `turn_dispatch_state` (273 lines), `turn_command`
   (596), `turn_stop` (162); facade 3,112. Trial on e3b94c6e: applies with the
   tool fix, ast_identity (3.12) 0 problems, 1 move, 100 changed imports. Test
   prep beef5ad1 (9 patch sites in 6 files, one computed-name loop).
3. DrainMixin, 4. RecoveryMixin, 5. HandoffMixin,
6. ClaimMixin, 7. LauncherMixin + RetryMixin, 8. AdmissionMixin
   (6-8 after stage 2 and TP-N3 reach integration, or regenerated over them).
Each cut: claim from the lead, recipe reviewed by A3, s3/split merge on
integration, ast_identity, G1, G2.
