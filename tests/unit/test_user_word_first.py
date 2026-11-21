"""The user's word goes first in an agent's queue (issue 481).

An agent takes one queued message per turn. The user's own word (an origin
the Host resolved as the user's, or a card the user answered in the app) is
read as soon as the current turn ends, ahead of other agents' messages, in the
order the user sent it; everything else keeps today's order behind it. The
tests go through the real dispatcher (``test_drain_pause_parity.World``):
admission, the memory queue, the durable rows, recovery after a restart, the
Stop pause and the release drain's fence.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import origins, turn_queue
from lib import turn_dispatch as td
from lib.turn_command import DispatchError

from test_drain_pause_parity import World


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(td, "_RUNTIME_CLIENT", None)
    td.reset_for_tests()
    yield
    td.reset_for_tests()


def _waiting(world):
    return [row["client_msg_id"] for row in turn_queue.pending(world.agent)]


def _memory(world):
    return [spec.client_msg_id for spec in td._SLOTS.queued_specs().get(world.agent, [])]


def _end_running(world):
    running = [c for c in world.provider.spawned if c["agent_id"] in world.provider.live]
    call = running[-1]
    world.provider.live.discard(call["agent_id"])
    call["on_result"]({"result": "done"})
    world._collect()


def _resume(world):
    assert turn_queue.set_paused(world.agent, False)
    world.service.recover_queued()
    world.settle()


def _busy_with_peers(world, peers=("p1", "p2", "p3")):
    world.send("t0")
    for cid in peers:
        world.send(cid, origin="agent", queue=True)


# --- the rule ------------------------------------------------------------------

def test_the_rule_reads_only_what_the_host_resolved():
    assert origins.is_user_word("user") and origins.is_user_word("oracle")
    for origin in ("agent", "automation", "schedule", "heartbeat", "janitor", "", None):
        assert not origins.is_user_word(origin)
    # A card the user answered in the app, or a designated session answered on
    # the user's behalf (resolved_by 'agent:<session>', written only after the
    # Host checked the designation and its turn token); not one that expired
    # or was dismissed.
    for choice in ("accepted", "rejected", "answered"):
        assert origins.is_user_word("automation", card_answered_by="user", card_choice=choice)
    assert not origins.is_user_word("automation", card_answered_by="user", card_choice="expired")
    assert not origins.is_user_word("automation", card_answered_by="user", card_choice="dismissed")
    assert origins.is_user_word("automation", card_answered_by="agent:desk",
                                card_choice="answered")
    assert not origins.is_user_word("automation", card_answered_by="agent:desk",
                                    card_choice="expired")
    assert not origins.is_user_word("automation", card_answered_by="", card_choice="answered")


def test_the_slot_never_overtakes_the_user_or_a_started_item():
    items = [("s", False, True), ("u1", True, False), ("p1", False, False),
             ("u2", True, False), ("p2", False, False)]
    slot = turn_queue.user_word_slot(items, is_user=lambda i: i[1], started=lambda i: i[2])
    assert slot == 4   # after u2, ahead of p2; p1 was already overtaken by u2
    assert turn_queue.user_word_slot([], is_user=bool, started=bool) == 0
    assert turn_queue.user_word_slot([("p", False, False)], is_user=lambda i: i[1],
                                     started=lambda i: i[2]) == 0


# --- the order -----------------------------------------------------------------

def test_the_users_word_is_read_next_in_the_order_sent(tmp_path):
    world = World(tmp_path)
    _busy_with_peers(world)
    world.send("u1", queue=True)
    world.send("p4", origin="agent", queue=True)
    world.send("u2", queue=True)
    assert _waiting(world) == ["u1", "u2", "p1", "p2", "p3", "p4"]
    assert _memory(world) == _waiting(world)
    world.settle()
    assert world.started == ["t0", "u1", "u2", "p1", "p2", "p3", "p4"]


def test_a_plain_user_send_to_a_busy_agent_goes_first_too(tmp_path):
    world = World(tmp_path)
    _busy_with_peers(world)
    world.send("u1")                       # not asked to queue: the busy path queues it
    world._collect()
    if world.started[-1] == "u1":
        pytest.skip("this backend steered the send into the running turn")
    assert _waiting(world)[0] == "u1"
    world.settle()
    assert world.started == ["t0", "u1", "p1", "p2", "p3"]


def test_an_agent_claiming_nothing_but_an_agent_origin_keeps_its_place(tmp_path):
    """Automation, schedules and other agents keep today's order among
    themselves; only the user's word moves."""
    world = World(tmp_path)
    world.send("t0")
    world.send("a1", origin="automation", queue=True)
    world.send("p1", origin="agent", queue=True)
    world.send("s1", origin="schedule", queue=True)
    assert _waiting(world) == ["a1", "p1", "s1"]
    world.settle()
    assert world.started == ["t0", "a1", "p1", "s1"]


