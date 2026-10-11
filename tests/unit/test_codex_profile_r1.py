"""R1: the remaining Host reads give an identified non-EU turn nothing of an
EU (provider-profile) agent, and the event stream fails closed.

Every request here carries an ordinary agent's turn identity
(``X-Clarp-Turn``). The control in each test is either the same read about an
ordinary agent, or the user's device (no turn identity), which sees
everything. Only requests that identify their turn can be filtered (every
agent runs as the same Unix user; see docs/codex-eu-profile.md).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import events

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "SENTINEL-EU-R1-93ad"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("clarp_server_r1", ROOT / "server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fleet():
    from lib import provider_background_jobs as jobs
    eu = agents_db.create_agent(persona="Eunice", voice_id="V", cwd="/tmp", session="eunice",
                                backend="codex", provider_profile="example-eu")
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp", session="plain",
                                   backend="codex")
    for agent_id, session in ((eu, "eunice"), (plain, "plain")):
        agents_db.start_runtime(agent_id, session)
    token, _ = jobs.identity_env(plain)
    jobs.record_identity(token, agent_id=plain, provider="codex", pid=1)
    return SimpleNamespace(eu=eu, plain=plain, turn=token)


class _Out:
    status = None
    body = ""


def _get(server, path, handler_name, *, turn="", args=()):
    """One GET through the real refusal and response writer."""
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.headers = {"X-Clarp-Turn": turn} if turn else {}
    handler.server = SimpleNamespace(ctx=SimpleNamespace(
        default_session="plain", auth_token="tok", stream=None,
        agents_path=Path("/nonexistent/agents.json")))
    out = _Out()
    import io
    # The real response writer runs (so whatever it does to a body is
    # tested); only the socket is in memory.
    handler.wfile = io.BytesIO()
    handler.send_response = lambda status, *_a: setattr(out, "status", status)
    handler.send_header = handler.end_headers = lambda *a, **k: None
    getattr(handler, "_eu_begin_request", lambda: None)()
    refusal = handler._eu_only_read_refusal(path.split("?", 1)[0])
    if refusal:
        out.status, out.body = 403, json.dumps({"error": refusal})
        return out
    getattr(handler, handler_name)(*args)
    out.body = handler.wfile.getvalue().decode(errors="replace")
    return out


def test_prompt_history_of_an_eu_agent_is_refused_to_another_agents_turn(server, fleet,
                                                                          monkeypatch):
    from lib import prompt_history
    monkeypatch.setattr(prompt_history, "get_server_info", lambda: {"server_id": "cid-1"})
    monkeypatch.setattr(prompt_history, "build_prompt_history", lambda **kw: {
        "session_id": kw["session_id"], "prompts": [{"text": f"customer {SENTINEL}"}]})

    eu = _get(server, "/identity/prompt-history?session_id=cid-1:eunice",
              "_handle_prompt_history", turn=fleet.turn)
    compat = _get(server, "/identity/prompt-history?session=eunice",
                  "_handle_prompt_history", turn=fleet.turn)
    plain = _get(server, "/identity/prompt-history?session_id=cid-1:plain",
                 "_handle_prompt_history", turn=fleet.turn)
    user = _get(server, "/identity/prompt-history?session_id=cid-1:eunice",
                "_handle_prompt_history")

    assert eu.status == 403 and compat.status == 403
    assert SENTINEL not in eu.body + compat.body
    assert plain.status == 200                       # control: an ordinary agent
    assert user.status == 200 and SENTINEL in user.body   # the user's device


def test_inbox_pinned_and_voice_reads_drop_an_eu_agent(server, fleet, monkeypatch):
    from lib import artifacts, attention_index, voice_events
    rows = [{"agent_id": fleet.eu, "session": "eunice", "title": SENTINEL},
            {"agent_id": fleet.plain, "session": "plain", "title": "plainword"}]
    monkeypatch.setattr(attention_index, "page", lambda **_k: {"artifacts": rows,
                                                               "next_cursor": ""})
    monkeypatch.setattr(artifacts, "list_pinned", lambda: rows)
    monkeypatch.setattr(artifacts, "response_representation", lambda row, _r: row)
    monkeypatch.setattr(voice_events, "query", lambda **_k: [
        {"session": "eunice", "text": SENTINEL}, {"session": "plain", "text": "plainword"}])
    monkeypatch.setattr(voice_events, "utterances", lambda **_k: [
        {"session": "eunice", "transcript": SENTINEL},
        {"session": "plain", "transcript": "plainword"}])

    for path, name in (("/attention/inbox", "_handle_attention_inbox"),
                       ("/pinned-artifacts", "_handle_pinned_artifacts"),
                       ("/voice-events", "_handle_voice_events_get"),
                       ("/voice-events/utterances", "_handle_voice_utterances_get")):
        as_agent = _get(server, path, name, turn=fleet.turn)
        as_user = _get(server, path, name)
        assert SENTINEL not in as_agent.body, path
        assert "plainword" in as_agent.body, path      # control: the rest still flows
        assert SENTINEL in as_user.body, path           # the user's device


def test_reads_by_plan_or_trace_id_refuse_an_eu_agents_content(server, fleet, monkeypatch,
                                                               tmp_path):
    from lib import goal_ledger, heard_audio, task_plans
    eu_plan = task_plans.create(session="eunice", title=f"Case {SENTINEL}",
                                items=[{"id": "a", "title": "step"}])
    plain_plan = task_plans.create(session="plain", title="plainword",
                                   items=[{"id": "a", "title": "step"}])
    monkeypatch.setattr(task_plans, "goal_document",
                        lambda plan_id, *_a: {"plan_id": plan_id, "text": SENTINEL})
    monkeypatch.setattr(goal_ledger, "events",
                        lambda plan_id, **_k: {"events": [{"note": SENTINEL}]})
    clip = tmp_path / "t-eu.wav"
    clip.write_bytes(SENTINEL.encode())
    monkeypatch.setattr(heard_audio, "lookup", lambda _dir, trace: (
        (clip, {"session": "eunice", "content_type": "audio/wav"}) if trace == "t-eu"
        else (clip, {"session": "plain", "content_type": "audio/wav"})))

    reads = ((f"/task-plan/document?plan_id={eu_plan['plan_id']}&name=goal",
              f"/task-plan/document?plan_id={plain_plan['plan_id']}&name=goal",
              "_handle_task_goal_document"),
             (f"/goal-ledger?plan_id={eu_plan['plan_id']}",
              f"/goal-ledger?plan_id={plain_plan['plan_id']}", "_handle_goal_ledger"),
             ("/transcription-audio?trace_id=t-eu", "/transcription-audio?trace_id=t-plain",
              "_handle_transcription_audio_get"))
    for eu_path, plain_path, name in reads:
        assert _get(server, eu_path, name, turn=fleet.turn).status == 403, eu_path
        assert _get(server, plain_path, name, turn=fleet.turn).status != 403, plain_path
        assert _get(server, eu_path, name).status != 403, eu_path   # the user's device


# --- the event stream fails closed -------------------------------------------------

def _sample(event_type: str, owner: str | None) -> dict:
    """An event of ``event_type`` with every field set: owner fields name
    ``owner`` (or are empty), and text fields carry the sentinel."""
    fields = events.FIELDS[event_type]
    event = {"type": event_type}
    owner_fields = events.OWNER_FIELDS.get(event_type, ())
    for name in fields:
        if name in owner_fields:
            if name == "participants":
                event[name] = [{"session": owner}] if owner else []
            elif name in ("agent",):
                event[name] = {"session": owner} if owner else None
            else:
                event[name] = owner or ""
        else:
            event[name] = SENTINEL
    return event


def test_every_event_type_has_an_owner_or_is_declared_content_free():
    for event_type, fields in events.FIELDS.items():
        owners = events.OWNER_FIELDS.get(event_type, ())
        free = event_type in events.CONTENT_FREE
        assert bool(owners) != free, f"{event_type}: owner fields xor content-free"
        assert set(owners) <= set(fields), event_type
        if free:   # content-free types carry no text that could hold an agent's words
            assert not set(fields) & events.TEXT_FIELDS, event_type


def test_the_stream_drops_every_eu_or_ownerless_event_for_another_agents_turn(fleet):
    from lib import codex_profiles
    ids, sessions = codex_profiles.hidden_from(fleet.plain)
    for event_type in events.FIELDS:
        if event_type in events.CONTENT_FREE:
            assert codex_profiles.event_visible(_sample(event_type, None), ids, sessions)
            continue
        for owner in (fleet.eu, "eunice"):
            assert not codex_profiles.event_visible(_sample(event_type, owner), ids, sessions), \
                (event_type, owner)
        assert not codex_profiles.event_visible(_sample(event_type, None), ids, sessions), \
            f"{event_type} without an owner must fail closed"
        assert codex_profiles.event_visible(_sample(event_type, "plain"), ids, sessions), \
            f"{event_type} of an ordinary agent still flows"
    assert not codex_profiles.event_visible({"type": "no-such-type"}, ids, sessions)
    # The user's device (no turn identity) is not filtered at all.
    assert all(codex_profiles.event_visible(_sample(t, "eunice"), frozenset(), frozenset())
               for t in events.FIELDS)


def test_the_live_stream_withholds_an_ownerless_event_from_another_agents_turn(server, fleet):
    """Wired through /events: an event that names no agent (here an
    orchestrator decision whose target is unset) is not delivered to an
    identified non-EU turn while an EU agent exists; the user still gets it."""
    import io
    import queue as queue_module
    sent = [
        {"event_id": 1, "type": "orchestrator-decision", "decision_id": "d1",
         "reason": f"route the invoice {SENTINEL}"},
        {"event_id": 2, "type": "agent-state", "agent_id": fleet.plain, "session": "plain",
         "kind": "done", "status_text": "plainword"},
    ]

    def stream_output(turn):
        q = queue_module.Queue()
        for event in sent:
            q.put(json.dumps(event))
        q.evicted = True
        handler = object.__new__(server.Handler)
        handler.path = "/events"
        handler.headers = {"X-Clarp-Turn": turn} if turn else {}
        handler.server = SimpleNamespace(ctx=SimpleNamespace(
            default_session="plain", stream=SimpleNamespace(
                subscribe=lambda live=None: q, recent=lambda _c: [],
                unsubscribe=lambda _q: None)))
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = \
            lambda *a, **k: None
        handler._live_subscription = lambda: None
        server.SERVER_TIMING = SimpleNamespace(sse_queue_timeout_sec=0.05)
        handler._sse()
        return handler.wfile.getvalue().decode()

    as_agent, as_user = stream_output(fleet.turn), stream_output("")
    assert SENTINEL not in as_agent and "plainword" in as_agent
    assert SENTINEL in as_user
