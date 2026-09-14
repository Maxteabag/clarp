# Audio Bookkeeper

The built-in `audio-bookkeeper` Janitor owns mechanical audio lifecycle observations.
It uses `{"executor":"deterministic","provider":"local"}` with no model or reasoning
budget. Primary agents still author semantic speech and may publish audio artifacts;
they never write this ledger or invoke a bookkeeping skill. No standalone audio
bookkeeping skill existed in the managed manifest at this migration. `clarp-audio`
is an artifact publisher, not a bookkeeping skill, and is retained.

SQLite triggers enqueue bounded producer/client lifecycle facts atomically with
`clips` insertion/status changes. The Host's existing Janitor tick drains up to32
observations per tick (2s default), under the same write transaction as authority
resolution, run/result creation and outbox completion. There is no provider call.
Disabling the Janitor preserves pending observations until enabled again. Upgrade
creates the new role enabled once; later user configuration is never overwritten.

Identity is `(Host database,clip_id,stage)`: this is the first observation of each
stage, **not a counter of playback attempts**. Clips retain their source agent,
turn/runtime IDs and trace reference in the private outbox; run history includes
bounded IDs and stage only, no speech text, filename, credential or provider error.
Existing audio transport and client status projections keep their contracts.
The ledger records reported stages, never claims acoustic output or that a user
heard speech. Historical clips are not backfilled; their future status transitions
are observed. A failed transaction leaves no partial receipt or completion mark.

Database migration84 installs the table/triggers and new demand-trigger definition.
Coordinate the version if another change claims84 before integration. Rollback to
older code stops draining new entries but existing clip behavior is unaffected;
retain the database table rather than deleting receipt history.

Validation: `python -m pytest -n 0 tests/unit/test_audio_bookkeeper.py
 tests/unit/test_janitor_builtins.py tests/unit/test_janitor_bootstrap.py
 tests/unit/test_janitor_options.py tests/unit/test_janitor_store.py
 tests/integration/test_clip_ack_contract.py` (one command).
Tests use fresh disposable databases and loopback Host processes with fake media;
no live Janitor configuration, provider spend or primary-agent dispatch.
