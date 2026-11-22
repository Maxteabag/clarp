"""R193: a queued message whose launch fails before its backend starts (the agent's
directory is gone) backed off, but the ownerless-queue start relaunched it at once,
every second for an hour, each try writing a turn row; and the row it left, shown
and 'queued', was never settled, listed or deletable. Now the back-off holds, the
trace keeps one turn row, the handoff gives up after HANDOFF_GIVE_UP tries (failed,
its sender told), and recovery ends such a row left from before."""
from __future__ import annotations

from lib import agents as agents_db
from lib import db, turn_queue
from lib import turn_dispatch as _td
from lib import turn_handoff
from lib.turn_dispatch import DispatchError

from test_turn_dispatch import _Backends, _make_service


class _GoneDirectoryBackends(_Backends):
    """Every launch fails before the backend starts, as a missing cwd does."""

    def __init__(self):
        super().__init__()
        self.tries = 0
        self.fail = True

    def spawn_turn(self, backend, **kwargs):
        if self.fail and kwargs.get("trace_id") == "t-queued":
            self.tries += 1
            raise DispatchError(500, "[Errno 2] No such file or directory: '/gone'")
        super().spawn_turn(backend, **kwargs)


def _queued_behind_a_turn(tmp_path):
    scheduled = []
    service, _backends, agent_id = _make_service(
        tmp_path, retry_scheduler=lambda delay, fn: scheduled.append((delay, fn)))
    backends = _GoneDirectoryBackends()
    service.backends = backends
    service.dispatch(text="first", requested_session="mike", trace_id="t-first",
                     synthesize_audio=False)
    service.dispatch(text="queued", requested_session="mike", trace_id="t-queued",
                     client_msg_id="m-queued", synthesize_audio=False, queue_if_busy=True)
    backends.live = False
    backends.spawned[0][1]["on_result"]({"duration_ms": 1})       # the first turn ends: handoff
    return service, backends, agent_id, scheduled


def _turn_rows(agent_id, trace_id):
    return db.conn().execute(
        "SELECT turn_id, ended_at, settled_at FROM turns WHERE agent_id=? AND trace_id=?",
        (agent_id, trace_id)).fetchall()


def test_the_ownerless_start_waits_for_the_back_off(tmp_path):
    service, backends, agent_id, scheduled = _queued_behind_a_turn(tmp_path)
    assert backends.tries == 1
    for _ in range(5):                                   # the leak check's passes meanwhile
        service._start_ownerless_queues(None)
    assert backends.tries == 1, "relaunched before its back-off was over"
    retries = [fn for delay, fn in scheduled if delay >= 1.0]
    retries[-1]()                                        # its own retry, when due
    assert backends.tries == 2
    assert len(_turn_rows(agent_id, "t-queued")) == 1, "one turn row per trace, every try"


def test_after_the_last_try_the_message_ends_failed_and_its_sender_is_told(tmp_path):
    service, backends, agent_id, scheduled = _queued_behind_a_turn(tmp_path)
    while backends.tries < turn_handoff.HANDOFF_GIVE_UP:
        pending = [fn for delay, fn in scheduled if delay >= 1.0]
        scheduled.clear()
        assert pending, f"no retry scheduled after {backends.tries} tries"
        pending[-1]()
    assert backends.tries == turn_handoff.HANDOFF_GIVE_UP
    assert turn_queue.status("m-queued") == "failed"
    assert [fn for delay, fn in scheduled if delay >= 1.0] == [], "not tried again"
    rows = _turn_rows(agent_id, "t-queued")
    assert len(rows) == 1 and rows[0]["ended_at"] is not None, "one turn row, closed"
    notice = db.conn().execute(
        "SELECT text FROM messages WHERE agent_id=? AND message_id LIKE ?",
        (agent_id, f"%{turn_queue.REFUSED_LAUNCH}:m-queued%")).fetchone()
    assert notice is not None and "No such file or directory" in notice["text"]
    assert _td._SLOTS.get(agent_id) in (None, ""), "the slot is free for what comes next"


def _stuck_shown_row(agent_id, *, queue_id="q-stuck", trace_id="t-stuck", age_ms=3_600_000,
                     dispatched=False, attempts=3):
    now = db.now_ms()
    turn_queue.enqueue(queue_id=queue_id, agent_id=agent_id, session="mike", text="stale ask",
                       trace_id=trace_id, client_msg_id=queue_id, synthesize_audio=False,
                       origin="agent", sender_agent_id="")
    con = db.conn()
    con.execute("UPDATE queued_turns SET shown_at=? WHERE queue_id=?", (now - age_ms, queue_id))
    for i in range(attempts):
        con.execute("INSERT INTO turns (agent_id, source, trace_id, synthesize_audio, started_at)"
                    " VALUES (?, 'pwa', ?, 0, ?)", (agent_id, trace_id, now - age_ms + i))
    if dispatched:
        agents_db.record_state(agent_id, "thinking", {"trace_id": trace_id, "source": "pwa",
                                                      "dispatch": "spawned"})


def test_recovery_ends_a_shown_row_whose_launches_never_reached_a_backend(tmp_path):
    service, _backends, agent_id = _make_service(tmp_path)
    _stuck_shown_row(agent_id)
    assert turn_queue.waiting_items(agent_id) == []              # GET never listed it
    assert turn_queue.remove("q-stuck") is False                  # DELETE refused it
    service._end_unlaunched_shown()
    assert turn_queue.status("q-stuck") == "failed", "no shown row is left 'queued'"
    assert all(r["ended_at"] is not None for r in _turn_rows(agent_id, "t-stuck"))


def test_recovery_leaves_a_row_that_reached_a_backend_or_is_recent(tmp_path):
    service, _backends, agent_id = _make_service(tmp_path)
    _stuck_shown_row(agent_id, queue_id="q-ran", trace_id="t-ran", dispatched=True)
    _stuck_shown_row(agent_id, queue_id="q-new", trace_id="t-new", age_ms=60_000)
    service._end_unlaunched_shown()
    assert turn_queue.status("q-ran") == "queued", "a backend started for it: not ours to fail"
    assert turn_queue.status("q-new") == "queued", "a launch a minute old may still be going"
