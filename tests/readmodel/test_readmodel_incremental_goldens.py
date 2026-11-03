"""The incremental import path against the S5 store goldens (design stage 0).

For each backend the incremental import supports, the corpus grows as in
``test_readmodel_goldens`` in two databases. Database A runs the production
import with the shadow admitted on every import, so it writes checkpoints.
Database B, from the same seed, imports the tail from A's checkpoint wherever
``decide`` allows it and runs a full import otherwise. After every step:

- A's rows equal the store golden: running the shadow changes no message row;
- B's rows equal A's: resuming from a checkpoint gives the full import's rows;
- the decision taken at each step, and the shadow's verdict, equal their golden.

The pinned verdicts include a false ``diverge:changed_rows`` (claude, codex,
step ``complete``): a tail turn within reach of a prefix turn is attributed
to it by time (``TurnIndex.trace_at``), so ``stamp_turn`` moves that turn's
summary off a prefix row. The applied tail does the same (B equals A), but
``_Shadow._compare`` counts only the planned rows, not the plan's recorded
UPDATEs, as changed.

The cut-by-cut variant does the same at every record boundary and inside every
record. It checks equality only and pins the decisions.

Characterization only: nothing serves the incremental result in production
(docs/architecture/readmodel-incremental-import.md).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from lib import db, eventlog, message_store, message_writes
from lib import transcript_incremental as inc

from tests.readmodel import corpus, world
from tests.readmodel.test_readmodel_goldens import GOLDENS, _check

INCREMENTAL = [b for b in corpus.BACKENDS if b in inc.SUPPORTED_BACKENDS]


@pytest.fixture
def importer(monkeypatch):
    conf = dict(inc._DEFAULTS, enabled=True, sample_every=1,
                max_cpu_seconds_per_hour=1e9, max_load_per_cpu=1e9,
                max_io_pressure=1e9, max_memory_pressure=1e9)
    monkeypatch.setattr(inc, "settings", lambda: dict(conf))
    monkeypatch.setattr(inc, "BUDGET", inc.ShadowBudget())
    monkeypatch.setattr(message_store, "IMPORT_COMMIT_EVERY", 0)
    monkeypatch.setattr(message_store, "IMPORT_WRITE_BUDGET_SECONDS", 1e9)
    monkeypatch.setattr(message_store, "IMPORT_WRITER_YIELD_SECONDS", 0)
    verdicts: list[str] = []
    real_emit = eventlog.emit

    def emit(source, event, **kw):
        if event == "import_shadow":
            verdicts.append(kw["detail"]["verdict"])
        return real_emit(source, event, **kw)
    monkeypatch.setattr(eventlog, "emit", emit)
    clock = world.Clock()
    clock.verdicts = verdicts
    monkeypatch.setattr(message_writes, "now_ms", clock)
    return clock


def _outcome(work) -> str:
    try:
        work()
    except Exception as exc:  # noqa: BLE001 - pinned like the store goldens
        return f"{type(exc).__name__}: {exc}"
    return "ok"


class Pair:
    """Database A (production import, shadow on) and B (incremental)."""

    def __init__(self, tmp_path: pathlib.Path, backend: str, clock: world.Clock):
        self.backend, self.clock = backend, clock
        self.a, self.b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
        for path in (self.a, self.b):
            world.prepare(path, backend)
            world.seed(backend, clock)

    def step(self, transcript: pathlib.Path) -> dict:
        self.clock.ms += 1000
        db.reset_for_tests(self.a)
        checkpoint = inc.load_checkpoint(db.conn(), corpus.AGENT, corpus.SESSION, str(transcript))
        a_outcome = _outcome(lambda: inc.import_transcript(
            backend=self.backend, path=transcript, agent_id=corpus.AGENT,
            backend_session_id=corpus.SESSION))
        a_rows = world.snapshot(transcript)
        db.reset_for_tests(self.b)
        ident = inc.identity(transcript)
        decision = inc.decide(checkpoint, self.backend, transcript, ident,
                              now_ms=checkpoint.audited_at if checkpoint else 0,
                              audit_every=10**9, audit_seconds=1e12,
                              max_tail_bytes=1 << 30)
        if decision:
            b_outcome = _outcome(lambda: message_store.store_transcript_turns(
                agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
                source_file=str(transcript),
                turns=inc.parse_range(self.backend, transcript, 0, ident.size)))
        else:
            b_outcome = _outcome(lambda: message_writes.store_transcript_tail(
                agent_id=corpus.AGENT, backend_session_id=corpus.SESSION,
                source_file=str(transcript),
                turns=inc.parse_range(self.backend, transcript, checkpoint.boundary_offset,
                                      ident.size),
                start_seq=checkpoint.boundary_seq, carry=checkpoint.store_carry))
        b_rows = world.snapshot(transcript)
        return {"decision": decision or "incremental", "a": a_outcome, "b": b_outcome,
                "a_rows": a_rows, "b_rows": b_rows}


@pytest.mark.parametrize("backend", INCREMENTAL)
def test_each_growth_step_resumed_from_a_checkpoint_matches_the_store_golden(
        tmp_path, backend, importer):
    golden = json.loads((GOLDENS / f"{backend}-store.json").read_text(encoding="utf-8"))
    pair = Pair(tmp_path, backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    decisions = []
    for expected, step in zip(golden["steps"], corpus.GROWTH, strict=True):
        corpus.apply(transcript, backend, step)
        got = pair.step(transcript)
        assert {"step": step, "import": got["a"], **got["a_rows"]} == expected, \
            f"{backend} {step}: the shadow changed the full import's rows"
        assert (got["b"], got["b_rows"]) == (got["a"], got["a_rows"]), \
            f"{backend} {step}: the incremental path ({got['decision']}) differs"
        decisions.append({"step": step, "decision": got["decision"]})
    _check(f"{backend}-incremental-decisions",
           {"backend": backend, "steps": decisions, "verdicts": importer.verdicts})


@pytest.mark.parametrize("backend", INCREMENTAL)
def test_cut_by_cut_growth_resumed_from_checkpoints_matches_the_full_import(
        tmp_path, backend, importer):
    data, cuts = corpus.jsonl_cuts(backend)
    pair = Pair(tmp_path, backend, importer)
    transcript = tmp_path / f"{backend}.jsonl"
    transcript.write_bytes(b"")
    decisions: dict[str, int] = {}
    for index, cut in enumerate(cuts):
        with open(transcript, "ab") as f:
            f.write(data[f.tell():cut])
        got = pair.step(transcript)
        assert (got["b"], got["b_rows"]) == (got["a"], got["a_rows"]), \
            f"{backend} cut {index} at byte {cut} ({got['decision']})"
        decisions[got["decision"]] = decisions.get(got["decision"], 0) + 1
    _check(f"{backend}-incremental-cuts",
           {"backend": backend, "cuts": len(cuts), "decisions": decisions})

