# PT-02 R: lead-settled compatibility-preserving bridge

DESIGN ONLY, base f666fb0db240513ae6dae1eda8227046b4001088.
This lead937 scope decision supersedes conflicting restrictions/lifetime proposals
in pt02-bridge-amendment.md and pt02-bridge121-descriptor-notes.md. Earlier documents
remain history/evidence, not additive approval conditions on this R.

## Exact R scope and acceptance

R must preserve native fresh install and legacy61..120 upgrade availability and
results. Initial121-only support restriction is REJECTED. R is judged by
NO REGRESSION + OWNER-ONLY MIGRATION IN NEW CODE. It is partial mitigation;
PT-02 stays OPEN until W and external containment close remaining issues.

| Input | Explicit owner behavior | R ordinary Host/runtime/CLI/hook behavior |
|---|---|---|
| Existing valid121 | Read-only verification, ZERO schema/data/user_version/settings/journal-mode writes | Compatibility check before application work; existing business writes unchanged |
| Missing/fresh0 | Run TODAY's exact schema creation and migration flow under companion lock | NOT_READY, no mkdir/file creation/schema repair; owner runs first in install/bootstrap |
| Legacy61..120 | Run TODAY's exact migration code under companion lock; same steps/result schema | NOT_READY, never auto-upgrade; installer/update owns preparation before activation |
| Below upgrade floor61, malformed or unsupported newer | Typed refusal according to reviewed existing/error principle | Refuse without repair; exact exceptional diagnostics still require signature |

No new schema/version/metadata/progress values. Do not batch or alter today's
legacy transforms in R. The present long write transaction is an explicit retained
PT-02 residual, not described as bounded because it moved to another process.
R does not satisfy the whole high finding or the later bounded-work criterion.

Only `python -m lib.persistence.migration_owner` from immutable candidate code may
initialize/upgrade in new production code. Ordinary conn and cache/stamp connectors
cannot call _migrate/_create_schema or create an absent DB. A private checked owner
context, derived from its actual held lock/DB identity, authorizes that call; no
environment value, public CLI switch or importing a library confers ownership.
Migration function compatibility names remain reachable for explicit private
rehearsal/fixture ownership, not as a second implicit production route.

Owner flow: resolve configured canonical DB -> validate lock/parent identity ->
open companion0600 NOFOLLOW/CLOEXEC -> nonblocking flock -> recheck identity ->
inspect existing schema through a nonmutating raw connection -> if valid121 close
without write PRAGMAs; else initialize/upgrade with today's exact code/opening
settings -> consume exact committed version/result -> close DB -> release lock.
Every SQLite transaction closes before activation/provider work. No busy retry
around the migration body or uncertain COMMIT. On uncertain completion no forward
activation; read back under newly obtained ownership before deciding next action.
Primary failure and rollback/diagnostic failure codes remain distinct, content-free.

Fresh canonical parent may be created ONLY by the explicit owner as today's
installer already does; ordinary check-only clients never do it. DB/file identity
verification remains required, including wrong-owner/alias refusal. Do not invent
native fresh-root support exclusion or an extra containment gate that prevents
supported install. If implementing identity checks exposes a concrete input
supported today that would regress, send the exact case to lead/A2 before code.

## Serialization and activation: deliberately limited R guarantee

The approved companion serializes NEW participating owners for the whole migration
run. It does NOT serialize every old installer, arbitrary raw SQL or old binary.
Do not require a new all-routes supervisor unit or universal native custodian to
ship R. The previously proposed companion-through-activation/death-safe custody
extension is NOT selected for R by this scope decision. Those proposals remain
possible later design work, not approved source/tooling changes.

Native fresh/update relies on install.sh's EXISTING serialization, no weaker than
today. Before current swap373/seeds/services, the immutable candidate helper
completes the explicit owner step. bin/clarp-admin forward activation shares that
preparation; check-only rollback never performs a forward migration. No second
lock path or new public env bypass. Docker: existing root restore/custody first,
then explicit owner, then identity/seeds/stack startup. Preserve ALL entrypoint
roles that bootstrap today, including a fresh-root maintenance invocation: the
ENTRYPOINT invokes the sole helper before that command, while the ordinary
maintenance command itself remains a nonowner. Do not silently refuse a currently
supported fresh maintenance startup. Existing approved restore exclusion rules stay intact; backup must skip
the exact companion and never install an archived lock as live state.

