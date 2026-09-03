from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib import agents as agents_db
from lib import prompt_admissions, settings_store
from lib.db import conn
from lib.orchestrator import (
    ORCHESTRATOR_VOICE_ID,
    OrchestratorSettings,
    OrchestratorService,
    build_context_packet,
    call_model,
    get_settings,
    record_routing_message,
)
from lib.tts_engine import FakeTTSEngine


class FakeStream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(dict(event))


def _ctx(tmp_path):
    # The orchestrator is now off by default; these tests exercise it on.
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_bool("orchestrator.fallback_only", False)
    tts = FakeTTSEngine(tmp_path / "audio")
    ctx = SimpleNamespace(
        default_session="mike",
        stream=FakeStream(),
        tts=tts,
        agents_path=tmp_path / "agents.json",
    )
    ctx.speak_announcement = (
        lambda text, voice_id, session=None: tts.synthesize(
            text, voice_id, session=session
        )
    )
    return ctx


def _seed_two_agents():
    agents_db.create_agent(
        persona="Mike",
        voice_id="mike-voice",
        cwd="/tmp",
        session="mike",
    )
    agents_db.create_agent(
        persona="Antoni",
        voice_id="antoni-voice",
        cwd="/tmp",
        session="antoni",
    )


def test_context_loads_recent_messages_for_all_agents(tmp_path):
    _seed_two_agents()
    record_routing_message(session="mike", role="user", text="check the logs")
    record_routing_message(session="antoni", role="user", text="review the UI")

    packet = build_context_packet(
        utterance="what about the UI?",
        requested_session="mike",
        trace_id="trace-1",
        hands_free=True,
        settings=get_settings(),
    )

    agents = {agent["session"]: agent for agent in packet["agents"]}
    assert set(agents) == {"mike", "antoni"}
    assert agents["mike"]["recent_user_messages"][-1]["text"] == "check the logs"
    assert agents["antoni"]["recent_user_messages"][-1]["text"] == "review the UI"
    assert packet["context_summary"]["message_count"] == 2


def test_orchestrator_defaults_to_openai_mini():
    settings = get_settings()

    assert settings.provider == "openai"
    assert settings.model == "gpt-5.4-mini"
    assert settings.timeout_ms == 30000


def test_cli_providers_can_use_their_own_model_and_effort_defaults():
    settings_store.set_text("orchestrator.provider", "claude")
    settings_store.set_text("orchestrator.model", "")
    settings_store.set_text("orchestrator.effort", "")

    settings = get_settings()

    assert settings.provider == "claude"
    assert settings.model == ""
    assert settings.effort == ""


def test_orchestrator_disabled_by_default():
    # Opt-in: the default path is talking to the open agent + spoken-name
    # fallback. Clients enable the OpenAI router via the settings toggle.
    assert get_settings().enabled is False
    assert OrchestratorSettings().enabled is False
    assert OrchestratorSettings().fallback_only is True
    assert OrchestratorSettings().confidence_threshold == 0.78


def test_fallback_only_router_skips_regular_hands_free_and_runs_failed_delegation(
    tmp_path,
):
    _seed_two_agents()
    settings_store.set_bool("orchestrator.enabled", True)
    settings_store.set_bool("orchestrator.fallback_only", True)
    calls = []
    service = OrchestratorService(
        _ctx(tmp_path),
        model_call=lambda *_: calls.append(True) or {
            "kind": "agent_message",
            "target_session": "mike",
            "confidence": 0.96,
            "addressing": True,
        },
    )
    settings_store.set_bool("orchestrator.fallback_only", True)

    regular = service.handle_send(
        text="Mike check this",
        requested_session="mike",
        trace_id="regular",
        hands_free=True,
        synthesize_audio=True,
        dispatch=lambda **_: SimpleNamespace(session="mike", backend="claude"),
    )
    fallback = service.handle_send(
        text="check the thing we discussed",
        requested_session="mike",
        trace_id="fallback",
        hands_free=True,
        synthesize_audio=True,
        fallback_request=True,
        dispatch=lambda **_: SimpleNamespace(session="mike", backend="claude"),
    )

    assert regular is None
    assert fallback is not None and fallback.action == "route"
    assert calls == [True]


