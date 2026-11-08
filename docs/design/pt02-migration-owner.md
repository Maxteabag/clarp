# PT-02: one migration owner and resumable upgrades

Status: DESIGN PROPOSAL ONLY; lead 937. No corrective implementation or runtime proof.
Author: rf-contractc-be17. Reviewer: A2 (rf-review-core2c-d509).
Source base: f666fb0db240513ae6dae1eda8227046b4001088, schema 121,
upgrade floor 61. HOST_CONTRACT and MIN_IOS_CONTRACT are outside this change.

## Problem and chosen owner

`server/lib/db.py:551-585` opens a connection and calls `_migrate` once per
PROCESS, not once per installation. Its Python lock cannot exclude another
interpreter. `db_migrations.py:21-201` puts the entire historical upgrade chain
and data work inside one BEGIN IMMEDIATE/COMMIT. The opening PRAGMAs can write
before the migration decision. `_migrate` also accepts ANY newer user_version
without validating whether this binary understands its schema. The auxiliary
stamp connection at db.py:276 can create a missing file independently.

Recommendation: ONE dedicated, short-lived release/bootstrap migrator process,
launched from the exact candidate code BEFORE activation, roster/identity seeding,
service workers, runtime recovery, or listening. Host, runtime, hooks and normal
CLIs are check-only database users. Neither module import nor `db.conn()` grants
migration authority. A private explicit migration entrypoint opens its own raw
connection, receives a validated target path and plan, acquires ownership, and
calls migration code. It does not call ordinary conn() to bootstrap itself.
Tests/rehearsals must select an explicit disposable owner instead of accidentally
relying on conn(). This is an ownership boundary, not a security sandbox against
arbitrary same-user Python/SQL.

Why release tooling instead of Host boot: native `systemd/clarp.service` WANTS
and starts AFTER clarp-runtime.service; runtime constructs production context and
recovers before serving its socket. Docker entrypoint opens conn(), establishes
identity and pairing before launching either process. Native setup's seed_fresh_roster
also uses a separate Python process. Merely moving _migrate to server.py main
would leave these earlier readers/writers ungated or create a startup dependency
cycle. Release tooling centralizes fresh install, upgrade and restored-database
initialization. Ordinary Host restart must NOT run migrations.

Proposed internal entrypoint: `python -m lib.persistence.migration_owner` from the
immutable release tree, not a new public CLI subcommand. Wire install.sh BEFORE
current-link activation and seeds; wire the Docker bootstrap AFTER any whole-root
restore completes and BEFORE line 92's schema/identity work. Rollback activation
checks compatibility but never calls the forward migrator. Developer launch of a
fresh data root needs an explicit bootstrap through the same owner, not an implicit
Host/CLI fallback. Release/tooling owners must agree these seams before edits.

## Cross-process authority and ordering

A process-wide lock or SQLite's per-transaction writer lock is insufficient when
an upgrade spans several committed batches. Proposal: a kernel exclusive flock
held by the sole migrator for the WHOLE run, acquired nonblocking before opening
SQLite. Canonical target database identity and its companion lock must agree;
symlink/hard-link aliases, nonregular/wrong-owner lock files and unverified storage
are refused. The lock FD is close-on-exec and is not inherited by providers.
A second owner refuses; ordinary clients never take this lock to become owners.
Owner death releases the OS lock; the next explicitly authorized owner resumes
from committed database progress. No time-expired lease steals a live owner.

Proposed companion: `<canonical database>.migration-owner.lock`. THIS IS A NEW
INTERNAL FILE PATH, requiring the lead's frozen-path ruling before implementation.
Do not silently reuse config/install.json.lock: it is keyed by config, not the
canonical database, and two configs can select one DB. Do not reuse or convert the
container restore users/owner locks: their existing semantics protect a whole-root
restore. Container order is existing root ownership/restore -> migration-owner lock
-> short SQLite transaction; release the migration lock before starting the stack.
No TURN_LOCK/slot lock/provider RPC is involved. Native release orchestration must
serialize release activation and restore; a live restore/replacement cannot race
migration. Never claim a file lock covers unsupported external clients/aliases.

