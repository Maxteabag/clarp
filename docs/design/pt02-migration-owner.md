# PT-02 migration ownership — current design index

DESIGN ONLY. Exact product base f666fb0db240513ae6dae1eda8227046b4001088,
schema121/floor61. Lead937 and A2's no-regression R boundary govern this lane.

The authoritative ownership-only R design is [pt02-r-lead-settled-scope.md](pt02-r-lead-settled-scope.md).
It preserves native fresh and legacy61..120 support: the explicit immutable-release
owner runs TODAY'S exact migration code under the approved companion before
activation. Existing121 verification is read-only; new ordinary consumers never
create/migrate. Existing serialization and830 apply. No new all-routes custodian,
old-process fence or fresh/legacy support restriction is a prerequisite for R.

Root cause remains db.conn's per-process migration flag and _migrate's single
whole-upgrade write transaction. R fixes implicit migration in NEW code only.
The old pre-R implicit-migrator race and historical unbounded transaction remain
named OPEN PT-02 residuals, not accepted risks. No schema/format/version change in R.

Source inventories/evidence:

- [pt02-openers.json](pt02-openers.json):61 lexical entrypoint/connector records,
  independently matched by A2; not a dynamic completeness proof.
- [pt02-bridge121-descriptor-notes.md](pt02-bridge121-descriptor-notes.md): early-gate
  anchors and two golden variants, diagnostic only; no new whole-schema allowlist.
- [pt02-bridge121-object-manifest.json](pt02-bridge121-object-manifest.json): complete
  separate fresh/upgraded golden object hashes, no database read or execution.

Earlier47a/f639 proposals are preserved in Git history only. Their support
restriction, extra custody prerequisite, full-shape allowlist and native-fresh HOLD
are withdrawn for R. Do not combine them with the lead-settled design.

W remains REQUIRED subsequent work, not an R acceptance prerequisite: separate
short expansion from bounded resumable backfills; persist reviewed typed progress
and immutable pre-DDL preconditions/cutoffs; commit data and progress together;
prove current-row eligibility and work behind frontiers; handle uncertain COMMIT
by readback; no provider effects under writer.100 examined rows/50ms hold/200ms
cooperative pass are provisional targets requiring captain measurements, not hard
SQL/IO bounds. Historical index builds/destructive upgrades need their own exact
plan and offline budget. v121 limit=None is not fixed by R. R cannot certify an
unknown W format: ship exact reader preparation after format review before W.
SAME-DB old/read/write/rollback and mixed Host/runtime proof remains mandatory.

Current evidence: docs/source only, no product code/import/SQLite/test/job/live
operation. A2 upfront and exact no-regression boundary criteria are received;
exact aligned R design YES, exceptional-output signature and normal source/gates
remain separate. Release-owner addendum878aba agrees the scoped route, not source.
ETA remains about1 working day for R after settlement, about2 further working days
for bounded-W work after its format/release prerequisites; captain slots extra.
PT-02 stays OPEN through the full corrective outcome.
