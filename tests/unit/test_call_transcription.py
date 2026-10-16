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
    _set(engine="google:chirp_3", call_languages="no,en")
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
    _set(call_languages="no,en", call_engines="google:chirp_3", agent_engines={"mike": "cartesia:ink-whisper"})
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
    _set(call_languages="no", call_engines="google:chirp_3",
         agent_engines={"mike": "faster-whisper:small.en,google:chirp_3"})
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["attempts"][0] == {"engine": "faster-whisper:small.en", "outcome": "skipped",
                                         "reason": "language_incompatible"}
    assert stt_record["effective_engine"] == "google:chirp_3"
    _set(agent_engines={"mike": "faster-whisper:small.en"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env), tid="tx-2")
    assert out.status == 409 and out.payload["error"] == "no compatible engine"
    assert out.payload["language"] == "no" and out.payload["engines"] == ["faster-whisper:small.en"]
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


def test_the_transcribe_event_carries_the_same_stt_record(env, monkeypatch):
    from lib import eventlog
    seen = []
    monkeypatch.setattr(eventlog, "emit", lambda *a, **k: seen.append((a, k)))
    _set(call_languages="no", call_engines="google:chirp_3")
    out = _transcribe(FakeSTT(), scope=_scope(env))
    [event] = [k for a, k in seen if a[1:2] == ("transcribe",)]
    assert event["detail"]["stt"] == out.payload["stt"]
    seen.clear()
    _transcribe(FakeSTT(), scope=None, tid="tx-plain")
    [event] = [k for a, k in seen if a[1:2] == ("transcribe",)]
    assert "stt" not in event["detail"]



# ---- review M1: compatibility from the engine's real capability ---------------------------

def test_M1_server_default_is_resolved_and_an_english_only_default_is_skipped_for_no():
    steps = ct.plan(["server-default"], "no")
    assert steps == [{"engine": "faster-whisper:small.en", "skip": "language_incompatible"}]
    assert ct.plan(["server-default"], "en") == [
        {"engine": "faster-whisper:small.en", "language": "en", "adapted": False, "local_default": True}]


def test_M1_a_multilingual_local_engine_never_receives_the_code_so_it_is_skipped():
    assert ct.plan(["faster-whisper:small"], "no") == [
        {"engine": "faster-whisper:small", "skip": "language_incompatible"}]
    assert ct.plan(["faster-whisper:small"], "en")[0]["skip"] == "language_incompatible"


def test_M1_a_call_language_the_default_engine_cannot_take_refuses_visibly(env):
    settings_store.set_text(ct.CALL_LANGUAGES_KEY, "no")         # stored before the rule existed
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["error"] == "no compatible engine"
    assert out.payload["language"] == "no"
    assert out.payload["attempts"] == [{"engine": "faster-whisper:small.en", "outcome": "skipped",
                                        "reason": "language_incompatible"}]
    assert stt.calls == []                                           # never transcribed as English


def test_M1_settings_refuse_a_policy_that_can_never_work():
    with pytest.raises(ValueError) as err:
        _set(call_languages="no")                                    # only the English-only default left
    assert "can never" in str(err.value)
    _set(engine="google:chirp_3", call_languages="no")
    with pytest.raises(ValueError):
        _set(engine="local")                                        # the global engine would break it
    assert stt_providers.status()["call_policy_error"] == ""
    settings_store.set_text(stt_providers.ENGINE_KEY, "local")      # changed behind the validation
    assert "no" in stt_providers.status()["call_policy_error"]


def test_M1_attempts_report_the_language_the_engine_actually_received(env):
    _set(call_languages="no,en", call_engines="google:chirp_3", agent_engines={"mike": "cartesia:ink-whisper"})
    stt_record = _transcribe(FakeSTT(), scope=_scope(env)).payload["stt"]
    assert stt_record["attempts"][0]["language"] == "no" and stt_record["language_adapted"] == "first_code"
    _set(agent_engines={"mike": ""})
    stt_record = _transcribe(FakeSTT(), scope=_scope(env), tid="tx-set").payload["stt"]
    assert stt_record["attempts"][0]["language"] == "no,en"


def test_M1_an_english_default_takes_an_english_call(env):
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["engines"] == {"value": ["faster-whisper:small.en"], "source": "global"}
    assert stt_record["effective_engine"] == "faster-whisper:small.en"
    # No call policy: exactly today's call (nothing bound); the model is English-only.
    assert stt.calls == [("server-default", None)]
    assert stt_record["attempts"][0]["language"] == "en"


# ---- review N2: outside calls, server-default is exactly 006e9e70 ------------------------------

