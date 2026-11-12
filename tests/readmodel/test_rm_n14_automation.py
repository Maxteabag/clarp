"""Sessions that hold an automation's /send row: a tail equals the full replay.

A frontier was never kept for a session with a /send row whose origin is not
user or agent (a heartbeat, a goal wake or schedule, a watcher event, a leader
tick, a maintenance run). Once a session had received one, every later import
of it was a full one: 83 of 176 live sessions on one Host, 40 percent of all
bytes read by imports there on 12 November 2026.

Each test runs the full-replay reference lane beside the candidate and
compares every product table after every tick, the heartbeat tables among
them. The automation's row is written the way /send writes it, into both
databases, with fixed times.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from lib.heartbeat import HEARTBEAT_OK, HEARTBEAT_PROMPT
from lib.team_leader import LEADER_NOOP, TICK_PROMPT
from tests.readmodel import corpus
from tests.readmodel import rm_n14_harness as harness
from tests.readmodel.test_rm_n14_equivalence import _clones

# origin of the /send row -> (the prompt as sent, the reply in the transcript)
AUTOMATIONS = {
    "heartbeat": (HEARTBEAT_PROMPT, HEARTBEAT_OK),
    "leader_tick": (TICK_PROMPT, LEADER_NOOP),
    "automation": ("Continue the goal: synthetic wake", "synthetic goal progress"),
    "watcher": ("Watcher event: synthetic build finished", "synthetic watcher answer"),
    "janitor": ("Maintenance: synthetic review", "synthetic maintenance answer"),
}


def _escaped(text: str) -> bytes:
    return json.dumps(text)[1:-1].encode()


def _turn(backend, ordinal, kind=None):
    """One complete turn; an automation's turn has its prompt and its reply."""
    data = harness.append_bytes(backend, ordinal)
    if kind is None:
        return data
    prompt, reply = AUTOMATIONS[kind]
    return (data.replace(f"synthetic request {ordinal}".encode(), _escaped(prompt), 1)
            .replace(f"synthetic answer {ordinal}".encode(), _escaped(reply), 1))


