"""Leak-path audit for the azure-eu provider profile.

An EU agent holds a sentinel in its messages, tool activity, team posts and
artifacts. Every non-EU model client a path could reach is stubbed and must
never see it; each test also shows the same path still works for an ordinary
agent, so a pass is not a path that simply stopped running.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import codex_profiles

pytestmark = pytest.mark.usefixtures("fake_codex_profile")

SENTINEL = "SENTINEL-EU-7f3a9c"
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def fleet():
    plain = agents_db.create_agent(persona="Plain", voice_id="V", cwd="/tmp",
                                   session="plain", backend="codex")
    eu = agents_db.create_agent(persona="Eunice", voice_id="V", cwd="/tmp",
                                session="eunice", backend="codex", provider_profile="azure-eu")
    return SimpleNamespace(plain=plain, eu=eu)


@pytest.fixture
def judge_calls(monkeypatch):
    """The remote judgment client (TypeSafe), stubbed: records every payload."""
    from lib import judgments
    calls: list[str] = []
    monkeypatch.setattr(judgments, "site_enabled", lambda _site: True)
    monkeypatch.setattr(judgments, "api_key", lambda: "test-key")
    monkeypatch.setattr(judgments, "_breaker_open", lambda: False)

    def fake_post(payload, _key, _seconds):
        calls.append(json.dumps(payload))
        raise RuntimeError("stub: no network")

    monkeypatch.setattr(judgments, "_post", fake_post)
    return calls


# --- fleet map world design ----------------------------------------------------

def test_the_fleet_map_world_never_carries_an_eu_agents_work(fleet):
    from lib import artifacts, db, viz_normalize
    for agent_id, word in ((fleet.plain, "plainword"), (fleet.eu, SENTINEL)):
        db.conn().execute(
            "INSERT INTO state_log(agent_id,ts,kind,detail) VALUES (?,1,'tool',?)",
            (agent_id, json.dumps({"tool": "Bash", "input": {
                "command": f"frobnicate --note {word} /srv/{word}"}})))
    artifacts.create(session="eunice", type="document", title=f"Invoice {SENTINEL}",
                     summary=SENTINEL, payload={"content": SENTINEL})

    payload = viz_normalize.build_fleet_map(0)
    designed = json.dumps({"world": payload["world"],
                           "clusters": payload["_unknown_clusters"]}, default=str)

    assert "plainword" in json.dumps(payload, default=str)
    assert SENTINEL not in designed
    assert SENTINEL not in json.dumps(payload, default=str)


# --- orchestrator: status line, routing --------------------------------------

def _orchestrator(tmp_path):
    from lib.orchestrator import OrchestratorService
    return OrchestratorService(SimpleNamespace(
        default_session="plain", stream=SimpleNamespace(broadcast=lambda _e: None),
        agents_path=tmp_path / "agents.json",
        speak_announcement=lambda *a, **k: None))


def test_the_spoken_status_of_an_eu_agent_never_quotes_it(fleet, tmp_path):
    from lib.orchestrator import record_routing_message
    record_routing_message(session="eunice", role="assistant", text=f"Done {SENTINEL}")
    record_routing_message(session="plain", role="assistant", text="Done plainword")
    service = _orchestrator(tmp_path)

    assert "plainword" in service._status_text("plain")
    assert SENTINEL not in service._status_text("eunice")


def test_a_message_naming_an_eu_agent_never_reaches_the_routing_model(
        fleet, tmp_path, monkeypatch):
    from lib import orchestrator, settings_store
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_bool("orchestrator.fallback_only", False)
    seen = []
    monkeypatch.setattr(orchestrator, "call_model",
                        lambda packet, *a, **k: seen.append(json.dumps(packet, default=str))
                        or pytest.fail("stub"))
    service = _orchestrator(tmp_path)
    try:  # the same request to an ordinary agent does reach the model
        service.handle_send(text="Plain, check plainword", requested_session="plain",
                            trace_id="t0", hands_free=True, synthesize_audio=False,
                            dispatch=lambda **_k: None)
    except BaseException:  # noqa: BLE001 - the stub stops it there
        pass
    assert any("plainword" in packet for packet in seen)

    for requested, text in (("plain", f"Eunice, file {SENTINEL}"),
                            ("eunice", f"file {SENTINEL}")):
        outcome = service.handle_send(
            text=text, requested_session=requested, trace_id="t",
            hands_free=True, synthesize_audio=False, dispatch=lambda **_k: None)
        assert outcome is None
    assert not any(SENTINEL in packet for packet in seen)


def test_addressing_an_eu_agent_by_name_resolves_locally(fleet, judge_calls):
    from lib.routing import resolve_agent_by_spoken_name
    roster = {"plain": {"name": "Plain"}, "eunice": {"name": "Eunice"}}

    assert resolve_agent_by_spoken_name(f"Eunice, file {SENTINEL}", roster) == (
        "eunice", f"file {SENTINEL}")
    assert judge_calls == []
    resolve_agent_by_spoken_name("Plain, check plainword", roster)
    assert judge_calls  # an ordinary utterance still asks the judgment


def test_a_message_to_a_focused_eu_agent_stays_local(fleet, judge_calls, tmp_path):
    from lib.send_service import resolve_send_target
    agents_file = tmp_path / "agents.json"
    agents_file.write_text(json.dumps({"plain": {"name": "Plain"},
                                       "eunice": {"name": "Eunice"}}))

    target = resolve_send_target(text=f"please file {SENTINEL}", requested_session="eunice",
                                 default_session="plain", agents_path=agents_file)

    assert target.session == "eunice"
    assert not any(SENTINEL in call for call in judge_calls)


# --- dictation -----------------------------------------------------------------

def _transcribe(stt, *, focus):
    from lib import transcription_pipeline
    return transcription_pipeline.transcribe(
        SimpleNamespace(stt=stt, vocab_prompt=lambda **_k: ""),
        audio_bytes=b"RIFF0000WAVE", ctype="audio/wav", hands_free=False,
        requested_model="elevenlabs:scribe_v2", transcription_id="tx", fingerprint="fp",
        record_voice=lambda *a, **k: None, spawn_voice_metrics=lambda *a, **k: None,
        focus_session=lambda: focus)


def test_dictation_to_an_eu_agent_is_never_sent_to_a_cloud_engine(fleet, monkeypatch):
    from lib import stt as stt_module
    cloud = object.__new__(stt_module.OpenAIWhisperSTT)
    cloud.available = True
    cloud.transcribe_bytes = lambda *a, **k: pytest.fail("cloud STT heard it")
    cloud.transcribe_model_bytes = lambda *a, **k: pytest.fail("cloud STT heard it")

    outcome = _transcribe(cloud, focus="eunice")

    assert outcome.status == 409


def test_dictation_to_an_eu_agent_uses_only_local_whisper(fleet, judge_calls, monkeypatch):
    from lib import stt as stt_module
    used = []
    local = object.__new__(stt_module.WhisperSTT)
    local.available = True
    local.ready = SimpleNamespace(is_set=lambda: True)
    local.transcribe_model_bytes = lambda engine, *a, **k: pytest.fail(
        f"{engine} heard the dictation")

    def transcribe_bytes(*_a, **_k):
        from lib.judgment_sites import is_junk
        used.append("local")
        is_junk(SENTINEL)  # the junk filter runs inside, as in WhisperSTT
        raise RuntimeError("stop after transcription")

    local.transcribe_bytes = transcribe_bytes
    monkeypatch.setattr("lib.heard_audio.retain", lambda *a, **k: None)

    _transcribe(local, focus="eunice")

    assert used == ["local"]
    assert judge_calls == []


# --- speech output -------------------------------------------------------------

def test_an_eu_agent_never_speaks_through_a_tts_provider(fleet):
    from lib import tts_queue

    assert tts_queue.enqueue(agent_id=fleet.eu, text=SENTINEL, voice_id="V",
                             session="eunice", source="test") == 0
    assert tts_queue.enqueue(agent_id=fleet.plain, text="plainword", voice_id="V",
                             session="plain", source="test") > 0
    rows = agents_db.conn().execute("SELECT text FROM tts_queue").fetchall()
    assert [row["text"] for row in rows] == ["plainword"]


# --- teams ------------------------------------------------------------------------

def test_an_eu_agents_team_posts_reach_only_teammates_on_its_profile(fleet):
    from lib import team_store
    eu_peer = agents_db.create_agent(persona="Euan", voice_id="V", cwd="/tmp",
                                     session="euan", backend="codex", provider_profile="azure-eu")
    team = team_store.create_team("Mixed", communication_enabled=True)
    for member in (fleet.plain, fleet.eu, eu_peer):
        team_store.add_member(team["team_id"], member)

    team_store.capture_assistant_message(
        agent_id=fleet.eu, source_message_id="m1", text=f"<team>{SENTINEL}</team>")
    team_store.capture_assistant_message(
        agent_id=fleet.plain, source_message_id="m2", text="<team>plainword</team>")

    plain_digest, _ = team_store.pending_digest(fleet.plain)
    peer_digest, _ = team_store.pending_digest(eu_peer)
    assert SENTINEL not in plain_digest
    assert SENTINEL in peer_digest and "plainword" in peer_digest


# --- label checker: an EU helper under an ordinary parent ----------------------

def test_the_label_checker_never_sends_an_eu_helpers_status(fleet, monkeypatch):
    from lib import label_audit
    helper = agents_db.create_agent(persona="Eva", voice_id="V", cwd="/tmp",
                                    session="eva", backend="codex", provider_profile="azure-eu")
    agents_db.set_lineage(helper, parent_agent_id=fleet.plain, role="helper")
    agents_db.conn().execute("UPDATE agents SET custom_status=? WHERE agent_id=?", (SENTINEL, helper))
    idle = SimpleNamespace(busy=False, idle_since_ms=0)
    monkeypatch.setattr(label_audit.stale_work, "activities",
                        lambda rows: {r["agent_id"]: idle for r in rows})
    monkeypatch.setattr(label_audit.snapshot, "shown_status", lambda *_a, **_k: "Working")

    sent = json.dumps(label_audit.candidates(10**13), default=str)

    assert SENTINEL not in sent


# --- Oracle and podcasts --------------------------------------------------------

def test_oracle_cannot_delegate_to_an_eu_agent(fleet):
    from lib import oracle_delegations
    with pytest.raises(ValueError):
        oracle_delegations.begin(
            delegation_id="dlg_" + "a" * 24, trace_id="t", client_msg_id="c",
            agent_id=fleet.eu, session="eunice", request_text=SENTINEL)


def test_the_legacy_realtime_proxy_replaces_a_phone_read_of_an_eu_agent(fleet):
    from lib import oracle_realtime
    blocked: set[str] = set()
    for call_id, agent in (("call-eu", "Eunice"), ("call-plain", "plain")):
        oracle_realtime.note_eu_only_call(json.dumps({
            "type": "response.function_call_arguments.done", "call_id": call_id,
            "name": "read_agent_messages", "arguments": json.dumps({"agent": agent})}),
            blocked)

    def output(call_id, text):
        raw = json.dumps({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": text}})
        return oracle_realtime._safe_client_event(
            raw, model="m", voice="v", eu_only_calls=blocked)

    assert SENTINEL not in output("call-eu", SENTINEL)
    assert "plainword" in output("call-plain", "plainword")


def test_a_podcast_detour_never_reads_an_eu_agents_episode(fleet):
    from lib import podcast_live
    with pytest.raises(ValueError):
        podcast_live.context_for({}, 1, 10, artifact={"agent_id": fleet.eu})
    with pytest.raises(ValueError):
        podcast_live.context_for({}, 1, 10, source={"agent_id": fleet.eu},
                                 artifact={"agent_id": fleet.plain})


# --- another agent's turn reading over the Host API -----------------------------

def _server():
    spec = importlib.util.spec_from_file_location("clarp_server_eu", ROOT / "server/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _turn_of(agent_id):
    from lib import provider_background_jobs as jobs
    token, _env = jobs.identity_env(agent_id)
    jobs.record_identity(token, agent_id=agent_id, provider="codex", pid=1)
    return token


def _handler(server, path, turn):
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.headers = {"X-Clarp-Turn": turn}
    handler.server = SimpleNamespace(ctx=SimpleNamespace())
    return handler


def test_another_agents_turn_cannot_read_an_eu_agent(fleet, monkeypatch):
    from lib import snapshot
    server = _server()
    agents_db.conn().execute("UPDATE agents SET custom_status=? WHERE agent_id=?", (SENTINEL, fleet.eu))
    plain_turn, eu_turn = _turn_of(fleet.plain), _turn_of(fleet.eu)

    assert _handler(server, "/log?session=eunice", plain_turn)._eu_only_read_refusal("/log")
    assert not _handler(server, "/log?session=eunice", eu_turn)._eu_only_read_refusal("/log")
    assert not _handler(server, "/log?session=eunice", "")._eu_only_read_refusal("/log")
    assert not _handler(server, "/log?session=plain", plain_turn)._eu_only_read_refusal("/log")

    monkeypatch.setattr(snapshot.SHARED, "body", lambda _ctx: json.dumps({"agents": [
        {"agent_id": fleet.eu, "session": "eunice", "last_message": SENTINEL,
         "status_text": SENTINEL},
        {"agent_id": fleet.plain, "session": "plain", "last_message": "plainword"},
    ]}).encode())
    sent = []
    for turn in (plain_turn, ""):
        handler = _handler(server, "/agents/snapshot", turn)
        handler._send = lambda status, body, *_a, **_k: sent.append(body.decode())
        handler._handle_snapshot()
    assert SENTINEL not in sent[0] and "plainword" in sent[0]
    assert SENTINEL in sent[1]  # the user's own device sees everything
