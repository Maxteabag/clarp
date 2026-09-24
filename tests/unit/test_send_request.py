"""SendRequest.from_payload: the field-by-field policy of POST /send."""
import pytest

from lib import agents as agents_db
from lib import prompt_admissions
from lib.send_request import SendRequest, SendRequestError


def _parse(data, **kw):
    kw.setdefault("default_session", "claude")
    kw.setdefault("trace_id_factory", lambda: "minted-trace-0001")
    kw.setdefault("authenticated", False)
    kw.setdefault("resolve_sender", lambda raw: "")
    return SendRequest.from_payload(data, **kw)


def test_defaults_for_a_minimal_typed_message():
    req = _parse({"text": "  hello  "})
    assert req.text == "hello"
    assert req.session == "claude"
    assert req.trace_id == "minted-trace-0001"
    assert req.client_msg_id == req.trace_id          # falls back to the trace
    assert req.transcription_id == ""
    assert req.voice_utterance_id == ""
    assert req.hands_free is False
    assert req.force_session is True                  # typed text never routes
    assert req.origin == "user"
    assert req.synthesize_audio is True               # user origin speaks by default
    assert req.unheard_audio_sessions == ()
    assert req.orchestrator_fallback is False
    assert req.queue_if_busy is False
    assert req.sender_agent_id == ""
    assert req.channel == "chat"
    assert req.authenticated is False


def test_explicit_ids_and_session_are_trimmed_and_kept():
    req = _parse({"text": "x", "session": " rachel ", "trace_id": " t-1 ",
                  "client_msg_id": " m-1 ", "utterance_id": " u-1 "})
    assert (req.session, req.trace_id, req.client_msg_id) == ("rachel", "t-1", "m-1")
    assert req.voice_utterance_id == "u-1"


def test_blank_session_falls_back_to_default():
    assert _parse({"text": "x", "session": "   "}).session == "claude"


def test_transcription_id_is_normalized_and_becomes_the_utterance_id():
    req = _parse({"text": "x", "transcription_id": "job-abc"})
    assert req.transcription_id == "job-abc"
    assert req.voice_utterance_id == "job-abc"
    assert req.channel == "voice"


def test_explicit_utterance_id_wins_over_transcription_id_and_is_capped():
    req = _parse({"text": "x", "transcription_id": "job-abc",
                  "utterance_id": "u" * 200})
    assert req.voice_utterance_id == "u" * 128


def test_bad_transcription_id_is_a_400_json_error_carrying_the_trace():
    with pytest.raises(SendRequestError) as ei:
        _parse({"text": "x", "transcription_id": "not valid!", "trace_id": "t-9"})
    assert ei.value.status == 400
    assert ei.value.json_body is True
    assert ei.value.trace_id == "t-9"
    assert str(ei.value)


def test_hands_free_routes_unless_force_session():
    assert _parse({"text": "x", "hands_free": True}).force_session is False
    assert _parse({"text": "x", "hands_free": True,
                   "force_session": True}).force_session is True
    # Only the JSON literal true counts.
    assert _parse({"text": "x", "hands_free": "true"}).hands_free is False
    assert _parse({"text": "x", "hands_free": 1}).hands_free is False


def test_orchestrator_fallback_from_route_or_body():
    assert _parse({"text": "x"}, orchestrator_fallback=True).orchestrator_fallback is True
    assert _parse({"text": "x", "orchestrator_fallback": True}).orchestrator_fallback is True
    assert _parse({"text": "x", "orchestrator_fallback": "yes"}).orchestrator_fallback is False


def test_queue_if_busy_requires_literal_true():
    assert _parse({"text": "x", "queue_if_busy": True}).queue_if_busy is True
    assert _parse({"text": "x", "queue_if_busy": 1}).queue_if_busy is False