def test_a_card_the_user_answered_goes_first(tmp_path):
    world = World(tmp_path)
    _busy_with_peers(world)
    world.service.dispatch(
        text="answer", requested_session="mike", trace_id="t-card",
        client_msg_id="decision-1", synthesize_audio=False, queue_if_busy=True,
        origin="automation", user_word=True)
    world.send("u1", queue=True)
    assert _waiting(world) == ["decision-1", "u1", "p1", "p2", "p3"]
    row = turn_queue.get("decision-1")
    assert row["origin"] == "automation" and row["user_word"] == 1
    world.settle()
    assert world.started == ["t0", "answer", "u1", "p1", "p2", "p3"]


def test_the_card_delivery_marks_only_the_users_own_answer(monkeypatch):
    from lib import artifacts
    from lib.dispatch_adapters import DispatchAdapters
    commands = []

    class Service:
        def submit(self, command):
            commands.append(command)

    adapters = DispatchAdapters(SimpleNamespace(), lambda _ctx: Service())
    monkeypatch.setattr(artifacts, "format_delivery_prompt", lambda pending: "answer")
    monkeypatch.setattr(artifacts, "mark_delivered", lambda _id: None)
    base = {"session": "mike", "response_type": "single_choice"}
    adapters.deliver_decision({**base, "decision_id": "d1", "choice": "answered",
                               "resolved_by": "user"})
    adapters.deliver_decision({**base, "decision_id": "d2", "choice": "expired",
                               "resolved_by": ""})
    adapters.deliver_decision({**base, "decision_id": "d3", "choice": "answered",
                               "resolved_by": "agent:desk"})
    assert [(c.client_msg_id, c.origin, c.user_word) for c in commands] == [
        ("decision-d1", "automation", True),
        ("decision-d2", "automation", False),
        ("decision-d3", "automation", True)]
    # The wire to the runtime names the field only when it is set, so a
    # runtime of the release before still takes every other command.
    assert commands[0].to_wire()["user_word"] is True
    assert "user_word" not in commands[1].to_wire()


# --- starvation ----------------------------------------------------------------

def test_agent_messages_still_drain_when_the_user_keeps_writing(tmp_path):
    """N agent messages wait; between turns the user writes again, a handful
    of times. Each user message waits at most behind the ones before it, and
    every agent message still runs, once, in its order."""
    world = World(tmp_path)
    peers = [f"p{n}" for n in range(12)]
    _busy_with_peers(world, peers)
    users = [f"u{n}" for n in range(5)]
    for cid in users:
        world.send(cid, queue=True)
        _end_running(world)               # one turn ends between each user message
    world.settle()
    started = world.started
    assert sorted(started) == sorted(["t0", *peers, *users])
    assert len(started) == len(set(started))
    assert [c for c in started if c.startswith("p")] == peers
    assert [c for c in started if c.startswith("u")] == users
    # Each user message was the very next turn after it was sent.
    assert started[:6] == ["t0", "u0", "u1", "u2", "u3", "u4"]
    assert _waiting(world) == []


