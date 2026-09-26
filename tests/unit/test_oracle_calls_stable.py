"""Characterization tests for lib.oracle_calls_stable (WebRTC Oracle tools).

Pins the realtime session contract shape, SDP offer validation, the spoken
status helpers, AgentTools resolution/dispatch identity/result filtering,
Sideband tool-output framing, and the per-principal call registry. Nothing
here opens a socket: dispatch, delegations and message reads are faked.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lib import agents, message_store, oracle_calls_stable as mod, oracle_delegations
from lib.oracle_prompt import PROMPT


# ---- session_config ---------------------------------------------------------


def test_session_config_shape_and_tool_set():
    cfg = mod.session_config(model="gpt-rt", voice="marin")
    assert cfg["type"] == "realtime" and cfg["model"] == "gpt-rt"
    assert cfg["instructions"] is PROMPT
    assert cfg["output_modalities"] == ["audio"]
    assert cfg["max_output_tokens"] == 4096
    assert cfg["tool_choice"] == "auto"
    assert cfg["audio"]["output"] == {"voice": "marin"}
    turn = cfg["audio"]["input"]["turn_detection"]
    assert turn == {"type": "semantic_vad", "eagerness": "low",
                    "create_response": True, "interrupt_response": True}
    assert cfg["audio"]["input"]["noise_reduction"] == {"type": "far_field"}
    assert "transcription" not in cfg["audio"]["input"]
    names = [t["name"] for t in cfg["tools"]]
    # Status is deliberately excluded (results reach the model directly);
    # message reads and the contact hand-off are added.
    assert "get_agent_status" not in names
    assert names[-2:] == ["read_agent_messages", "investigate_with_oracle"]
    assert {"list_agents", "delegate_to_agent", "cancel_agent"} <= set(names)
    assert len(names) == len(set(names))


def test_session_config_cancel_tool_gains_optional_request():
    cfg = mod.session_config(model="m", voice="v")
    cancel = next(t for t in cfg["tools"] if t["name"] == "cancel_agent")
    assert cancel["parameters"]["properties"]["request"] == {"type": "string", "maxLength": 16000}
    assert cancel["description"].endswith("Optional request replaces cancelled work atomically.")
    assert "request" not in cancel["parameters"]["required"]
    investigate = next(t for t in cfg["tools"] if t["name"] == "investigate_with_oracle")
    assert investigate["parameters"]["required"] == ["request"]


def test_session_config_transcription_is_opt_in():
    cfg = mod.session_config(model="m", voice="v", transcription="whisper-1")
    assert cfg["audio"]["input"]["transcription"] == {"model": "whisper-1"}


def test_session_config_does_not_mutate_shared_tool_table():
    from lib.oracle_realtime import _ORACLE_TOOLS
    before = json.dumps(_ORACLE_TOOLS, sort_keys=True)
    mod.session_config(model="m", voice="v")
    assert json.dumps(_ORACLE_TOOLS, sort_keys=True) == before


# ---- validate_offer ---------------------------------------------------------


@pytest.mark.parametrize("sdp", [
    "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
    "v=0 m=audio ",
])
def test_validate_offer_accepts_bounded_audio_offer(sdp):
    assert mod.validate_offer(sdp) is sdp


@pytest.mark.parametrize("sdp", [
    None, 12, b"v=0 m=audio ",
    "",
    "m=audio 9\r\n",                       # must start with v=0
    "v=0\r\nm=video 9\r\n",                # audio section required
    "v=0\r\nm=audio\r\n",                  # needs 'm=audio ' with trailing space
    "v=0 m=audio " + "x" * 128000,         # over the size bound
])
def test_validate_offer_rejects(sdp):
    with pytest.raises(ValueError, match="bounded audio SDP offer"):
        mod.validate_offer(sdp)


# ---- time helpers -----------------------------------------------------------


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


# Parameters must be static: xdist compares collected test IDs across workers,
# so the "now"-relative timestamp is built inside the test, not at import.
@pytest.mark.parametrize("delta,zulu,expected", [
    ({"seconds": 5}, False, "just now"),
    ({"minutes": 1, "seconds": 1}, False, "1 minute ago"),
    ({"minutes": 7}, False, "7 minutes ago"),
    ({"hours": 1, "seconds": 30}, False, "1 hour ago"),
    ({"hours": 5}, False, "5 hours ago"),
    ({"days": 1, "minutes": 1}, False, "1 day ago"),
    ({"days": 3}, False, "3 days ago"),
    ({"seconds": 5}, True, "just now"),
])
def test_age_text(delta, zulu, expected):
    stamp = _ago(**delta)
    if zulu:
        stamp = stamp.replace("+00:00", "Z")
    assert mod._age_text(stamp) == expected


@pytest.mark.parametrize("stamp", ["", None, "not a date"])
def test_age_text_unparseable(stamp):
    assert mod._age_text(stamp) == ""


def test_age_text_clamps_future_to_just_now():
    future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert mod._age_text(future) == "just now"


@pytest.mark.parametrize("value,expected", [
    (0, "1970-01-01T00:00:00+00:00"),
    (1_700_000_000_000, "2023-11-14T22:13:20+00:00"),
    ("1700000000000", "2023-11-14T22:13:20+00:00"),
    (None, ""), ("x", ""),
])
def test_datetime_from_ms(value, expected):
    assert mod.datetime_from_ms(value) == expected


# ---- AgentTools -------------------------------------------------------------


def _roster():
    return [
        {"agent_id": "a-mike", "persona": "Mike", "session": "mike-cb43", "backend": "claude",
         "archived_at": None, "is_janitor": 0, "personality": "dry"},
        {"agent_id": "a-omar", "persona": "Omar", "session": "omar-5c9d", "backend": "codex",
         "archived_at": None, "is_janitor": 0, "personality": None},
        {"agent_id": "a-old", "persona": "Old", "session": "old-1", "backend": "claude",
         "archived_at": 1, "is_janitor": 0},
        {"agent_id": "a-jan", "persona": "Janitor", "session": "jan-1", "backend": "claude",
         "archived_at": None, "is_janitor": 1},
    ]


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr(agents, "list_agents", _roster)
    stopped = []
    t = mod.AgentTools(SimpleNamespace(media_dir="/nowhere"), "phone-1", "mike-cb43",
                       stopped.append)
    t._stopped = stopped
    return t


def test_roster_excludes_archived_and_janitor(tools):
    assert [a["session"] for a in tools.roster()] == ["mike-cb43", "omar-5c9d"]


@pytest.mark.parametrize("name", ["Mike", "mike", "MIKE-CB43", "  mike-cb43 "])
def test_resolve_by_persona_or_session_casefolded(tools, name):
    assert tools.resolve(name)["agent_id"] == "a-mike"


@pytest.mark.parametrize("name", ["", None, "Nobody", "old-1", "jan-1", "Mik"])
def test_resolve_rejects_unknown_hidden_or_partial(tools, name):
    with pytest.raises(ValueError, match="Unknown or ambiguous agent"):
        tools.resolve(name)


def test_resolve_rejects_ambiguous_persona(tools, monkeypatch):
    dupe = _roster()[:2]
    dupe[1] = {**dupe[1], "persona": "Mike"}
    monkeypatch.setattr(agents, "list_agents", lambda: dupe)
    with pytest.raises(ValueError):
        tools.resolve("Mike")


def test_list_agents_output_shape(tools):
    out = tools.execute("list_agents", {}, "c1")
    assert out == {
        "agents": [
            {"name": "Mike", "session": "mike-cb43", "backend": "claude", "personality": "dry"},
            {"name": "Omar", "session": "omar-5c9d", "backend": "codex", "personality": ""},
        ],
        "oracle_contact": "mike-cb43",
        "note": "These are the agents you can reach; oracle_contact is your main colleague.",
    }


def test_list_agents_without_contact_reports_none(tools):
    tools.fallback = ""
    assert tools.execute("list_agents", {}, "c1")["oracle_contact"] is None


def test_investigate_without_contact_is_a_spoken_error_not_an_exception(tools):
    tools.fallback = ""
    out = tools.execute("investigate_with_oracle", {"request": "look"}, "c1")
    assert out == {"error": "No Oracle contact selected; ask which contact should investigate"}


@pytest.fixture
def fake_dispatch(monkeypatch):
    calls = []

    def dispatch(**kw):
        calls.append(kw)
        return {"status": "accepted", "delegation_id": kw["delegation_id"]}

    monkeypatch.setattr(oracle_delegations, "dispatch", dispatch)
    return calls


def _expected_id(principal, call_id, follow=False):
    seed = f"{principal}:follow:{call_id}" if follow else f"{principal}:{call_id}"
    return "rtc-" + hashlib.sha256(seed.encode()).hexdigest()[:40]


@pytest.mark.parametrize("tool,args,session", [
    ("delegate_to_agent", {"agent": "Omar", "request": "run the tests"}, "omar-5c9d"),
    ("investigate_with_oracle", {"request": "run the tests"}, "mike-cb43"),
])
def test_dispatch_identity_is_derived_from_principal_and_call(tools, fake_dispatch, tool, args, session):
    out = tools.execute(tool, args, "call-77")
    ident = _expected_id("phone-1", "call-77")
    assert out == {"status": "accepted", "operation_id": ident, "session": session,
                   "note": "Handed off, not finished. Tell the user what you asked and who is doing it."}
    assert fake_dispatch[0]["delegation_id"] == ident
    assert fake_dispatch[0]["session"] == session
    assert fake_dispatch[0]["request_text"] == "run the tests"
    assert fake_dispatch[0]["authenticated_at_admission"] is True
    assert fake_dispatch[0]["owner_principal"] == "phone-1"
    assert tools.delegations == {ident}


@pytest.mark.parametrize("request_text", ["", "   ", None, "x" * 16001])
def test_delegate_rejects_empty_or_oversized_request(tools, fake_dispatch, request_text):
    with pytest.raises(ValueError, match="1 to 16000"):
        tools.execute("delegate_to_agent", {"agent": "Omar", "request": request_text}, "c")
    assert fake_dispatch == []


def test_unknown_tool_raises(tools):
    with pytest.raises(ValueError, match="Unknown Oracle tool"):
        tools.execute("make_coffee", {"agent": "Omar"}, "c")


def test_cancel_agent_without_follow_up(tools, fake_dispatch, monkeypatch):
    cancelled = []

    def cancel_for_session(session, *, stop, owner_principal):
        cancelled.append((session, owner_principal))
        stop()
        return []

    monkeypatch.setattr(oracle_delegations, "cancel_for_session", cancel_for_session)
    superseded = []
    tools.supersede = superseded.append
    out = tools.execute("cancel_agent", {"agent": "omar-5c9d"}, "c9")
    assert out == {"cancelled": True, "note": "Confirm the cancellation in one sentence."}
    assert cancelled == [("omar-5c9d", "phone-1")]
    assert tools._stopped == ["omar-5c9d"]
    assert superseded == ["omar-5c9d"]
    assert fake_dispatch == []


def test_cancel_agent_with_follow_up_unpauses_and_redispatches(tools, fake_dispatch, monkeypatch):
    monkeypatch.setattr(oracle_delegations, "cancel_for_session",
                        lambda session, *, stop, owner_principal: [])
    unpaused = []
    monkeypatch.setattr("lib.turn_queue.set_paused",
                        lambda agent_id, paused: unpaused.append((agent_id, paused)))
    out = tools.execute("cancel_agent", {"agent": "Omar", "request": "  do this instead "}, "c9")
    ident = _expected_id("phone-1", "c9", follow=True)
    assert out["operation_id"] == ident and out["status"] == "accepted"
    assert unpaused == [("a-omar", False)]
    assert fake_dispatch[0]["request_text"] == "do this instead"
    assert tools.delegations == {ident}


def test_cancel_agent_follow_up_too_long(tools, fake_dispatch):
    with pytest.raises(ValueError, match="1 to 16000"):
        tools.execute("cancel_agent", {"agent": "Omar", "request": "x" * 16001}, "c")


def test_read_agent_messages_shape_and_budget(tools, monkeypatch):
    rows = [{"id": f"m{i}", "role": role, "timestamp": _ago(minutes=30 - i), "text": text}
            for i, (role, text) in enumerate([
                ("system", "hidden"), ("user", "hello"), ("assistant", "hi"),
                ("tool", "hidden"), ("user", "x" * 5000),
            ])]
    seen = {}

    def list_messages(**kw):
        seen.update(kw)
        return rows

    monkeypatch.setattr(message_store, "list_messages", list_messages)
    out = tools.execute("read_agent_messages", {"agent": "Mike"}, "c")
    assert seen == {"agent_id": "a-mike", "limit": 20, "include_automated": False}
    assert out["agent"] == "Mike"
    # Only user/assistant rows, chronological, each text capped at 2000 chars.
    assert [m["id"] for m in out["messages"]] == ["m1", "m2", "m4"]
    assert len(out["messages"][2]["text"]) == 2000
    assert set(out["messages"][0]) == {"id", "role", "timestamp", "text"}
    assert out["newest_message_age"] == "26 minutes ago"
    assert out["note"].startswith("Summarize in one natural sentence")


def test_read_agent_messages_budget_spreads_across_rows(tools, monkeypatch):
    rows = [{"id": f"m{i}", "role": "user", "timestamp": _ago(minutes=9 - i), "text": "y" * 3000}
            for i in range(8)]
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: rows)
    out = tools.execute("read_agent_messages", {"agent": "Mike"}, "c")
    # 12000 budget / 2000 cap = six full rows; the seventh gets 0 chars and stops the walk.
    assert [m["id"] for m in out["messages"]] == ["m2", "m3", "m4", "m5", "m6", "m7"]
    assert all(len(m["text"]) == 2000 for m in out["messages"])


def test_read_agent_messages_skips_an_empty_newest_message(tools, monkeypatch):
    rows = [{"id": "m1", "role": "user", "timestamp": _ago(minutes=5), "text": "hello"},
            {"id": "m2", "role": "assistant", "timestamp": _ago(minutes=1), "text": ""}]
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: rows)
    out = tools.execute("read_agent_messages", {"agent": "Mike"}, "c")
    assert [m["id"] for m in out["messages"]] == ["m1"]


def test_read_agent_messages_keeps_chronological_order(tools, monkeypatch):
    rows = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant",
             "timestamp": _ago(minutes=20 - i), "text": f"t{i}"} for i in range(14)]
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: rows)
    out = tools.execute("read_agent_messages", {"agent": "Mike"}, "c")
    assert [m["id"] for m in out["messages"]] == [f"m{i}" for i in range(4, 14)]


def test_get_agent_status_busy_summary(tools, monkeypatch):
    monkeypatch.setattr(agents, "latest_state", lambda agent_id: {
        "kind": "tool", "ts": 1_700_000_000_000,
        "detail": {"tool": "Bash", "summary": "running pytest"}})
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: [
        {"id": "1", "role": "user", "timestamp": _ago(hours=2), "text": "go"},
        {"id": "2", "role": "assistant", "timestamp": _ago(hours=1), "text": "  "},
        {"id": "3", "role": "assistant", "timestamp": _ago(minutes=3), "text": "On it"},
    ])
    out = tools.execute("get_agent_status", {"agent": "Omar"}, "c")
    assert out["agent"] == "Omar" and out["session"] == "omar-5c9d"
    assert out["state"] == "working"
    assert out["current_step"] == "running pytest"
    assert out["last_said"] == "On it"
    assert out["last_said_age"] == "3 minutes ago"
    assert out["since"].endswith("ago")
    assert set(out) == {"agent", "session", "state", "since", "current_step",
                        "last_said", "last_said_age", "note"}


def test_get_agent_status_idle_without_history(tools, monkeypatch):
    monkeypatch.setattr(agents, "latest_state", lambda agent_id: None)
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: [])
    out = tools.execute("get_agent_status", {"agent": "Omar"}, "c")
    assert out["state"] == "idle" and out["since"] == ""
    assert out["current_step"] == "" and out["last_said"] == "" and out["last_said_age"] == ""


@pytest.mark.parametrize("kind,expected", [
    ("thinking", "working"), ("spawned", "working"), ("waiting", "waiting"), ("done", "done"),
])
def test_get_agent_status_state_mapping(tools, monkeypatch, kind, expected):
    monkeypatch.setattr(agents, "latest_state", lambda agent_id: {"kind": kind, "ts": None, "detail": "x"})
    monkeypatch.setattr(message_store, "list_messages", lambda **kw: [])
    assert tools.execute("get_agent_status", {"agent": "Omar"}, "c")["state"] == expected


def test_results_only_returns_terminal_rows_for_current_ids(tools, monkeypatch):
    rows = {"a": {"delegation_id": "a", "status": "completed"},
            "b": {"delegation_id": "b", "status": "queued"},
            "c": {"delegation_id": "c", "status": "failed"}}
    monkeypatch.setattr(oracle_delegations, "get", lambda ident: rows.get(ident))
    tools.delegations.update({"a", "b", "c", "gone"})
    assert sorted(r["delegation_id"] for r in tools.results()) == ["a", "c"]


# ---- Sideband ---------------------------------------------------------------


class _Socket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, raw):
        self.sent.append(raw)

    def close(self):
        self.closed = True


@pytest.fixture
def sideband():
    tools = SimpleNamespace(principal="phone-1", lock=threading.Lock(), delegations=set(),
                            execute=None, results=lambda: [])
    band = mod.Sideband(_Socket(), tools, None)
    yield band
    band.close()


def _drain(band):
    items = []
    while not band.incoming.empty():
        items.append(band.incoming.get_nowait())
    return items


@pytest.mark.parametrize("name,speak", [
    ("list_agents", True), ("read_agent_messages", True), ("get_agent_status", True),
    ("delegate_to_agent", False), ("investigate_with_oracle", False), ("cancel_agent", False),
])
def test_sideband_execute_frames_output_and_speak_flag(sideband, name, speak):
    sideband.tools.execute = lambda n, a, c: {"ok": n, "args": a}
    sideband.execute({"name": name, "call_id": "c1", "arguments": json.dumps({"agent": "Omar"})})
    assert _drain(sideband) == [("tool", ("c1", json.dumps({"ok": name, "args": {"agent": "Omar"}}), speak))]


def test_sideband_execute_missing_arguments_is_empty_object(sideband):
    sideband.tools.execute = lambda n, a, c: {"got": a}
    sideband.execute({"name": "list_agents", "call_id": "c1"})
    assert _drain(sideband) == [("tool", ("c1", '{"got": {}}', True))]


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]", '"str"'])
def test_sideband_execute_bad_arguments_become_spoken_error(sideband, arguments):
    sideband.tools.execute = lambda *a: pytest.fail("must not reach the tools")
    sideband.execute({"name": "delegate_to_agent", "call_id": "c1", "arguments": arguments})
    [(kind, (call_id, output, speak))] = _drain(sideband)
    assert kind == "tool" and call_id == "c1" and speak is True
    assert "error" in json.loads(output)


def test_sideband_execute_tool_error_forces_speech_and_is_truncated(sideband):
    def boom(*a):
        raise ValueError("x" * 900)

    sideband.tools.execute = boom
    sideband.execute({"name": "delegate_to_agent", "call_id": "c1", "arguments": "{}"})
    [(_, (_, output, speak))] = _drain(sideband)
    assert speak is True
    assert json.loads(output) == {"error": "x" * 500}


def test_sideband_need_choice_output_is_spoken_even_for_delegation(sideband):
    sideband.tools.execute = lambda *a: {"need_choice": ["Mike", "Omar"]}
    sideband.execute({"name": "delegate_to_agent", "call_id": "c1", "arguments": "{}"})
    assert _drain(sideband)[0][1][2] is True


def test_sideband_send_is_compact_json_and_silent_after_close(sideband):
    sideband.send({"type": "response.create", "a": [1, 2]})
    assert sideband.socket.sent == ['{"type":"response.create","a":[1,2]}']
    sideband.close()
    sideband.send({"type": "late"})
    assert len(sideband.socket.sent) == 1
    assert sideband.socket.closed is True


def test_sideband_installs_supersede_hook_on_tools(sideband):
    sideband.tools.supersede("omar-5c9d")
    assert _drain(sideband) == [("supersede", "omar-5c9d")]


# ---- call registry ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_calls():
    with mod._LOCK:
        mod._CALLS.clear()
    yield
    with mod._LOCK:
        mod._CALLS.clear()


class _Band:
    def __init__(self):
        self.closed = False
        self.tools = SimpleNamespace(lock=threading.Lock(), delegations={"d1", "d2"})

    def close(self):
        self.closed = True


def test_close_call_only_matches_current_attempt():
    band = _Band()
    mod._CALLS["phone"] = {"attempt": "att-1", "sideband": band}
    mod.close_call("phone", "att-2")
    assert "phone" in mod._CALLS and band.closed is False
    mod.close_call("phone", "att-1")
    assert "phone" not in mod._CALLS and band.closed is True
    mod.close_call("phone", "att-1")  # idempotent


def test_call_results_returns_known_rows_for_matching_attempt(monkeypatch):
    band = _Band()
    mod._CALLS["phone"] = {"attempt": "att-1", "sideband": band}
    monkeypatch.setattr(oracle_delegations, "get",
                        lambda ident: {"delegation_id": ident} if ident == "d1" else None)
    assert mod.call_results("phone", "att-1") == [{"delegation_id": "d1"}]
    assert mod.call_results("phone", "att-other") == []
    assert mod.call_results("other", "att-1") == []


def test_create_call_rejects_bad_offer_before_touching_registry():
    with pytest.raises(ValueError):
        mod.create_call(ctx=None, principal="p", attempt_id="a", sdp="nope",
                        fallback="", stop=lambda s: None)
    assert mod._CALLS == {}


def test_create_call_replays_response_for_same_attempt_and_sdp(monkeypatch):
    sdp = "v=0\r\nm=audio 9\r\n"
    digest = hashlib.sha256(sdp.encode()).hexdigest()
    response = {"sdp": "answer", "call_id": "rtc_1", "attempt_id": "att-1"}
    mod._CALLS["p"] = {"attempt": "att-1", "digest": digest, "response": response}
    monkeypatch.setattr(agents, "list_agents", list)
    assert mod.create_call(ctx=None, principal="p", attempt_id="att-1", sdp=sdp,
                           fallback="", stop=lambda s: None) is response


def test_create_call_same_attempt_different_sdp_is_rejected(monkeypatch):
    sdp = "v=0\r\nm=audio 9\r\n"
    mod._CALLS["p"] = {"attempt": "att-1", "digest": "other"}
    monkeypatch.setattr(agents, "list_agents", list)
    with pytest.raises(ValueError, match="different SDP"):
        mod.create_call(ctx=None, principal="p", attempt_id="att-1", sdp=sdp,
                        fallback="", stop=lambda s: None)


def test_create_call_pending_same_attempt_is_rejected(monkeypatch):
    sdp = "v=0\r\nm=audio 9\r\n"
    mod._CALLS["p"] = {"attempt": "att-1", "digest": hashlib.sha256(sdp.encode()).hexdigest()}
    monkeypatch.setattr(agents, "list_agents", list)
    with pytest.raises(ValueError, match="pending or failed"):
        mod.create_call(ctx=None, principal="p", attempt_id="att-1", sdp=sdp,
                        fallback="", stop=lambda s: None)


def test_create_call_without_key_closes_previous_and_records_attempt(monkeypatch):
    sdp = "v=0\r\nm=audio 9\r\n"
    old = _Band()
    mod._CALLS["p"] = {"attempt": "att-0", "digest": "x", "sideband": old}
    monkeypatch.setattr(agents, "list_agents", list)
    monkeypatch.setattr(mod.config, "load", lambda: SimpleNamespace(openai_key=lambda: ""))
    with pytest.raises(ValueError, match="OpenAI key"):
        mod.create_call(ctx=None, principal="p", attempt_id="att-1", sdp=sdp,
                        fallback="", stop=lambda s: None)
    assert old.closed is True
    assert mod._CALLS["p"]["attempt"] == "att-1"
    assert mod._CALLS["p"]["digest"] == hashlib.sha256(sdp.encode()).hexdigest()
    assert "response" not in mod._CALLS["p"]


def test_create_call_validates_contact_before_registering(monkeypatch):
    monkeypatch.setattr(agents, "list_agents", list)
    with pytest.raises(ValueError, match="Unknown or ambiguous agent"):
        mod.create_call(ctx=None, principal="p", attempt_id="att-1", sdp="v=0\r\nm=audio 9\r\n",
                        fallback="ghost", stop=lambda s: None)
    assert mod._CALLS == {}


def test_read_agent_transcript_reads_newest_messages_without_dispatch(tools, monkeypatch):
    rows = [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "timestamp": _ago(minutes=40 - i),
             "text": f"message {i} " + ("z" * (7000 if i == 38 else 10))} for i in range(40)]
    rows.insert(5, {"id": "tool", "role": "tool", "timestamp": _ago(minutes=35), "text": "hidden"})
    seen = {}

    def list_messages(**kw):
        seen.update(kw)
        return rows[-kw["limit"]:]
    monkeypatch.setattr(message_store, "list_messages", list_messages)
    monkeypatch.setattr(mod.oracle_delegations, "dispatch",
                        lambda **kw: pytest.fail("reading a transcript must not prompt the agent"))
    out = tools.execute("read_agent_transcript", {"agent": "Mike", "limit": 4}, "c")
    assert seen["agent_id"] == "a-mike" and seen["include_automated"] is False
    assert [m["id"] for m in out["messages"]] == ["m36", "m37", "m38", "m39"]
    assert out["messages"][2]["truncated"] is True and len(out["messages"][2]["text"]) == 6000
    assert "truncated" not in out["messages"][0]
    assert out["truncated"] is True          # older messages exist beyond the window
    assert out["agent"] == "Mike" and out["newest_message_age"] == "1 minute ago"
    # Limit is bounded and defaults to ten.
    assert len(tools.execute("read_agent_transcript", {"agent": "Mike", "limit": 999}, "c")["messages"]) == 30
    assert len(tools.execute("read_agent_transcript", {"agent": "Mike"}, "c")["messages"]) == 10