Peter-machine supervised activation retains decision830's route, coordinated with
rf-releasec. It verifies completed preactivation owner/DB compatibility, then its
existing one restart/300s/r3 flow; no migration on ordinary boot or second implicit
migrator. Do not alter accepted rc5/r3 snapshots or claim their lock graph became
a product-wide DB-custody protocol. Explicit developer fresh root invokes this
same owner module before starting Host/runtime. An ordinary supervisor restart
never migrates, and must not report healthy before compatibility.

Crash before activation leaves whatever the unchanged old migration transaction
actually committed and the previous code selection; no new progress journal is
fabricated. Failure after activation keeps the SAME DB and uses existing code
rollback with an explicit compatibility check; no downgrade stamp or stale-backup
restore to force success. The same-DB release proof still needs actual binaries
and writes; this document does not establish that proof from schema numbers.

## Named residuals — NOT accepted risks or completed work

PT-02-R-OLD-OPENER: an OLD pre-R hook/CLI/helper binary may call its old _migrate
without companion ownership, including concurrent fresh/legacy conversion. That
race exists TODAY; R must not enlarge it and does not claim to fence it. Host/runtime
stop, process census, new flock, PID marker and parent-death handling are not proof
of old-opener exclusion. W plus an external containment protocol must close it.
Keep it visible in the high finding/remaining work; no risk acceptance or silent
support removal is implied. Only the lead edits program registers.

PT-02-R-LONG-TXN: existing legacy schema/data/index work, including v121 limit=None,
continues in today's single migration transaction. R introduces one new-code owner,
not bounded backfill semantics. W's exact job/index/frontier/cutoff/pre-DDL evidence,
atomic data+progress, old-writer rule and bounded synthetic proof remain required.

PT-02-W-FORMAT: additive progress format is held until R ships; reader-first exact
compatibility remains necessary. R does not certify arbitrary W. A reader-only R2
must precede W if the exact later format cannot already be read by shipped R.
This sequencing detail remains to settle; no new values/DDL in R.

## Readiness and schema evidence under the no-regression rule

The full two-variant object manifest is DIAGNOSTIC INPUT, not a newly imposed
whole-schema allowlist for R. Five fresh/upgraded objects differ. An extra benign
index or another currently supported121 shape cannot be rejected merely because
it is absent from those two fixtures. Exact121 structural validation must accept
all supported existing/migration-matrix shapes and avoid changing successful
business behavior. Unknown custom schemas are not automatically certified either;
A2 must settle the minimum required-shape predicate and concrete counterexamples.
No author live-schema probe is authorized. Tests use supported synthetic fixtures.

Ordinary check first opens EXISTING store and validates compatibility without
DDL/data/journal-mode writes, then establishes its normal connection settings.
Cached existing transaction is never invalidated inside caller ownership; outer
business-unit validation and supported no-live-incompatible-replacement rule are
required. R has no job-readiness generation; schema_version is shape observation
only, not a solution to W progress changes. R2/W unit/readiness protocol remains
held. Startup check must precede actual dependent imports/context/recovery/bind;
the existing early-gate source notes are starting anchors, not full reachability
proof. Existing successful CLI/hook outputs remain byte-compatible.

## Next exact review, not implementation permission

A2 must now review this narrowed CHANGE, not an unsupported121-only product scope:
owner-only migration in new code, unchanged supported fresh/legacy migration result,
no new schema/data format, explicit old-opener/long-TXN residuals. If A2 identifies
where this route is WORSE than today, return that concrete case to lead937.
Otherwise finish the bounded exact R readiness/import/exception and owner protocol
criteria without making universal old-binary fencing a new R acceptance claim.
Release owner must confirm this lead-selected existing830/native/Docker insertion
route before edits. Exact A2 YES and exceptional-output signature still required.
No test/job/product import/source edit/deployment/restart is authorized by this note.


Release-owner no-regression addendum received/fullread:
`reviews/pt02-937-lead-no-regression-R-addendum.md`, SHA256
878aba24716e3158470a3080b3126a5e8304fde112b6be799cd9c60f04832d40.
It supersedes prior support-restriction/custody prerequisites for R and agrees
this lead-selected route in principle, not source clearance. Additional exact
proof targets: all today-produced61..120->121 variants are admitted; the owner
raw connection is used transitively through the migration chain without ordinary
conn reentry; every Docker role which bootstraps today keeps that availability;
configured DB path is unchanged; preactivation failure and SAME-DB rollback do not
regress. These are future qualification obligations, not reproduced bug claims.
