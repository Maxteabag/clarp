"""Call transcription policy (contract §12, §12.1, §12.2; HOST_CONTRACT 67).

Scoped to calls: a clip is in a call only when its request names the call
(`X-Clarp-Call-Id`) and that call is live, the requester's, on this Host, and
the captured session is a reachable participant. A present header is checked
before any provider sees the audio and refuses explicitly; an absent header is
today's chat transcription, byte for byte. Language and engines resolve per
field: agent > call policy (in call only) > device header (engines) > global.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from lib import call_transcription as ct
from lib import db, group_calls as calls, settings_store, stt_language, stt_providers
from lib import transcription_pipeline as tp

PHONE = "device_phone1"


def _agent(agent_id, persona, session):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, role)"
        " VALUES (?,?,?,?,?,?,?)", (agent_id, persona, "v", "/tmp", session, db.now_ms(), "agent"))
    db.conn().execute(
        "INSERT INTO runtimes(agent_id, session, backend_session_id, started_at) VALUES (?,?,?,?)",
        (agent_id, session, "bs-" + agent_id, db.now_ms()))


class FakeSTT:
    """Records every provider call with the language it was given."""
    available = True

    def __init__(self, fail=()):
        self.ready = threading.Event()
        self.ready.set()
        self.calls, self.fail = [], set(fail)

    def transcribe_model_bytes(self, engine, audio, ctype, prompt, *, wait=0.0):
        self.calls.append((engine, stt_language.current_language()))
        if engine in self.fail:
            raise RuntimeError(f"{engine} is down")
        return "hei", True, 1.0

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("server-default", stt_language.current_language()))
        return "hello", True, 1.0


@pytest.fixture
def env(monkeypatch):
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-mike", "Mike", "mike-86db")
    calls.bind(SimpleNamespace(stream=None))
    monkeypatch.setattr(calls, "_host_id", lambda: "srv-call")
    monkeypatch.setattr(ct, "_server_id", lambda: "srv-call")
    call = calls.start(PHONE, ["theo-97e5", "mike-86db"])["call"]
    return SimpleNamespace(call=call)


def _set(**values):
    return stt_providers.update_settings(values)


def _transcribe(stt, *, scope=None, requested_model="", focus="theo-97e5", tid="tx-1"):
    ctx = SimpleNamespace(stt=stt, vocab_prompt=lambda delegated=False: "", default_session="theo-97e5",
                          herald=None)
    return tp.transcribe(ctx, audio_bytes=b"RIFF....", ctype="audio/wav", hands_free=False,
                         requested_model=requested_model, transcription_id=tid, fingerprint="fp-" + tid,
                         record_voice=lambda *a, **k: None, spawn_voice_metrics=lambda *a, **k: None,
                         focus_session=lambda: focus, call_scope=scope)


def _scope(env, session="mike-86db", principal=PHONE):
    return ct.validate_scope(principal, env.call["call_id"], session)


def _refused(fn, status, error):
    with pytest.raises(calls.CallError) as err:
        fn()
    assert (err.value.status, err.value.error) == (status, error), err.value.body()
    return err.value


# ---- settings ------------------------------------------------------------------------

def test_call_settings_are_unset_by_default_and_read_back():
    status = stt_providers.status()
    assert status["call_languages"] == "" and status["call_engines"] == ""
    out = _set(call_languages="no,en", call_engines="google:chirp_3,elevenlabs:scribe_v2")
    assert out["call_languages"] == "no,en" and out["call_engines"] == "google:chirp_3,elevenlabs:scribe_v2"
    assert stt_providers.status()["call_languages"] == "no,en"
    out = _set(call_languages="", call_engines="")
    assert out["call_languages"] == "" and out["call_engines"] == ""


@pytest.mark.parametrize("values,message", [
    ({"call_languages": "norsk"}, "ISO"),
    ({"call_languages": "no,en,de,fr"}, "at most three"),
    ({"call_engines": "google:chirp_3,nope:x"}, "unknown transcription engine"),
    ({"call_languages": "no,en", "call_engines": "cartesia:ink-whisper"}, "single language"),
    ({"call_languages": "no,en", "call_engines": "google:chirp_3,cartesia:ink-whisper"}, "single language"),
])
def test_call_settings_are_validated(values, message):
    with pytest.raises(ValueError) as err:
        _set(**values)
    assert message in str(err.value)
    assert stt_providers.status()["call_languages"] == ""


def test_a_set_already_stored_blocks_a_later_single_code_engine():
    _set(call_languages="no,en")
    with pytest.raises(ValueError):
        _set(call_engines="cartesia:ink-whisper")


# ---- fail-closed scope (before any provider) ----------------------------------------------

def test_scope_refusals(env, monkeypatch):
    call_id = env.call["call_id"]
    _refused(lambda: ct.validate_scope(PHONE, "gcl_xyz", "theo-97e5"), 400, "bad call id")
    _refused(lambda: ct.validate_scope(PHONE, "gcl_" + "0" * 32, "theo-97e5"), 404, "unknown call")
    _refused(lambda: ct.validate_scope("device_other", call_id, "theo-97e5"), 403, "not your call")
    err = _refused(lambda: ct.validate_scope(PHONE, call_id, "janitor-0000"), 409, "not a call participant")
    assert err.extra["session"] == "janitor-0000"
    _refused(lambda: ct.validate_scope(PHONE, call_id, ""), 409, "not a call participant")
    calls.change(PHONE, "hold", "mike-86db")
    _refused(lambda: ct.validate_scope(PHONE, call_id, "mike-86db"), 409, "not a call participant")
    monkeypatch.setattr(ct, "_server_id", lambda: "srv-other")
    _refused(lambda: ct.validate_scope(PHONE, call_id, "theo-97e5"), 403, "not your call")
    monkeypatch.setattr(ct, "_server_id", lambda: "srv-call")
    assert ct.validate_scope(PHONE, call_id, "theo-97e5")["session"] == "theo-97e5"
    calls.end(PHONE)
    err = _refused(lambda: ct.validate_scope(PHONE, call_id, "theo-97e5"), 409, "call ended")
    assert err.extra["call_id"] == call_id and err.extra["reason"] == "ended"


def test_a_superseded_call_is_ended(env):
    call_id = env.call["call_id"]
    calls.start(PHONE, ["theo-97e5"])
    err = _refused(lambda: ct.validate_scope(PHONE, call_id, "theo-97e5"), 409, "call ended")
    assert err.extra["reason"] == "superseded"


# ---- per-field precedence and visibility ---------------------------------------------------

def test_no_header_is_todays_transcription_byte_for_byte(env):
    _set(call_languages="no", call_engines="google:chirp_3")
    stt = FakeSTT()
    out = _transcribe(stt, scope=None)
    assert out.status == 200 and "stt" not in out.payload
    assert set(out.payload) == {"text", "ends_terminal", "trace_id", "herald_consumed", "hands_free",
                                "orchestrator_skip_herald", "vocab_run_id", "cached"}
    assert stt.calls == [("server-default", None)]             # the call policy never applies


def test_in_call_the_policy_applies_per_field(env):
    _set(call_languages="no,en", call_engines="google:chirp_3,elevenlabs:scribe_v2")
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env), focus="theo-97e5")
    stt_record = out.payload["stt"]
    assert stt_record["language"] == {"value": "no,en", "source": "call_policy"}
    assert stt_record["engines"] == {"value": ["google:chirp_3", "elevenlabs:scribe_v2"], "source": "call_policy"}
    assert stt_record["effective_engine"] == "google:chirp_3"
    assert stt_record["attempts"][0]["engine"] == "google:chirp_3" and stt_record["attempts"][0]["outcome"] == "ok"
    assert stt_record["language_adapted"] is None and stt_record["requested_model"] is None
    assert stt.calls == [("google:chirp_3", "no,en")]
    assert set(stt_record) == {"language", "engines", "effective_engine", "attempts", "language_adapted",
                               "requested_model"}


def test_an_agent_override_on_one_field_shows_mixed_sources(env):
    """Also proves the session comes from the request (mike), not the UI focus (theo)."""
    _set(call_languages="no", call_engines="google:chirp_3", agent_languages={"mike": "pt"})
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["language"] == {"value": "pt", "source": "agent"}
    assert stt_record["engines"]["source"] == "call_policy"
    assert stt.calls == [("google:chirp_3", "pt")]


def test_the_agent_engine_always_wins_and_a_shadowed_header_is_reported(env):
    _set(call_engines="google:chirp_3", agent_engines={"mike": "elevenlabs:scribe_v2"})
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env), requested_model="cartesia:ink-whisper").payload["stt"]
    assert stt_record["engines"] == {"value": ["elevenlabs:scribe_v2"], "source": "agent"}
    assert stt_record["requested_model"] == {"value": "cartesia:ink-whisper", "applied": False, "reason": "agent"}


def test_in_call_the_policy_engine_shadows_the_device_header(env):
    _set(call_engines="google:chirp_3")
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env), requested_model="cartesia:ink-whisper").payload["stt"]
    assert stt_record["engines"]["source"] == "call_policy"
    assert stt_record["requested_model"] == {"value": "cartesia:ink-whisper", "applied": False,
                                             "reason": "call_policy"}
    assert [engine for engine, _ in stt.calls] == ["google:chirp_3"]


def test_in_call_without_a_policy_the_device_header_applies_as_today(env):
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env), requested_model="cartesia:ink-whisper").payload["stt"]
    assert stt_record["engines"] == {"value": ["cartesia:ink-whisper"], "source": "device"}
    assert stt_record["language"] == {"value": "en", "source": "global"}
    assert stt_record["requested_model"] == {"value": "cartesia:ink-whisper", "applied": True, "reason": None}


def test_in_call_with_nothing_set_everything_is_global(env):
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["language"] == {"value": "en", "source": "global"}
    assert stt_record["engines"]["source"] == "global"


# ---- compatibility and the fallback chain ----------------------------------------------------

def test_a_set_never_reaches_a_single_code_engine(env):
    _set(call_languages="no,en", agent_engines={"mike": "cartesia:ink-whisper"})
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt.calls == [("cartesia:ink-whisper", "no")]
    assert stt_record["language_adapted"] == "first_code"


def test_the_fallback_chain_is_preserved_and_every_attempt_recorded(env):
    _set(call_engines="google:chirp_3,elevenlabs:scribe_v2", call_languages="no")
    stt = FakeSTT(fail={"google:chirp_3"})
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert [a["engine"] for a in stt_record["attempts"]] == ["google:chirp_3", "elevenlabs:scribe_v2"]
    assert [a["outcome"] for a in stt_record["attempts"]] == ["error", "ok"]
    assert stt_record["effective_engine"] == "elevenlabs:scribe_v2"


def test_an_incompatible_engine_is_skipped_and_none_compatible_is_an_explicit_error(env, monkeypatch):
    monkeypatch.setattr(stt_providers, "_model_row", lambda engine: {"id": engine})
    _set(call_languages="no", agent_engines={"mike": "faster-whisper:small.en,google:chirp_3"})
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["attempts"][0] == {"engine": "faster-whisper:small.en", "outcome": "skipped",
                                         "reason": "language_incompatible"}
    assert stt_record["effective_engine"] == "google:chirp_3"
    _set(agent_engines={"mike": "faster-whisper:small.en"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env), tid="tx-2")
    assert out.status == 400 and "no compatible transcription engine" in out.payload["error"]
    assert stt.calls == []


# ---- the call record shows it --------------------------------------------------------------

def test_the_call_record_shows_transcription_only_when_policy_or_override_applies(env):
    assert "transcription" not in calls.snapshot(PHONE)["call"]
    _set(call_languages="no", call_engines="google:chirp_3", agent_languages={"theo": "pt"})
    record = calls.snapshot(PHONE)["call"]["transcription"]
    assert record["theo-97e5"]["language"] == {"value": "pt", "source": "agent"}
    assert record["mike-86db"]["language"] == {"value": "no", "source": "call_policy"}
    assert record["mike-86db"]["engines"] == {"value": ["google:chirp_3"], "source": "call_policy"}


def test_the_host_advertises_the_feature():
    from lib import server_identity
    assert "call_transcription_policy" in server_identity.FEATURES
    assert server_identity.FEATURE_CONTRACTS["call_transcription_policy"] == 67