Historical (<121) conversion is OFFLINE: all old Host/runtime/hook users must be
stopped/fenced by the release owner before conversion, because old binaries still
contain implicit migration code. New check-only clients refuse incomplete schema.
A lock newly added in this release cannot stop an OLD binary from migrating.
For later expand-only releases, the previous certified reader/writer may continue
only when every intermediate committed phase is compatible with it. Otherwise
plan a release-owner drain; do not quietly restart a live runtime.

## Readiness and mismatch rules

Separate three facts: physical shape installed, data backfill progress, and which
reader/writer releases are certified. user_version alone cannot encode all three.
Ordinary open first uses mode=rw on an EXISTING file (read-only inspectors use ro),
consumes the version/readiness checks and validates the exact supported contract
BEFORE journal-mode changes, application writes, cache warming or recovery. Missing
file, version 0, unsupported older/newer version, partial required phase or corrupt
metadata raises a typed SchemaNotReady/SchemaUnsupported refusal. It never creates,
upgrades, deletes, resets, renumbers or repairs the file. Close a refused connection;
do not install it in thread-local or stamp caches. Revalidate each new connection;
long-lived connections must recheck before new units of work when schema generation
changes. Do not poll or invalidate inside a caller-owned transaction.

An exact release manifest defines the accepted version/shape window and mandatory
phase readiness, INCLUDING the previous release's writes and defaults. Never
replace `>= current` with another unbounded newer-version acceptance. Unknown future
schemas are refused. A previous release may accept its declared next expansion
only when its descriptor/shape is known and compatibility is proven; no inferred
compatibility from a high number. Upgrade floor 61 is an input-import promise,
NOT permission to run current code on an unmigrated v61 database or roll back to
an arbitrary v61 binary.

Runtime: check before context construction, recovery, markers consumed, socket bind,
release monitor or providers start. Proposed process supervisor retry with existing
restart policy rather than a new unbounded wait inside Python. Host: fail before
production context/seed/reconcile/listeners; no healthy-ready report on mismatch.
A compatible already-running old runtime is not restarted merely to migrate.
CLI: nonzero refusal and bounded content-free stderr for a DB-dependent command;
no success JSON on failure, no background migration/wait. Commands not using state
remain unchanged. Hook: refuse its Clarp DB operation promptly, report via existing
content-free hook error channel, preserve the exact existing provider hook response
and exit convention; never block the provider turn waiting for migration. Lost hook
observation is an explicit availability limit, not a hidden queued replay guarantee.
Any new mismatch output is an intended exceptional difference needing lead signature;
normal successful outputs/routes/hook responses stay byte-compatible.

## Exact-base process and connection inventory

This is a repository SOURCE inventory, not a live process census. "Uses conn"
means can invoke the per-process implicit migrator today. No tracked code can
inventory arbitrary external sqlite3/Python consumers; unsupported bypasses must
be named, not claimed fenced. Current default mismatch for conn users is: create
at 0; auto-upgrade 61..120; refuse 1..60; silently accept >=121, with later SQL errors
possible on incompatible shape. Exceptions can propagate or be caught by the
entrypoint; there is no common schema-readiness policy today.

