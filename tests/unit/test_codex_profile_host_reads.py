"""H1/H2: another agent's turn never receives, or writes over, an EU agent.

Requests that carry an ordinary agent's turn identity (``X-Clarp-Turn``) see
no content of a provider-profile agent on the Host's read paths, and cannot
schedule text onto an agent outside a profile agent's own profile. The user's
devices (no turn identity) keep full access: that is each test's control.
"""
from __future__ import annotations

import importlib.util
import io
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import artifacts, background_jobs, scheduler, team_store

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "SENTINEL-EU-HOST-5b0e"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("clarp_server_h1", ROOT / "server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fleet():
    eu = agents_db.create_agent(persona="Eunice", voice_id="V", cwd="/tmp", session="eunice",
                                backend="codex", provider_profile="example-eu")
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp", session="plain",
                                   backend="codex")
    for agent_id, session in ((eu, "eunice"), (plain, "plain")):
        agents_db.start_runtime(agent_id, session)
    from lib import provider_background_jobs as jobs
    token, _ = jobs.identity_env(plain)
    jobs.record_identity(token, agent_id=plain, provider="codex", pid=1)
    eu_token, _ = jobs.identity_env(eu)
    jobs.record_identity(eu_token, agent_id=eu, provider="codex", pid=1)
    return SimpleNamespace(eu=eu, plain=plain, plain_turn=token, eu_turn=eu_token)


class _Out:
    def __init__(self):
        self.status, self.body = None, None


def _call(server, method, path, *, turn="", data=None, default_session="eunice", stream=None):
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.headers = {"X-Clarp-Turn": turn} if turn else {}
    handler.server = SimpleNamespace(ctx=SimpleNamespace(
        default_session=default_session, stream=stream,
        agents_path=Path("/nonexistent/agents.json")))
    out = _Out()

    def json_out(status, value):
        out.status, out.body = status, json.dumps(value, default=str)

    handler._json = json_out
    handler._json_ok = lambda value: json_out(200, value)
    handler._json_error = lambda status, message: json_out(status, {"error": message})
    handler._send = lambda status, body=b"", *a, **k: json_out(
        status, {"raw": body.decode() if isinstance(body, bytes) else str(body)})
    handler._read_json = lambda: data
    if method == "GET":
        refusal = handler._eu_only_read_refusal(path.split("?", 1)[0])
        if refusal:
            json_out(403, {"error": refusal})
            return out
    return out, handler


def _get(server, path, handler_name, *, turn="", args=(), **kw):
    result = _call(server, "GET", path, turn=turn, **kw)
    if isinstance(result, _Out):
        return result
    out, handler = result
    getattr(handler, handler_name)(*args)
    return out


def test_log_without_a_session_never_falls_back_to_a_focused_eu_agent(server, fleet):
    refused = _get(server, "/log", "_handle_log", turn=fleet.plain_turn)
    assert refused.status == 403
    assert _get(server, "/log", "_handle_log", turn=fleet.plain_turn,
                default_session="plain").status != 403        # its own default is fine


def test_artifact_reads_hide_an_eu_agents_artifacts_from_another_agent(server, fleet):
    artifacts.create(session="eunice", type="document", title=f"Invoice {SENTINEL}",
                     summary=SENTINEL, payload={"content": SENTINEL})
    artifacts.create(session="plain", type="document", title="plainword",
                     summary="plainword", payload={"content": "plainword"})

    assert _get(server, f"/artifacts?agent_id={fleet.eu}", "_handle_artifacts_list",
                turn=fleet.plain_turn).status == 403
    for path in ("/artifacts", "/artifacts?search=Invoice", "/artifacts?search=plainword"):
        listed = _get(server, path, "_handle_artifacts_list", turn=fleet.plain_turn)
        assert SENTINEL not in listed.body
    assert "plainword" in _get(server, "/artifacts", "_handle_artifacts_list",
                               turn=fleet.plain_turn).body
    assert SENTINEL in _get(server, "/artifacts", "_handle_artifacts_list").body  # the user