# --- held, fenced, stopped, paused --------------------------------------------

def test_the_users_word_after_a_stop_runs_past_the_pause_and_the_rest_waits(tmp_path):
    """Fresh user intent already runs past a Stop's pause (docs/protocol.md,
    send rules); nothing about that changes. The other agents' messages stay
    held for Resume, in their order."""
    world = World(tmp_path)
    _busy_with_peers(world)
    world.stop()
    world.send("u1", queue=True)
    world.settle()
    assert world.started == ["t0", "u1"]
    assert turn_queue.is_paused(world.agent)
    assert _waiting(world) == ["p1", "p2", "p3"]
    _resume(world)
    assert world.started == ["t0", "u1", "p1", "p2", "p3"]


def test_work_queued_before_a_stop_is_held_with_the_user_first(tmp_path):
    world = World(tmp_path)
    _busy_with_peers(world)
    world.send("u1", queue=True)
    world.stop()
    world.settle()
    assert world.started == ["t0"]
    assert _waiting(world) == ["u1", "p1", "p2", "p3"]
    _resume(world)
    assert world.started == ["t0", "u1", "p1", "p2", "p3"]


@pytest.mark.parametrize("mode", ["lifted", "handoff"])
def test_the_release_drain_holds_the_users_word_first(tmp_path, mode):
    world = World(tmp_path)
    world.send("t0")
    world.service.begin_admission_fence()
    world.send("p1", origin="agent", queue=True)
    world.send("u1", queue=True)
    world.send("p2", origin="agent", queue=True)
    _end_running(world)
    assert world.started == ["t0"]         # the fence holds everything
    if mode == "lifted":
        world.service.lower_admission_fence()
        world.service.start_held_work()
        world.settle()
    else:
        world.service.persist_held(world.agent)
        world.restart()
    assert world.started == ["t0", "u1", "p1", "p2"]


def test_the_order_survives_a_restart(tmp_path):
    world = World(tmp_path)
    _busy_with_peers(world)
    world.send("u1", queue=True)
    world.restart()
    assert world.started == ["t0", "u1", "p1", "p2", "p3"]


def test_an_item_already_taken_is_not_overtaken(tmp_path):
    """A head whose launch failed goes back to the front; the user's word
    waits behind it (its turn was taken), ahead of the rest."""
    from dataclasses import replace
    world = World(tmp_path)
    _busy_with_peers(world)
    head = td._SLOTS.queued_specs()[world.agent][0]
    td._SLOTS.drop_queued(world.agent, lambda spec: spec is head)
    td._SLOTS.requeue_front(world.agent, replace(head, handoff_failures=1))
    world.send("u1", queue=True)
    assert _memory(world)[:2] == ["p1", "u1"]


def test_a_row_whose_turn_started_keeps_the_head_of_the_durable_queue(tmp_path):
    """The release drain parks in-flight work at the head with its turn row;
    the user's word goes behind it."""
    world = World(tmp_path)
    con = agents_db.conn()
    con.execute("BEGIN IMMEDIATE")
    turn_queue.park_at_head(
        queue_id="recovery-park-x", agent_id=world.agent, session="mike", text="x",
        trace_id="t-x", client_msg_id="x", synthesize_audio=False, origin="agent",
        sender_agent_id="other-agent")
    con.execute("COMMIT")
    _insert_turn(con, world.agent, "t-x")
    turn_queue.requeue_rows(["recovery-park-x"])
    turn_queue.enqueue(queue_id="p1", agent_id=world.agent, session="mike", text="p1",
                       trace_id="t-p1", client_msg_id="p1", synthesize_audio=False,
                       origin="agent", sender_agent_id="other-agent")
    turn_queue.enqueue(queue_id="u1", agent_id=world.agent, session="mike", text="u1",
                       trace_id="t-u1", client_msg_id="u1", synthesize_audio=False,
                       origin="user", sender_agent_id="", user_word=True)
    assert _waiting(world) == ["x", "u1", "p1"]