| Process / entrypoint today | Current opening and mismatch behavior | Proposed behavior |
|---|---|---|
| Host `server/server.py`, request threads, boot/recovery, maintenance, watchers, transcript/TTS/HTTP workers | Shared process, thread-local db.conn; any first worker can migrate. BOOT is not the only caller. | Gate before context/workers, then check-only per connection; refuse startup or new DB unit on unsupported shape. |
| Runtime `server/runtime.py`, runtime RPC/recovery/background threads | Production context/dispatch and lib stores use conn; separate process and migration flag. | Check before runtime state changes/recovery/socket; fail to existing supervisor, never migrate. |
| `bin/clarp-admin.py` direct store commands | sessions directly calls conn (:1053); explanations, pairing/identity, prompts/requests/replies/caller identity, model, peers, janitors/controller actions, DB-backed TTS status and setup seeding reach lib stores. HTTP-only paths depend on Host instead. | Every actual DB use is check-only; keep HTTP-only/no-DB commands usable. Refuse DB-dependent operation, retain successful command formats. Audit each command, not just sessions. |
| Fresh setup `seed_fresh_roster` Python subprocess (:590), `lib.roster_seed` | Can create/migrate before services. `_fresh_roster_needed` uses separate read-only direct SQLite, returns False on SQLite error. | Bootstrap owner completes first; seeding checks schema. Freshness inspector remains ro; mismatch cannot silently authorize seeding. |
| Container `docker/entrypoint.sh` inline Python | db.conn at :93 before identity, skills and pairing. Holds existing whole-root ownership/restore locks. | Explicit sole-owner bootstrap after restore and before application initialization; no conn migration. |
| Native install/update/rollback `install.sh`, clarp-admin activate_release | Installer switches current before service start; rollback switches code/services, does not revert schema. Setup may spawn seeds. | Pre-activation candidate owner for forward install; rollback is check-only and retains SAME DB. Existing health/automatic code rollback remains required. |
| `scripts/agent_tasks.py` (clarp-goal/tasks/bookkeeping aliases) | task_plans/goal_ledger/caller identity use conn; separate Python process. | Check-only; fail command before ledger mutation on mismatch. |
| `scripts/agent_bg.py` (background-job aliases) | background_jobs/agents store uses conn. | Check-only; no job admission or bookkeeping mutation when unsupported. Existing subprocesses are not replayed by migration. |
| `scripts/leader_decision.py`, `portrait_generation_job.py`, `server_update_job.py`, `transcription_model_job.py`, `generate_orchestrator_phrases.py` | Imported leader_memory/background_jobs/agents/conn can open state. Update worker is not itself a migrator until it delegates explicit release bootstrap. | Check before owned store work; release helper alone may migrate; worker/provider side effects never repeat after uncertain commit. |
| `scripts/agent_artifacts.py` | Mostly HTTP; form connect_origins imports html_forms and can resolve DB identities. | HTTP unchanged; local DB branch check-only. |
| `scripts/viz_activate_view.py`, `viz_publish_flow.py` | viz_library can use conn for stored settings/view state. | Check-only local state access; no implicit bootstrap. |
| Hooks `notification_state.py`, `pre_compact.py`, `pwa_source_flag.py`, `stop_state.py`, `subagent_stop.py`, `tool_activity.py`, `tool_finished.py` | `_clarp_lib` resolves code; agents/turn_lifecycle and associated stores call conn in fresh interpreters. Most report/catch failures with their own existing fallback. | Prompt schema refusal via existing hook failure handling; preserve each original stdout/exit contract; no migration or wait. |
| Hook `agy_state.py` | Initial direct ro SELECT on runtimes (:24) treats SQLite error as "full check needed"; record_event then reaches ordinary stores. | Early ro mismatch cannot authorize state work; full store gate still refuses. Preserve `{}` and existing no-block behavior. |
| Hook helpers `_clarp_lib.py`, `_hook_report.py` | Path setup / eventlog diagnostic helper, not independent state migrators. | Must not initialize state while reporting a schema failure; telemetry/file fallback only. |
| `db._stamp_connection` inside Host/runtime/store consumers | Direct SQLite connect, mkdir/create possible, no migration; change_stamp used by caches. | Existing-file, check-only opening; no bypass through cache initialization. |
| `lib.janitor_context.build_context` (caller-supplied DB path) | Direct mode=ro (:117), then schema-dependent evidence queries, no migration. | Validate supported readable shape first, refuse mismatch; no bootstrap. |
| Backup process `lib.instance_backup.create/verify` via clarp-admin | Raw SQLite backup to private snapshot; verify extracted DB integrity/schema number. Neither calls _migrate. | Raw preservation/structural verification stays nonmigrating. Keep backup possible for unsupported versions; do not call conn or interpret settings. |
| Restore `instance_backup` / container entrypoint | Replaces whole data root/files under existing exclusive restore protocol, no per-key merge. Next conn migrates. | Restore remains raw replacement. Explicit bootstrap owner checks/restores compatibility before any new service user. No restoration into live migration. |
| Inspectors `scripts/live_console.py`, `debug_bundle.py`, `viz_author.py`, `viz_evolve.py`, `viz_preview.py` | Direct mode=ro; viz tools may replace db.conn with ro connection. No migration but missing columns can fail or be treated empty by individual helpers. | Shape gate before evidence-producing reads; no false empty/healthy success. Keep diagnostics able to report unsupported version structurally. |
| `scripts/find-session.sh`, `sqlite-query.sh` | Raw sqlite3 CLI opens supplied/default state; no shared db.py or explicit schema check; sqlite shell can create absent DB. | Supported read-only inspection needs explicit existing-file/version preflight; arbitrary SQL remains operator-controlled and is not a migration owner. Changing shell read/write semantics needs separate scope/signature. |
| `scripts/test_clarp_pipeline.py` | Explicit direct state SQLite connector, no common schema gate; legacy live integration probe. | No migration authority; future captain-controlled isolated probe must gate shape. No live run authorized here. |
| Rehearsal `scripts/migration_rehearsal.py`, skill `rehearse_state_upgrade.py`; synthetic hotpath/probe/scenario/test helpers; refactor `product_capture.py` | Direct SQLite or db.conn/reset; some explicitly db._migrate on disposable copies. Benchmark can ro-copy supplied DB. | Explicit DISPOSABLE owner only on private copy; no implicit prod migration through imports. Captain validates no original-source writes. |
| `bin/clarp-tui.py`, desktop/iOS clients, most skill wrappers | CLI/HTTP transport, no own Clarp state connector found; provider databases in codex/agy/opencode transcript modules and telemetry.sqlite are different stores. | Inherit server/CLI refusal; do not add state connections or apply this policy to provider DBs accidentally. |

