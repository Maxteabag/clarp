# U02: backend identity and catalogue ownership

Design only, under lead 678 and review-first 673c. No source implementation or
landing authority is asserted here. Base: integration
`a733101781144cbfaf5e6dcdc3386cd71f445be0` (current remote integration when
this note was written). Discovery: `989779c0bced90abe07bbd9ed41079c741d999bc`,
report SHA-256 `f543f56b9890b004b171fd4306d808c4a229857c98fbd4b41e0f25828ddb5591`.
Reviewer: A2, `rf-review-core2c-d509`; acceptance cases read from
`/var/tmp/clarp-refactor-program/reviews/U02-A2-acceptance.md` before any code.
The discovery's U02 section, overlap table and suggested sequence are binding.

## One owner for values, separate execution binding

Add `lib/backend_catalogue.py`, a standard-library-only leaf outside the runtime
facade. It owns ordered immutable backend definitions: identity, aliases,
presentation, declared capability/login/effort/model values, fallback rows and
probe descriptors. Move `AgentBackend` and `BackendBrand` here, retaining their
original import names as aliases in protocol/base/backends. Preserve
`AgentBackend.valid()` exactly, including its existing distinction from the
seven-entry live catalogue; this is not an opportunity to repair that policy.

A probe descriptor is inert data: operation key, parser key, command arguments,
existing timeout, provenance detail/kind and fallback/merge policy. It contains
no imported parser, strategy, filesystem callback or runtime singleton. Definitions
for OpenCode and DeepSeek deliberately share a listing operation but retain their
own parser/filter/provenance and fallback rows. Claude's cache-and-append policy,
Muse's sorted cache aggregation, AGY's remembered ids and each command probe stay
distinct. Descriptor keys select operations; provider-name switches do not define
the same policy again in the discovery orchestrator.

`lib/backend/registry.py` separately owns id-to-strategy-class bindings, singleton
construction and locks. It imports normalization from the leaf, never backends.
Its class map retains all seven classes and the current singleton identity and
order. Execution bodies, inheritance (DeepSeek/OpenCode), process registries,
account pools, locks, dispatch, Stop and Codex callbacks remain where they are.
Adding a genuinely new executor still requires one explicit execution binding;
that is a legitimate separate obligation from defining a catalogue row.

`provider_capabilities.py` consumes the leaf and runs the descriptor through its
existing explicit resolve/run/cache collaborators. Parsing, settings persistence,
threading, version/model parallelism, cache generations and background refresh
remain there; no account reads or subprocess work move into the leaf. The
operation handlers preserve existing exception/log policy, not a new universal
probe fallback. Existing private helper names remain compatibility entrypoints.

Backend class metadata delegates to its definition rather than redeclaring the
same literals. A narrow compatibility attribute adapter must preserve existing
instance/class access and supported monkeypatch/restore behavior; execution
callers still obtain the same strategy instances. Before code, census those
sites and distinguish production metadata consumption from execution overrides.
No production path will recover descriptor values by importing the facade.
Pure declared-metadata entrypoints use definitions directly; complete catalogue
projection joins execution-derived facts at the explicit boundary below. `get`, `by_id`, adapter
enumeration and execution operations remain strategy APIs. Old facade constants,
lookup names and type aliases remain available until actual callers migrate.

## Obligations removed

- U02.1: registry normalization depends on the leaf. The registry-to-backends
  backedge disappears; normalize/is_valid share the same alias table without
  triggering strategy construction. Invalid accepted inputs/errors remain exact.
- U02.2: inert ids, presentation and declared capabilities live below runtime
  status/configuration. Metadata consumers target the leaf, not the runtime
  facade. `capabilities()` still resolves the executable at its explicit dynamic
  boundary; declared required_binary is not substituted for Claude's configured
  executable. Neutral unknown catalogue rows remain separate from execution's
  unknown/empty-to-Claude normalization.
- U02.3: the descriptor in each authoritative backend definition owns discovery
  selection and fallback policy beside its catalogue data. Discovery handlers
  consume it, with no second Codex/Claude/etc. policy catalogue or switch.

Import direction: protocol -> leaf; backend base/strategies -> leaf; registry ->
leaf + strategies; facade -> leaf + registry; provider discovery -> leaf + probe
implementation collaborators. The leaf imports none of those consumers. A cold
leaf import/read must not construct a strategy/runtime, read DB/config/home,
resolve an executable, start a worker or probe a CLI. This is an import-boundary
claim to test, not a claim that every legacy facade import becomes inert.

## Frozen surfaces and change rehearsal

No backend id/alias, ordering, response key/value, model row, neutral fallback,
CLI/env/path, hook/skill, schema or client-contract change is proposed. Preserve
all provider-specific commands, deadlines, parsers, provenance, availability,
installation and authentication semantics. iOS and Rust desktop continue to
receive identical catalogue responses. No PWA-only work or local cargo.

Rehearsal: in a disposable synthetic control, change Grok's catalogue detail and
its model-probe timeout in its ONE definition. The real catalogue projection and
injected discovery run must observe both changes without edits to backends,
registry or a provider-name switch. Restore the original values before shipping.
The executor binding stays untouched. Also exercise the separate executor
binding check so the rehearsal does not imply a new backend needs no runner.

## Evidence, overlap and delivery

Use A2's four acceptance groups: identity corpus across actual consumers; ordered
catalogue snapshots including neutral unknowns and dynamic binaries; every
provider's success/missing/malformed/nonzero/timeout/cache/fallback cases; cold
metadata plus unchanged runtime delegation. Preserve strong existing tests and
add only gaps. Planned negative controls alter aliases/defaults, reorder a row,
lose one probe policy, or import execution from the leaf. Assertions use retained
old-code observations or independent expectations, not descriptor self-comparison.

