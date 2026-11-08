# Event wire implementation boundary — source only

`event_wire.py` freezes version1 FIELD declarations for nine separate column
banks. It does not create banks, collect real events, certify a producer,
implement partial-order replay or approve cardinality. Its local field checks
are not native semantic acceptance. Storage is still unexecuted/unreviewed.

## What must bind before the observer can use the fields

- Exact raw domain declaration: attempt; producer boot/PID/start/role/incarnation;
  source pin/manifest/profile; physical DB path/device/inode/incarnation and its
  initial seed/schema/counter. These are declared bytes only until the admitted
  parent matches actual source/kernel/closed-seed/launch records. Merges retain
  the complete declarations and reject missing producers or PID/DB reuse, never
  join only on pathname/PID/hash. Both pins retain their own source profile.
- Native-call IDs and connection-local sequence describe observed calls on the
  actual weakly referenced connection. Global append order is not native order.
  Keep original-call before/after brackets, source method role, exception status
  and transaction state. Sequence values cannot heal an overlap or missing call.
- A range has exact connection/transaction/operation/source-owner/savepoint
  membership and a positive native signed64 starting revision. A per-attempt
  table STILL retains the actual counter-statement call reference. Compression
  cannot drop that native-return-to-attempt edge or conflate a rollback reuse.
- Key edges retain the native writer call, exact typed PK, transaction/operation,
  writer/source role, attempt OR exact seed-retention provenance, revision and
  replace_revision separately. DELETE and head replacement are distinct from
  ASSIGN/RETAIN/MAX; no universal highest-revision acceptance.
- Resolution intervals [first_offset,stop_offset) are append-only terminal facts
  about a specific range. A rollback can cut through its middle. A resolution
  needs the actual native boundary call, same connection/epoch, proper savepoint
  ancestry and a known successful result/state. Overlap, missing coverage,
  unknown acknowledgement or a later failure cannot become a successful prefix.
- An operation's native wall/monotonic brackets retain source/write eligibility,
  not general volatility. A well-shaped time does not justify an arbitrary raw
  timestamp inside the import's broad bracket. Every raw field remains visible.

## Fixed-buffer arithmetic is not the peak or fit verdict

The worksheet function requires ALL nine tables, no implicit zero/missing table.
Each table reserves a positive row capacity, just as the Bank does; dictionary
entry/byte capacity may be0. It computes sum(row_width*reserved_rows), exact
raw dictionary bytes,4*(reserved_entries+1) offsets and128 independent refusal
bytes PER BANK per producer. Exceeding
any primitive ceiling is explicit, not an invitation to raise it. No bank is
allocated by this function, and it never reports source capacity or corpus fit.

Still missing: exact all-producer/schedule maxima for every table, range
fragmentation and connection lifetimes; per-identity count/bytes/lookup object
cost; live connection/epoch indices; all transient/closure/hash/export/consumer
peaks and common page-touch/instrumentation policy. Do not substitute17000
exchanges for17000 native writes, or three agents for three TLS connections.
No repeated-import bound or all-producer count is guessed here.

B's native store_transcript_turns has a read probe that may fail into a locked
write pass, incremental checkpoint savepoints and yield/reacquire transactions.
A and B differ here. Actual loop/callsite/source predicates must bound those
paths independently before a capacity sheet becomes an admission input.
The direct prompt_admissions counter is not wrapped _next_revision and needs
its own native-return-to-key proof or visible refusal; no cursor proxy/fetch.

The strongest control endpoint is the native collector/replay/domain merger:
retain all f628 old semantic negatives, packed-column corruption controls and
C's missing-producer/collision/direct-writer/out-of-order-arrival/overlapping-
nonallocating-writer tests there. An isolated check that a declared domain hash
changes is not actual producer coverage. These controls, whole event schemas'
source mapping, recorder/export validation and capacity arithmetic still need
exact review and admitted buildbox execution. No local tests or measurement.


## Conditional capacity boundary accepted by C (764 source facts)

The C capacity-boundary report separates successful-path capacity, detection/
retained-history and bounded in-flight/drain. No exhaustive arbitrary-failure
schedule bound is required, but source-qualified expected probe flow/native
retry/approved absence fallback remains part of the clean path. The earlier
finite Witness is NOT yet that complete exception-classification profile.

The new terminal primitive records the first refusal plus a last-publication
watermark and explicit missing-trigger/prefix-integrity/unknown-suffix state,
using storage independent of the main columns/dictionary. It is memory-only
unless the owner supplies separately qualified persistent/shared storage. A
zero channel is UNRECORDED, not success; a torn/lost channel is UNKNOWN. No
actual kernel sharing, survival across process death or lifecycle is proven.

A refused Bank can expose read-only prefix segments for the separately
qualified owner to preserve; the prefix is never promoted to a full native
history. Positive producer quiescence/exit or qualified termination and known
ownership are still needed. No automatic second export, product retry, root
reuse or copied diagnostic prefix may qualify a cell. Emergency bytes and
encoding/export transients join the ALL-producer resource worksheet; native
handlers that outlive clients and actual drain remain independently bounded.
