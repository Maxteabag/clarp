# Producer/capacity facts — source-only, before numeric admission claims

Exact per-pin source/AST references are in producer_capacity_facts.json. Both
A4f33 and B007 were read independently. No module/product/SQLite/control/test,
provider, VM or measurement was executed. These are necessary distinctions,
not actual process/thread/connection counts or approved capacity maxima.

## External offered reads are not native handler/connection counts

The60s declared steady treatment offers120 direct runtime-status calls and120
HTTP-status reads. Both Handler._handle_status implementations call
RuntimeStatusView.block; its fetch is not a result-cache lookup. It invokes the
runtime client status RPC. With a connected runtime client and every HTTP
handler reaching that path, those HTTP reads contribute another120 runtime
status method calls, yielding240 status calls in that narrow offered-read
subgraph. This EXCLUDES readiness polls, startup/worker traffic, connect retries,
other RPC clients and all DB connection reopenings. No whole-domain upper bound
or multiplier of one DB connection per request follows.

RuntimeClient._connect can create multiple sockets during backlog EAGAIN
backoff. Those are not multiple accepted RPCs by definition, nor are they a
count of tracked SQLite connections. RuntimeClient.status_within already has a
native whole-call total_s boundary; this is the candidate direct measurement
API for the declared500ms client cap. RuntimeStatusView's native HTTP-side
status call uses STATUS_RPC_TIMEOUT0.75s with status(timeout=...), which is a
per-operation socket timeout, not that whole-call method. A500ms external HTTP
client timeout does not cancel, join or prove completion of its server-side
handler or its internal RPC. Preserve actual native behavior and account for
in-flight work/drain; never shorten the product's timeout to satisfy the tool.
No workload/client implementation or exact-API qualification is claimed here.

## The worker registry and normal window do not bound all incarnations

The cache has one _worker variable at a time. A drained worker closes its local
DB and exits; a later _ensure_worker may start a new Thread. _threads prunes dead
objects and explicitly does not cap total lifetimes. Three transcript owners
therefore do not establish three connections, three threads, or lifetime512.

_BACKGROUND_MIN_INTERVAL10s applies to the normal owner deadline. Direct
import_if_changed has separate admission; urgency bypasses the normal deadline.
The background window is updated after successful validation, not every failed
attempt. db.retry_locked has a source-fixed default of THREE attempts and50ms
base sleep; explicit retries arguments and other entrypoints still require
source qualification. Do not use a10s floor as the universal successful/failed/
urgent/direct/import or connection bound. Native caught errors and earlier
committed batches remain relevant even if a later operation fails.

## Native seed and startup include other data/actors

_seed_startup_invariants calls personas.ensure_builtins and Janitor bootstrap.
Both pinned built-in role tuples contain SIX roles in addition to the THREE
fixture transcript owners; their actual persisted identities/configuration and
work must be read from the admitted native setup, not removed or declared absent.
The initial native constructor calls these invariants. An exactly-three-agents
or three-TLS-producers assumption is not compatible with that source path.

Bookkeeping listener process launch is conditional on active goal-delegation
rows. Cache warm-up performs native model capability discovery. Runtime recovery
also starts its goal-move recovery thread. Their private data/source/guard
eligibility remains unqualified. Prove raw initial data and actual activity; do
not disable workers, skip recovery, create tombstones or expand the provider/
process/network allowlist to get a measurement. Empty private credentials and
a three-file transcript recipe alone are not a whole-root compatibility proof.

## Proposed capacity proof boundary: clarify before filling numbers

Numeric capacity needs the concrete source-qualified root/worker/client roles,
not an exchange-count guess. For the future common bank policy, propose proving
claimed clean-workload source-path volume (including native retries and every
admitted producer) and retaining all events up to the first measured-cell
refusal/censor. Any unexpected writer/path, native operation failure, unknown
acknowledgement, export failure or capacity exhaustion refuses qualification and
stops the remaining campaign under its already-declared censor rule. Preserve
native calls/results/primary errors; no production retry or scheduler change.

This is a proposed proof boundary, NOT permission to ignore caught native
errors, stop recording while calling the cell complete, or assert that all
failure/recovery schedules fit a normal-window bound. Unknown cleanup/drain
still preserves roots/stops; byte exhaustion cannot manufacture a valid prefix.
Need C to confirm whether that conditional clean-path proof plus full captured
pre-censor history is sufficient, or identify the additional finite path bound
required before measured integration. Then freeze concrete roles and arithmetic.
Until resolved all producer/cardinality/peak cells in the worksheet stay UNKNOWN.
