# Contract

Machine-checkable core client contract. `docs/protocol.md` is the prose;
these files are the parts a computer can verify.

- `schemas/` — hand-written JSON Schema (draft 2020-12) for the nine core
  endpoints, `sse.json` for the eleven core event types, and
  `clips-recoverable.json` for the voice-recovery extension. Every schema
  allows additional properties: the core is additive-only and clients must
  ignore what they do not know.
- `fixtures/` — golden sync/delivery/audio scenarios: JSON steps plus the
  expected end state, seeded from bugs both clients already hit. The vitest
  runner (`tests/contract/fixtures.test.js`) feeds them through the pure
  modules (`static/lib/conversation-sync.js`, `delivery.js`, `protocol.js`).
  The PWA's conversation store (`web/src/stores/conversations.svelte.js`)
  is an adapter over the same reducer, so the fixtures test what ships;
  `tests/contract/test_fixtures_validate.py` checks the embedded payloads
  against `schemas/` so fixtures cannot describe a server that does not
  exist.
- `ios/` — `sync-fixtures.sh` copies the fixtures into the clarp-ios
  CoreBehaviorTests target (separate repo, separate PR).
  `--check DEST` compares content hashes and provenance. Fixtures may declare
  `clients` plus a `client_scope_reason` for platform-specific UI policy; other
  fixtures are shared. Native consumers must reject unsupported expectations,
  not count an ignored scenario as verified.
- `reference-client/` — `clarp-client.mjs`, a minimal working client
  (under 250 lines, no dependencies beyond global fetch).
  `tests/contract/test_reference_client.py` runs it through one turn
  against the harness. If it cannot stay small while passing, the
  contract is too complex.
