"""A message that waits for a running turn enters the conversation where its turn starts (issue 491).

The case: message one starts a turn; message two arrives 7.6 s later, while that turn is still
thinking; the turn answers at 10.2 s without having seen it; message two's own turn starts at
10.5 s. Before this change /log showed one, two, then an answer that ignores two: a plain send's
row was written when it was admitted. Now a send that waits is admitted as a queue item, as a
send that asks to wait (queue_if_busy) always was, and its conversation row is written by the
write that starts its turn, keeping the time it was sent in ``sent_at``. No row is ever moved or
re-timed.

At every moment an accepted message is EITHER a queue item a client can read (GET /turn-queue:
turn_queue.waiting_items) OR a conversation row (/log), never both and never neither; the write
that starts its turn writes the row and takes the item out of the queue read in one transaction.

Every case runs through the real dispatcher (the queue tests' harness, test_drain_pause_parity's
World) on a fake wall clock, and reads /log as a client does: a tail page and a delta from the
cursor it held. The ways a message meets a busy agent: a plain send (a backend that cannot be
steered), queue_if_busy, a steered send (Codex: accepted, refused), a send held behind the
release drain or the user's Stop; then a restart, a retry, an edit, a deletion while it waits."""
from __future__ import annotations

import time

import pytest

from lib import agents as agents_db
from lib import db, message_store, turn_queue
from lib import turn_dispatch as td
from lib.clock import iso_from_ms
from lib.conversation import load_conversation

from test_drain_pause_parity import Provider
from test_held_send_runs_after_resume import _World, _delivery, _resume

BASE = 1_795_000_000_000           # a wall clock in Nov 2026, in ms


class _Clock:
    def __init__(self):
        self.ms = BASE

    def at(self, offset_ms: int) -> None:
        self.ms = BASE + offset_ms


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "time", lambda: c.ms / 1000)
    return c


def _iso(offset_ms: int) -> str:
    return iso_from_ms(BASE + offset_ms)


def _tail():
    return load_conversation(session="mike", limit=500)


def _texts(page):
    return [t["text"] for t in page["turns"]]


def _row(page, cid):
    return next((t for t in page["turns"] if t["id"] == f"u-{cid}"), None)


def _queue(world):
    """GET /turn-queue's items, as (client_msg_id, text, kind)."""
    return [(item["client_msg_id"], item["text"], item["kind"])
            for item in turn_queue.waiting_items(world.agent)]


def _where(world, cid):
    """Where a client finds the message: 'queue', 'log', both or neither (the invariant)."""
    in_queue = any(c == cid for c, _t, _k in _queue(world))
    in_log = _row(_tail(), cid) is not None
    return {(True, False): "queue", (False, True): "log",
            (True, True): "both", (False, False): "neither"}[(in_queue, in_log)]


def _import(world, turns, session=None):
    """One pass of the transcript import over the CLI's transcript as it stands."""
    message_store.store_transcript_turns(
        agent_id=world.agent,
        backend_session_id=session or agents_db.live_backend_session(world.agent),
        source_file=str(world.tmp_path / "transcript.jsonl"),
        turns=[{"role": role, "text": text, "timestamp": _iso(at)} for role, text, at in turns])


def _finish_running(world):
    """The running turn ends (its process exits and reports), and the queue hands off."""
    world._collect()
    running = [c for c in world.provider.spawned if c["agent_id"] in world.provider.live]
    assert len(running) == 1
    world.provider.live.discard(running[0]["agent_id"])
    running[0]["on_result"]({"result": "done"})
    world._collect()


def _send(world, cid, *, queue=False):
    return world.service.dispatch(
        text=cid, requested_session="mike", trace_id=f"t-{cid}", client_msg_id=cid,
        synthesize_audio=False, queue_if_busy=queue, origin="user", sender_agent_id="")


ONE = [("user", "one", 50), ("assistant", "answer to one", 10_200)]
BOTH = ONE + [("user", "two", 10_600), ("assistant", "answer to two", 12_000)]