def test_call_model_uses_claude_code_for_default_provider(monkeypatch):
    calls = []

    monkeypatch.setattr("lib.orchestrator.shutil.which", lambda name: f"/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='{"kind":"ignored","confidence":1,"reason":"test"}',
            stderr="",
        )

    monkeypatch.setattr("lib.orchestrator.subprocess.run", fake_run)

    raw = call_model(
        {"utterance": "noise", "agents": [], "pending": []},
        OrchestratorSettings(
            provider="claude", model="haiku", effort="high", timeout_ms=8000),
    )

    cmd, kwargs = calls[0]
    assert raw["kind"] == "ignored"
    assert cmd[:4] == ["claude", "-p", "--dangerously-skip-permissions",
                       "--no-session-persistence"]
    assert cmd[4:6] == ["--model", "haiku"]
    assert cmd[6:8] == ["--effort", "high"]
    assert "--print=" not in cmd[-1]
    assert kwargs["timeout"] == 8.0


def test_call_model_supports_codex_model_and_effort(monkeypatch):
    calls = []
    monkeypatch.setattr("lib.orchestrator.shutil.which", lambda name: f"/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"thread.started","thread_id":"t1"}\n'
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"{\\"kind\\":\\"ignored\\",\\"confidence\\":1}"}}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("lib.orchestrator.subprocess.run", fake_run)

    raw = call_model(
        {"utterance": "noise", "agents": [], "pending": []},
        OrchestratorSettings(
            provider="codex", model="gpt-5.4-mini", effort="low", timeout_ms=9000),
    )

    cmd, kwargs = calls[0]
    assert raw["kind"] == "ignored"
    assert cmd[:3] == ["codex", "exec", "--json"]
    assert ["--model", "gpt-5.4-mini"] == cmd[cmd.index("--model"):cmd.index("--model") + 2]
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=low"
    assert "--ephemeral" in cmd
    assert kwargs["timeout"] == 9.0


def test_call_model_passes_antigravity_effort(monkeypatch):
    calls = []
    monkeypatch.setattr("lib.orchestrator.shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        "lib.orchestrator.subprocess.run",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or SimpleNamespace(
            returncode=0,
            stdout='{"kind":"ignored","confidence":1}',
            stderr="",
        ),
    )

    call_model(
        {"utterance": "noise", "agents": [], "pending": []},
        OrchestratorSettings(
            provider="agy", model="gemini-3.7-flash-low", effort="medium"),
    )

    cmd, _kwargs = calls[0]
    assert cmd[:2] == ["agy", "--dangerously-skip-permissions"]
    assert ["--model", "gemini-3.7-flash-low"] == cmd[2:4]
    assert ["--effort", "medium"] == cmd[4:6]


def test_openai_rejects_unknown_effort_instead_of_silently_coercing():
    with pytest.raises(RuntimeError, match="unsupported OpenAI reasoning effort"):
        call_model(
            {"utterance": "noise", "agents": [], "pending": []},
            OrchestratorSettings(provider="openai", effort="xhigh"),
        )


def test_focused_context_keeps_roster_but_only_loads_current_messages(tmp_path):
    _seed_two_agents()
    record_routing_message(session="mike", role="user", text="check the logs")
    record_routing_message(session="antoni", role="user", text="review the UI")

    packet = build_context_packet(
        utterance="what about the logs?",
        requested_session="mike",
        trace_id="trace-focused",
        hands_free=True,
        settings=get_settings(),
        context_scope="focused",
    )

    agents = {agent["session"]: agent for agent in packet["agents"]}
    assert set(agents) == {"mike", "antoni"}
    assert agents["mike"]["recent_user_messages"][-1]["text"] == "check the logs"
    assert agents["antoni"]["recent_user_messages"] == []
    assert packet["context_summary"]["scope"] == "focused"
    assert packet["context_summary"]["message_count"] == 1


def test_failed_delegation_context_excludes_agents_inactive_for_thirty_minutes(
    tmp_path,
):
    _seed_two_agents()
    antoni = agents_db.get_by_session("antoni")
    assert antoni is not None
    conn().execute("UPDATE agents SET created_at = 1 WHERE agent_id = ?", (antoni["agent_id"],))
    conn().execute("UPDATE state_log SET ts = 1 WHERE agent_id = ?", (antoni["agent_id"],))
    agents_db.create_agent(
        persona="Rachel", voice_id="rachel-voice", cwd="/tmp", session="rachel"
    )

    packet = build_context_packet(
        utterance="finish the thing we discussed",
        requested_session="mike",
        trace_id="trace-fallback-context",
        hands_free=True,
        settings=get_settings(),
        fallback_request=True,
    )

    assert {agent["session"] for agent in packet["agents"]} == {"mike", "rachel"}
    assert packet["routing_policy"]["recent_agent_window_minutes"] == 30


