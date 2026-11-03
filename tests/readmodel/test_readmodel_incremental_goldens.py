"""The incremental import path against the S5 store goldens (design stage 0).

For each backend the incremental import supports, the corpus grows as in
``test_readmodel_goldens`` in two databases. Database A runs the production
import with the shadow admitted on every import, so it writes checkpoints.
Database B, from the same seed, imports the tail from A's checkpoint wherever
``decide`` allows it and runs a full import otherwise. After every step:

- A's rows equal the store golden: running the shadow changes no message row;
- B's rows equal A's: resuming from a checkpoint gives the full import's rows;
- the decision taken at each step, and the shadow's verdict, equal their golden.

The shadow's verdicts carry no divergence (RM-N3: its comparison used to
report a turn summary re-stamped off a row the plan never saw change).

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



@pytest.mark.parametrize("backend", INCREMENTAL)
def test_the_shadow_reports_no_divergence_where_the_tail_import_matches(
        tmp_path, backend, importer):
    """RM-N3: a tail turn attributed by time to a prefix turn re-stamps that
    turn's summary off a row the plan never saw change. The tail import does
    the same, so the shadow must not report it as a divergence."""
    pair = Pair(tmp_path, backend, importer)
    transcript = corpus.transcript_path(tmp_path, backend)
    for step in corpus.GROWTH:
        corpus.apply(transcript, backend, step)
        got = pair.step(transcript)
        assert got["b_rows"] == got["a_rows"]
    assert not [v for v in importer.verdicts if v.startswith("diverge")], importer.verdicts


def _corrupt_after_stamp(monkeypatch, *, trace: str, pick):
    """During the authoritative (locked) import only, after it re-stamps
    ``trace``, rewrite one more row of that trace: an extra write the plan
    did not predict."""
    real = message_writes.stamp_turn

    def stamp_turn(database, agent_id, trace_id, next_revision):
        out = real(database, agent_id, trace_id, next_revision)
        if trace_id == trace and getattr(database, "locked", False) \
                and isinstance(database, message_writes._ImportWriter):
            rows = database.execute(
                "SELECT message_id, turn_json FROM messages WHERE agent_id=? AND trace_id=?"
                " AND source_file NOT LIKE 'client:%' ORDER BY seq",
                (agent_id, trace_id)).fetchall()
            victim = pick(rows)
            database.execute("UPDATE messages SET text = text || ' [planted]', revision=? "
                             "WHERE message_id=?", (next_revision(database), victim))
        return out
    monkeypatch.setattr(message_writes, "stamp_turn", stamp_turn)


@pytest.mark.parametrize("victim", ["another_row_of_the_turn", "the_row_that_lost_the_summary"])
def test_an_extra_write_on_a_restamped_turn_still_diverges(tmp_path, importer, monkeypatch, victim):
    """RM-N3 negative controls (reviewer B): explaining a re-stamp must not
    hide any other change to a row of the same turn."""
    pick = {"another_row_of_the_turn": lambda rows: rows[0]["message_id"],
            "the_row_that_lost_the_summary": lambda rows: next(
                r["message_id"] for r in rows if r["message_id"] == "final-1")}[victim]
    world.prepare(tmp_path / "state.sqlite", "claude")
    world.seed("claude", importer)
    transcript = corpus.transcript_path(tmp_path, "claude")
    for step in ("grow", "partial"):
        corpus.apply(transcript, "claude", step)
        importer.ms += 1000
        inc.import_transcript(backend="claude", path=transcript, agent_id=corpus.AGENT,
                              backend_session_id=corpus.SESSION)
    _corrupt_after_stamp(monkeypatch, trace="tr-1", pick=pick)
    corpus.apply(transcript, "claude", "complete")
    importer.ms += 1000
    inc.import_transcript(backend="claude", path=transcript, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)
    assert importer.verdicts[-1] == "diverge:changed_rows", importer.verdicts


def test_the_comparator_flags_an_unpredicted_write_to_a_row_of_a_planned_turn(tmp_path):
    """Comparator-level control, after reviewer B's probe: a prefix row of a
    turn the plan changed, rewritten without losing a summary, diverges."""
    from types import SimpleNamespace
    world.prepare(tmp_path / "state.sqlite", "claude")
    path = tmp_path / "synthetic.jsonl"
    prefix, tail = message_store.store_transcript_turns(
        agent_id=corpus.AGENT, backend_session_id=corpus.SESSION, source_file=str(path),
        turns=[{"role": "assistant", "text": "synthetic prefix", "timestamp": corpus.iso(0, 0)},
               {"role": "user", "text": "synthetic tail", "timestamp": corpus.iso(0, 1)}])
    c = db.conn()
    shadow = inc._Shadow.__new__(inc._Shadow)
    shadow.agent_id, shadow.backend_session_id, shadow.path = corpus.AGENT, corpus.SESSION, path
    shadow.rows_before = {r["message_id"]: inc._restamp_view(r) for r in c.execute(
        f"SELECT {inc._RESTAMP_COLUMNS} FROM messages WHERE agent_id=?", (corpus.AGENT,))}
    shadow.ids_before = set(shadow.rows_before)
    floor = 10_000
    c.execute("UPDATE messages SET trace_id='tr-x', revision=?, text='synthetic corruption' "
              "WHERE message_id=?", (floor + 1, prefix["id"]))
    c.execute("UPDATE messages SET trace_id='tr-x', revision=? WHERE message_id=?",
              (floor + 2, tail["id"]))
    c.commit()
    shadow.old = SimpleNamespace(boundary_seq=1)
    shadow.plan = {"revision_floor": floor, "out": [{**tail, "revision": floor + 2}],
                   "updated": set(), "deleted": set()}
    shadow.mismatches, shadow.out_seqs, shadow.detail = [], [0, 1], {}
    shadow._compare([{**prefix, "revision": floor}, {**tail, "revision": floor + 2}])
    assert shadow.verdict == "diverge:changed_rows", shadow.verdict


# ---- RM-N3 residual R1: the shadow checks turn summaries ---------------------

def _summary_world(tmp_path, *, settled: bool | None):
    """Three transcript rows of trace tr-s, stamped as stamp_turn would.
    ``settled`` None: no turns row at all."""
    world.prepare(tmp_path / "state.sqlite", "claude")
    c = db.conn()
    if settled is not None:
        start = int(corpus.epoch(0, 0) * 1000)
        c.execute("""INSERT INTO turns(agent_id, source, trace_id, started_at, ended_at,
                     settled_at, outcome) VALUES (?, 'pwa', 'tr-s', ?, ?, ?, ?)""",
                  (corpus.AGENT, start, start + 6000, start + 6000 if settled else None,
                   "completed" if settled else None))
    for seq in range(3):
        c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                     seq, role, timestamp, text, updated_at, revision, trace_id)
                     VALUES (?, ?, ?, '/t', ?, 'assistant', ?, ?, 1, 1, 'tr-s')""",
                  (f"m{seq}", corpus.AGENT, corpus.SESSION, seq, corpus.iso(0, seq + 1), f"r{seq}"))
    message_writes.stamp_turn(c, corpus.AGENT, "tr-s", message_writes._next_revision)
    c.commit()
    return c