`docs/design/pt02-openers.json` records the deterministic tracked-source scan
(calls/import anchors, not dynamic reachability proof). A2 must challenge missing
entrypoints and transitive initialization before accepting this census. Raw
external SQL is not made safe by a Python change; operator/release documentation
must require the owner route.

## Bounded backfills and crash recovery

Do not simply put a LIMIT around _migrate_to_v121 and stamp 121: that can permanently
skip the rest. Separate schema steps, mandatory data steps and optional online
backfills. Each durable job has a fixed identifier, plan hash, source/target schema,
status, typed keyset cursor, fixed cutoff/transform version, counts and last error
CODE. No message/prompt content or arbitrary SQL is persisted in the journal.

Proposal for review: one additive internal `schema_migration_jobs` relation, created
by the migrator only. Composite primary key (target_version, job_id); immutable plan
hash/source version/cutoff; status pending/running/done/failed; cursor INTEGER or
bounded TEXT with a declared per-job type; numeric changed/examined counts. No
settings namespace, no metadata file as a substitute for atomic database progress.
The exact DDL, version number and visibility rules are NOT approved by this note;
book the next free schema version only after A2/lead settlement. Do not bump 121
in a design commit. Journal must travel with raw backups and SAME-DB rollback.
Previous release ignores unused jobs but must understand readiness of the expansion
it is certified to use. Unknown job versions/plans fail closed, not skip-as-done.