def _insert_turn(con, agent_id, trace_id):
    con.execute("INSERT INTO turns (agent_id, source, trace_id, started_at, ended_at)"
                " VALUES (?, 'test', ?, 1, 2)", (agent_id, trace_id))


# --- cost ----------------------------------------------------------------------

def test_placing_the_users_word_reads_one_agents_waiting_rows_by_index(tmp_path):
    con = agents_db.conn()
    plan = " ".join(row[3] for row in con.execute(
        """EXPLAIN QUERY PLAN SELECT * FROM queued_turns
            WHERE agent_id = ? AND status IN ('queued', 'parked') AND queue_id <> ?
            ORDER BY queue_seq""", ("a", "q")))
    assert "idx_queued_turns_waiting (agent_id=? AND status=?)" in plan, plan
    assert "SCAN queued_turns" not in plan, plan
    plan = " ".join(row[3] for row in con.execute(
        """EXPLAIN QUERY PLAN SELECT * FROM queued_turns
             WHERE status = 'queued' AND agent_id = ? ORDER BY queue_seq""", ("a",)))
    assert "idx_queued_turns_waiting (agent_id=? AND status=?)" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


def test_the_upgrade_marks_waiting_rows_of_the_users_own_origin():
    import sqlite3
    from lib import db_migrations
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE queued_turns (queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                   queue_id TEXT, agent_id TEXT, origin TEXT, status TEXT)""")
    con.executemany("INSERT INTO queued_turns (queue_id, agent_id, origin, status) VALUES (?, 'a', ?, 'queued')",
                    [("p1", "agent"), ("u1", "user"), ("o1", "oracle"), ("a1", "automation")])
    db_migrations._migrate_to_v132(con)
    db_migrations._migrate_to_v132(con)       # a rerun is a no-op
    assert dict(con.execute("SELECT queue_id, user_word FROM queued_turns")) == {
        "p1": 0, "u1": 1, "o1": 1, "a1": 0}
    assert [r[0] for r in con.execute("SELECT queue_id FROM queued_turns ORDER BY queue_seq")] == \
        ["p1", "u1", "o1", "a1"]               # no row is moved by the upgrade
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_queued_turns_waiting'").fetchone()


def test_a_stop_joins_what_arrived_during_it_by_the_same_rule():
    from lib.turn_slots import TurnSlots
    spec = lambda cid, user=False: SimpleNamespace(trace_id=f"t-{cid}", client_msg_id=cid, user_word=user)
    slots = TurnSlots()
    slots.enqueue("a", spec("p1"))
    slots.enqueue("a", spec("p2"))
    snapshot, _dropped = slots.begin_stop("a")
    slots.enqueue("a", spec("u1", True))      # admitted while the barrier was up
    slots.enqueue("a", spec("p3"))
    slots.restore_queue("a", snapshot.as_dict(), [])
    assert [s.client_msg_id for s in slots.queued["a"]] == ["u1", "p1", "p2", "p3"]


def test_the_queue_updated_event_carries_the_new_order_s_revision(tmp_path):
    world = World(tmp_path)
    events = []
    world.service.ctx.stream = SimpleNamespace(broadcast=events.append)
    _busy_with_peers(world)
    world.send("u1", queue=True)
    updates = [e for e in events if e.get("type") == "queue-updated"]
    assert updates, events
    last = updates[-1]
    assert last["queue_depth"] == 4
    assert last["queue_revision"] == turn_queue.revision(world.agent)
    assert _waiting(world)[0] == "u1"


def test_a_front_desk_relay_goes_first_and_stays_an_agents_message(tmp_path):
    """What the send route marks for the front desk's own turn: first in line,
    and nothing else about it changes (origin, sender, PR 482's mark)."""
    world = World(tmp_path)
    _busy_with_peers(world)
    world.service.dispatch(
        text="relay", requested_session="mike", trace_id="t-desk", client_msg_id="desk",
        synthesize_audio=False, queue_if_busy=True, origin="agent",
        sender_agent_id="desk-agent", user_word=True)
    assert _waiting(world) == ["desk", "p1", "p2", "p3"]
    row = turn_queue.get("desk")
    assert (row["origin"], row["sender_agent_id"], row["user_word"]) == ("agent", "desk-agent", 1)
    assert row["text"] == "relay" and row["trace_id"] == "t-desk"
    world.settle()
    assert world.started == ["t0", "relay", "p1", "p2", "p3"]
    marked = agents_db.conn().execute(
        "SELECT audience, sender_agent_id FROM coordination_traces WHERE trace_id = 't-desk'"
    ).fetchone()
    assert marked is not None and tuple(marked) == ("agents", "desk-agent")


# --- the two stores agree ------------------------------------------------------

class _FailingProvider:
    """Wraps the harness provider: the next launch fails before the backend
    starts, so the dispatcher puts the head back (requeue_front)."""

    def __init__(self, inner):
        self.inner = inner
        self.fail_next = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def spawn_turn(self, backend, **kwargs):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("launch failed before the backend started")
        return self.inner.spawn_turn(backend, **kwargs)


def _arm(world):
    world.provider = world.provider if isinstance(world.provider, _FailingProvider) \
        else _FailingProvider(world.provider)
    world.service.backends = world.provider


def _durable_order(world):
    return [row["client_msg_id"] for row in agents_db.conn().execute(
        """SELECT client_msg_id FROM queued_turns WHERE agent_id = ?
             AND status IN ('queued', 'parked') ORDER BY queue_seq""", (world.agent,))]


def _agree(world, where):
    memory = _memory(world)
    durable = [cid for cid in _durable_order(world) if cid in set(memory)]
    assert memory == durable, (where, memory, _durable_order(world))


SEQUENCES = 120


@pytest.mark.parametrize("seed", range(SEQUENCES))
def test_memory_and_durable_order_agree_over_random_sequences(tmp_path, seed):
    """Any mix of admissions (agent, user, a card answer, a front desk relay),
    turn ends, a Stop and its Resume, the drain's fence lifted or handed over,
    a failed launch put back at the head, and a restart: after every step the
    memory queue is the durable queue's order, and in the end every item ran
    exactly once."""
    import random
    rng = random.Random(seed)
    world = World(tmp_path)
    world.send("t0")
    sent: list[str] = ["t0"]
    fenced = False
    count = 0

    def running():
        return [c for c in world.provider.spawned if c["agent_id"] in world.provider.live]

    for step in range(rng.randint(10, 22)):
        action = rng.choice(["agent", "agent", "agent", "user", "card", "desk", "end", "end",
                             "stop", "resume", "fence", "lift", "handover", "restart",
                             "fail", "retry", "prepfail", "legacy"])
        count += 1
        cid = f"{action[0]}{count}"
        if action in ("agent", "user", "card", "desk"):
            try:
                if action == "agent":
                    world.send(cid, origin="agent", queue=True)
                elif action == "user":
                    world.send(cid, queue=True)
                else:
                    world.service.dispatch(
                        text=cid, requested_session="mike", trace_id=f"t-{cid}",
                        client_msg_id=cid, synthesize_audio=False, queue_if_busy=True,
                        origin="automation" if action == "card" else "agent",
                        sender_agent_id="" if action == "card" else "desk-agent",
                        user_word=True)
                sent.append(cid)
            except DispatchError:
                pass                      # its own launch failed: the sender was told
        elif action == "end" and running():
            _end_running(world)
        elif action == "stop":
            world.stop()
        elif action == "resume" and turn_queue.is_paused(world.agent):
            turn_queue.set_paused(world.agent, False)
            world.service.recover_queued()
        elif action == "fence" and not fenced:
            world.service.begin_admission_fence(); fenced = True
        elif action == "lift" and fenced:
            world.service.lower_admission_fence(); world.service.start_held_work(); fenced = False
        elif action == "handover" and fenced:
            world.service.persist_held(world.agent)
            world._collect(); td.reset_for_tests(); world._new_process(); fenced = False
        elif action == "restart":
            world._collect(); td.reset_for_tests(); world._new_process(); fenced = False
        elif action == "fail":
            _arm(world); world.provider.fail_next = True
        elif action == "retry":
            world.service._retry_handoff(world.agent)
        elif action == "prepfail" and not running() and not fenced:
            _fail_before_the_turn_opens(world)
        elif action == "legacy":
            # Rows stamped by no release before this one: only a turn row says
            # one was chosen. A restart re-admits them by that judgement.
            agents_db.conn().execute("UPDATE queued_turns SET taken = 0")
            world._collect(); td.reset_for_tests(); world._new_process(); fenced = False
        world._collect()
        _agree(world, (seed, step, action))
    # Let everything run: lift the fence, resume, finish every turn.
    if fenced:
        world.service.lower_admission_fence(); world.service.start_held_work()
    if isinstance(world.provider, _FailingProvider):
        world.provider.fail_next = False
    for _ in range(3):
        if turn_queue.is_paused(world.agent):
            turn_queue.set_paused(world.agent, False)
        world.service._retry_handoff(world.agent)
        world.settle()
        _agree(world, (seed, "settle"))
    world._collect()
    texts = world.started              # every launch, across the restarts
    # A row whose own launch failed in an unknown state is never run again
    # (it may have reached the backend): it stays, and is the only kind that may.
    uncertain = {row["client_msg_id"] for row in turn_queue.pending(world.agent)} - {
        row["client_msg_id"] for row in turn_queue.pending_for_dispatch()}
    assert set(_waiting(world)) <= uncertain, (seed, _durable_order(world), uncertain)
    assert sorted(set(texts) | uncertain) == sorted(set(sent) | uncertain), (seed, texts, sent)
    assert len(texts) == len(set(texts)), (seed, texts)


def test_resume_puts_held_user_word_ahead_of_a_later_one_that_ran_past_the_pause(tmp_path):
    """Found by the random sequences (seed 19): a card the user answered was
    held by a Stop; a later user message waited in memory behind a turn that
    ran past the pause. On Resume the card is re-admitted and goes first, as
    the durable order has it: a user's item never overtakes an earlier one."""
    world = World(tmp_path)
    world.send("t0")
    world.service.dispatch(
        text="c1", requested_session="mike", trace_id="t-c1", client_msg_id="c1",
        synthesize_audio=False, queue_if_busy=True, origin="automation", user_word=True)
    world.send("p1", origin="agent", queue=True)
    world.stop()
    world.send("u2")                       # runs past the pause
    world.send("u3", queue=True)           # waits behind u2, in memory
    world._collect()
    assert world.started == ["t0", "u2"]
    assert turn_queue.set_paused(world.agent, False)
    world.service.recover_queued()
    assert _memory(world) == ["c1", "u3", "p1"]
    assert _waiting(world) == ["c1", "u3", "p1"]
    world.settle()
    assert world.started == ["t0", "u2", "c1", "u3", "p1"]


def test_the_users_word_admitted_during_a_stop_starts_first_and_the_rest_is_held(tmp_path):
    from test_stop_holds_queue import _stop_with_arrivals
    world = World(tmp_path)
    world.send("t0")
    world.send("p_pre", origin="agent", queue=True)
    world.send("p_pre2", origin="agent", queue=True)
    _stop_with_arrivals(world, [("u_mid", "user", False)])
    world.service.start_held_work()
    world.settle()
    assert world.started == ["t0", "u_mid"]
    assert _waiting(world) == ["p_pre", "p_pre2"]
    assert turn_queue.is_paused(world.agent)
    _resume(world)
    assert world.started == ["t0", "u_mid", "p_pre", "p_pre2"]


def _fail_before_the_turn_opens(world):
    """The head of the memory queue takes the free slot and its launch fails
    in the 'prepare' phase, before any turn row: the dispatcher puts it back
    at the head (requeue_front)."""
    with td._TURN_LOCK:
        if td._SLOTS.get(world.agent) or not td._SLOTS.queue_depth(world.agent):
            return None
        head = td._SLOTS.pop_next(world.agent)
    if head is None:
        return None
    world.service._requeue_failed_handoff(
        world.agent, head, RuntimeError("prepare failed"), phase="prepare")
    return head


def test_a_launch_that_failed_before_its_turn_is_taken_in_both_stores(tmp_path):
    """Review lead L5: requeue_front marks it taken in memory; the durable row
    is stamped too, so a user's word arriving meanwhile goes behind it on both
    sides (held behind the drain's fence so nothing starts), and still after
    a restart."""
    world = World(tmp_path)
    world.send("t0")
    world.send("p1", origin="agent", queue=True)
    world.send("p2", origin="agent", queue=True)
    td._SLOTS.release(world.agent, "t-t0")     # t0's slot is free; its process is not ours
    world.provider.live.discard(world.agent)
    head = _fail_before_the_turn_opens(world)
    assert head is not None and head.client_msg_id == "p1"
    assert turn_queue.get("p1")["taken"] == 1
    assert not turn_queue._has_turn(turn_queue.get("p1"))
    world.service.begin_admission_fence()      # nothing starts while we look
    world.send("u1", queue=True)
    assert _memory(world) == ["p1", "u1", "p2"]
    assert _waiting(world) == ["p1", "u1", "p2"]
    world.service.persist_held(world.agent)
    world.restart()
    assert world.started[-3:] == ["p1", "u1", "p2"]


def _proven_unlaunched(con, agent_id, trace_id):
    """The durable state a launch leaves when the Host has proof it never
    reached the backend: a turn settled 'failed' and an 'interrupted' state
    naming that turn with dispatch_not_started. Recovery re-admits such a row."""
    import json
    cur = con.execute(
        "INSERT INTO turns (agent_id, source, trace_id, started_at, ended_at, settled_at, outcome)"
        " VALUES (?, 'pwa', ?, 1, 2, 2, 'failed')", (agent_id, trace_id))
    con.execute("INSERT INTO state_log (agent_id, ts, kind, detail) VALUES (?, 3, 'interrupted', ?)",
                (agent_id, json.dumps({"trace_id": trace_id, "dispatch_not_started": True,
                                       "dispatch_not_started_turn_id": cur.lastrowid})))


def test_a_re_admitted_row_whose_turn_opened_is_taken_in_memory(tmp_path):
    """Review lead L1: a row whose launch had begun (a turn row) is taken by
    the durable judgement. Re-admitted by recovery while the agent is busy, its
    memory spec is taken too, so a later user's word lands after it on both
    sides. It carries no taken stamp (only its turn row speaks)."""
    world = World(tmp_path)
    con = agents_db.conn()
    world.send("t0")                                         # busy with an owned turn
    world.send("a1", origin="agent", queue=True)
    world.send("q1", origin="agent", queue=True)
    _proven_unlaunched(con, world.agent, "t-a1")
    td._SLOTS.drop_queued(world.agent, lambda spec: spec.queue_id == "a1")   # memory lost it
    row = turn_queue.get("a1")
    assert not row["taken"] and turn_queue.row_taken(row)
    assert "a1" in {r["client_msg_id"] for r in turn_queue.pending_for_dispatch()}
    world.service.recover_queued()             # re-admits a1 behind t0, durable order
    assert _memory(world) == ["a1", "q1"]
    assert [s.taken for s in td._SLOTS.queued_specs()[world.agent]] == [True, False]
    world.send("u1", queue=True)
    assert _memory(world) == ["a1", "u1", "q1"]
    assert _waiting(world) == ["a1", "u1", "q1"]


def test_an_agent_withdrawing_its_card_is_not_the_users_word():
    """Review lead L3: the only other writer of resolved_by writes 'agent'
    (no session) with choice 'withdrawn'; neither is an answer."""
    assert not origins.is_user_word("automation", card_answered_by="agent", card_choice="withdrawn")
    assert not origins.is_user_word("automation", card_answered_by="agent:desk",
                                    card_choice="withdrawn")
    assert not origins.is_user_word("automation", card_answered_by="agent", card_choice="answered")


def test_recovery_passes_over_a_busy_agent_leave_its_rows_overtakable(tmp_path):
    """Review: a recovery pass that defers or skips a busy agent's rows does
    not make them taken; a later user's word still goes first."""
    world = World(tmp_path)
    _busy_with_peers(world)
    td._SLOTS.drop_queued(world.agent, lambda spec: True)   # durable only, as after a restart
    for _ in range(3):
        world.service.recover_queued()                      # busy: deferred each time
    assert not any(turn_queue.row_taken(turn_queue.get(c)) for c in ("p1", "p2", "p3"))
    world.send("u1", queue=True)
    assert _waiting(world) == ["u1", "p1", "p2", "p3"]
    world.settle()
    assert world.started == ["t0", "u1", "p1", "p2", "p3"]


def test_a_claim_released_unlaunched_leaves_the_row_overtakable(tmp_path):
    """Review: a claim that is released without a launch (recovery backing
    off, a withdrawn send) is not a choice that sticks."""
    world = World(tmp_path)
    _busy_with_peers(world)
    assert turn_queue.claim("p1") is not None
    assert turn_queue.release_claim("p1")
    assert not turn_queue.row_taken(turn_queue.get("p1"))
    world.send("u1", queue=True)
    assert _waiting(world) == ["u1", "p1", "p2", "p3"]


def test_a_withdrawn_card_records_no_answer_and_no_delivery(tmp_path):
    """Review lead L3, through the store: withdraw writes resolved_by 'agent'
    and choice 'withdrawn' and wakes nobody; neither reads as the user's word."""
    from lib import artifacts
    World(tmp_path)
    card = artifacts.create_decision(session="mike", title="T", question="Proceed?")
    decision_id = card["decision"]["decision_id"]
    artifacts.withdraw(decision_id, session="mike")
    row = agents_db.conn().execute(
        "SELECT resolved_by, resolved_choice FROM artifact_decisions WHERE decision_id = ?",
        (decision_id,)).fetchone()
    assert tuple(row) == ("agent", "withdrawn")
    assert not origins.is_user_word("automation", card_answered_by=row[0], card_choice=row[1])
    assert decision_id not in {d["decision_id"] for d in artifacts.pending_deliveries()}


def test_a_card_the_user_resolved_is_delivered_as_the_users_word(tmp_path):
    """The delivery row the worker reads carries resolved_by and choice, the
    two facts the rule reads."""
    from lib import artifacts
    World(tmp_path)
    card = artifacts.create_decision(
        session="mike", title="T", question="Which?", response_type="single_choice",
        options=[{"id": "a", "label": "A"}, {"id": "b", "label": "B"}])
    decision_id = card["decision"]["decision_id"]
    artifacts.resolve(decision_id, expected_revision=1, answer={"text": "keep A"})
    delivery = next(d for d in artifacts.pending_deliveries() if d["decision_id"] == decision_id)
    assert (delivery["resolved_by"], delivery["choice"]) == ("user", "answered")
    assert origins.is_user_word("automation", card_answered_by=delivery["resolved_by"],
                                card_choice=delivery["choice"])
