# 937 report expansion method proposal — PRE-CODE, UNEXECUTED

The exact08a06bd9 analysis identity C1 is source-closed by B report
criterion5-937-08a06bd9-B-render-C1-source.md, SHA256
6dd8f47b0bd65222dedf7d72343422a93b4293c148f9793cf6973d6648334e79.
This does not qualify execution, data, resource fit or the whole package.

## Source-derived problem

The accompanying coverage-report-expansion-source.json hashes all434 exact B007
server source blobs and four pinned coverage7.17 source files. AST-only census:
5,908,891 source bytes,131,815 physical lines,6,152 function/async definitions,
476 class definitions. These are source counts, NOT executable denominators.

Pinned JsonReporter.report_one_file calls contexts_by_lineno for the whole file;
make_region_data calls it again using narrowed_analysis.filename, not a region
filter. The reporter also emits an outside region for each of functions/classes.
RegionFinder appends at most one region per corresponding AST definition.
Therefore calls per file are structurally bounded by3+functions+classes:
sum7,930 across the full inventory; max154 for server/server.py. These are
upper bounds on calls for the frozen full scope, not observed calls, retained
JSON region counts or measured runtime. Repeated qualified names can overwrite
JSON region entries AFTER calls; they do not improve this work bound.

In branch mode each call scans that file's stored arc/context rows, builds a
set per positive endpoint and materializes lists. If U_f is native arc/context
row count, distinct endpoint/context incidences P_f <=2*U_f. Nonpositive-only
arcs contribute no JSON line context; independent accessor evidence retains
them. The full call/scan structural expression is at most sum(R_f*U_f), where
R_f=3+function_defs_f+class_defs_f; output/context object references scale with
sum(R_f*P_f) plus line keys and escaped names. Native U_f, name byte volumes,
API memory layout, source parse/analysis work and total costs remain UNKNOWN.
No physical-line count, node count or post-generation512MiB limit substitutes
for these missing bounds. This document assigns no seconds or memory fit.

## Proposed explicit method amendment for B review BEFORE code

Request changing ONLY the report JSON context projection: public
json_report(show_contexts=False), frozen policy/meta expectation false and
consumer checks requiring no contexts members at file/function/class levels.
Keep report_contexts=None (unfiltered); changing output projection is not a
query filter. Retain branch=True, all434 absolute morfs, all native regions,
source hashes/cwd/relative_files, exclusions, missing/partial/branch units,
analysis2/branch_stats consistency, warnings, data ownership and exact cleanup.
No native/product/tracer/test selection or denominator change.

This is an explicit proposal, NOT an implemented profile or granted shrink.
The current owner still requests show_contexts=True and the current consumer
requires it. Do not run it or treat this proposal as approval to alter those
contracts. Ask B to accept/reject this method before implementation.

All attribution stays mandatory in the independent closed aggregate capture:
full global contexts, every file/context arc set including negative-only arcs,
independent unfiltered lines/arcs and exact union checks across all9 roles.
The future package must persist that complete typed graph as a separately
hash-bound artifact, bind its exact source/data/role versions to the report,
and retain every original producer and closed aggregate. It must not merely
retain a pointer, summary or success flag. That persistence/producer custody is
still OPEN, not established by existing in-memory capture return values.
No hash dedup, context cap increase, attribution truncation or dropped roles.
The current native region context maps are whole-file repeats, not independent
region-specific observations. Removing those repeats from JSON must not remove
or weaken the independent attribution contract. Do not invent region-specific
coverage by intersecting a source range with an unqualified graph.

## Review and finite-control implications

If accepted, amend the SAME existing actual434 API-STORAGE control, not a new
campaign: require explicit meta false and absence of contexts at file/native
region levels, unchanged full file/analysis/source scope, and exact original
closed capture graph including a named context retained in the separate
attribution artifact. Supplied storage arcs remain storage-tier, NOT traced
Python, pytest participation, kernel/producer identity or actual denominator
proof. Raw graph persistence/bytes must be independently checked at its actual
owner boundary; do not manufacture passing fields in a record.

Existing lower record/control consumers must support only the newly frozen
explicit policy after review; do not accept either profile opportunistically.
A source-derived wrong-meta/context-member refusal belongs to their existing
record table. All controls remain UNEXECUTED. No old true-profile report trial,
extra full suite, run allowance, runtime comparison or new compute is requested.

Removing these redundant maps does NOT bound the remaining work. Full native
parser/analysis per434 files, regions and JSON arrays, raw graph/producer/API
materialization, peak heap/disk, serialized bytes/hash costs and all controls
still need concrete pre-generation and whole-package bounds. Any accepted
projection change must carry a revised source-bound numeric resource worksheet;
post-generation caps are refusals, not fit certificates. No blind trial.

Full exact B/C package qualification and rf-computec admission plus fresh captain
LOWEST gap remain required before ANY controls/measurements.6000s/100VMmin/~17NOK
includes all setup/controls/copy/fetch/drain/tails; no FUND/preemption/delay.
If useful faithful scope cannot be justified inside the cap, STOP/report lead
before compute. Actions program gates are not benchmark authority; herald931
platform HOLD and historical raw failures remain unchanged.
