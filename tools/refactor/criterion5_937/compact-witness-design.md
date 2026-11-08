# Compact transaction evidence: proposal before measured integration

SOURCE DESIGN ONLY. No compact recorder, measured loader, root or job is
implemented by this document. C's f628 finite-component clearance stays at its
original scope. No new execution or full SOURCE_READY is requested.

## Requirements that do not change

Preserve the actual native args, result/cursor, primary exception and call count;
add no SQL/fetch/transaction/clock change. Keep every native boundary, allocation
attempt, keyed assignment/deletion and committed/discarded/unknown outcome.
Native keyed raw records and clocks remain unmodified. All processes writing the
same physical database must contribute to one complete domain; a single local
receipt cannot prove the domain by matching a final delta. No partial bank,
unknown path or capacity exhaustion may qualify a cell.

The old8192 Python-dict event inventory and lifetime connection512 limit cannot
qualify S/L. Their control receipts remain qualified only as finite source
preparation. This proposal replaces the REPRESENTATION; it does not simply
increase those constants or discard older committed batches. A total capacity
proof and observed overhead remain mandatory before measured use.

## Proposed representation, not yet authored

Use fixed-width typed columns in pre-sized, privately owned byte buffers for
high-volume facts, with bounded dictionaries only for unique source/operation/
owner/key identities. Store native numeric revisions and attempt identities,
never only counts or normalized values. No per-row dict, formatted stack,
unbounded list or per-row disk write. A source-pinned schema version and byte
lengths make every column range independently inspectable after process exit.
The recorder uses explicit offsets and checks byte capacity before each append.
No silent overwriting, circular eviction, reallocating beyond the signed bank or
compressed aggregate that loses an assignment.

1. Connection incarnations: an increasing u64 birth ID and weak actual native
   reference, with live lookup keyed by object identity. Reclaim ONLY the live
   lookup after its native lifetime ends; never recycle the evidence birth ID.
   Closed/expired connection histories remain in the typed bank. Record separate
   bounds for simultaneous live lookups, total incarnations and total bytes.
   Unknown destruction while evidence is pending remains UNKNOWN. The old total
   lifetime cap must not be relabelled as a simultaneous-live cap.
2. Transaction/savepoint boundaries: compact tuples retain connection birth,
   transaction epoch, native before/after, exact native boundary role/outcome,
   BEGIN record ID, savepoint parent/epoch and rollback-to/release suffix limits.
   A pending transaction keeps offsets into the append-only attempt and edge
   columns. Commit or rollback adds one terminal tuple; it does not rewrite all
   rows or discard the transaction history. Nested RELEASE is not a commit.
3. Allocations: consecutive native revisions may share a range header ONLY
   within the SAME connection/transaction/savepoint membership, owner, operation
   and actual caller role, and only while every value is consecutive. Each
   returned allocation still has an unambiguous (range ID, offset) attempt ID.
   Rollback numeric reuse starts another attempt/range even if the values match.
   Interrupted/coalescing metadata must not hide unknown acknowledgements.
   Nonconsecutive or different-membership allocations use another range. Both
   range fragmentation AND retained row/key edges have independent byte caps.
4. Keyed assignments: every actual source-qualified SQL write gets an edge
   `(native event order, key ID, attempt ID OR exact seed-revision reference,
   transaction/savepoint identity, writer/caller role)`. Raw PK strings have a
   lossless length-prefixed bounded dictionary; a digest alone is not an
   identity. Rewrites/deletes/multiple writes are retained, not just the latest
   value. Head revision/replace_revision and no-allocation source/seq mutations
   require their OWN exact SQL/native predicates. This design does not equate
   them with the finite component's highest-revision example.
5. End-of-process export: a bounded bulk export preserves the original column
   bytes, used lengths, identity dictionaries and terminal status. Hashes bind
   them to attempt, process PID/start/boot, source, physical DB and schema. The
   export is not itself a native commit certificate. Acknowledgement/transport/
   process loss keeps the domain UNKNOWN/PARTIAL. No retry or reconstructed
   successful receipt. A failed export must never suppress a native primary.