Per job: acquire owner outside SQLite; select a bounded indexed keyset page with
at most 100 EXAMINED candidates; begin one short writer transaction; reread current
eligibility/identity, apply at most that page, update cursor/counts IN THE SAME
transaction, commit and release writer. Effects/RPC/file operations are forbidden
inside. Seek on stable keys, not OFFSET; concurrent older writers cannot be skipped
forever: per-job eligibility generation, residual sweep and convergence proof are
required before marking done. Each transformation must be idempotent under restart,
rollback and current-row mutation; no blind replay of uncertain COMMIT. Reopen/read
journal and exact rows to establish outcome before next batch. Cancellation stops
between batches; exceptions roll back only the admitted batch; already committed
progress stays resumable.

Initial proposed scheduling envelope: <=100 examined rows/batch, target <=50 ms
writer hold, <=200 ms cooperative pass, <=50 ms writer admission capped by remaining
pass budget; yield between passes. These are proposed PT-02 targets, NOT inherited
reaper proof or a claim of hard interruptibility. Include selection, retries,
triggers, revision updates and commit in measurements; one slow query/index/IO can
overrun. Review indexed plans and VM-step limits, measure worst observed hold and
whole pass with production-shaped SYNTHETIC data, and refuse promotion when budgets
fail. No multiplied busy retries or one transaction encompassing all batches.

Mandatory backfills complete before publishing their ready version. Compatible
optional backfills may continue under the SAME explicit migration owner after
activation only if previous/current readers and writers tolerate every partial
state. No maintenance worker, CLI or hook may quietly become a second owner.
Owner exit means an explicitly re-launched release helper resumes, not conn().

Historical data work audit anchors: v62 JSON/path/settings rewrites, v70 decision
answer copies, v71 team flags, v93 janitor roles, v96 status timestamps and v121
provider evidence expiry. v71/v93 column-absence preconditions must be recorded
BEFORE expansion, otherwise restart may skip the intended data transformation.
v96's timestamp and v121's age cutoff must be fixed once per plan, not advance on
each batch. v121 currently passes limit=None to expire_lost_evidence; its selection
and nested project_native_task_evidence_lost writes must become bounded together.
Do not change its terminal policy or infer provider success. All schema-string
inserts/triggers and migrations need a complete statement classification before code.

DDL has a separate limit: SQLite CREATE INDEX/rebuild is one statement and cannot
be made a series of committed row batches by adding LIMIT. Historical index builds
(v65/91/92/105/106/114/115/119, among others) need offline, timed rehearsal and release
budget; do not call them bounded backfills. If required DDL cannot fit the approved
maintenance/downtime budget, STOP for a separate migration strategy, not an invented
online-index guarantee. Legacy destructive v62/v101/v114 steps are historical code,
not authority to add new drops/rebuilds in PT-02. Preserve/import legacy shapes only
under a reviewed offline plan; do not claim all v61 history is expand-only.

## Release sequence and one-release-back compatibility

R is a bridge release at existing schema121: no new stored values/shape, conn is
check-only, explicit release bootstrap owns migrations, all entrypoints gated.
Publish no expansion until R is shipped and direct old migrators are inventoried;
rollback R -> actual previous release uses the unchanged121 DB. Historical old DB
bootstrap is separately qualified offline; ownership-only is a PARTIAL mitigation,
not full PT-02 closure while legacy unbounded data steps remain.

R+1 may add the approved journal/expansion, after R contains its exact known-next
compatibility/readiness descriptor. R and R+1 both read/write legacy fields and
recognize missing/default new fields. Backfill new derived values without deleting,
renaming, retyping or changing meaning of old data. Avoid new enum/status values
old code cannot interpret. Old R writes during a backfill must be preserved or
cause a detectable invalidation/revisit; dual-write or read fallback is per-feature
reviewed, never assumed. New writes are enabled only after readers ship and the
mixed-version proof passes. Journal-only infrastructure does not justify unrelated
new data policy.