def _the_case(world, clock, *, queue=False):
    """Message one; message two at 7.6 s; turn one answers at 10.2 s and ends at 10.5 s."""
    clock.at(0)
    _send(world, "one")
    world._collect()
    assert world.started == ["one"]
    clock.at(7_600)
    answer = _send(world, "two", queue=queue)
    assert world.started == ["one"]
    waiting = _tail()
    seen = {"queued_answer": answer, "waiting": waiting, "queue": _queue(world),
            "where_waiting": _where(world, "two")}
    clock.at(10_200)
    _import(world, ONE)
    seen["answered"] = _tail()
    clock.at(10_500)
    _finish_running(world)
    return seen


def _assert_in_its_place(page, *, sent=7_600, started=10_500):
    texts = [t for t in _texts(page) if t in ("one", "answer to one", "two", "answer to two")]
    assert texts[:3] == ["one", "answer to one", "two"], _texts(page)
    two = _row(page, "two")
    assert two["timestamp"] == _iso(started)
    assert two["sent_at"] == _iso(sent)
    assert "sent_at" not in _row(page, "one")


def test_a_plain_send_that_waits_has_no_row_until_its_turn_starts(tmp_path, clock):
    world = _World(tmp_path)
    seen = _the_case(world, clock)
    answer = seen["queued_answer"]
    assert answer.queued is True and answer.queue_revision > 0 and answer.queue_depth == 1
    # While it waits: a queue item another device can draw, no conversation row.
    assert _texts(seen["waiting"]) == ["one"]
    assert seen["queue"] == [("two", "two", turn_queue.KIND_WAITING)]
    assert seen["where_waiting"] == "queue"
    assert _texts(seen["answered"]) == ["one", "answer to one"]
    # Its turn started at 10.5 s: its row is written there, under the answer that did not see it.
    assert world.started == ["one", "two"]
    _assert_in_its_place(_tail())
    assert _queue(world) == [] and _where(world, "two") == "log"
    assert _delivery(world, "two")[0] == "handed_over"
    # A client that held the cursor of the answered page gets the row appended, in its place.
    delta = load_conversation(session="mike", after_revision=seen["answered"]["latest_revision"])
    ids = [t["id"] for t in delta["turns"]]
    assert "u-two" in ids and delta["replace_required"] is False   # (the settle re-stamps one's answer)


def test_both_times_survive_an_import_pass_after_the_turn(tmp_path, clock):
    world = _World(tmp_path)
    _the_case(world, clock)
    before = _row(_tail(), "two")
    clock.at(12_500)
    _import(world, BOTH)
    _import(world, BOTH)               # and a second pass over the same transcript
    tail = _tail()
    assert _texts(tail) == ["one", "answer to one", "two", "answer to two"]
    _assert_in_its_place(tail)
    assert _row(tail, "two")["revision"] == before["revision"]


def test_a_queue_if_busy_send_keeps_the_time_it_was_sent(tmp_path, clock):
    world = _World(tmp_path)
    seen = _the_case(world, clock, queue=True)
    assert seen["queue"] == [("two", "two", turn_queue.KIND_LATER)]
    assert seen["where_waiting"] == "queue"
    assert world.started == ["one", "two"]
    _assert_in_its_place(_tail())


