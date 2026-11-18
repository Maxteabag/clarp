# Native form wire vectors

Run `python tests/fixtures/render_spec/scenarios.py > vectors.json` from the repository root. The generator uses the Python standard library. Each scenario starts with empty storage and a capable client. Each step contains the request, HTTP status, expected receipt or refusal when applicable, resulting artifact status and submission count. A successful publish includes the single-read content identity. The list marker adds the row’s update time. Successful submissions include the exact agent prompt and payload hash.

The catalog and refusal sentences ship in `contract/catalogs/`. The generator also exports the catalog digest, canonical UTF-8 examples, and client headers with their expected result at the development threshold. The production threshold is unset.

Publish checks the payload fields and canonical size, catalog and version, spec and initial-state limits, answer schema, component properties and tree, initial values, then the submit declaration. Submission checks client capability first. It checks request fields, ID, canonical answer size, answer envelope and audio preference before checking for a receipt. A new ID is checked against terminal state, current version, declared intent and the closed answer state, in that order. Each refusal returns the first failing rule. Paths inside a payload are relative to the payload.
