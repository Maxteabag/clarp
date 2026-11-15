# Conformance: the Rust Host against the Python Host

The Rust Host joins the fleet on the contract the apps already speak. This directory is how
that is checked: the same client behaviour is run against both Hosts, and everything a client
can see is compared: every response (status, contract headers, body, field by field) and every
event of the stream (order within a producer, wire id, payload).

It is the replay harness of `tests/replay` run across implementations. Its capture, its
normalizer (ids, clocks, ports become surrogates), its differ and its fake Claude are used as
they are; what is new is a corpus that only does what a client can do, and a second Host.

## The pieces

| File | What it is |
|---|---|
| `contract_scenarios.py` | The corpus. A scenario is steps; HTTP steps are compared, waits watch the snapshot and the conversation the way a client would |
| `contract_plants.py` | Deliberate changes to a copy of the Python server that the suite must see. A reviewer adds their own |
| `conform.py` | The driver: `record`, `plants`, `compare`, `cross` |
| `golden/` | What the Python Host answered, normalized, one file per scenario. Written by the workflow, committed by hand |
| `intended.txt` | Ids of differences that are meant to be there (the reasoning is in `../docs/differences.md`) |

## Running it

Recording and planting start Python test Hosts. Those run only where the replay harness may
run: contained, in GitHub Actions, never on a machine with a live Host.

```bash
gh workflow run conformance.yml -R Maxteabag/clarp --ref <branch>     # everything, contained
gh run download <run> -R Maxteabag/clarp -n conformance-report         # report.txt, golden/, rust/
```

Comparing the Rust Host with what is written down starts only the Rust Host, so it is the
everyday loop, on the build machine:

```bash
server-rs/tools/build conform                       # the whole corpus
server-rs/tools/build conform --scenarios text-turn # one scenario
```

## Reading the report

A difference is `id class scenario / channel / locator / kind: base -> head`, base being the
Python Host. Every difference is exactly one of:

1. **Our bug.** Fix the Rust Host.
2. **Intended.** Write it into `../docs/differences.md` with the reason, then add its id to
   `intended.txt`. The id is a hash of the place and both values, so it covers that exact
   difference and nothing else.
3. **A defect of the reference.** Write it into `../docs/reference-defects.md` and report it
   to whoever owns the Python Host's releases.

The suite counts only while its proof holds: the reference run twice gives no difference, and
every planted change is caught by a difference of the kind it names.

## When the Python Host changes

`golden/` is the Python Host at the commit the workflow ran on. After a change to the contract
(a bump of `HOST_CONTRACT`), run the workflow again and commit the new `golden/`.