def test_a_send_that_starts_at_once_costs_no_queue_write(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    row = _row(_tail(), "one")
    assert row["timestamp"] == _iso(0) and "sent_at" not in row
    assert db.conn().execute("SELECT COUNT(*) FROM queued_turns").fetchone()[0] == 0


def test_the_invariant_holds_inside_the_handoff_and_across_a_failed_launch(tmp_path, clock, monkeypatch):
    """The row and the item's leaving the queue read are one write: checked right after it,
    before the launch, and when that launch fails and is retried."""
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    observed = []
    real = td.TurnDispatchService._start_queued_row

    def watched(self, spec, **kwargs):
        observed.append(("before", _where(world, spec.client_msg_id)))
        out = real(self, spec, **kwargs)
        observed.append(("after", _where(world, spec.client_msg_id)))
        return out

    monkeypatch.setattr(td.TurnDispatchService, "_start_queued_row", watched)
    fails = {"left": 1}
    real_spawn = world.provider.spawn_turn

    def spawn_once_failing(backend, **kwargs):
        if kwargs.get("text") == "two" and fails["left"]:
            fails["left"] -= 1
            raise RuntimeError("the CLI could not be started")
        return real_spawn(backend, **kwargs)

    world.provider.spawn_turn = spawn_once_failing
    scheduled = []
    world.service.retry_scheduler = lambda _delay, fn: scheduled.append(fn)
    clock.at(10_500)
    _finish_running(world)
    assert world.started == ["one"] and _where(world, "two") == "log"   # failed: kept, not lost
    clock.at(13_000)                         # past the one-second retry backoff
    while scheduled:
        scheduled.pop(0)()                   # the retry the scheduler runs
    world.settle(recover=True)
    assert observed[0] == ("before", "queue") and observed[1] == ("after", "log")
    assert all(where in ("queue", "log") for _when, where in observed), observed
    assert world.started.count("two") == 1
    assert _where(world, "two") == "log"
    assert sum(1 for t in _tail()["turns"] if t["id"] == "u-two") == 1


class _SteeredProvider(Provider):
    """Codex as far as steering goes: a follow-up is taken into the running turn, or refused."""

    def __init__(self, accept=True):
        super().__init__()
        self.accept = accept
        self.steered: list[str] = []

    def steer_turn(self, backend, agent_id, text, **kwargs):
        self.steered.append(kwargs.get("client_msg_id", ""))
        return self.accept

    def by_id(self, backend):
        from types import SimpleNamespace
        return SimpleNamespace(id="codex", supports_steer=True)


class _SteeredWorld(_World):
    accept = True

    def make_provider(self):
        return _SteeredProvider(self.accept)


def test_an_accepted_steer_writes_the_row_then(tmp_path, clock):
    """The running turn took it in: its answer did see it, so its row stands above that answer."""
    world = _SteeredWorld(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    answer = _send(world, "two")
    assert world.provider.steered == ["two"] and answer.queued is False
    assert _where(world, "two") == "log"
    clock.at(10_200)
    _import(world, [("user", "one", 50), ("user", "two", 7_650), ("assistant", "answer to both", 10_200)])
    clock.at(10_500)
    _finish_running(world)
    tail = _tail()
    assert _texts(tail) == ["one", "two", "answer to both"]
    assert _row(tail, "two")["timestamp"] == _iso(7_600) and "sent_at" not in _row(tail, "two")
    assert world.started == ["one"]


def test_a_refused_steer_waits_without_a_row(tmp_path, clock):
    world = _SteeredWorld.__new__(_SteeredWorld)
    world.accept = False
    _SteeredWorld.__init__(world, tmp_path)
    seen = _the_case(world, clock)
    assert world.provider.steered == ["two"]
    assert seen["where_waiting"] == "queue"
    assert world.started == ["one", "two"]
    _assert_in_its_place(_tail())


@pytest.mark.parametrize("end", ["handoff", "lift"])
def test_a_send_held_behind_the_release_drain(tmp_path, clock, end):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    world.service.begin_admission_fence()
    clock.at(7_600)
    _send(world, "two")
    assert _where(world, "two") == "queue"
    clock.at(10_200)
    _import(world, ONE)
    clock.at(10_500)
    _finish_running(world)
    assert world.started == ["one"]          # held: the fence is up
    written_in = agents_db.live_backend_session(world.agent)
    if end == "handoff":
        world.restart()                      # the next runtime recovers the held send
        # The stand-in CLI wrote no transcript, so the next runtime starts a fresh native
        # session (ghostSessionReset); the rows stay in the one they were written in.
        assert world.started == ["one", "two"]
        rows = message_store.list_messages(agent_id=world.agent, backend_session_id=written_in,
                                           limit=500)
        assert [t["text"] for t in rows] == ["one", "answer to one"]
        two = _row(_tail(), "two")
        assert two["timestamp"] == _iso(10_500) and two["sent_at"] == _iso(7_600)
        return
    world.service.lower_admission_fence()
    world.service.start_held_work()
    world._collect()
    assert world.started == ["one", "two"]
    _assert_in_its_place(_tail())


def test_a_stop_while_it_waits_keeps_it_in_the_paused_queue(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    clock.at(10_200)
    _import(world, ONE)
    world.stop_as_the_host()
    world.settle(recover=True)
    assert world.started == ["one"] and turn_queue.is_paused(world.agent)
    # What the user sees: the message in the queue, the queue paused; nothing in the chat.
    assert _queue(world) == [("two", "two", turn_queue.KIND_WAITING)]
    assert _where(world, "two") == "queue"
    clock.at(10_500)
    _resume(world)
    assert world.started == ["one", "two"]
    _assert_in_its_place(_tail())


def test_a_restart_while_it_waits_runs_it_once(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    clock.at(10_500)
    world.restart()                          # memory gone; the turn's process with it
    assert world.started.count("two") == 1
    rows = [t for t in message_store.list_messages(agent_id=world.agent, limit=500)
            if t["id"] == "u-two"]
    assert len(rows) == 1 and rows[0]["sent_at"] == _iso(7_600)


def test_a_retry_of_the_same_message_while_it_waits(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    again = world.service.dispatch(
        text="two", requested_session="mike", trace_id="t-two-again", client_msg_id="two",
        synthesize_audio=False, origin="user", sender_agent_id="")
    assert again.queued is True and again.queue_revision > 0
    assert _queue(world) == [("two", "two", turn_queue.KIND_WAITING)]
    clock.at(10_500)
    _finish_running(world)
    world.settle(recover=True)
    assert world.started.count("two") == 1
    assert sum(1 for t in _tail()["turns"] if t["id"] == "u-two") == 1


def test_a_waiting_message_can_be_edited_until_its_turn_starts(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    assert turn_queue.update_text("two", "two, edited")
    assert _queue(world) == [("two", "two, edited", turn_queue.KIND_WAITING)]
    clock.at(10_500)
    _finish_running(world)
    assert world.started == ["one", "two, edited"]
    assert _row(_tail(), "two")["text"] == "two, edited"
    assert turn_queue.update_text("two", "too late") is False       # the conversation's now
    assert turn_queue.remove("two") is False
    assert _row(_tail(), "two")["text"] == "two, edited"


def test_an_edit_racing_the_start_lands_whole_or_not_at_all(tmp_path, clock, monkeypatch):
    """The edit arrives as the turn starts. Either it commits before the write that starts the
    turn (row and turn both carry it) or it is refused after it: never a row with one text and a
    turn run on another."""
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    real = td.TurnDispatchService._start_queued_row
    outcome = {}

    def edit_first(self, spec, **kwargs):
        outcome["before"] = turn_queue.update_text("two", "edited just before")
        out = real(self, spec, **kwargs)
        outcome["after"] = turn_queue.update_text("two", "edited just after")
        return out

    monkeypatch.setattr(td.TurnDispatchService, "_start_queued_row", edit_first)
    clock.at(10_500)
    _finish_running(world)
    assert outcome == {"before": True, "after": False}
    assert world.started == ["one", "edited just before"]
    assert _row(_tail(), "two")["text"] == "edited just before"


def test_a_waiting_message_deleted_never_runs(tmp_path, clock):
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    assert turn_queue.remove("two")
    assert _where(world, "two") == "neither"          # deleted: a device drops its bubble
    clock.at(10_500)
    _finish_running(world)
    world.settle(recover=True)
    assert world.started == ["one"]
    assert _row(_tail(), "two") is None


def test_a_message_admitted_with_its_row_is_never_also_a_queue_item(tmp_path, clock, monkeypatch):
    """A send that met the agent idle (so was admitted with its row) and found it busy only after
    (another turn took the slot in between) keeps today's behaviour: its row stands where it was
    written, never re-timed; its queue item is kept for the dispatcher but not shown."""
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    monkeypatch.setattr(td.TurnDispatchService, "_waits_on_arrival", lambda self, spec, slot: False)
    clock.at(7_600)
    _send(world, "two")
    assert _queue(world) == [] and _where(world, "two") == "log"
    clock.at(10_500)
    _finish_running(world)
    assert world.started == ["one", "two"]
    two = _row(_tail(), "two")
    assert two["timestamp"] == _iso(7_600) and "sent_at" not in two


def test_a_waiting_user_message_never_marks_its_turn_for_agents(tmp_path, clock):
    world = _World(tmp_path)
    _the_case(world, clock)
    clock.at(12_500)
    _import(world, BOTH)
    for turn in _tail()["turns"]:
        assert turn["audience"] == "user", turn


def test_a_user_message_steered_into_an_agents_turn_makes_it_mixed(tmp_path, clock):
    """PR 482: a turn another agent's message started reads 'agents'. A user's send that waits
    (a queue item) and is then steered into that turn makes it 'mixed', never left 'agents';
    its rows are shown to the user."""
    from lib import coordination
    world = _SteeredWorld(tmp_path)
    clock.at(0)
    world.service.dispatch(
        text="from a peer", requested_session="mike", trace_id="t-peer", client_msg_id="peer-1",
        synthesize_audio=False, origin="agent", sender_agent_id="other-agent")
    world._collect()
    assert world.started == ["from a peer"]
    assert coordination.audience_of(world.agent, "t-peer") == coordination.AGENTS
    clock.at(5_000)
    _send(world, "user word")
    assert world.provider.steered == ["user word"]
    stored = db.conn().execute(
        "SELECT audience FROM coordination_traces WHERE agent_id = ? AND trace_id = ?",
        (world.agent, "t-peer")).fetchone()["audience"]
    assert stored == coordination.MIXED                       # loosened, never left 'agents'
    assert coordination.audience_of(world.agent, "t-peer") == coordination.USER   # shown
    assert _where(world, "user word") == "log"
    assert all(t["audience"] == "user" for t in _tail()["turns"])


class _Crash(BaseException):
    """The process dies here (not an error any handler takes for a failed launch)."""


@pytest.mark.parametrize("when", ["before the write", "after the write"])
def test_the_invariant_survives_a_crash_around_the_write_that_starts_the_turn(
        tmp_path, clock, monkeypatch, when):
    """Addendum 3 f: the row and the item's leaving the queue read are ONE transaction, so a
    crash can only fall before it (the message is still a queue item) or after it (it is a
    row). Either way, after the restart, it is in exactly one place, runs once, has one row."""
    world = _World(tmp_path)
    clock.at(0)
    _send(world, "one")
    world._collect()
    clock.at(7_600)
    _send(world, "two")
    real = td.TurnDispatchService._start_queued_row
    seen = {}

    def crashing(self, spec, **kwargs):
        if spec.client_msg_id != "two":
            return real(self, spec, **kwargs)
        if when == "after the write":
            real(self, spec, **kwargs)
        seen["at_crash"] = _where(world, "two")
        raise _Crash()

    monkeypatch.setattr(td.TurnDispatchService, "_start_queued_row", crashing)
    clock.at(10_500)
    with pytest.raises(_Crash):
        _finish_running(world)
    assert seen["at_crash"] == ("queue" if when == "before the write" else "log")
    monkeypatch.setattr(td.TurnDispatchService, "_start_queued_row", real)
    clock.at(11_000)
    world.restart()                          # memory gone, the database is not
    assert world.started.count("two") == 1
    rows = [t for t in message_store.list_messages(agent_id=world.agent, limit=500)
            if t["id"] == "u-two"]
    assert len(rows) == 1 and rows[0]["sent_at"] == _iso(7_600)
    assert not any(c == "two" for c, _t, _k in _queue(world))