Coordinate provider_capabilities with sweep b before implementation; retain its
accepted extraction and do not concurrently edit it. U09's runtime DTO ownership
is separate. Existing S6 cut2/_Client notification work remains pending and is
not authorized by this design. This note does not activate a split recipe.

Re-run discovery analysis against the actual implementation base and result in
owned output directories, preserving the discovery baseline. The supplied scripts
hardcode ROOT/OUT: use task-local copies with only those paths redirected, never
overwrite shared evidence. Compare import backedges, special-case sites and
U02.1-.3 explicitly; retained protocol discriminants/expected parse fallbacks are
not defects or promised count reductions. Record exact pins and script hashes.

No tests/jobs have run for this design. G0 where required, one exact remote G1,
own-base G2, A2 source verdict and captain gates remain owed. No intermediate-tip
CI. Implement on a branch from the current integration layout only after the
design/acceptance gate; nothing lands before big merge #1. After that ship, land
small batches on main through the captain. Release holds override scheduling;
this design assignment does not resume the older paused S6 runtime goal.

## A2 amendment: field ownership and override boundary

This section narrows the earlier phrase "declared capability": compaction support
is NOT a declared bit. No supports_compact field belongs in a backend definition.

| Field group | Authoritative owner | Read boundary |
|---|---|---|
| ids/order/aliases, presentation, declared booleans, login/effort/model/fallback values and probe descriptors | Immutable leaf definition | Pure definition queries remain inert; compatibility strategy attributes delegate to these defaults |
| supports_auth, model_carries_effort, effort_compatibility_unknown, bool coercions and detail/symbol defaults | Pure derivation over the selected values | Leaf projection can derive them for explicit values; legacy projection supplies current strategy attribute values so accepted overrides remain visible |
| supports_compact | The strategy's current compaction method and Backend.supports_compact property | Complete catalogue projection reads the current strategy property, invoking compaction(""); only Unsupported becomes False; all other exceptions propagate as before |
| configured executable in capabilities() | Current strategy.executable() | Explicit dynamic execution/config boundary; declared required_binary in the leaf and CAPABILITIES snapshot retain their original distinct meanings |

The complete response is not claimed inert. backends.catalogue_fields retains a
strategy-backed compatibility projection; provider discovery obtains that same
projection through a separately bound execution-side collaborator, not through
backends and never through a leaf backedge. The leaf exposes declared metadata
and a pure formatter accepting explicit values; it never imports, resolves or
calls a strategy. Strategy.catalogue_fields supplies current attribute values and
execution-derived supports_compact to that formatter. Neutral unknown rows use
the existing default Unsupported behavior and neutral presentation, not Claude.
This intentionally retains one execution dependency for a complete row; U02.2
removes the runtime-status facade dependency from inert consumers, not the
legitimate behavior-derived fact from the public catalogue.

Override precedence is ordinary Python lookup: an instance override shadows the
metadata default, a class override shadows its inherited/default value, and
removing an override reveals the original definition or inherited override.
Use a small non-data attribute descriptor for compatibility default access if
needed, with class access also returning the declared value; no metaclass,
facade patch propagation or mutable global descriptor edits. A DeepSeek default
resolves against DeepSeek's definition, while genuinely inherited behavior (such
as compaction) follows its unchanged MRO. A DeepSeek-only override must never
mutate OpenCode, and restoration must expose the prior inherited behavior.
Overrides affect legacy strategy-backed catalogue/capability projection, not the
immutable leaf definition or import-time snapshot tables. No support bit is
cached in a second owner. Class-level replacement/restoration of a compatibility
attribute must preserve its original descriptor, rather than materializing a
copy as a permanent class literal.

Narrow source census at a7331017 (source read only):
- backend/base.py:191-199 derives supports_compact from compaction; 201-226
  reads current instance metadata for catalogue_fields. No runtime mutation of
  catalogue data was found in the inspected production entrypoints.
- tests/unit/test_backend_capabilities.py:133-134 explicitly protects the
  behavior-derived compaction property; test_backend_strategy.py:69-79 requires
  facade/get/table entries to return the original singleton itself.
- Supported instance binary patches exist in test_backend_fork.py:31/47,
  test_codex_connection_recovery.py:23, test_codex_app_server.py:232,
  test_backend_prompt_size.py:50 and test_backend_strategy.py:588; replay host
  also replaces Codex.required_binary. Preserve those execution overrides.
- test_backends.py:163-193 covers actual catalogue compactable, Unsupported and
  unknown-neutral rows. Existing tests do not establish class-level compaction
  override coverage; A2's requested cases will add that contract explicitly.
This is a scoped census of the contested boundary, not a whole-program dynamic
mutation proof. Repeat the AST patch-site census before implementation changes.

Additional prewritten acceptance: actual catalogue consumers before/during/after
instance metadata and compaction overrides; separate class override/restore;
DeepSeek inherited compaction override/restore with OpenCode sibling isolation;
compaction success, Unsupported and non-Unsupported errors; login/effort-derived
values and dynamic executable resolution at the correct boundary. Mutants that
freeze compact support or bypass overrides must fail. Cold tests concern the
leaf/declared queries, not a falsely inert full response. The policy rehearsal
also changes one strategy compaction implementation and observes catalogue
support change without editing a support bit. Synthetic changes are restored.

Sweep-b confirms its provider extraction never started and has no G0, helper or
production edits; there is no extraction pin to preserve. Lead679 approves the
original design; this A2 boundary amendment still awaits A2 acceptance before
implementation. No tests/jobs or runtime edits have been performed.
