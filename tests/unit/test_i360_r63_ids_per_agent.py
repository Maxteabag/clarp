"""Issue 360, R63: the queue's key and a user row's id are Host-wide, so a
second agent's message under an app id another agent holds was dropped as a
duplicate of the first, while /send answered queued:true. It is refused now,
409 'client_msg_id is held by another agent', with nothing written. The apps
mint a fresh id per message; only a client bug meets this."""
from __future__ import annotations

import itertools

import pytest

from lib import agents as agents_db
from lib import db, turn_queue
from lib.turn_dispatch import DispatchError

from test_turn_dispatch import _make_service


def _two_agents(tmp_path):
    service, backends, mike = _make_service(tmp_path)
    counter = itertools.count(1)
    service.uuid_factory = lambda: f"backend-session-{next(counter)}"
    bella = agents_db.create_agent(persona="Bella", voice_id="V", cwd=str(tmp_path),
                                   session="bella", backend="claude")
    agents_db.start_runtime(bella, "bella")
    return service, backends, mike, bella


def _written_for(agent_id):
    con = db.conn()
    return [con.execute(sql, (agent_id,)).fetchone()[0] for sql in (
        "SELECT COUNT(*) FROM queued_turns WHERE agent_id=? AND client_msg_id='same-id'",
        "SELECT COUNT(*) FROM prompt_admissions WHERE agent_id=? AND client_admission_id='same-id'",
        "SELECT COUNT(*) FROM messages WHERE agent_id=? AND message_id='u-same-id'")]


@pytest.mark.parametrize("first_waits", [True, False], ids=["queue-item", "user-row"])
def test_a_second_agents_send_under_a_held_id_is_refused_not_dropped(tmp_path, first_waits):
    service, backends, mike, bella = _two_agents(tmp_path)
    for session in ("mike", "bella"):           # both busy
        service.dispatch(text=f"first for {session}", requested_session=session,
                         trace_id=f"t-first-{session}", synthesize_audio=False)
    if not first_waits:                         # mike idle again: his send starts and writes its row
        backends.spawned[0][1]["on_result"]({"duration_ms": 1})
    service.dispatch(text="for mike", requested_session="mike", trace_id="t-mike",
                     client_msg_id="same-id", synthesize_audio=False, queue_if_busy=True)
    with pytest.raises(DispatchError) as refused:
        service.dispatch(text="for bella", requested_session="bella", trace_id="t-bella",
                         client_msg_id="same-id", synthesize_audio=False, queue_if_busy=True)
    assert (refused.value.status, str(refused.value)) == (409, turn_queue.CLIENT_ID_HELD_ELSEWHERE)
    assert _written_for(bella) == [0, 0, 0]


def test_the_same_agent_retrying_its_own_id_is_still_a_retry(tmp_path):
    service, backends, mike, bella = _two_agents(tmp_path)
    service.dispatch(text="first", requested_session="mike", trace_id="t-first",
                     synthesize_audio=False)
    for _ in range(2):
        result = service.dispatch(text="for mike", requested_session="mike", trace_id="t-mike",
                                  client_msg_id="same-id", synthesize_audio=False, queue_if_busy=True)
        assert result.queued is True
    assert [item["text"] for item in turn_queue.pending(mike)] == ["for mike"]