Contract/removal requires a later release whose immediate predecessor no longer
depends on the old shape, and a separately approved frozen-surface decision. PT-02
contains NO contract/drop phase. No down migration, PRAGMA user_version decrement,
column stripping or restoring a stale backup as ordinary code rollback. R on the
R+1 SAME database must preserve new rows/columns/journal; R+1 on that SAME DB after
R writes must resume and preserve those writes. Expand-only DDL is necessary but
not sufficient: incompatible defaults, constraints/triggers and old SELECT/INSERT
assumptions can still break old writers.

## Acceptance evidence required before closure

A2 upfront criteria were received via r2f9db6e032cf and read in full:
`/var/tmp/clarp-refactor-program/reviews/pt02-937-A2-upfront-f666fb0.md`.
They are criteria ONLY, not design/format/source permission. No source code until
this exact design and its required boundaries settle. Release-tooling seams and
new internal lock path require their owners/lead; journal exact DDL needs contract
review and expand-only proof. Normal outputs and all intended refusal/boot changes
must be registered/signed before replay. No tests written or executed by this note.

Captain-owned qualification proposal, subject to A2 (not job admission):

1. Source census/guard: only explicit owner can reach migration/schema/backfill
   writes; import/conn, stamp, every CLI/hook, Host/runtime startup cannot. Two
   independent owner processes contend; nonowners do not migrate or create files.
2. Real older/newer/missing/partial/corrupt schema mismatch at each process boundary;
   no provider launch/recovery/socket/healthy response before readiness. No changed
   successful hook stdout or CLI JSON; stderr contains codes, not records/content.
3. Fresh database and all supported synthetic upgrades61..121 with schema goldens,
   migration matrix, data invariants and row preservation. Rehearsal profile70304313
   is approximate sizing input only, never actual performance/owner proof.
4. Crash before/after each schema/batch commit, uncertain COMMIT, cancellation,
   failed admission, restart and owner collision: no lost/duplicate rows, no skipped
   precondition job, no false done/version stamp, no long all-jobs transaction.
5. Production-shaped synthetic data includes >=1M state_log and skewed affected
   tables, holes and oldest/no-op pages; prove examined and written bounds,
   indexed access, whole-pass/writer hold and DDL duration. No live DB needed.
6. Actual release binaries and SAME DB sequence: old -> R -> old -> R -> R+1 -> R
   (read AND write) -> R+1; also R Host/R+1 runtime and reverse, plus old hooks/CLIs.
   Capture versions/shapes/counters/digests, FK/integrity checks and durable journal
   progress. Unknown future format refuses. Baseline old implicit migrator limitation
   is explicit; don't turn a copy/recreated DB into same-database downgrade proof.
7. Exact-pin G1/G2/CI/G4 and release-owner backup/health/automatic code-rollback
   receipt remain independent. Raw backup restore is disaster recovery proof, not
   downgrade compatibility. No live migration or restart by this agent.

## Intended differences, scope and ETA

Intended differences needing signature: nonowner opens no longer create/upgrade;
unsupported-newer/partial schema refuses early rather than accepting it; setup,
container boot and forward activation run the explicit sole owner; historical
upgrade data becomes resumable batches; no single all-upgrade writer transaction;
new owner-lock path and additive job metadata only if approved. Compatible ready
schema behavior/rows/normal APIs/CLI/hook outputs must stay unchanged. Ownership
changes and bounded transformations are fix-lane changes, not pure refactors.

Proposed implementation scope after criteria: db.py/db_migrations.py + explicit owner
module + precise boot/install/container/CLI/hook gate seams + tests/rehearsal/schema
goldens if journal expansion is approved. Preserve unrelated 933 convergence,
TP-N3, F4, lifecycle kit and reaper changes; re-pin actual main before implementation.