def test_context_packet_compacts_large_messages_and_state(tmp_path):
    _seed_two_agents()
    mike = agents_db.get_by_session("mike")
    assert mike is not None
    long_text = "please inspect " + ("very noisy transcript block " * 80)
    long_state = {"command": "pytest " + ("tests/unit/test_orchestrator.py " * 80)}
    record_routing_message(session="mike", role="user", text=long_text)
    conn().execute(
        "INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) "
        "VALUES (?, NULL, 9999999999999, 'tool', ?)",
        (mike["agent_id"], str(long_state)),
    )

    packet = build_context_packet(
        utterance="what about that?",
        requested_session="mike",
        trace_id="trace-compact",
        hands_free=True,
        settings=get_settings(),
    )

    mike_packet = next(agent for agent in packet["agents"] if agent["session"] == "mike")
    message = mike_packet["recent_user_messages"][-1]["text"]
    detail = mike_packet["state"]["detail"]
    assert len(message) <= 240
    assert message.endswith("...")
    assert len(detail) <= 240
    assert detail.endswith("...")


def test_uncertain_focused_decision_runs_broad_scan_before_routing(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)
    record_routing_message(session="mike", role="user", text="server deployment")
    record_routing_message(session="antoni", role="user", text="native UI settings")
    scopes = []
    calls = []

    def fake_model(packet, _settings):
        scopes.append(packet["context_summary"]["scope"])
        if packet["context_summary"]["scope"] == "focused":
            return {
                "kind": "ambiguous",
                "target_session": "antoni",
                "confidence": 0.5,
                "reason": "Focused context does not fit.",
            }
        return {
            "kind": "agent_message",
            "target_session": "antoni",
            "confidence": 0.93,
            "addressing": True,
            "text_to_send": "can you finish the settings UI?",
            "reason": "Broad context matches Antoni's native UI work.",
        }

    def dispatch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="can you finish the settings UI?",
        requested_session="mike",
        trace_id="trace-two-stage",
        hands_free=True,
        synthesize_audio=True,
        dispatch=dispatch,
    )

    assert scopes == ["focused", "all"]
    assert outcome is not None and outcome.action == "route"
    assert calls[0]["forced_session"] == "antoni"
    row = conn().execute(
        "SELECT final_action, target_session, context_message_count "
        "FROM orchestrator_decisions"
    ).fetchone()
    assert tuple(row) == ("route", "antoni", 2)


def test_high_confidence_agent_message_forces_dispatch_and_logs(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)
    calls = []

    def fake_model(_packet, _settings):
        return {
            "kind": "agent_message",
            "target_session": "mike",
            "confidence": 0.96,
            "addressing": True,
            "text_to_send": "can you check the logs",
            "reason": "Mike was addressed; Mark is likely a transcription error.",
            "name_corrections": [{"heard": "Mark", "intended": "Mike"}],
        }

    def dispatch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")

    admission = prompt_admissions.create(
        authenticated_at_admission=True,
        origin="user",
        sender_agent_id="",
        channel="voice",
        observed_at=1234,
        client_admission_id="u-routed",
        trace_id="trace-2",
        original_text="hey Mark can you check the logs",
    )

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="hey Mark can you check the logs",
        requested_session="antoni",
        trace_id="trace-2",
        prompt_admission=admission,
        hands_free=True,
        synthesize_audio=True,
        dispatch=dispatch,
    )

    assert outcome is not None and outcome.action == "route"
    assert calls[0]["forced_session"] == "mike"
    assert calls[0]["routed_by_orchestrator"] is True
    assert calls[0]["prompt_admission"] == admission
    assert calls[0]["client_msg_id"] == "u-routed"
    row = conn().execute(
        "SELECT final_action, target_session, confidence FROM orchestrator_decisions"
    ).fetchone()
    assert tuple(row) == ("route", "mike", 0.96)


def test_status_query_speaks_with_orchestrator_voice_and_does_not_dispatch(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)

    def fake_model(_packet, _settings):
        return {
            "kind": "status_query",
            "target_session": "mike",
            "confidence": 0.9,
            "addressing": False,
            "status_text": "Mike is checking the logs.",
            "reason": "the user asked about Mike, not to Mike.",
        }

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="what is Mike doing",
        requested_session="antoni",
        trace_id="trace-3",
        hands_free=True,
        synthesize_audio=True,
        dispatch=lambda **_: (_ for _ in ()).throw(AssertionError("dispatched")),
    )

    assert outcome is not None and outcome.action == "status"
    assert ctx.tts.calls[-1]["voice_id"] == ORCHESTRATOR_VOICE_ID
    assert ctx.tts.calls[-1]["text"] == "Mike is checking the logs."


