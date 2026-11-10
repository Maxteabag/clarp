# Transcript import progress storage v1

Status: storage-container design bound; no migration or writer is implemented by
this document. The root importer's continuation grammar, coherent bounded input
validation, DB epoch and effect publication proof remain prerequisites to its
executable format. This records the lead's 10 November 2026 additive-storage
fallback, not a claim that the root fix is ready or rollback-tested.

## Separate progress relation

The storage header is one new relation. Existing checkpoint and business table
columns, constraints and meanings stay unchanged.

```sql
CREATE TABLE IF NOT EXISTS transcript_import_progress_v1 (
    agent_id TEXT NOT NULL,
    backend_session_id TEXT NOT NULL,
    source_file TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (agent_id, backend_session_id, source_file)
);
```

There is no additional index, foreign key, default row or backfill. A checkpoint
need not exist: that is the reason progress needs its own relation. The native
conversation ID is `backend_session_id`; it is not the Clarp agent's session
slug. The source key uses the importer's existing canonical source identity.

Rows contain computation progress only. No phase, row existence, timestamp or
surviving generation certifies products or makes a source fresh. The only
phases are `scan`, `materialize`, `validate` and `ready_to_publish`; the last
still requires final validation and atomic publication.

## Payload envelope

The v1 top-level fields are exactly `version`, `owner`, `generation`, `step`,
`phase`, `parent_checkpoint`, `closure`, `db_binding`, `source_binding`,
`file_hash`, `inputs`, `effects`, `continuation` and `debt`.

`version` is integer 1. A generation is 32 lowercase hexadecimal characters;
it identifies a producer generation, not a DB incarnation. The step is an
integer from 0 through 2^63-1. Owner identity repeats and must match all three
relational keys, plus backend. Parent checkpoint is null for genuine absence
or the exact digest of the complete observed checkpoint row. The target source
extent stays fixed for the generation; later appends remain separate dirty work.

Canonical JSON is UTF-8, sorted by Unicode object-key order, without insignificant
whitespace, duplicate keys, NaN or Infinity. Binary values use canonical base64.
Booleans are rejected for numeric fields. Identity strings are at most 4096
UTF-8 bytes. The entire payload is at most 262144 encoded bytes, depth 16 and
1024 aggregate container elements. Reject rather than truncate.

There is one row per key and at most 1024 progress rows globally, including old
or unknown entries, with at most 64 active or queued producers. The payload cap
is 256 MiB across 1024 rows, plus SQLite/index/WAL overhead; it is not a bound
on process memory or physical storage. Capacity refusal preserves pending work
and never permits an unbounded full-read escape.

`inputs` is null or the exact five ordered, typed commitments for turns, client
provenance, latest-user provenance, all agent messages and all Oracle rows.
Null or partial inputs/effects are unknown. Source/file hashes and a physical
DB binding are not proof of unchanged present bytes, restored DB identity or
completed effects. A generation nonce, counts, MAX and PRAGMA/data_version are
not positive input authority. The concrete backend continuation grammar,
oversized-record representation, portable SHA-256 state codec, coherent bounded
DB validation, epoch and effect protocol must be bound and tested before a
writer may interpret this container as resumable work. Unknown subformats
remain protected; this container definition grants no arbitrary JSON state.

## Ownership, publication and retirement

Create/CAS under a short legal writer transaction against exact prior canonical
payload bytes and the exact parent checkpoint, including absence. Generation
and step are inside those compared bytes. Global capacity admission must be
atomic with creation. Uncertain commit requires exact-generation readback;
never replay effects or create replacement work blindly.

External reads and parser work occur outside the writer. Restart must validate
owner, source, closure and current DB/effect lineage before continuing. Progress
must survive restart even when no checkpoint exists. No fabricated certified
legacy checkpoint is a substitute.

Final publication rechecks the exact progress generation/step, parent checkpoint,
all five current input groups on the actual probe and writer paths, complete
products and effect authority. Products, real checkpoint/carry/certified witness
and retirement of that progress generation commit in one legal final transaction
or existing savepoint protocol. A stale finalizer cannot retire a newer generation.
No incomplete operation advances legacy certified fields or freshness.

No wall-age TTL discards unresolved progress or audit debt. Explicit owner/source
removal or supersession needs durable replacement/debt ownership before metadata
retirement. Cleanup scheduling and per-pass bounds are part of the implementation
proof, not permission for an unbounded sweep.

## Migration and one-release rollback

Use the next booked additive schema version: provisionally 122 while main is
121; the upgrade floor remains 61. Fresh creation includes this definition; the
versioned migration creates the empty relation idempotently and verifies its
shape. Before treating an existing name as compatible, verify a table with the
four TEXT/NOT NULL columns in this order, primary-key positions 1/2/3/0, no
column defaults, and the declared primary-key constraint. Refuse a view or
incompatible columns, constraints or unexpected triggers; never ALTER, DROP,
repair or silently replace them. CREATE IF NOT EXISTS alone is not verification.
No live DDL runs before the release carrying the migration.

Old runtime checkpoint loading remains unchanged. Its SELECT * constructs a
fixed Checkpoint object, so adding checkpoint columns is prohibited. An optional
`_authoritative_frontier` version-1 object may accompany a genuine checkpoint's
existing store_carry_json only with all eleven legacy carry fields preserved;
its phase is certified and progress is null. Old full/shadow capture can erase
that object or overwrite a checkpoint at the same size. New code must invalidate
that witness, and must revalidate business inputs changed by old writers. The
progress relation itself never contains a certified checkpoint.

The released 9e695b4e migration code accepts user_version >= 121 without an
upper-version refusal, supporting this additive version at source tier. This
is not executed rollback proof. Bind the actual previous release at shipping
and run old -> new -> old -> new on the SAME disposable DB: old startup and
ordinary business reads/writes must work with the new relation present, and
re-upgrade must reject stale progress before resuming. Test erasure, same-size
checkpoint replacement, restore, unknown versions, capacity, crashes and uncertain
publication. No destructive down-migration is needed. Old readers ignoring a
new table alone do not prove effect/currentness or normal operation.

This is internal storage. HOST_CONTRACT, MIN_IOS_CONTRACT and feature maps do
not change for it. A later public payload or behavior change needs its own
contract assessment. The compatibility record below remains explicitly pending
implementation and executed rollback proof.
