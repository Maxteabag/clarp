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
| `contract_scenarios.py` | The corpus. A scenario is steps; HTTP steps are compared, waits watch the snapshot and the conversation the way a client would. A step sends JSON, or a body as it is (`raw`: a file an agent publishes) |
| `contract_plants.py` | Deliberate changes to a copy of the Python server that the suite must see. A reviewer adds their own |
| `conform.py` | The driver: `record`, `plants`, `compare`, `cross` |
| `golden/`, `golden-again/` | What the Python Host answered, normalized, one file per scenario, recorded twice in the same run. Written by the workflow, committed by hand |
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

The suite counts only while its proof holds: every planted change is caught by a difference
of the kind it names.

The reference is recorded twice in every run. Where the two recordings differ, the Python
Host itself answers in more than one way: the report says so in its first part, the defect
goes into `../docs/reference-defects.md`, and both answers are kept. At such a place the Rust
Host conforms when it gives one of the two answers; the report lists those places. A place
where it differs from both recordings is a difference, as everywhere else. Two races in one
scenario give four combinations and the two recordings hold two of them, which is why this is
decided place by place and not scenario by scenario (run 43703416886: in `turn-timer` the
Python Host showed the agent's model in one recording and none in the other, and wrote two
rows in either order).

## What is compared as what a client may rely on

Some things are each Host's own choice, and comparing their values would only compare the
choices. They are compared as what a client can rely on (`core` in `conform.py`, the table in
`../docs/differences.md`): a conversation's revisions by the cursors a client holds (a head as
its place among the cursors the capture handed out, a row's revision as how many of them it is
newer than), the ids of the rows an agent wrote as the order they are first read in, the ids of
what agents make as the same name wherever they are written, the id of a file an agent
published as the order a client first learns of it (in the path it is fetched by and in the
Markdown that shows it, too), a Host's version as "a version".
A queue's revision is compared as the number it is. Fields and events of features the Rust Host
does not have yet are left out of both sides.

The start of the turn that runs (`turn_started_at` in the snapshot) is what an app counts a
running turn from (issue 354). It is a reading of a clock, and is compared as what it is a
reading of (`TurnClock` in `conform.py`): which turn of the capture it belongs to, so that the
same turn has the same start however often it is read and the next turn has another, and
which message this client had last sent when the turn began. The scenarios `turn-timer` and
`turn-timer-after-stop` read the snapshot while a turn runs, while more is sent, and after;
no other scenario reads it while an agent is busy.

## When the Python Host changes

`golden/` and `golden-again/` are the Python Host at the commit the workflow ran on. After a
change to the contract (a bump of `HOST_CONTRACT`), or to the corpus or the view in
`conform.py`, run the workflow again and commit what it recorded.