def test_unheard_audio_sessions_dedupes_trims_and_caps_at_64():
    raw = [" a ", "b", "a", "", 7, None, "b"] + [f"s{i}" for i in range(100)]
    req = _parse({"text": "x", "unheard_audio_sessions": raw})
    # Only the first 64 raw entries are considered, then filtered.
    expected = tuple(dict.fromkeys(
        str(v).strip() for v in raw[:64] if isinstance(v, str) and v.strip()))
    assert req.unheard_audio_sessions == expected
    assert req.unheard_audio_sessions[:2] == ("a", "b")


def test_unheard_audio_sessions_ignores_non_lists():
    assert _parse({"text": "x", "unheard_audio_sessions": "a"}).unheard_audio_sessions == ()
    assert _parse({"text": "x", "unheard_audio_sessions": None}).unheard_audio_sessions == ()


def test_sender_resolution_flips_origin_to_agent_and_mutes_audio():
    req = _parse({"text": "x", "sender": " bella "},
                 resolve_sender=lambda raw: {"bella": "agent-bella"}.get(raw, ""))
    assert req.sender_agent_id == "agent-bella"
    assert req.origin == "agent"
    assert req.synthesize_audio is False            # agents are silent unless asked


def test_unknown_sender_stays_user_origin():
    req = _parse({"text": "x", "sender": "ghost"}, resolve_sender=lambda raw: "")
    assert req.sender_agent_id == ""
    assert req.origin == "user"


def test_sender_is_resolved_by_session_or_agent_id_in_the_db():
    agent_id = agents_db.create_agent(persona="Bella", voice_id="V",
                                      cwd="/tmp", session="bella")
    by_session = SendRequest.from_payload(
        {"text": "x", "sender": "bella"}, default_session="claude",
        trace_id_factory=lambda: "t", authenticated=False)
    by_id = SendRequest.from_payload(
        {"text": "x", "sender": agent_id}, default_session="claude",
        trace_id_factory=lambda: "t", authenticated=False)
    assert by_session.sender_agent_id == agent_id
    assert by_id.sender_agent_id == agent_id


@pytest.mark.parametrize("origin", ["oracle", "schedule", "automation",
                                    "watcher", "heartbeat", "dreaming", "agent"])
def test_client_settable_origins_are_honoured_case_insensitively(origin):
    req = _parse({"text": "x", "origin": origin.upper()})
    assert req.origin == origin
    # Non-user origins are silent unless synthesize_audio is literally true.
    assert req.synthesize_audio is False
    assert _parse({"text": "x", "origin": origin,
                   "synthesize_audio": True}).synthesize_audio is True
    assert _parse({"text": "x", "origin": origin,
                   "synthesize_audio": "yes"}).synthesize_audio is False


def test_unknown_origin_falls_back_to_user():
    assert _parse({"text": "x", "origin": "martian"}).origin == "user"


def test_user_synthesize_audio_is_on_unless_literally_false():
    assert _parse({"text": "x", "synthesize_audio": False}).synthesize_audio is False
    assert _parse({"text": "x", "synthesize_audio": 0}).synthesize_audio is True
    assert _parse({"text": "x", "synthesize_audio": None}).synthesize_audio is True


def test_require_text_is_a_bare_text_400_after_parsing():
    req = _parse({"text": "   ", "trace_id": "t-2"})
    assert req.text == ""
    with pytest.raises(SendRequestError) as ei:
        req.require_text()
    assert ei.value.status == 400
    assert str(ei.value) == "empty text"
    assert ei.value.json_body is False
    assert ei.value.trace_id == "t-2"
    _parse({"text": "ok"}).require_text()  # no raise


def test_admit_records_the_admission_even_for_empty_text(monkeypatch):
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        return "admission"

    monkeypatch.setattr(prompt_admissions, "create", fake_create)
    req = _parse({"text": "", "transcription_id": "job-1", "client_msg_id": "m-1"},
                 authenticated=True)
    assert req.admit() == "admission"
    assert seen["authenticated_at_admission"] is True
    assert seen["origin"] == "user"
    assert seen["sender_agent_id"] == ""
    assert seen["channel"] == "voice"
    assert seen["client_admission_id"] == "m-1"
    assert seen["trace_id"] == req.trace_id
    assert seen["original_text"] == ""
    assert isinstance(seen["observed_at"], int)