ETA (engineering estimate, not captain capacity promise): design/census and A2
upfront round today; bridge ownership implementation 1 working day after approval;
bounded historical plan/journal and compatibility controls another 2 working days;
captain remote qualification/release scheduling additional and unestimated until
slots assigned. Index/legacy-shape failures may require a separate approved plan.
PT-02 remains OPEN/DEFERRED in high-findings until full corrective evidence; neither
this note, profiling, nor the bridge alone closes it.


## A2 criteria reconciliation and precise unresolved choices

The independent criteria require production readiness before imports with actual
DB side effects as well as before main(). Implementation must either prove those
imports inert or establish the gate before importing them; adding a check halfway
through main is insufficient. No early runtime socket or public HTTP bind is
proposed. For each entrypoint a failed compatibility check is a terminal operation
failure; ordinary authorized business writes remain enabled after compatibility.

Proposed error matrix (lead approval pending; successful outputs unchanged):

| Condition | Owner | Host/runtime | DB CLI | Hook |
|---|---|---|---|---|
| Missing/empty DB | Explicit fresh bootstrap only; create/schema atomically | Exit 1 before context/workers; existing supervisor retry | Exit 1, stderr fixed schema_not_ready code, no success JSON | Existing fallback/exit/stdout; report schema_not_ready only |
| Known upgradeable older schema | Offline explicit plan; no owner fallback in client | Same refusal/retry | Same refusal | Same fallback |
| Below floor61 / unknown newer / malformed metadata | Refuse exit1, no moving/deleting/resetting | Exit1 unsupported, no public readiness | Exit1 unsupported | Existing fallback, report unsupported |
| Known compatible prior/next expansion | Check plan; run only authorized unfinished jobs | Ordinary business use, checked readiness | Ordinary business use | Ordinary business use |
| Required phase pending/failed | Resume only after durable plan/identity verification | Not ready; no recovery/provider work | Not ready, no partial command mutation | Existing fallback |
| Read lock/BUSY/IO error | One bounded attempt; primary code retained; uncertain commit readback later | Exit1 before startup; no migrate/reset; supervisor may retry startup | Nonzero existing failure envelope/SQLite code; no retry command body | Existing bounded fallback; no DB logging recursion |
| Owner lock contention/unverifiable lock | Nonblocking refusal exit1; no second migration | Not an owner; readiness gate alone applies | Not an owner | Not an owner |

Exact error spelling/channel mappings must be checked against each existing CLI
and hook envelope before implementation. No new public JSON fields or HTTP error
shape is proposed. A2/lead must settle whether existing generic errors suffice or
new error wording needs a frozen-output exception. Proposed schema-check admission
ceiling is 100 ms per connection using existing timeout machinery, bounded small
metadata reads, and no internal polling loop. This is not a hard IO deadline;
actual error/latency receipt is required. No two busy-timeout budgets are multiplied.

Lock order, with no concurrent restore: release/restore orchestration serialization
-> owner flock -> bounded SQLite write transaction -> COMMIT -> release SQLite
writer; repeat batches while holding flock; release flock -> activate/start services.
No network/provider/file operation inside the write unit. Restore uses its existing
root locks before migration begins; never acquire restore exclusivity from inside
a SQLite transaction. Replacement invalidates every ready cache by canonical
path device/inode plus schema generation, not PID or a process bool. In-place
restore/replacement while users are open is refused by release orchestration;
inode checks alone cannot make an unsupported live replacement safe. Long-lived
mixed-version users need compatible expansion or an explicit quiescence boundary;
open-time validation alone is not concurrency proof.

Job completion protocol proposal: each transaction commits business transformations
and its typed frontier together; only a final writer transaction, after a residual
eligibility check under the same owner, publishes job done/required readiness and
version. If a predicate can be re-enabled by old writers, residual emptiness alone
is NOT completion: require a proven dual-write/fallback contract or drain those
writers before finalization. Journal loss/corruption refuses; never infer done from
an absent row. A failed rollback preserves/logs both primary and rollback codes,
without retrying an uncertain admitted write. A long keyset traversal across a
legacy schema must retain which conditional additions/data conversions were
needed before any DDL commit changed that evidence.

