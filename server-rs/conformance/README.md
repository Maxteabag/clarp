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
| `contract_scenarios.py` | The corpus. A scenario is steps; HTTP steps are compared, waits watch the snapshot and the conversation the way a client would. A step sends JSON, or a body as it is (`raw`: a file an agent publishes or a user attaches); it may say when it is sent (`body_now`: an app's report of itself) and keep something of its answer for a later step (`save`: a cursor) |
| `contract_plants.py` | Deliberate changes to a copy of the Python server that the suite must see. A reviewer adds their own |
| `conform.py` | The driver: `record`, `plants`, `compare`, `cross`; `selftest` tries its own rule for a reference that wavers, and starts no Host |
| `golden/`, `golden-again/` | What the Python Host answered, normalized, one file per scenario, recorded twice in the same run. Written by the workflow, committed by hand. `golden-more/` is beside them when a run recorded a scenario a third time (below): it is committed with them, and removed when the next run made none |
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
of the kind it names, in a scenario that ran to its end. A scenario that did not run to its
end under a plant (the planted Python Host timed out in it) differs from the recording in
every way and proves nothing, for or against: its differences are not counted, it is listed
as a `RUN ERROR`, and it does not undo what the other scenarios showed. Before 16 Nov 2026 one
such scenario made a plant count as missed that fourteen differences elsewhere showed (run
43820942327, first attempt: `question` timed out under `state-transition`); a run error of the
two recordings the Rust Host is compared with still fails the run, as it must.

The reference is recorded twice in every run. Where the two recordings differ, the Python
Host itself answers in more than one way: the report says so in its first part, the defect
goes into `../docs/reference-defects.md`, and both answers are kept. At such a place the Rust
Host conforms when it gives one of the two answers; the report lists those places. A place
where it differs from both recordings is a difference, as everywhere else. Two races in one
scenario give four combinations and the two recordings hold two of them, which is why this is
decided place by place and not scenario by scenario (run 43703416886: in `turn-timer` the
Python Host showed the agent's model in one recording and none in the other, and wrote two
rows in either order).

One defect the two recordings can share. In the first turn of a conversation the reference
sometimes replaces the row a reply was written into while the turn ran by the transcript's row:
the reply's notification then names a row the conversation does not have, and the turn's summary
is on no row (defect R19 in `../docs/reference-defects.md`; in some runs, in some scenarios).
Two recordings that both have it agree with each other, and neither is the answer the reference
gives when it does not: run 43985072956 ended with two differences in `who-is-told` for that
reason alone, where the run before it, on the same crates and the same corpus, had none. A
recording says of itself that it has the defect: one of its notifications names no row though
the conversation, as that capture read it, has the answer to the same message. No other Host is
looked at to tell it. So a scenario whose every recording carries that mark is recorded again,
up to three times, and the first answer without the mark is kept as one more recording
(`golden-more/`): one more of the ways the reference answers, held to the same rule as the
other two. Where the mark is there every time, that is how the reference answers, nothing is
added, and the differences are differences. The report says which it was. The rule is tried on
captures made for it before every run that records (`conform.py selftest`: a sound recording is
not marked, one that never read the answer is not marked, a sound third answer is kept and
nothing asked after it, a mark every time adds nothing, a try that did not run to its end is no
answer, and a scenario with one sound recording is not recorded again).

A test Host of the reference sometimes does not come to rest. On an agent's first turn the
Python Host can take the tool's end before it has written down that the turn began; it refuses
the end and then writes the beginning, and the agent stays busy (defect R43). It takes a tool
that answers within the instant its Host needs for its own bookkeeping, which the stand-in did:
in thirteen runs 25 of 122 recordings of the reference were left so, and it failed each of the
last three. Two things follow. The stand-in takes half a second before it does anything in
the first turn of a conversation, as every real tool does (`fake_claude.py`,
`CLARP_QA_ANSWER_AFTER`), for the reference and not for the Rust Host (see below), unless that turn is slow by its own mark and is there
to be stopped or queued behind (run 44101297003: stopped before it had written anything, such
a turn differed in 138 places in six scenarios); in no other turn, since a moment in every turn turned
another race of the reference the other way (run 44096945408: a reply that closes a later
turn was left without its mark in a quarter of such rows instead of a sixth, defect R6, and
the run differed at 21 places nobody had seen it differ at). And
a recording of the reference that still did not run a scenario to its end is asked again on a
fresh Host, as it was asked the first time, at most twice: the first answer that ran to its
end takes its place, and the report says every time it happened. A scenario the reference
never runs to its end still fails the run. The Rust Host is never asked again: a scenario it
does not run to its end is a run error. What a test Host left of such a scenario is kept with
the report, under `unfinished/`: the end of its log without the requests it answered, what its
stand-in tool did, and (the reference) the agent's last states. The rule is tried before every
run that records (`conform.py selftest`: a recording that ran to its end is not asked again, a
first answer that is whole takes the other's place and nothing beside it is touched, a second
is taken when the first is cut short again, and a scenario that is never whole stays cut short).

The Rust Host is asked without that moment. It does not have the race the moment is for, and
the moment showed a weakness of its own: it tells the state an agent is in when what there is
to read of its tool has been read, so a state the agent passed through within one read is not
told, and one that a read happens to end in is. The stand-in prints a turn in one breath; with
a moment before it the Host is already reading when the breath begins and may read it in two.
In run 44111344344 it did, in `tool-turns` (it told that the agent was using a tool) and in
`live-items` (that it was answering): 29 differences from a sequence it had given the same way
in every run before. Which states an app is told must not depend on that, and is to be mended
in the Host (`../REBUILD.md`); until then the suite does not lean on it
(`CLARP_QA_ANSWER_AFTER_RUST` gives the Rust Host a moment, to try it).

Four defects of the reference are told by their shape, not by the ids of their places. They
come and go with its clock, at places nobody can list, and two recordings can both have one
where no run had it before: then they agree with each other, the Rust Host differs from both,
and a register of ids lacks that place. On 17 Nov 2026 three runs failed so (44089630630 and
44093481589 on a place of R17 each, 44096945408 on thirteen of R6 and R12). A rule says where
such a difference lies, in which direction, and what the
two rows it lies in have to say of themselves (`SHAPES` in `conform.py`):

- **R6**, `turns[n].phase`, nothing where this Host says `final`: both rows are the reply that
  closes a completed turn, with the same words.
- **R12**, `agents[n].last_completed_message`: both rows are at rest (`done`), and what this Host
  gives is what the reference's own row names as the agent's last message.
- **R13**, `agents[n].model`, an empty text where this Host names a model: the reference shows
  that model for that agent somewhere else in what was recorded.
- **R17**, `agents[n].live_activity`, nothing where this Host has the line: both rows say `done`.

A rule holds only where the reference's row gives the defect away or this Host's row shows that
what it says is what the contract asks for; anything else at such a place is a difference like
any other, and so is the same difference the other way round. The report counts them apart
(`defect R6` beside `intended` and `UNEXPLAINED`). The rules are tried before every run
(`selftest`: 41 cases, of which 6 are the defects and 35 are what they must not take for them),
and were tried on the runs that were written down: in run 44096945408 they tell 8 places of R6
and 5 of R12 and leave 8 differences that are none of the four; in run 44101297003, whose 138
differences were this suite's own doing, they tell none. With nothing registered they tell the
places the register lists for these defects (run 44081827558: 7 of R6 and 9 of R12).

A run that was written down can be judged again without starting a Host, as the rules and the
register are now: `conform.py judge --golden <report>/golden --out <report>` reads the
recordings of the reference (`golden`, `golden-again`, `golden-more`) and what the Rust Host
answered (`rust/`).

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

`golden/` and `golden-again/` (and `golden-more/`, when a run made one) are the Python Host at the commit the workflow ran on. After a
change to the contract (a bump of `HOST_CONTRACT`), or to the corpus or the view in
`conform.py`, run the workflow again and commit what it recorded.