def test_ignored_utterance_logs_without_dispatch_or_speech(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)

    def fake_model(_packet, _settings):
        return {
            "kind": "ignored",
            "confidence": 0.91,
            "addressing": False,
            "reason": "The utterance is an accidental nonsensical dictation fragment.",
        }

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="blorf salmiakki fjord seven no agent",
        requested_session="mike",
        trace_id="trace-ignore",
        hands_free=True,
        synthesize_audio=True,
        dispatch=lambda **_: (_ for _ in ()).throw(AssertionError("dispatched")),
    )

    assert outcome is not None and outcome.action == "ignored"
    assert outcome.ok is True
    assert ctx.tts.calls == []
    row = conn().execute(
        "SELECT final_action, decision_kind, utterance, reason "
        "FROM orchestrator_decisions"
    ).fetchone()
    assert tuple(row) == (
        "ignored",
        "ignored",
        "blorf salmiakki fjord seven no agent",
        "The utterance is an accidental nonsensical dictation fragment.",
    )


def test_ambiguous_decision_holds_utterance_and_asks_as_current_agent(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)

    def fake_model(_packet, _settings):
        return {
            "kind": "ambiguous",
            "target_session": "antoni",
            "confidence": 0.52,
            "addressing": False,
            "spoken_text": "Was that for Antoni?",
            "reason": "Context points to Antoni but not strongly enough.",
        }

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="can you make the sidebar tighter",
        requested_session="mike",
        trace_id="trace-4",
        hands_free=True,
        synthesize_audio=True,
        dispatch=lambda **_: (_ for _ in ()).throw(AssertionError("dispatched")),
    )

    assert outcome is not None and outcome.action == "clarify"
    pending = conn().execute(
        "SELECT utterance, candidate_session, speak_as_session, status "
        "FROM orchestrator_pending_utterances"
    ).fetchone()
    assert tuple(pending) == (
        "can you make the sidebar tighter",
        "antoni",
        "mike",
        "pending",
    )
    assert ctx.tts.calls[-1]["voice_id"] == "mike-voice"
    assert ctx.tts.calls[-1]["session"] == "mike"


def test_low_confidence_agent_message_is_held_and_spoken(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)

    def fake_model(_packet, _settings):
        return {
            "kind": "agent_message",
            "target_session": "antoni",
            "confidence": 0.49,
            "addressing": False,
            "reason": "Antoni is plausible, but the utterance is not a clear address.",
        }

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="can you tighten the sidebar",
        requested_session="mike",
        trace_id="trace-4b",
        hands_free=True,
        synthesize_audio=True,
        dispatch=lambda **_: (_ for _ in ()).throw(AssertionError("dispatched")),
    )

    assert outcome is not None and outcome.action == "clarify"
    assert ctx.tts.calls[-1]["text"] == "Was that for Antoni?"
    assert conn().execute(
        "SELECT COUNT(*) AS n FROM orchestrator_pending_utterances"
    ).fetchone()["n"] == 1