6. Controller consumer: streaming validation reconstructs the same raw logical
   evidence semantics as f628, including suffix rollback and intermediate or
   deleted rows. It merges all process domains by source/DB/operation identity,
   validates native committed revision ownership and every final raw keyed row,
   and retains unused/intermediate allocations. Same-role row swaps still fail.
   Input dictionaries are not a generic revision/time normalizer.

## Concrete source inventory and unresolved paths

`native_allocator_inventory.json` statically parses ALL server Python sources
at A4f33 and B007, retains exact per-file and per-enclosing-AST hashes, direct
names, imports, callback arguments and counter-writer literals. It is lexical
source evidence, not a runtime profile or complete dynamic-alias certificate.
A has360 parsed Python files and B434. `_next_revision` lives in A message_writes
and B message_import_common; message_live imports it, B message_import_rows and
message_writes retain aliases, and stamp_turn receives a next_revision callback.
Each pin needs its own native caller/alias profile, including reentrant imports.
No blanket global stamp_turn AST carry is claimed.

The inventory ALSO finds direct counter allocation in
`prompt_admissions.update_for_queued_edit`, independent of `_next_revision`, plus
schema/migration initialization. The fixed synthetic workload must not simply
assume these routes absent. A reachable direct allocation needs its own exact
source/native-return-to-key binding or must visibly refuse the cell. The native
counter statement's returned cursor must not be consumed/proxied just to obtain
a revision. Schema/migration writes need explicit initial-domain boundaries;
no native seed value can disappear into an unlabelled baseline reset.

## Capacity and cost: still OPEN, no fit assertion

Before choosing concrete bank bytes, derive per-pin distinct keys and maximum
allocation/edge/transaction/range/connection cardinalities from the frozen
recipe AND actual startup/import/recovery/append/control source paths. The L
recipe contains17000 initial exchanges, not merely17000 total native writes;
stamps, retained rows, intermediate allocations and repeated full import may
add work. Three owners and eight cells do not bound background TLS incarnations.
Unknown repeated import/transaction fragmentation is not a licence to inflate
memory or silently drop/censor the interesting part of the protocol.

Present actual row-width and dictionary-byte arithmetic, live-lookup memory,
range worst case and every bank/producer allocation to C/B BEFORE integration.
Include measured/declared recorder cost and scheduling perturbation; no guessed
overhead subtraction. Preallocation cost and every export are charged inside
lead937's6000s/~17NOK inclusive budget. If complete finite storage and useful
native work cannot be demonstrated within that protocol, STOP/report the lead
rather than claim MET, shrink S/L, change the treatment or expand the budget.

## Required controls before readiness, none executed

- Reuse the finite component's native rollback, reuse, C closure, acknowledgement
  unknown, commit-then-error, plan-only, missing/duplicate receipt, row-swap and
  actual cursor/primary identity contracts against the NEW representation.
- Exact old-record-to-new-column logical reconstruction for a finite native
  receipt, independently checked against actual native tables/counter. A tiny
  self-comparison is not the sole proof.
- Fragmented/interleaved ranges, rollback suffix crossing range boundaries,
  repeated writes/deletion, retained seed edge and head replacement semantics.
- Actual connection death/rebirth with preserved evidence and deliberate wrong
  incarnation evidence, not merely a fabricated count. Any numeric object-ID
  reuse claim requires an observed reused ID; otherwise label it unproved.
- Wrong column type/length/offset, missing/duplicate/interchanged key or attempt,
  truncated terminal/bank/export, changed source/DB/process identity and each
  independent byte/live-lookup/range capacity exhaustion all refuse proof.
- Native result/exception preserved when the recorder is exhausted or export
  fails. ALL producers enumerated; unobserved direct/external counter writer
  fails even if raw totals match another missing writer.

No new capacity number is approved by this proposal. No observer implementation
or control execution is inferred from a favourable design review.
