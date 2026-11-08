# PT-02 bridge amendment: exact scope and unresolved activation authority

DESIGN ONLY, NOT APPROVED. Base f666fb0db240513ae6dae1eda8227046b4001088.
Supersedes conflicting bridge/protocol suggestions in pt02-migration-owner.md;
its full bounded-work plan remains OPEN, not removed. No source/test changes.

Inputs: A2 AMEND/HOLD reviews/pt02-design-47a210c81103-A2.md; release-owner
reviews/pt02-937-release-owner-seams.md (SHA256
574d52fad0476a461a0928a52c3eb8ec0e7f7c9d738a7b05026ba3136a098874);
lead937 additive-path and in-principle route/refusal rulings. Release agrees
insertion points, NOT full activation protocol, code, or legacy conversion.

## Proposed scope decision, requiring A2/lead agreement

Bridge R changes ownership on an EXISTING, structurally valid schema121 database.
Its preactivation owner is a read-only compatibility verifier: no schema/data/
user_version/settings/journal/journal-mode writes. Ordinary clients become
check-only for MIGRATION authority, retaining business writes on compatible121.
No schema bump, persisted progress record, future-version acceptance or live
backfill. This remains a partial mitigation; PT-02 cannot close on it.

Fresh bootstrap is a separate owner-only branch, admitted only in an explicitly
exclusive root with verified custody; if custody cannot be proved, refuse BEFORE
creating the directory/database. Container root custody can use its existing
verified entrypoint ownership. Native fresh-root custody is NOT established by
this proposal and remains HOLD. A default missing root or a list of currently
running processes is not exclusivity proof. Existing CLI/hooks may create a missing
DB today and do not honor the proposed lock.

Legacy61..120 conversion remains a REQUIRED later work item. In bridge R,
nonowners refuse an old DB without modifying it, and no automatic native owner
conversion is enabled. This narrows current automatic upgrade availability and
requires explicit lead/A2 agreement; it is not silently accepted here. Preserve
historical migration source/floor61 fixtures and the intended supported import
promise while developing an enforceable offline conversion route. Do not expose
a helper override that converts a live legacy DB just because the caller has
permission to invoke Python. Existing old binaries can call _migrate without the
new lock; stopping just Host/runtime or taking a process census does not fence
their present or future hooks/CLIs. An external containment/maintenance protocol,
not guessed SQL, is necessary before that route can be enabled.

## One proposed native custodian, no second lock path

The release owner found no product-wide canonical-DB serializer today. Do not
claim program release.lock or config/install.json.lock fills that gap.

Proposal for technical/lead review: extend the SAME sole migration-owner process
into the custody parent of the activation operation. It holds the approved
canonicalDB.migration-owner.lock from before the DB check through the last possible
DB-dependent activation/fallback operation, not merely until the preactivation
child exits. Its inner migration phase still finishes and releases every SQLite
transaction BEFORE activation. The lock FD remains noninheritable; children do not
hold it. This changes the previously proposed 'release flock, then activate'
lifetime and therefore needs release-owner/A2/lead agreement, not implementation
by assumption. No extra outer lock path or generic shell-command capability.

All supported forward install, manual activation, rollback/fallback and native
restore entrypoints must delegate their fixed internal operation to this SAME
custodian. Public commands/env/output stay unchanged. The custodian is the sole
process allowed to create/acquire the migration lock; a check-only rollback
custodian uses the same ownership protocol but performs no forward migration.
This rollback locking detail also needs the lead's exact ruling on 'sole migrator'.
An old/nonparticipating installer is outside this proposed serialization guarantee;
its operational exclusion must be explicit before calling serialization universal.

Custodian lifetime must cover an activation child after parent death, too. Proposed
supported Linux route: activation runs in a release-owned supervised process group;
custodian death stops its fixed children; a new custodian cannot mutate/replace
until the supervisor proves that exact group retired. No PID-age inference. This
is NOT yet a verified implementation or generic cross-platform guarantee. The
release owner must select the exact existing830 supervisor interface or another
reviewed fixed-action custody primitive. macOS/native manual execution has no
assumed equivalent and stays HOLD until pinned. If no acceptable custody route is
available under approved scope, keep native activation held and return to lead;
do not add an env bypass or a second lock path.

The simpler alternative is to restrict bridge activation to an existing externally
serialized release route. That is a product support restriction, not a source fix
for direct install/rollback. It needs an explicit lead decision and cannot be
represented as universal migration ownership. Neither alternative clears A2 yet.

## Exact insertion/order proposal on f666