def test_recipient_correction_sends_held_utterance(tmp_path):
    _seed_two_agents()
    ctx = _ctx(tmp_path)
    pending_id = "pending-1"
    held_admission = prompt_admissions.create(
        authenticated_at_admission=True,
        origin="user",
        sender_agent_id="",
        channel="voice",
        observed_at=1,
        client_admission_id="u-held",
        trace_id="trace-old",
        original_text="can you tighten the sidebar",
    )
    conn().execute(
        """INSERT INTO orchestrator_pending_utterances (
               pending_id, trace_id, utterance, requested_session,
               candidate_session, speak_as_session, reason, created_at,
               expires_at, status, prompt_admission_json
           ) VALUES (?, 'trace-old', ?, 'mike', 'antoni', 'mike', '', 1,
                     9999999999999, 'pending', ?)""",
        (pending_id, "can you tighten the sidebar", held_admission.to_json()),
    )
    calls = []

    def fake_model(_packet, _settings):
        return {
            "kind": "recipient_correction",
            "target_session": "antoni",
            "pending_id": pending_id,
            "confidence": 0.97,
            "addressing": True,
            "reason": "The user confirmed Antoni.",
        }

    def dispatch(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(session=kwargs["forced_session"], backend="claude")

    outcome = OrchestratorService(ctx, model_call=fake_model).handle_send(
        text="yes",
        requested_session="mike",
        trace_id="trace-correction",
        prompt_admission=prompt_admissions.create(
            authenticated_at_admission=True,
            origin="user",
            sender_agent_id="",
            channel="voice",
            observed_at=2,
            client_admission_id="u-confirmation",
            trace_id="trace-correction",
            original_text="yes",
        ),
        hands_free=True,
        synthesize_audio=True,
        dispatch=dispatch,
    )

    assert outcome is not None and outcome.action == "route"
    assert calls[0]["forced_session"] == "antoni"
    assert calls[0]["text"] == "can you tighten the sidebar"
    assert calls[0]["prompt_admission"] == held_admission
    assert calls[0]["client_msg_id"] == "u-held"
    assert conn().execute(
        "SELECT status FROM orchestrator_pending_utterances WHERE pending_id = ?",
        (pending_id,),
    ).fetchone()["status"] == "sent"


def test_disabled_or_non_hands_free_skips_orchestrator(tmp_path):
    _seed_two_agents()
    settings_store.set_bool("orchestrator.enabled", True)
    ctx = _ctx(tmp_path)
    service = OrchestratorService(
        ctx,
        model_call=lambda *_: (_ for _ in ()).throw(AssertionError("model called")),
    )
    assert service.handle_send(
        text="hello",
        requested_session="mike",
        trace_id="trace-5",
        hands_free=False,
        synthesize_audio=True,
        dispatch=lambda **_: None,
    ) is None


def test_provider_options_list_every_routing_backend_plus_openai(monkeypatch):
    from lib import orchestrator
    monkeypatch.setattr(orchestrator.shutil, "which",
                        lambda name: f"/bin/{name}" if name in {"claude", "grok"} else None)
    options = orchestrator.provider_options()
    ids = [row["id"] for row in options]
    assert ids == ["claude", "codex", "agy", "grok", "opencode", "openai"]
    by_id = {row["id"]: row for row in options}
    assert by_id["grok"]["installed"] is True and by_id["codex"]["installed"] is False
    assert by_id["grok"]["kind"] == "backend" and by_id["grok"]["catalog_backend"] == "grok"
    assert by_id["openai"]["kind"] == "api" and by_id["openai"]["catalog_backend"] == "codex"
    assert by_id["openai"]["effort_options"] == ["minimal", "low", "medium", "high"]
    assert by_id["claude"]["detail"] == "Runs an isolated Claude request on this Host."


def test_update_settings_normalizes_aliases_and_rejects_unknown_providers():
    from lib import orchestrator
    settings = orchestrator.update_settings({"provider": "antigravity"})
    assert settings.provider == "agy"
    settings = orchestrator.update_settings({"provider": "gpt"})
    assert settings.provider == "openai"
    with pytest.raises(ValueError, match="unsupported orchestrator provider"):
        orchestrator.update_settings({"provider": "gemini-cli"})


def test_call_model_routes_grok_through_its_runner(monkeypatch):
    calls = []
    monkeypatch.setattr("lib.orchestrator.shutil.which", lambda name: f"/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"turn_started"}\n'
                '{"type":"assistant","role":"assistant","content":"{\\"kind\\":\\"ignored\\","}\n'
                '{"type":"assistant","role":"assistant","content":"\\"confidence\\":1}"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("lib.orchestrator.subprocess.run", fake_run)
    raw = call_model(
        {"utterance": "noise", "agents": [], "pending": []},
        OrchestratorSettings(provider="grok", model="grok-4.6", effort="low"),
    )
    assert raw["kind"] == "ignored"
    cmd = calls[0]
    assert cmd[0] == "grok" and "--output-format" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4.6"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    assert cmd[-2] == "-p"


def test_call_model_routes_opencode_through_its_runner(monkeypatch):
    calls = []
    monkeypatch.setattr("lib.orchestrator.shutil.which", lambda name: f"/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"text","part":{"text":"{\\"kind\\":\\"ignored\\",\\"confidence\\":1}"}}\n',
            stderr="",
        )

    monkeypatch.setattr("lib.orchestrator.subprocess.run", fake_run)
    raw = call_model(
        {"utterance": "noise", "agents": [], "pending": []},
        OrchestratorSettings(provider="opencode", model="opencode/gpt-5.4", effort="high"),
    )
    assert raw["kind"] == "ignored"
    cmd = calls[0]
    assert cmd[:2] == ["opencode", "run"]
    assert cmd[cmd.index("--model") + 1] == "opencode/gpt-5.4"
    assert cmd[cmd.index("--variant") + 1] == "high"


def test_call_model_rejects_a_provider_the_registry_cannot_route():
    with pytest.raises(RuntimeError, match="unsupported orchestrator provider"):
        call_model(
            {"utterance": "noise", "agents": [], "pending": []},
            OrchestratorSettings(provider="gemini-cli"),
        )
