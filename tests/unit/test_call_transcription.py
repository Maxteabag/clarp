"""Call transcription policy (contract §12, §12.1, §12.2; introduced with listen-first at one booked contract number).

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
    introduced = server_identity.FEATURE_CONTRACTS["call_transcription_policy"]
    assert introduced == server_identity.FEATURE_CONTRACTS["agent_ring_listen_first"]
    assert 1 < introduced <= server_identity.HOST_CONTRACT


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
    # The stored policy can never work with the global engine: refused as invalid (R2).
    assert out.status == 409 and out.payload["error"] == "call policy invalid"
    assert out.payload["language"] == "no"
    assert "can never work" in out.payload["call_policy_error"]
    assert out.payload["attempts"] == []
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
    # FakeSTT is no Host->engine boundary, so nothing is observed (L2): null, never a guess.
    assert stt_record["attempts"][0]["language"] is None and stt_record["language_adapted"] == "first_code"
    _set(agent_engines={"mike": ""})
    stt_record = _transcribe(FakeSTT(), scope=_scope(env), tid="tx-set").payload["stt"]
    assert stt_record["attempts"][0]["language"] is None                    # not observed (L2)


def test_M1_an_english_default_takes_an_english_call(env):
    stt = FakeSTT()
    stt_record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt_record["engines"] == {"value": ["faster-whisper:small.en"], "source": "global"}
    assert stt_record["effective_engine"] == "faster-whisper:small.en"
    # No call policy: exactly today's call (nothing bound); the model is English-only.
    assert stt.calls == [("server-default", None)]
    assert stt_record["attempts"][0]["language"] is None                    # not observed (L2)


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
    assert record["attempts"][0]["language"] is None                   # not observed (L2); never claim "en"
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
    assert record["attempts"][0]["language"] is None                   # not observed (L2)


def test_D1_c_with_a_policy_a_local_multilingual_engine_is_still_skipped(env, monkeypatch):
    monkeypatch.setattr(stt_providers, "_model_row", lambda engine: {"id": engine})
    _set(call_languages="no", call_engines="google:chirp_3", agent_engines={"mike": "faster-whisper:small"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["error"] == "no compatible engine"
    assert out.payload["attempts"] == [{"engine": "faster-whisper:small", "outcome": "skipped",
                                        "reason": "language_incompatible"}]
    assert stt.calls == []


# ---- R2: one policy snapshot per clip, published at once, refused when invalid ----------------

def _after_resolve(monkeypatch, change):
    """Change the stored settings once, right after the clip resolved its policy."""
    real = ct.resolve

    def resolve(*args, **kwargs):
        out = real(*args, **kwargs)
        change()
        monkeypatch.setattr(ct, "resolve", real)
        return out
    monkeypatch.setattr(ct, "resolve", resolve)


def test_R2_a_policy_set_during_a_request_does_not_apply_to_it(env, monkeypatch):
    plain = FakeSTT()
    _transcribe(plain, scope=None, focus="mike-86db", tid="tx-plain")
    _after_resolve(monkeypatch, lambda: _set(engine="google:chirp_3", call_languages="no",
                                             call_engines="google:chirp_3"))
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 200
    assert stt.calls == plain.calls == [("server-default", None)]      # today's chain, nothing bound
    record = out.payload["stt"]
    assert record["language"] == {"value": "en", "source": "global"}
    assert record["engines"] == {"value": ["faster-whisper:small.en"], "source": "global"}
    assert record["attempts"][0]["language"] is None                    # not observed (L2)


def test_R2_a_policy_cleared_during_a_request_still_applies_to_it(env, monkeypatch):
    _set(call_languages="no,en", call_engines="google:chirp_3")
    _after_resolve(monkeypatch, lambda: _set(call_languages="", call_engines=""))
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 200
    assert stt.calls == [("google:chirp_3", "no,en")]                   # the policy's language, as reported
    record = out.payload["stt"]
    assert record["language"] == {"value": "no,en", "source": "call_policy"}
    assert record["engines"] == {"value": ["google:chirp_3"], "source": "call_policy"}
    assert record["attempts"][0]["language"] is None                    # not observed (L2)


def test_R2_a_pair_changed_while_a_clip_resolves_is_never_mixed(env, monkeypatch):
    _set(call_languages="no", call_engines="google:chirp_3")
    real = stt_language.engines_for                                      # read between the two keys before R2

    def engines_for(session):
        monkeypatch.setattr(stt_language, "engines_for", real)
        settings_store.set_text(ct.CALL_LANGUAGES_KEY, "en")
        settings_store.set_text(ct.CALL_ENGINES_KEY, "elevenlabs:scribe_v2")
        return real(session)
    monkeypatch.setattr(stt_language, "engines_for", engines_for)
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert stt.calls == [("google:chirp_3", "no")]                      # the old pair, whole
    record = out.payload["stt"]
    assert record["language"] == {"value": "no", "source": "call_policy"}
    assert record["engines"] == {"value": ["google:chirp_3"], "source": "call_policy"}


def test_R2_the_settings_writer_publishes_the_pair_at_once():
    seen = []
    real = settings_store.set_text

    def set_text(key, value, **kwargs):
        real(key, value, **kwargs)
        if key == ct.CALL_LANGUAGES_KEY:
            # Another connection, between the two writes: it must see neither.
            reader = threading.Thread(target=lambda: seen.append(
                settings_store.get_many([ct.CALL_LANGUAGES_KEY, ct.CALL_ENGINES_KEY])))
            reader.start()
            reader.join()
    import unittest.mock
    with unittest.mock.patch.object(settings_store, "set_text", set_text):
        _set(call_languages="no,en", call_engines="google:chirp_3")
    assert seen == [{}]
    assert settings_store.get_many([ct.CALL_LANGUAGES_KEY, ct.CALL_ENGINES_KEY]) == {
        ct.CALL_LANGUAGES_KEY: "no,en", ct.CALL_ENGINES_KEY: "google:chirp_3"}


@pytest.mark.parametrize("languages,engines,message", [
    ("no,en", "cartesia:ink-whisper", "single language"),               # an impossible pair
    ("norsk", "google:chirp_3", "ISO"),                                  # an unreadable language
    ("no", "google:chirp_3,nope:x", "unknown transcription engine"),
])
def test_R2_an_invalid_stored_policy_is_refused_before_any_provider(env, languages, engines, message):
    settings_store.set_text(ct.CALL_LANGUAGES_KEY, languages)            # behind the settings validation
    settings_store.set_text(ct.CALL_ENGINES_KEY, engines)
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["ok"] is False
    assert out.payload["error"] == "call policy invalid"
    assert message in out.payload["call_policy_error"]
    assert out.payload["call_policy_error"] == stt_providers.status()["call_policy_error"]
    assert out.payload["attempts"] == []
    assert stt.calls == []                                               # never silently unset or global
    assert "transcription" not in calls.snapshot(PHONE)["call"]          # the view is unknown, not global


# ---- R1: an explicit server-default or local header in a call is today's chat call --------------

class MethodSTT(FakeSTT):
    """Records which provider method ran, with what, and refuses the aliases
    the way the real Whisper engine does (they are not installed model ids)."""

    def __init__(self, *, refuse_aliases=False):
        super().__init__()
        self.refuse_aliases = refuse_aliases

    def transcribe_model_bytes(self, engine, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("transcribe_model_bytes", engine, stt_language.current_language()))
        if self.refuse_aliases and engine in ("server-default", "local"):
            from lib.stt import STTUnknownModelError
            raise STTUnknownModelError(f"transcription model not installed: {engine}")
        return "hei", True, 1.0

    def transcribe_bytes(self, audio, ctype, prompt, *, wait=0.0):
        self.calls.append(("transcribe_bytes", None, stt_language.current_language()))
        return "hello", True, 1.0


@pytest.mark.parametrize("header", ["server-default", "local"])
@pytest.mark.parametrize("refuse_aliases", [False, True])
def test_R1_an_explicit_alias_header_in_a_call_is_the_chat_call(env, header, refuse_aliases):
    chat = MethodSTT(refuse_aliases=refuse_aliases)
    plain = _transcribe(chat, scope=None, requested_model=header, focus="mike-86db", tid="tx-plain")
    stt = MethodSTT(refuse_aliases=refuse_aliases)
    out = _transcribe(stt, scope=_scope(env), requested_model=header)
    assert stt.calls == chat.calls == [("transcribe_model_bytes", header, None)]
    assert out.status == plain.status and out.payload.get("error") == plain.payload.get("error")
    if refuse_aliases:
        assert out.status == 400 and out.payload["error"] == f"transcription model not installed: {header}"
        return
    record = out.payload["stt"]
    assert record["engines"]["source"] == "device"
    assert record["requested_model"] == {"value": header, "applied": True, "reason": None}


def test_R1_without_a_header_the_global_default_is_unchanged(env):
    chat = MethodSTT()
    _transcribe(chat, scope=None, focus="mike-86db", tid="tx-plain")
    stt = MethodSTT()
    record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert stt.calls == chat.calls == [("transcribe_bytes", None, None)]
    assert record["engines"]["source"] == "global" and record["requested_model"] is None


@pytest.mark.parametrize("header", ["server-default", "local"])
def test_R1_the_policy_and_the_agent_still_shadow_an_alias_header(env, header):
    _set(call_engines="google:chirp_3")
    stt = MethodSTT()
    record = _transcribe(stt, scope=_scope(env), requested_model=header).payload["stt"]
    assert stt.calls == [("transcribe_model_bytes", "google:chirp_3", "en")]
    assert record["requested_model"] == {"value": header, "applied": False, "reason": "call_policy"}
    _set(agent_engines={"mike": "elevenlabs:scribe_v2"})
    stt = MethodSTT()
    record = _transcribe(stt, scope=_scope(env), requested_model=header, tid="tx-agent").payload["stt"]
    assert stt.calls == [("transcribe_model_bytes", "elevenlabs:scribe_v2", "en")]
    assert record["requested_model"] == {"value": header, "applied": False, "reason": "agent"}


# ---- C3(ii): Scribe drops a language set, so a configured set skips it -------------------------

def test_C3_a_configured_set_skips_scribe_and_runs_the_next_engine(env):
    _set(call_languages="no,en", call_engines="elevenlabs:scribe_v2,google:chirp_3")
    stt = FakeSTT()
    record = _transcribe(stt, scope=_scope(env)).payload["stt"]
    assert record["attempts"][0] == {"engine": "elevenlabs:scribe_v2", "outcome": "skipped",
                                     "reason": "language_incompatible"}
    assert record["effective_engine"] == "google:chirp_3"
    assert stt.calls == [("google:chirp_3", "no,en")]


def test_C3_a_configured_set_with_only_scribe_is_no_compatible_engine(env):
    _set(call_languages="no,en", call_engines="google:chirp_3", agent_engines={"mike": "elevenlabs:scribe_v2"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["error"] == "no compatible engine"
    assert out.payload["attempts"] == [{"engine": "elevenlabs:scribe_v2", "outcome": "skipped",
                                        "reason": "language_incompatible"}]
    assert stt.calls == []


def test_C3_settings_refuse_a_set_only_scribe_can_take_and_a_stored_one_is_invalid(env):
    with pytest.raises(ValueError) as err:
        _set(call_languages="no,en", call_engines="elevenlabs:scribe_v2")
    assert "can never work" in str(err.value)
    settings_store.set_text(ct.CALL_LANGUAGES_KEY, "no,en")
    settings_store.set_text(ct.CALL_ENGINES_KEY, "elevenlabs:scribe_v2")
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env))
    assert out.status == 409 and out.payload["error"] == "call policy invalid"
    assert "can never work" in out.payload["call_policy_error"]
    assert stt.calls == []


def test_C3_pins_single_code_scribe_under_a_policy_and_no_policy_sets(env):
    _set(call_languages="no", call_engines="elevenlabs:scribe_v2")
    stt = FakeSTT()
    assert _transcribe(stt, scope=_scope(env)).status == 200
    assert stt.calls == [("elevenlabs:scribe_v2", "no")]
    _set(call_languages="", call_engines="", agent_languages={"mike": "no,en"},
         agent_engines={"mike": "elevenlabs:scribe_v2"})
    stt = FakeSTT()
    out = _transcribe(stt, scope=_scope(env), tid="tx-nopolicy")
    assert out.status == 200 and stt.calls == [("elevenlabs:scribe_v2", None)]   # today's chain, unbound


# ---- L2: attempts[].language is what the Host handed at the named engine boundary ----------------
# Recording stand-ins sit BEHIND the production boundaries: a real WhisperSTT with a recording
# model, the real stt_providers handoff with recording provider adapters, and the real custom
# adapter handoff with a recording _request. Null means the boundary was not observed.

class RecordingModel:
    """faster-whisper stand-in: records the language= it is handed."""

    def __init__(self, fail=False):
        self.languages, self.fail = [], fail

    def transcribe(self, path, language=None, **kwargs):
        self.languages.append(language)
        if self.fail:
            raise RuntimeError("model crashed")
        return iter([SimpleNamespace(text="hei", no_speech_prob=0.0)]), None


def _whisper(model_name="small.en", *, cls=None, fail=False):
    from lib import stt as stt_mod
    engine = (cls or stt_mod.WhisperSTT)(model_name)
    engine._model = RecordingModel(fail)
    engine.ready.set()
    engine.load_done.set()
    return engine


def _cloud(monkeypatch, *, fail_after=(), no_key=()):
    """Recording provider adapters behind the real stt_providers handoff."""
    from lib import cartesia_stt, deepgram_stt, eleven_stt, google_stt
    handed = []

    def recorder(provider):
        def run(**kwargs):
            handed.append((provider, kwargs["language"]))
            if provider in fail_after:
                raise RuntimeError(f"{provider} failed after the handoff")
            return "hei", 1.0
        return run
    for module, provider in ((eleven_stt, "elevenlabs"), (google_stt, "google"),
                             (deepgram_stt, "deepgram"), (cartesia_stt, "cartesia")):
        monkeypatch.setattr(module, "transcribe", recorder(provider))
    monkeypatch.setattr(stt_providers, "_key_for", lambda cfg, provider: "" if provider in no_key else "k")
    return handed


def _attempts(out):
    return [(a["engine"], a["outcome"], a.get("language")) for a in out.payload["stt"]["attempts"]]


def test_L2_local_in_process_records_the_language_handed_to_the_model(env, monkeypatch):
    engine = _whisper("small.en")
    out = _transcribe(engine, scope=_scope(env))
    assert engine._model.languages == ["en"]
    assert _attempts(out) == [("faster-whisper:small.en", "ok", "en")]
    _local_default(monkeypatch, "small")
    engine = _whisper("small")
    out = _transcribe(engine, scope=_scope(env), tid="tx-multi")
    assert engine._model.languages == [None]
    assert _attempts(out) == [("faster-whisper:small", "ok", "auto")]          # no constraint, not detection


def test_L2_the_subprocess_worker_and_its_fallback_record_the_engine_language(env):
    from lib import stt as stt_mod
    engine = _whisper("small.en", cls=stt_mod.SubprocessWhisperSTT)
    requests = []

    class Worker:
        stdin = property(lambda self: self)

        def poll(self):
            return None

        def write(self, line):
            request = json.loads(line)
            requests.append(request)
            event, box = engine._pending[request["id"]]
            box["segments"] = [{"text": "hei", "no_speech_prob": 0.0}]
            event.set()

        def flush(self):
            pass
    engine._proc = Worker()
    out = _transcribe(engine, scope=_scope(env))
    assert [r["op"] for r in requests] == ["transcribe"] and "language" not in requests[0]  # fixed at load
    assert _attempts(out) == [("faster-whisper:small.en", "ok", engine.language)] and engine.language == "en"
    engine._fallback_in_process = True
    out = _transcribe(engine, scope=_scope(env), tid="tx-fallback")
    assert engine._model.languages == ["en"]
    assert _attempts(out) == [("faster-whisper:small.en", "ok", "en")]


def test_L2_a_custom_adapter_records_no_constraint(env, monkeypatch):
    from lib import custom_stt_adapters
    payloads = []
    manifest = SimpleNamespace(id="acme", default_model="m1")
    monkeypatch.setattr(custom_stt_adapters, "get", lambda provider: manifest if provider == "acme" else None)
    monkeypatch.setattr(custom_stt_adapters, "models", lambda m: [{"id": "acme:m1", "model": "m1"}])

    def request(m, payload):
        payloads.append(payload)
        return {"text": "hei"}
    monkeypatch.setattr(custom_stt_adapters, "_request", request)
    out = _transcribe(_whisper(), scope=_scope(env), requested_model="acme:m1")
    assert len(payloads) == 1 and "language" not in payloads[0]
    assert _attempts(out) == [("acme:m1", "ok", "auto")]


@pytest.mark.parametrize("policy,engine,handed", [
    ({"call_languages": "no", "call_engines": "deepgram:nova-3"}, "deepgram", "no"),
    ({"call_languages": "no,en", "call_engines": "google:chirp_3"}, "google", "no,en"),      # codes; Google maps tags
    ({"call_languages": "no", "call_engines": "elevenlabs:scribe_v2"}, "elevenlabs", "no"),  # single-code Scribe
    ({"call_languages": "no,en", "call_engines": "google:chirp_3", "agent_engines": {"mike": "cartesia:ink-whisper"}},
     "cartesia", "no"),                                                                      # first code
])
def test_L2_cloud_records_the_argument_handed_to_the_adapter(env, monkeypatch, policy, engine, handed):
    recorded = _cloud(monkeypatch)
    _set(**policy)
    out = _transcribe(_whisper(), scope=_scope(env))
    assert recorded == [(engine, handed)]
    assert _attempts(out)[-1][2] == handed


def test_L2_scribe_without_a_policy_is_handed_no_constraint_for_a_set(env, monkeypatch):
    recorded = _cloud(monkeypatch)
    _set(agent_languages={"mike": "no,en"}, agent_engines={"mike": "elevenlabs:scribe_v2"})
    out = _transcribe(_whisper(), scope=_scope(env))
    assert recorded == [("elevenlabs", None)]                     # the set was always dropped for Scribe
    assert _attempts(out) == [("elevenlabs:scribe_v2", "ok", "auto")]


def test_L2_an_unobserved_failure_is_null_an_observed_one_keeps_its_value_and_nothing_leaks(env, monkeypatch):
    _set(agent_languages={"mike": "no,en"}, agent_engines={"mike": "google:chirp_3,elevenlabs:scribe_v2"})
    recorded = _cloud(monkeypatch, no_key={"google"})               # fails with no handoff noted: unobserved
    out = _transcribe(_whisper(), scope=_scope(env))
    assert recorded == [("elevenlabs", None)]
    assert _attempts(out) == [("google:chirp_3", "error", None), ("elevenlabs:scribe_v2", "ok", "auto")]
    recorded = _cloud(monkeypatch, fail_after={"google"})           # fails after the handoff
    out = _transcribe(_whisper(), scope=_scope(env), tid="tx-after")
    assert recorded == [("google", "no,en"), ("elevenlabs", None)]
    assert _attempts(out) == [("google:chirp_3", "error", "no,en"), ("elevenlabs:scribe_v2", "ok", "auto")]


def test_L2_a_local_failure_unobserved_and_observed(env):
    engine = _whisper("small.en")
    engine._model = None                                            # fails with no boundary noted: unobserved
    out = _transcribe(engine, scope=_scope(env))
    assert out.status == 500 and _attempts(out) == [("faster-whisper:small.en", "error", None)]
    engine = _whisper("small.en", fail=True)                        # the model fails after it was handed "en"
    out = _transcribe(engine, scope=_scope(env), tid="tx-after")
    assert out.status == 500 and _attempts(out) == [("faster-whisper:small.en", "error", "en")]


def test_L2_D1_no_policy_dispatch_is_the_chat_dispatch(env, monkeypatch):
    _local_default(monkeypatch, "small")
    chat, call = _whisper("small"), _whisper("small")
    _transcribe(chat, scope=None, focus="mike-86db", tid="tx-plain")
    out = _transcribe(call, scope=_scope(env))
    assert call._model.languages == chat._model.languages == [None]
    assert _attempts(out) == [("faster-whisper:small", "ok", "auto")]
    recorded = _cloud(monkeypatch)
    _set(agent_languages={"mike": "no"}, agent_engines={"mike": "deepgram:nova-3"})
    _transcribe(_whisper(), scope=None, focus="mike-86db", tid="tx-plain-cloud")
    out = _transcribe(_whisper(), scope=_scope(env), tid="tx-call-cloud")
    assert recorded == [("deepgram", "no"), ("deepgram", "no")]     # the same argument as chat
    assert _attempts(out) == [("deepgram:nova-3", "ok", "no")]


def test_L2_captures_are_per_attempt_nested_and_per_thread():
    with stt_language.capturing_handed() as outer:
        stt_language.note_handed("no")
        with pytest.raises(RuntimeError):
            with stt_language.capturing_handed() as inner:
                stt_language.note_handed(None)
                raise RuntimeError("boom")
        assert inner == {"language": "auto"} and outer == {"language": "no"}
        with stt_language.capturing_handed() as unobserved:
            pass
        assert unobserved == {} and outer == {"language": "no"}
    stt_language.note_handed("en")                                    # no capture: a no-op
    barrier, seen = threading.Barrier(2), {}

    def clip(name, language):
        with stt_language.capturing_handed() as mine:
            barrier.wait()
            stt_language.note_handed(language)
            barrier.wait()
            seen[name] = dict(mine)
    threads = [threading.Thread(target=clip, args=("a", "no")), threading.Thread(target=clip, args=("b", "en"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": {"language": "no"}, "b": {"language": "en"}}


# ---- L2 ruling: whisper.cpp and OpenAI whisper note at their own boundaries -------------------------

def _whisper_cpp(monkeypatch, model_name="small.en", *, returncode=0):
    """A real WhisperCppSTT whose whisper-cli run is recorded (its argv) instead of executed."""
    import subprocess as subprocess_mod
    from lib import stt as stt_mod
    engine = stt_mod.WhisperCppSTT(model_name, model_source="/models/ggml.bin", runtime_source="/bin/whisper-cli")
    engine._model = "/models/ggml.bin"
    engine.ready.set()
    engine.load_done.set()
    argv = []

    def run(command, **kwargs):
        argv.append(list(command))
        output = command[command.index("-of") + 1]
        with open(output + ".json", "w") as handle:
            json.dump({"transcription": [{"text": "hei"}]}, handle)
        return subprocess_mod.CompletedProcess(command, returncode, "", "cli failed" if returncode else "")
    monkeypatch.setattr(stt_mod.subprocess, "run", run)
    monkeypatch.setattr(stt_mod, "_write_whisper_cpp_wav", lambda audio, destination: destination.write_bytes(audio))
    return engine, argv


def _l_value(argv):
    return argv[argv.index("-l") + 1]


def test_L2_whisper_cpp_records_the_l_value_it_runs_with(env, monkeypatch):
    engine, argv = _whisper_cpp(monkeypatch, "small.en")
    out = _transcribe(engine, scope=_scope(env))
    assert out.status == 200 and _l_value(argv[0]) == "en"
    assert _attempts(out)[0][1:] == ("ok", "en")
    _local_default(monkeypatch, "small")
    engine, argv = _whisper_cpp(monkeypatch, "small")
    out = _transcribe(engine, scope=_scope(env), tid="tx-multi")
    assert _l_value(argv[0]) == "auto" and _attempts(out)[0][1:] == ("ok", "auto")


def test_L2_whisper_cpp_failing_after_its_boundary_keeps_the_observed_value(env, monkeypatch):
    engine, argv = _whisper_cpp(monkeypatch, "small.en", returncode=1)
    out = _transcribe(engine, scope=_scope(env))
    assert out.status == 500 and len(argv) == 1
    assert _attempts(out)[0][1:] == ("error", "en")


class RecordingOpenAIModel:
    """openai-whisper stand-in: records the language= kwarg it is handed."""

    def __init__(self, fail=False):
        self.languages, self.fail = [], fail

    def transcribe(self, path, language=None, **kwargs):
        self.languages.append(language)
        if self.fail:
            raise RuntimeError("model crashed")
        return {"text": "hei"}


@pytest.mark.parametrize("model_name,handed,recorded", [("small.en", "en", "en"), ("small", None, "auto")])
def test_L2_openai_whisper_records_the_language_kwarg(env, monkeypatch, model_name, handed, recorded):
    from lib import stt as stt_mod
    _local_default(monkeypatch, model_name)
    engine = stt_mod.OpenAIWhisperSTT(model_name)
    engine._model = RecordingOpenAIModel()
    engine.ready.set()
    out = _transcribe(engine, scope=_scope(env))
    assert engine._model.languages == [handed]
    assert _attempts(out)[0][1:] == ("ok", recorded)


def test_L2_openai_whisper_failing_after_its_boundary_and_an_unnoted_engine_is_null(env):
    from lib import stt as stt_mod
    engine = stt_mod.OpenAIWhisperSTT("small.en")
    engine._model = RecordingOpenAIModel(fail=True)
    engine.ready.set()
    out = _transcribe(engine, scope=_scope(env))
    assert out.status == 500 and engine._model.languages == ["en"]
    assert _attempts(out)[0][1:] == ("error", "en")
    disabled = stt_mod.DisabledSTT()
    disabled.available = True                                        # its transcribe raises, unnoted
    out = _transcribe(disabled, scope=_scope(env), tid="tx-disabled")
    assert _attempts(out)[0][1:] == ("error", None)
