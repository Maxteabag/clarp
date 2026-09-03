# Contract

Machine-checkable core client contract. `docs/protocol.md` is the prose;
these files are the parts a computer can verify.

- `schemas/` — hand-written JSON Schema (draft 2020-12) for the eight core
  endpoints plus `sse.json` for the eleven core event types. Every schema
  allows additional properties: the core is additive-only and clients must
  ignore what they do not know.
- `fixtures/` — golden sync/delivery/audio scenarios: JSON steps plus the
  expected end state, seeded from bugs both clients already hit. The vitest
  runner (`tests/contract/fixtures.test.js`) feeds them through the pure
  modules (`static/lib/conversation-sync.js`, `delivery.js`, `protocol.js`);
  `tests/contract/test_fixtures_validate.py` checks the embedded payloads
  against `schemas/` so fixtures cannot describe a server that does not
  exist.
- `ios/` — `sync-fixtures.sh` copies the fixtures into the clarp-ios
  CoreBehaviorTests target (separate repo, separate PR).
- `reference-client/` — minimal working client (phase 4).
