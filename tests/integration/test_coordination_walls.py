"""The walls that keep an agent from passing its message off as the user's (issue 478, F1),
each pinned through the real routes. Each test fails on the reviewer's surviving mutant:

W1: POST /send itself refuses a trace that belongs to another message (the route's call of
    SendRequest.require_new_trace removed).
W3: a delta of GET /log reads a mark forced onto a trace that holds the user's word as the
    user's (TRACE_AUDIENCE_SQL replaced by the bare mark in the delta's query).
W5: a send naming another sender that reuses the user's trace and client id is no retry (a retry
    matched by client id and origin only, not by sender)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server, server_module)


def _call(base, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read() or b"null"
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode(errors="replace")


def _users_send(agent_id, *, admission=False):
    """The user's own message on trace t-user, client c-user (and its admission when asked)."""
    from lib import agents, prompt_admissions
    session = agents.live_backend_session(agent_id)
    if not session:
        session = "native"
        agents.bind_backend_session(agent_id, session)
    if admission:               # filed before its row, as /send files it
        assert prompt_admissions.record(prompt_admissions.create(
            authenticated_at_admission=True, origin="user", sender_agent_id="", channel="chat",
            observed_at=1, client_admission_id="c-user", trace_id="t-user", original_text="the user's words"),
            agent_id=agent_id, session="claude")
    agents.record_user_message(agent_id=agent_id, backend_session_id=session, client_msg_id="c-user",
                               text="the user's words", trace_id="t-user", origin="user")
    return session


def _no_dispatch(monkeypatch):
    from lib.turn_dispatch import TurnDispatchService
    submitted = []
    monkeypatch.setattr(TurnDispatchService, "submit", lambda self, command: submitted.append(command))
    return submitted


def test_w1_the_send_route_refuses_another_messages_trace(running_server, monkeypatch):
    from lib import agents
    submitted = _no_dispatch(monkeypatch)
    base, _ctx, _srv = running_server
    _users_send(agents.get_by_session("claude")["agent_id"])
    status, body = _call(base, "POST", "/send", {"text": "mine now", "session": "claude", "origin": "agent",
                                                 "trace_id": "t-user", "client_msg_id": "c-agent"})
    assert status == 409 and "trace_id already belongs to another message" in str(body)
    assert submitted == []


def test_w5_another_sender_reusing_the_users_trace_and_client_id_is_no_retry(running_server, monkeypatch):
    from lib import agents
    submitted = _no_dispatch(monkeypatch)
    base, _ctx, _srv = running_server
    agents.create_agent(persona="Boss", session="boss", voice_id="v", cwd="/tmp")
    _users_send(agents.get_by_session("claude")["agent_id"], admission=True)
    # The same client id, trace and origin as the user's own send: only the sender differs.
    status, _ = _call(base, "POST", "/send", {"text": "mine now", "session": "claude", "origin": "user",
                                              "sender": "boss", "trace_id": "t-user", "client_msg_id": "c-user"})
    assert status == 409 and submitted == []


def test_w3_a_delta_reads_a_forced_mark_on_the_users_trace_as_the_users(running_server):
    from lib import agents, db
    base, _ctx, _srv = running_server
    agent_id = agents.get_by_session("claude")["agent_id"]
    agents.update_agent(agent_id, backend="claude")     # an agent that can leave a note: one that can be hidden
    session = agents.live_backend_session(agent_id)
    if not session:
        session = "native"
        agents.bind_backend_session(agent_id, session)
    agents.record_user_message(agent_id=agent_id, backend_session_id=session, client_msg_id="c-earlier",
                               text="an earlier message", trace_id="t-earlier", origin="user")
    status, before = _call(base, "GET", "/log?session=claude")
    assert status == 200
    cursor = int(before["latest_revision"])
    assert cursor > 0                                   # so the next read is a delta, not a tail page
    _users_send(agent_id)
    with db.conn() as con:      # past every writer: the mark forced onto the user's own turn
        con.execute("INSERT INTO coordination_traces(agent_id, trace_id, sender_agent_id, audience, created_at,"
                    " updated_at) VALUES (?, 't-user', '', 'agents', 1, 1)", (agent_id,))
    db.close_local()
    status, delta = _call(base, "GET", f"/log?session=claude&after_revision={cursor}")
    assert status == 200
    rows = [t for t in delta["turns"] if t.get("trace_id") == "t-user"]
    assert rows and {t["audience"] for t in rows} == {"user"}
    assert all(t.get("trace_id") != "t-earlier" for t in delta["turns"])   # it was a delta
