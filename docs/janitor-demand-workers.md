# Janitors that serve requests

The message delegator and tool explainer use the same persistent agent identity,
Janitor configuration, trigger attachments and run history as task maintenance.
The existing routing and explanation services are execution adapters. They read
the selected Janitor's frozen settings instead of owning a second model or enable
flag. No worker agent has to report extra bookkeeping.

## Ownership and triggers

`janitor_builtins.ensure_builtins` runs during Host initialization, importing
legacy routing policy once. It never overwrites later user configuration or
resurrects a removed identity. Routing remains opt-in on a fresh Host; defaults
use Codex Spark 5.3. Task labels keep their ordinary managed turn executor.

`routing-requested@1` and `tool-explanation-requested@1` are immutable catalog
entities. Attach compatible custom Janitors to reuse them. Select the active
subscriber by job, enabled attachment and source-agent scope. Overlapping active
owners of the same effect are rejected; separate responsibilities may coexist.
Ephemeral demand workers are excluded from chat routing and runtime restoration.

## One request, one guarded run

1. Resolve the trusted source agent on the Host, then select a subscriber.
2. Call `begin_run` using stable request identity and bounded metadata. Freeze
   model, backend, provider, options, scope, attachment version and generation.
3. Atomically claim the run before invoking the provider. Repeated requests
   observe the same claim or receipt; they must not invoke another paid model.
4. Validate the frozen configuration before execution and before applying effects.
   No provider call or runtime RPC may hold the SQLite writer transaction.
5. Complete with a bounded durable result receipt. Explanation publication and
   Janitor receipt acceptance share a transaction so stale output cannot enter
   the cache. Route dispatch retains the existing message admission identity.

Demand runs have a 180-second expiry, allowing both bounded routing passes.
Recovery retires expired claims rather than interrupting a live request owner.
Check expiry when replaying the same request as well as admitting new requests.
Pause and configuration changes fence effects immediately; a still-running
provider claim remains visible until completion or expiry. It cannot be released
to ordinary chat while that claim is live.

Run history contains counts, stable hashes and short outcomes, not copied raw
utterances or tool payloads. Existing explanation redaction, viewport demand
leases, batching and expiry cleanup remain in the adapter. Cache identity
includes the Janitor configuration and trusted source target; batches must not
mix owners, option sets or source scopes.

## Options and compatibility

Job templates expose typed `options` descriptors, supported providers and
triggers. Configurations store the values; enabling is solely `Janitor.enabled`.
The old orchestrator API is a compatibility adapter to these same values.
New clients remove the independent Chat tool-detail and Host Orchestrator editors.

The explainer's legacy device detail preference is adopted once through
`POST /janitors/{session}/adopt-options`. This imports only an unset `detail_level`,
preserves enabled/paused state and fences the old generation. The first saved
value wins across devices. After adoption, caller-requested detail cannot
override the Janitor's option. Older unconfigured clients retain their existing
behavior until adoption; custom Janitors use their configured job defaults.

Native clients preserve unknown options and full attachment drafts when switching
responsibilities. Runtime availability gates managed creation/enabling only;
inspection, pause and ephemeral demand work remain available.

## Verification

Write failing behavior tests before the implementation, using isolated SQLite
and fake providers. Cover duplicate and expired retries, scoped replacements,
pause/reconfiguration while a provider runs, atomic publication, restart recovery,
typed option validation, one-time migration and return-to-chat handoff. Test HTTP
authentication and canonical source selection as well as store functions.

Portable native tests cover option decoding, preserved drafts and capability
decisions. Linux parsing and logic tests do not verify rendered SwiftUI or an
Xcode app build; report that boundary when the user chooses Linux-only checks.