def _sent(databases, kind, number, *, at_ordinal):
    """The automation's /send row and the turn it opens, just before the
    provider writes turn ``at_ordinal``."""
    prompt, _reply = AUTOMATIONS[kind]
    at = harness.BASE_MS + at_ordinal * 1000 - 500
    stamp = (dt.datetime.fromtimestamp(at / 1000, dt.timezone.utc)
             .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    for database in databases:
        with sqlite3.connect(database) as other:
            revision = other.execute(
                "UPDATE message_clock SET revision=revision+1 WHERE singleton=0 RETURNING revision"
            ).fetchone()[0]
            other.execute(
                "INSERT INTO messages(message_id,agent_id,backend_session_id,source_file,seq,role,"
                "timestamp,text,updated_at,revision,origin,trace_id) "
                "VALUES (?,?,?,?,?,'user',?,?,?,?,?,?)",
                (f"u-{kind}-{number}", corpus.AGENT, corpus.SESSION, f"client:{kind}-{number}",
                 -5000 - number, stamp, prompt, at, revision, kind, f"{kind}-trace-{number}"))
            other.execute(
                "INSERT INTO turns(agent_id,source,trace_id,started_at,ended_at,settled_at,outcome) "
                "VALUES (?,'pwa',?,?,?,?,'completed')",
                (corpus.AGENT, f"{kind}-trace-{number}", at, at + 900, at + 900))


def _decision(report):
    [event] = [e for e in report["tail_events"] if e["event"] == "transcriptTailDecision"]
    return event["code"] + (f"({event['reason']})" if "reason" in event else "")


def _append(transcript, data):
    with transcript.open("ab") as output:
        output.write(data)


def _frontiers(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT count(*) FROM transcript_import_frontiers").fetchone()[0]


@pytest.mark.parametrize("kind", list(AUTOMATIONS))
@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_a_session_an_automation_was_sent_to_is_imported_by_its_tail(tmp_path, backend, kind):
    reference, candidate = _clones(tmp_path, backend)
    both = (reference, candidate)
    transcript = tmp_path / "synthetic.jsonl"
    # Six turns of history; the third is the automation's, sent through /send.
    _sent(both, kind, 0, at_ordinal=2)
    transcript.write_bytes(b"".join(_turn(backend, i, kind if i == 2 else None) for i in range(6)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            assert _decision(actual.tick(0)) == "full_no_frontier(missing_unknown)"
            harness.assert_product_equal(reference, candidate)
            assert _frontiers(candidate) == 1

            # The conversation goes on: tails.
            for tick, ordinal in ((1, 6), (2, 7)):
                _append(transcript, _turn(backend, ordinal))
                full.tick(tick)
                assert _decision(actual.tick(tick)) == "tail_applied"
                harness.assert_product_equal(reference, candidate)

            # The automation is sent again, with the same words as before.
            # Its /send row is new, so that import is a full one (the rows the
            # frontier was published with changed); the one after it is a tail.
            _sent(both, kind, 1, at_ordinal=8)
            _append(transcript, _turn(backend, 8, kind))
            full.tick(3)
            assert _decision(actual.tick(3)) == "full_carry_inputs_changed"
            harness.assert_product_equal(reference, candidate)
            _append(transcript, _turn(backend, 9))
            full.tick(4)
            assert _decision(actual.tick(4)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)

        # A Host start: the stored frontier is taken up for this session too.
        _append(transcript, _turn(backend, 10))
        full.tick(5)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
            assert _decision(restarted.tick(5)) == "tail_applied"
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("kind", ["heartbeat", "automation"])
@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_automation_that_was_sent_and_never_ran_does_not_disturb_the_tail(
        tmp_path, backend, kind):
    """Its /send row stays in the store with no turn in the transcript (the
    agent was busy, the wake was retired). Later turns are tails, and when
    the automation does run later, with the same words, its turn is linked
    and answered as in the full replay."""
    reference, candidate = _clones(tmp_path, backend)
    both = (reference, candidate)
    transcript = tmp_path / "synthetic.jsonl"
    _sent(both, kind, 0, at_ordinal=3)                      # sent, never ran
    transcript.write_bytes(b"".join(_turn(backend, i) for i in range(6)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            harness.assert_product_equal(reference, candidate)
            _append(transcript, _turn(backend, 6))
            full.tick(1)
            assert _decision(actual.tick(1)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)
            # Now one runs, in the tail, while the row of the one that never
            # ran is still there with the same words.
            _sent(both, kind, 1, at_ordinal=7)
            _append(transcript, _turn(backend, 7, kind))
            full.tick(2)
            actual.tick(2)
            harness.assert_product_equal(reference, candidate)
            _append(transcript, _turn(backend, 8))
            full.tick(3)
            assert _decision(actual.tick(3)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)


def test_a_heartbeat_is_accounted_once_whether_its_turn_arrives_by_tail_or_in_full(tmp_path):
    """The heartbeat's reply is counted under its /send row's id, once: by
    the import that stores it, a tail here, and never again by a later full
    import or a Host start."""
    backend = "claude"
    reference, candidate = _clones(tmp_path, backend)
    both = (reference, candidate)
    transcript = tmp_path / "synthetic.jsonl"
    _sent(both, "heartbeat", 0, at_ordinal=1)
    transcript.write_bytes(b"".join(_turn(backend, i, "heartbeat" if i == 1 else None)
                                    for i in range(4)))

    def accounted(database):
        with sqlite3.connect(database) as connection:
            return sorted(connection.execute("SELECT * FROM heartbeat_accounting"))

    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            first = accounted(candidate)
            assert len(first) == 1 and accounted(reference) == first
            _append(transcript, _turn(backend, 4))
            full.tick(1)
            assert _decision(actual.tick(1)) == "tail_applied"
            # A second heartbeat arrives and is answered while tails apply.
            _sent(both, "heartbeat", 1, at_ordinal=5)
            _append(transcript, _turn(backend, 5, "heartbeat"))
            full.tick(2)
            actual.tick(2)
            _append(transcript, _turn(backend, 6))
            full.tick(3)
            assert _decision(actual.tick(3)) == "tail_applied"
            second = accounted(candidate)
            assert len(second) == 2 and accounted(reference) == second
            harness.assert_product_equal(reference, candidate)
        _append(transcript, _turn(backend, 7))
        full.tick(4)
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as restarted:
            assert _decision(restarted.tick(4)) == "tail_applied"
        assert accounted(candidate) == second
        harness.assert_product_equal(reference, candidate)


@pytest.mark.parametrize("kind", list(AUTOMATIONS))
@pytest.mark.parametrize("backend", harness.BACKENDS)
def test_an_automations_reply_that_arrives_by_tail_is_the_full_replays(tmp_path, backend, kind):
    """The usual order on a live Host: the prompt is imported first (a full
    import, its /send row being new), and the steps and the reply follow in
    later ticks, as tails. What the reply does (a heartbeat is accounted
    under its /send row, a leader tick's no-op is noted) goes by the state
    the frontier carried from the prompt."""
    reference, candidate = _clones(tmp_path, backend)
    both = (reference, candidate)
    transcript = tmp_path / "synthetic.jsonl"
    transcript.write_bytes(b"".join(_turn(backend, i) for i in range(4)))
    with harness.PersistentLane(reference, transcript, backend, "reference") as full:
        with harness.PersistentLane(candidate, transcript, backend, "candidate") as actual:
            full.tick(0)
            actual.tick(0)
            _sent(both, kind, 0, at_ordinal=4)
            records = _turn(backend, 4, kind).splitlines(keepends=True)
            pieces = [records[:-2], records[-2:-1], records[-1:]]   # prompt (and start), a step, the reply
            assert all(pieces) and b"".join(b"".join(piece) for piece in pieces) == _turn(backend, 4, kind)
            decisions = []
            for tick, piece in enumerate(pieces, start=1):
                _append(transcript, b"".join(piece))
                full.tick(tick)
                decisions.append(_decision(actual.tick(tick)))
                harness.assert_product_equal(reference, candidate)
            assert decisions == ["full_carry_inputs_changed", "tail_applied", "tail_applied"]
            _append(transcript, _turn(backend, 5))
            full.tick(4)
            assert _decision(actual.tick(4)) == "tail_applied"
            harness.assert_product_equal(reference, candidate)