def test_N2_outside_calls_server_default_while_loading_is_unchanged():
    """Runs unchanged on 006e9e70 (no call_scope argument)."""
    stt = FakeSTT()
    stt.ready.clear()
    ctx = SimpleNamespace(stt=stt, vocab_prompt=lambda delegated=False: "", default_session="theo-97e5",
                          herald=None)
    out = tp.transcribe(ctx, audio_bytes=b"RIFF....", ctype="audio/wav", hands_free=False,
                        requested_model="server-default", transcription_id="tx-n2", fingerprint="fp-n2",
                        record_voice=lambda *a, **k: None, spawn_voice_metrics=lambda *a, **k: None,
                        focus_session=lambda: "theo-97e5")
    assert out.status == 200 and stt.calls == [("server-default", None)]
    assert "stt" not in out.payload


# ---- §12.4: the view is derived per read for a live call, never stored or on the SSE ----------

def test_the_transcription_view_is_never_stored_or_streamed_and_derives_for_a_live_call(env):
    _set(call_languages="no", call_engines="google:chirp_3", agent_languages={"theo": "pt"})
    stream = SimpleNamespace(events=[])
    stream.broadcast = lambda event: stream.events.append(json.loads(json.dumps(event)))
    calls.bind(SimpleNamespace(stream=stream))
    call_id = calls.start(PHONE, ["theo-97e5", "mike-86db"])["call"]["call_id"]

    def stored():
        return json.loads(settings_store.get(calls.KEY + PHONE))["calls"]
    steps = [("start", lambda: None),
             ("hold", lambda: calls.change(PHONE, "hold", "mike-86db")),
             ("resume", lambda: calls.change(PHONE, "resume", "mike-86db")),
             ("route", lambda: calls.route(PHONE, call_id, "theo-97e5")),
             ("end", lambda: calls.end(PHONE))]
    for name, step in steps:
        step()
        assert all("transcription" not in c for c in stored()), name          # never persisted
        assert all("transcription" not in e for e in stream.events
                   if e["type"] == "group-call"), name                        # never on the SSE
        live = calls.get_call(call_id)
        if name != "end":
            assert live["transcription"]["theo-97e5"]["language"] == {"value": "pt", "source": "agent"}, name
            assert live["transcription"]["mike-86db"]["language"]["source"] == "call_policy", name
        else:
            assert "transcription" not in live                               # not live: absent



# ---- delta review D1: without a call policy, in-call transcription is today's ------------------

def _local_default(monkeypatch, model):
    from lib import config
    real = config.load

    import dataclasses
    monkeypatch.setattr(config, "load", lambda *a, **k: dataclasses.replace(real(*a, **k), whisper_model=model))


def test_D1_a_without_a_policy_a_local_multilingual_host_transcribes_as_today(env, monkeypatch):
    """The reviewer's probe with the policy unset: no capability skip, no 409."""
    _local_default(monkeypatch, "small")
    plain = FakeSTT()
    assert _transcribe(plain, scope=None, focus="mike-86db", tid="tx-plain").status == 200
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 200
    assert stt.calls == plain.calls == [("server-default", None)]       # the same call as chat (006e9e70)
    record = out.payload["stt"]
    assert record["language"] == {"value": "en", "source": "global"}
    assert record["engines"] == {"value": ["faster-whisper:small"], "source": "global"}
    assert record["attempts"][0]["language"] == "auto"                 # it auto-detects; never claim "en"
    assert record["language_adapted"] is None


def test_D1_b_without_a_policy_an_agent_override_on_a_local_engine_is_as_today(env, monkeypatch):
    monkeypatch.setattr(stt_providers, "_model_row", lambda engine: {"id": engine})
    _set(agent_languages={"mike": "no"}, agent_engines={"mike": "faster-whisper:small.en"})
    plain = FakeSTT()
    _transcribe(plain, scope=None, focus="mike-86db", tid="tx-plain")
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 200
    assert stt.calls == plain.calls == [("faster-whisper:small.en", None)]
    record = out.payload["stt"]
    assert record["language"] == {"value": "no", "source": "agent"}
    assert record["engines"] == {"value": ["faster-whisper:small.en"], "source": "agent"}
    assert record["attempts"][0]["language"] == "en"                   # what the English-only model got


def test_D1_c_with_a_policy_a_local_multilingual_engine_is_still_skipped(env, monkeypatch):
    monkeypatch.setattr(stt_providers, "_model_row", lambda engine: {"id": engine})
    _set(call_languages="no", call_engines="google:chirp_3", agent_engines={"mike": "faster-whisper:small"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["error"] == "no compatible engine"
    assert out.payload["attempts"] == [{"engine": "faster-whisper:small", "outcome": "skipped",
                                        "reason": "language_incompatible"}]
    assert stt.calls == []
