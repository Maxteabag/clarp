# Conformance: the Rust Host against the Python Host (retired)

This directory held the check that the Rust Host answers every client-visible request and event
as the Python Host did: the same client behaviour run against both Hosts, everything a client can
see compared field by field. That check is done and recorded (criterion 2 of the rebuild), and
the Rust Host has moved past that reference. The driver, its corpus and its planted changes were
removed with the Python Host; git history keeps them.

What stays, as the record:

| File | What it is |
|---|---|
| `golden/`, `golden-again/` | What the Python Host answered, normalized, one file per scenario, recorded twice in the same run. A Host test reads `golden/who-is-there.json` |
| `intended.txt` | Ids of differences that were meant to be there (the reasoning is in `../docs/differences.md`) |
| `awaited.txt` | Ids of differences that were owed when the check was retired |

The fixtures under `server-rs/crates/*/tests/fixtures` whose header names a `server-rs/tools/*-vectors.py`
were generated from the Python Host, now retired; those generators were removed with it, and the
fixtures stay as the Rust tests read them.