def test_attention_jobs_team_log_and_schedules_hide_an_eu_agent(server, fleet, monkeypatch):
    monkeypatch.setattr(artifacts, "attention", lambda **_k: [
        {"agent_id": fleet.eu, "session": "eunice", "question": SENTINEL},
        {"agent_id": fleet.plain, "session": "plain", "question": "plainword"}])
    monkeypatch.setattr(background_jobs, "snapshot", lambda **_k: {"jobs": [
        {"job_id": "j1", "agent_id": fleet.eu, "title": SENTINEL},
        {"job_id": "j2", "agent_id": fleet.plain, "title": "plainword"}]})
    team = team_store.create_team("Mixed", communication_enabled=True)
    for member in (fleet.eu, fleet.plain):
        team_store.add_member(team["team_id"], member)
    team_store.capture_assistant_message(agent_id=fleet.eu, source_message_id="m1",
                                         text=f"<team>{SENTINEL}</team>")
    team_store.capture_assistant_message(agent_id=fleet.plain, source_message_id="m2",
                                         text="<team>plainword</team>")
    scheduler.create_schedule("eunice", "eu", "0 * * * *", f"file {SENTINEL}")
    scheduler.create_schedule("plain", "plain", "0 * * * *", "plainword")

    reads = (("/attention", "_handle_attention", ()),
             ("/background-jobs", "_handle_background_jobs", ()),
             (f"/teams/{team['team_id']}/messages", "_handle_team_messages", (team["team_id"],)),
             ("/agent-schedules", "_handle_agent_schedules_get", ()))
    for path, name, args in reads:
        as_agent = _get(server, path, name, turn=fleet.plain_turn, args=args)
        as_user = _get(server, path, name, args=args)
        assert SENTINEL not in as_agent.body, path
        assert "plainword" in as_agent.body, path               # control: still works
        assert SENTINEL in as_user.body, path                    # the user's device


def test_the_event_stream_never_carries_an_eu_agents_events_to_another_agent(server, fleet):
    events = [{"event_id": 1, "type": "agent-state", "agent_id": fleet.eu,
               "session": "eunice", "status_text": SENTINEL},
              {"event_id": 2, "type": "user-notification", "session": "eunice",
               "preview": SENTINEL},
              {"event_id": 3, "type": "agent-state", "session": "plain",
               "status_text": "plainword"}]

    def stream_output(turn):
        q = queue.Queue()
        for event in events:
            q.put(json.dumps(event))
        q.evicted = True
        stream = SimpleNamespace(subscribe=lambda live=None: q, recent=lambda _c: [],
                                 unsubscribe=lambda _q: None)
        out, handler = _call(server, "GET", "/events", turn=turn, stream=stream)
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = handler.end_headers = \
            lambda *a, **k: None
        handler._live_subscription = lambda: None
        server.SERVER_TIMING = SimpleNamespace(sse_queue_timeout_sec=0.05)
        handler._sse()
        return handler.wfile.getvalue().decode()

    as_agent, as_user = stream_output(fleet.plain_turn), stream_output("")
    assert SENTINEL not in as_agent and "plainword" in as_agent
    assert SENTINEL in as_user


def test_an_eu_turn_cannot_schedule_its_text_onto_an_agent_outside_its_profile(server, fleet):
    def post(turn, session):
        out, handler = _call(server, "POST", "/agent-schedules", turn=turn, data={
            "session": session, "name": "n", "cron_expression": "0 * * * *",
            "prompt": f"file {SENTINEL}"})
        handler._handle_agent_schedules_post()
        return out

    refused = post(fleet.eu_turn, "plain")
    assert refused.status == 403 and "provider_profile_boundary" in refused.body
    assert post(fleet.eu_turn, "eunice").status == 201          # its own profile
    assert post(fleet.plain_turn, "plain").status == 201        # control

    plain_schedule = scheduler.create_schedule("plain", "p2", "0 * * * *", "plainword")
    out, handler = _call(server, "PATCH", f"/schedules/{plain_schedule['schedule_id']}",
                         turn=fleet.eu_turn, data={"prompt": f"now {SENTINEL}"})
    handler._handle_schedule_patch(plain_schedule["schedule_id"])
    assert out.status == 403
    assert SENTINEL not in json.dumps(scheduler.get_schedule(plain_schedule["schedule_id"]))