def _set_summary(c, message_id, value):
    c.execute("UPDATE messages SET turn_json=? WHERE message_id=?", (value, message_id))
    c.commit()


@pytest.mark.parametrize("settled", [True, False, None])
def test_a_stamped_turn_is_consistent(tmp_path, settled):
    c = _summary_world(tmp_path, settled=settled)
    assert inc._summary_consistent(c, corpus.AGENT, "tr-s")
    holders = [r[0] for r in c.execute(
        "SELECT message_id FROM messages WHERE turn_json IS NOT NULL ORDER BY seq")]
    assert holders == (["m2"] if settled else [])


@pytest.mark.parametrize("control", ["corrupted_holder", "second_holder", "holder_on_wrong_row",
                                     "summary_on_an_active_turn", "summary_without_a_turns_row"])
def test_each_summary_inconsistency_is_caught(tmp_path, control):
    settled = {"summary_on_an_active_turn": False, "summary_without_a_turns_row": None}.get(control, True)
    c = _summary_world(tmp_path, settled=settled)
    stored = c.execute("SELECT turn_json FROM messages WHERE message_id='m2'").fetchone()[0]
    if control == "corrupted_holder":
        _set_summary(c, "m2", stored.replace('"tool_count":0', '"tool_count":7'))
    elif control == "second_holder":
        _set_summary(c, "m0", stored)
    elif control == "holder_on_wrong_row":
        _set_summary(c, "m1", stored)
        _set_summary(c, "m2", None)
    else:
        _set_summary(c, "m1", '{"turn_id":"tr-s","status":"completed"}')
    assert not inc._summary_consistent(c, corpus.AGENT, "tr-s")


