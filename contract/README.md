# Contract

Machine-checkable core client contract. `docs/protocol.md` is the prose;
these files are the parts a computer can verify.

- `schemas/` — hand-written JSON Schema (draft 2020-12) for the eight core
  endpoints plus `sse.json` for the eleven core event types. Every schema
  allows additional properties: the core is additive-only and clients must
  ignore what they do not know.
- `fixtures/` — golden sync/delivery/audio scenarios (phase 3).
- `reference-client/` — minimal working client (phase 4).

`tests/contract/` runs the conformance suite against the real server. A
shape disagreement fails there: fix the server or the prose, not the schema.