| Route | Before effects | DB phase | Activation and fallback |
|---|---|---|---|
| Existing121 install/update | Prepare immutable candidate/env and rollback snapshots; enter custody BEFORE install.sh373 current swap | Candidate owner checks121 read-only, closes check transaction; recheck same DB identity | Switch current; seeds/skills check-only; start/watch; previous-code compatibility check before any rollback switch; leave custody only when children/fallback retired |
| Manual forward activate_release | Enter same custody BEFORE bin/clarp-admin.py2542 swaps current | Same exact candidate verifier; no independent conn migration | Same forward/fallback procedure, not a separate owner |
| cmd_rollback / installer trap | Same serialized check-only custody; preserve primary failure | Previous binary's descriptor must accept SAME DB, no downgrade stamp or forward migrate | Restore code/toolchain/unit snapshots only if compatible; unknown refuses, no backup overwrite to force rollback |
| Native ordinary Host/runtime supervisor retry | No owner invocation | Compatible121 check only | Fail before production imports/context/recovery/bind; existing supervisor retry does not create migration authority |
| Docker stack start | Existing root-owner/users locks, apply/recover pending restore FIRST | Only stack/bootstrap role obtains migration ownership before entrypoint92 conn/identity | Verified compatible state before children; maintenance role never becomes migrator; release only after bootstrap child effects retire |
| Docker restore | Existing exclusive root protocol proves all old users/owners absent | Validate restored bytes; never restore companion lock; acquire current-path owner only AFTER replacement | Same stack bootstrap check; mismatch cannot start services |
| Fresh native / legacy conversion | Exclusive containment NOT yet proven | HOLD; no create/convert in R pending scope decision | No implicit bootstrap fallback |

830 normal bridge route: preactivation custody/verifier once; existing121 no-op
means old compatible runtime can remain during Host watch. Before its ONE supervised
restart,830 must revalidate exact candidate/DB compatibility without calling migration
or opening via ordinary conn. Its current300s/r3 continuation sequence stays a
separate reviewed phase, with no migration inside the boot deadline. Custody has
to cover this phase or transfer to the proven supervisor without an exclusion gap;
that exact interface remains release-owner HOLD. Do not modify accepted rc5/r3
snapshots. Offline legacy startup is different: target runtime first/with Host,
not today's Host-first watch followed by an unnecessary second restart.

## Bridge121 readiness descriptor and connection boundaries

R's descriptor is CODE, not new stored metadata: descriptor ID pt02-bridge121-v1,
allowed user_versions {121}, required schema object names/types and normalized
DDL variants derived from BOTH exact121 fresh and v61-upgraded schema goldens.
No arbitrary extra trigger/table/column variant is certified by the version stamp.
The concrete generated object manifest must be reviewed before source acceptance;
the current lexical opener JSON is not that manifest. Do not embed unverified
expected digests or require one golden's textual CREATE spelling for both shapes.
Query existing sqlite_schema only; no temp tables/DDL/ANALYZE/journal-mode changes.
All cursors are consumed; structural examination is bounded by a fixed object/DDL
byte cap from that manifest. Oversize/unknown shape refuses, never truncates into
success. Successful validation causes zero schema/data writes.

No next-version descriptor exists in R: all >121 refuse as UNSUPPORTED, and all
<121 refuse as NOT_READY (below61 UNSUPPORTED). This singleton interval is deliberate
for the zero-expansion bridge; it is NOT a claim strict equality suffices for the
later writer release. R -> baseline O rollback preserves the SAME121 database.
Before W introduces ANY progress format/schema change, ship another reader-only
R2 with that exact format/compatibility descriptor and ZERO new semantic writes;
then W. Lead's 'W after R' is necessary but does not authorize skipping R2 if R
cannot already read W. No fixed R can certify an unspecified W. This explicit
R/R2/W separation and any schedule change require lead/A2 settlement.

No new durable schema/readiness generation is introduced in R. Job readiness can
change without DDL in W, so do not pretend inode/user_version sees it. R admits
ONLY immutable121 shape and forbids concurrent schema/restore mutation under the
supported release protocol. Therefore its ordinary data changes have no readiness
phase transitions; optional journal status cannot become a hidden readiness source.
R2/W must define and validate real progress metadata at their actual work-unit
boundary, or use explicit quiescence. That design remains HOLD.