def test_the_shadow_reports_a_corrupted_summary(tmp_path, importer, monkeypatch):
    """End to end: the authoritative import stores a wrong summary for a turn
    it stamped; the shadow's verdict names it."""
    real = message_writes.stamp_turn

    def stamp_turn(database, agent_id, trace_id, next_revision):
        out = real(database, agent_id, trace_id, next_revision)
        if trace_id == "tr-1" and isinstance(database, message_writes._ImportWriter) \
                and database.locked:
            database.execute("UPDATE messages SET turn_json='{\"corrupt\":1}', revision=? "
                             "WHERE agent_id=? AND trace_id=? AND turn_json IS NOT NULL",
                             (next_revision(database), agent_id, trace_id))
        return out
    world.prepare(tmp_path / "state.sqlite", "claude")
    world.seed("claude", importer)
    transcript = corpus.transcript_path(tmp_path, "claude")
    for step in ("grow", "partial"):
        corpus.apply(transcript, "claude", step)
        importer.ms += 1000
        inc.import_transcript(backend="claude", path=transcript, agent_id=corpus.AGENT,
                              backend_session_id=corpus.SESSION)
    monkeypatch.setattr(message_writes, "stamp_turn", stamp_turn)
    corpus.apply(transcript, "claude", "complete")
    importer.ms += 1000
    inc.import_transcript(backend="claude", path=transcript, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)
    assert "summary" in importer.verdicts[-1], importer.verdicts


def test_the_shadow_checks_a_turn_whose_summary_holder_is_in_another_session(
        tmp_path, importer, monkeypatch):
    """Reviewer C's control (R1): the turn's summary sits on a later row of
    the same agent in another backend session; the import's stamp rewrites
    only that row. The shadow must still check the turn."""
    world.prepare(tmp_path / "state.sqlite", "claude")
    world.seed("claude", importer)
    path = corpus.transcript_path(tmp_path, "claude")
    corpus.apply(path, "claude", "grow")
    inc.import_transcript(backend="claude", path=path, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)
    c = db.conn()
    c.execute("""INSERT INTO messages(message_id, agent_id, backend_session_id, source_file,
                 seq, role, timestamp, text, updated_at, revision, trace_id)
                 VALUES ('foreign', ?, 'other-session', '/foreign', 100, 'assistant', ?,
                         'synthetic foreign reply', 1, ?, 'tr-1')""",
              (corpus.AGENT, corpus.iso(1, 6), message_writes._next_revision(c)))
    message_writes.stamp_turn(c, corpus.AGENT, "tr-1", message_writes._next_revision)
    c.commit()
    with path.open("ab") as f:          # a fresh checkpoint after the foreign holder
        f.write(b"\n")
    importer.ms += 1000
    inc.import_transcript(backend="claude", path=path, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)
    c.execute("UPDATE turns SET outcome='interrupted' WHERE trace_id='tr-1'")
    c.commit()
    real = message_writes.stamp_turn
    corrupted = []

    def stamp(database, agent_id, trace_id, next_revision):
        result = real(database, agent_id, trace_id, next_revision)
        if trace_id == "tr-1" and isinstance(database, message_writes._ImportWriter) \
                and database.locked:
            assert database.execute("SELECT turn_json FROM messages WHERE message_id='foreign'"
                                    ).fetchone()["turn_json"] is not None
            database.execute("UPDATE messages SET turn_json='{}', revision=? "
                             "WHERE message_id='foreign'", (next_revision(database),))
            corrupted.append(True)
        return result
    monkeypatch.setattr(message_writes, "stamp_turn", stamp)
    with path.open("ab") as f:
        f.write(b"\n")
    importer.ms += 1000
    inc.import_transcript(backend="claude", path=path, agent_id=corpus.AGENT,
                          backend_session_id=corpus.SESSION)
    assert corrupted, "the control did not reach the locked foreign summary stamp"
    assert not inc._summary_consistent(c, corpus.AGENT, "tr-1")
    assert "summary" in importer.verdicts[-1], importer.verdicts