Explicit implementation decisions still OPEN/HOLD:

1. Lead frozen-path approval for the owner lock and release-owner/captain agreement
   on native/container bootstrap and serialization. No current install/restore
   helper is assumed to serialize all these routes already.
2. A2 exact route/readiness/error/compatibility review. A known-next manifest must
   describe the actual expansion; R cannot approve an unspecified future schema.
3. Journal exact additive DDL/format and reader-first release separation. Ownership
   bridge can avoid new schema, but fully resumable legacy work cannot just be
   declared complete without durable progress or a per-job proven idempotent
   convergence alternative. These alternatives need an exact plan, not improvisation.
4. Complete statement-level plan for historical schema strings, dependency ordering,
   destructive legacy steps and blocking index duration before claiming all upgrades
   bounded. This initial design identifies the class and concrete examples; it is
   not a final per-version executable migration plan or a complete DDL cost proof.
5. Current future code target must be re-pinned after urgent main work. This design
   remains bound to f666fb0d; no production edit on a moved base is implied.

A2-required negative plants for the eventual existing owner tests: nonowner invokes
_migrate; ready emitted before final COMMIT; locked recheck omitted; replacement
reuses cached readiness; version stamped before required data; checkpoint advances
without row mutation; resumed non-idempotent chunk runs twice; strict equality
rejects supported rollback; unknown future schema accepted; one hook/CLI/import
path still creates/migrates. Use actual old-schema fixtures and real disposable
SQLite/process barriers, not a version stamp on a current shape. Controls include
blank and old DB contenders, unrelated DB independence, read-only disk/lock failure,
NULL/deleted/reordered/frontier-behind rows, old runtime retained during Host-only
upgrade, crash after chunk commit before bookkeeping and after final commit before
readiness. They extend existing migration/startup/rehearsal tests; no duplicate
campaign or jobs are requested by this design.


## Lead 937 rulings received; A2 AMEND remains in force

Lead 937 approved the ADDITIVE internal companion path
`<canonical database>.migration-owner.lock` with only the sole migrator creating
and holding it, kernel flock for the whole run, nonblocking acquisition,
close-on-exec and alias/wrong-owner refusal. Existing paths/env meanings do not
change; install.json.lock is not reused. Backup/restore must not restore a lock as
live state. Container ordering is root restore -> owner lock -> migration. The
lead will register the implemented difference; this note does not edit registers.

The private immutable-release module route is approved IN PRINCIPLE: native
install before activation/seeding, Docker after root restore before schema/identity,
rollback check-only, developer fresh roots explicit bootstrap, ordinary Host restart
never migrates. These seams belong to rf-releasec and require its agreement before
edits, INCLUDING decision830's supervised activation path. No permission to bypass
that ownership is inferred from the module name or this design.

Typed mismatch refusal is approved in principle; exact exceptional outputs await
A2 YES and lead signature on the proven forms. Normal successful outputs stay
byte-compatible. Progress DDL/format remains HOLD until bridge R ships; W follows R
with SAME-DB mixed-version/rollback proof. Captain priority is after S5 and TP-02.

A2's full exact review `reviews/pt02-design-47a210c81103-A2.md` is AMEND/HOLD,
not YES. AMEND1 requires concrete native/container activation, old-opener fencing,
lock identity and crash protocol; AMEND2 requires actual readiness/generation and
per-entrypoint boundaries; AMEND3 requires the concrete bounded job/format and
reader-first matrix before full PT-02 closure. Lead rulings settle the in-principle
path/route questions, not these technical proof obligations. Production code,
tests/jobs and final format remain unauthorized pending that settlement.

Next dependency: release owner source-only seam agreement and actual830 activation
protocol on f666; then amend the bridge design with that protocol and exact
readiness/connection-unit rules for A2. No repeated A2 review of unchanged47a is
requested and no old result is relabeled as approval.
