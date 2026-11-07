877 source-only test preparation
==============================
No local execution. Only a captain-authorized buildbox window may run these.

Primary boundary: tests/readmodel/test_store_txn_characterization.py, new
current-input-normalization independent SQLite writer witness. Baseline780 must
fail its three modes with real SQLITE_BUSY; setup or unrelated failure is not RED.
Candidate must retain IDs/products and allow every current-input witness write.
Persisted candidate raw text differs from current markup, so DB-candidate cleaning
remains protected and does not trigger the current-input witness.

Product oracle: use this exact product_capture plugin and final characterization
file bytes in isolated baseline780 and candidate source roots (old test file
replaced by exact final test source, no production substitution). Run sequentially
with -n0 and -p tools.refactor.ship877.product_capture, isolated HOME/DB/temp,
SHIP877_PRODUCTS separate output directories. Freeze wall time before fixtures;
clock/batch characterization fixture is identical. All schema/tables/columns/raw
values/multiplicity and sqlite_sequence captured, including message_clock, turns,
head and side-effect tables. Compare artifacts BY exact nodeid/hash, bytes only;
no ignored columns, row-ID remapping, timestamp scrubbing or empty-directory pass.
Report differing raw fields safely (synthetic only), source hashes/tool versions,
actual pytest exits and capture errors. Expected baseline witness failures remain
failures and do not qualify passing product/control tests. Capture does not cover
JSONL or external effect streams; existing real effect spies/controls retain
assertions and output comparison must identify that limit.

C-required extra controls still to add/source review before dispatch: provenance
change at locked admission, explicit empty/legacy helper calls, shifted/parked and
after-yield mutation coverage selection. Existing characterization scenarios are
the primary owners for branch products, no mocked twin-choice returns. No executed
RED/GREEN or byte equality yet, no independent comparison gate result implied.