For R each new connection opens existing DB mode=rw (ro for inspectors), validates
before application PRAGMAs; stamp connection opens existing mode=ro and cannot
create directories/files. Before returning a cached ordinary connection OUTSIDE a
transaction, recheck canonical parent/DB identity, user_version and supported shape
unless an unchanged same-connection SQLite schema_version observation proves the
already-validated immutable121 schema unchanged. Schema_version is only a shape
cache key here, never W progress readiness. No process `_MIGRATED` success flag.
For a joined active transaction, keep its connection/snapshot and do not invalidate
or poll; the owning outer unit must have validated BEFORE BEGIN. New implicit writes
begin from a validated conn. Long-lived cursors/explicit transactions may finish
only under the no-concurrent-incompatible-mutation release rule. That rule, not an
inode cache, supplies safety across the unit. Concurrent raw replacement or
nonparticipating DDL is unsupported and cannot be made safe by repeated checking.

Before returning a mismatch: close refused connections; do not cache them; no store
logs which recurse into conn. Fixed codes/version/type only. Existing backup remains
raw and available for unsupported schemas; structure inspection cannot migrate.

## Error/import boundary amendment

Host gate placement is BEFORE the first DB-dependent top-level import in server.py,
not merely before build_server or serve_forever. Inspect top-level calls in the
transitive startup import graph before assigning the exact line; those not proven
inert must be moved behind the gate or an equivalent early check. Runtime similarly
gates before importing/constructing production context/dispatch/recovery machinery.
No claim the61-record lexical census proves this. CLI/hook DB imports either remain
inert or defer their first store operation until the same check; the central conn
and stamp gates are defense in depth for a missed entrypoint.

Proposed fixed exceptional diagnostics, still awaiting exact lead signature:
`schema_not_ready version=N phase=open` or
`schema_unsupported version=N phase=open`; malformed/unreadable version uses
`version=unknown` plus SQLite numeric code/type, no exception body/path/content.
Host/runtime exit1 before listeners/recovery; CLI DB-dependent command exit1 with
bounded stderr and no success JSON; existing CLI failures with documented distinct
exit code must preserve that code and map only the diagnostic text. Hooks retain
their EXACT existing stdout/exit/error-report conventions; prompt hooks may omit
DB-derived context on refusal, never turn it into a provider control decision.
Do not promise this family-level proposal is per-command proof: exact exception
catch boundaries and hook fallback outputs remain part of the next source census.

## Failure table and pending approvals

| Failure boundary | Durable facts and allowed next action |
|---|---|
| Before owner lock/read-only121 check | No DB mutation; no activation; nonblocking refusal preserves original failure |
| Check complete, before current switch | Previous code remains; new attempt obtains verified custody and rechecks SAME DB; no filesystem receipt alone proves ready |
| Existing121 commit-before-activation | R performs NO DB commit; this case applies only to future approved bootstrap/W. Preserve actual committed schema/data; reconcile exact journal before replay |
| After current switch, service/start/watch failure | Preserve DB; check previous code compatibility before fallback; incompatible/unknown keeps admission closed and reports primary plus fallback refusal |
| Custodian death with activation child alive | Do NOT start a competing activation/restore on released flock alone; supervisor must prove exact child group retired. No implementation proof yet |
| Lock path renamed/recreated/DB replaced | Refuse identity mismatch; don't unlink/repair a live lock; recovery requires exclusive custody and new full readiness check |
| Restore archive includes owner companion | Reject that member (exact canonical companion, not blanket *.lock); never install it as live lock state |

Companion identity procedure: pin canonical parent fd; reject symlink path components,
regular DB/lock must match uid and single link; use dirfd-relative nofollow/CLOEXEC
open, verify fstat vs named entry before/after flock and before activation, retain
lock inode without unlinking on exit. Parent replacement/symlink aliases fail.
These checks DETECT races; outer supervised custody must exclude supported actors
from replacement. They cannot fence hostile/same-user raw SQL; no contrary claim.
Storage allowlist must match a verified local filesystem and effective kernel flock;
unknown/network/shared storage refuses, with no new override env var. macOS storage
behavior and source-level portable verification remain explicit review items.

Decision packet to lead/release/A2: choose the native custody implementation or an
explicitly restricted serialized R release route; agree the existing121-only bridge
and fresh/legacy HOLD without retiring the floor61 requirement; agree R2 before W
when the format is not known at R. After that, finish per-entrypoint reachability /
object-manifest proof and obtain exact A2 design YES and exceptional-output signature.
No product code, test, import, SQL, job, deployment, service or runtime action is
performed or authorized by this amendment. PT-02 stays OPEN until ownership AND
bounded historical conversion AND bidirectional proof are actually complete.
