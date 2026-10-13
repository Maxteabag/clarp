"""Agent rings: an agent calls its owner (docs/agent-rings.md).

The real ``agent_rings`` module against a temporary database: the policy
(owner switch, the agent rings only as itself, rate limits, one ring at a
time), the audit trail, the state machine (ringing, answered, declined,
missed, cancelled), answering into a group call with the opening line, the
push, the routes and the CLI.
"""
from __future__ import annotations

import json
import pathlib
import threading

import pytest

from lib import agent_rings as rings
from lib import db, group_calls as calls

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = "device_phone1"


def _agent(agent_id, persona, session, *, voice="voice-1", janitor=False, archived=False, muted=False):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, archived_at,"
        " is_janitor, role, muted) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (agent_id, persona, voice, "/tmp", session, db.now_ms(), db.now_ms() if archived else None,
         1 if janitor else 0, "agent", 1 if muted else 0))
    db.conn().execute(
        "INSERT INTO runtimes(agent_id, session, backend_session_id, started_at) VALUES (?,?,?,?)",
        (agent_id, session, "bs-" + agent_id, db.now_ms()))


class Stream:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def broadcast(self, event):
        with self.lock:
            self.events.append(json.loads(json.dumps(event)))

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


class FakeTTS:
    def __init__(self, tmp_path, fail=False):
        self.calls, self.tmp, self.fail = [], tmp_path, fail

    def synthesize(self, text, voice_id, *, session=None):
        if self.fail:
            raise RuntimeError("tts down")
        self.calls.append((text, voice_id, session))
        path = self.tmp / f"{len(self.calls)}__{session}.mp3"
        path.write_bytes(b"mp3")
        return path


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1000)


@pytest.fixture
def env(tmp_path, monkeypatch):
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-mike", "Mike", "mike-86db")
    _agent("a-jan", "Janitor", "janitor-0000", janitor=True)
    _agent("a-mute", "Quiet", "quiet-0000", muted=True)
    stream, tts, clock, pushes = Stream(), FakeTTS(tmp_path), Clock(), []
    monkeypatch.setattr(rings, "now_ms", clock)
    monkeypatch.setattr(rings, "_push", lambda ring, kind: pushes.append((kind, dict(ring))) or {"sent": 1})
    monkeypatch.setattr(rings, "_schedule_expiry", lambda ring: None)
    ctx = type("Ctx", (), {"stream": stream, "tts": tts})()
    rings.bind(ctx)
    calls.bind(ctx)
    return type("Env", (), {"stream": stream, "tts": tts, "clock": clock, "pushes": pushes})()


def _ring(agent="a-theo", **kw):
    body = {"reason": "Possible water leak at pump 01",
            "opening_line": "Camera 2 shows a possible water leak at pump 01. Shall I find a field worker?"}
    body.update(kw)
    return rings.ring(caller_agent_id=agent, **body)


# ---- ringing ----------------------------------------------------------------

def test_an_agent_rings_its_owner_with_a_reason_and_an_opening_line(env):
    out = _ring()
    ring = out["ring"]
    assert out["ok"] and ring["ring_id"].startswith("rng_") and ring["state"] == "ringing"
    assert ring["session"] == "theo-97e5" and ring["persona"] == "Theo"
    assert ring["reason"] == "Possible water leak at pump 01"
    assert ring["opening_line"].startswith("Camera 2 shows")
    assert ring["expires_at"] == env.clock.now + rings.RING_TIMEOUT_MS
    # The opening line is voiced now, in the agent's own voice, so answering is instant.
    assert env.tts.calls == [(ring["opening_line"], "voice-1", "theo-97e5")]
    assert ring["opening_audio_url"].startswith("/audio/") and ring["opening_audio_url"].endswith(".mp3")
    assert [kind for kind, _ in env.pushes] == ["ring"]
    [event] = env.stream.of("agent-ring")
    assert event["ring_id"] == ring["ring_id"] and event["state"] == "ringing"
    assert rings.current()["ring_id"] == ring["ring_id"]


def test_a_ring_still_rings_when_the_voice_cannot_be_made(env):
    env.tts.fail = True
    ring = _ring()["ring"]
    assert ring["state"] == "ringing" and ring["opening_audio_url"] == ""
    assert [kind for kind, _ in env.pushes] == ["ring"]


def test_a_ring_needs_a_reason_and_an_opening_line_and_caps_them(env):
    for bad in ({"reason": ""}, {"opening_line": "  "}):
        with pytest.raises(calls.CallError) as err:
            _ring(**bad)
        assert err.value.status == 400
    ring = _ring(reason="r" * 900, opening_line="o" * 2000)["ring"]
    assert len(ring["reason"]) == rings.REASON_CHARS and len(ring["opening_line"]) == rings.OPENING_CHARS


# ---- policy -----------------------------------------------------------------

def test_the_owner_switch_turns_rings_off_and_the_refusal_is_audited(env):
    assert rings.settings()["enabled"] is True
    rings.update_settings({"enabled": False})
    with pytest.raises(calls.CallError) as err:
        _ring()
    assert err.value.status == 403 and err.value.error == "agent calls are turned off"
    assert env.pushes == [] and rings.current() is None
    entry, switched = rings.audit()
    assert switched == {"at": env.clock.now, "outcome": "settings", "enabled": False}
    assert entry["outcome"] == "refused" and entry["error"] == "agent calls are turned off"
    assert entry["session"] == "theo-97e5" and entry["reason"].startswith("Possible water leak")
    rings.update_settings({"enabled": True})
    assert _ring()["ring"]["state"] == "ringing"
    assert rings.audit()[0]["outcome"] == "ringing"


def test_an_agent_rings_only_as_itself(env):
    with pytest.raises(calls.CallError) as err:
        rings.ring(caller_agent_id="a-theo", agent="Mike", reason="x", opening_line="y")
    assert err.value.status == 403 and err.value.error == "an agent can only ring as itself"
    # The owner's own tools (no turn proof) name the agent that calls.
    assert rings.ring(caller_agent_id="", agent="Mike", reason="x", opening_line="y")["ring"]["persona"] == "Mike"


def test_janitors_unknown_and_muted_agents_cannot_ring(env):
    for agent, status in (("a-jan", 403), ("a-nobody", 404), ("a-mute", 403)):
        with pytest.raises(calls.CallError) as err:
            _ring(agent=agent)
        assert err.value.status == status, agent
    with pytest.raises(calls.CallError) as err:
        rings.ring(caller_agent_id="", agent="", reason="x", opening_line="y")
    assert err.value.status == 400


def test_one_ring_at_a_time(env):
    _ring()
    with pytest.raises(calls.CallError) as err:
        rings.ring(caller_agent_id="a-mike", reason="x", opening_line="y")
    assert err.value.status == 409 and err.value.error == "the user is already being called"


def test_an_agent_waits_between_rings_and_the_host_caps_rings_per_hour(env):
    first = _ring()["ring"]
    rings.decline(PHONE, first["ring_id"])
    with pytest.raises(calls.CallError) as err:
        _ring()
    assert err.value.status == 429 and err.value.extra["retry_after_s"] > 0
    env.clock.advance(rings.AGENT_INTERVAL_MS / 1000 + 1)
    rings.cancel("a-theo", _ring()["ring"]["ring_id"])
    count = 2
    while count < rings.HOURLY_LIMIT:
        env.clock.advance(rings.AGENT_INTERVAL_MS / 1000 + 1)
        rings.cancel("a-theo", _ring()["ring"]["ring_id"])
        count += 1
    env.clock.advance(rings.AGENT_INTERVAL_MS / 1000 + 1)
    with pytest.raises(calls.CallError) as err:
        rings.ring(caller_agent_id="a-mike", reason="x", opening_line="y")
    assert err.value.status == 429 and err.value.error == "too many calls this hour"
    env.clock.advance(3600)
    assert rings.ring(caller_agent_id="a-mike", reason="x", opening_line="y")["ok"]


# ---- the state machine ----------------------------------------------------

def test_answering_starts_a_group_call_with_the_agent_and_returns_the_opening(env):
    ring = _ring()["ring"]
    out = rings.answer(PHONE, ring["ring_id"])
    assert out["ring"]["state"] == "answered" and out["ring"]["answered_by"] == PHONE
    call = out["call"]
    assert call["state"] == "live" and call["floor"] == "theo-97e5" and call["principal"] == PHONE
    assert out["ring"]["call_id"] == call["call_id"]
    assert out["opening"] == {"text": ring["opening_line"], "audio_url": ring["opening_audio_url"],
                              "session": "theo-97e5", "persona": "Theo"}
    assert rings.current() is None
    assert [kind for kind, _ in env.pushes] == ["ring", "answered"]
    assert [e["state"] for e in env.stream.of("agent-ring")] == ["ringing", "answered"]
    # A retried answer from the same phone gets the same call back.
    again = rings.answer(PHONE, ring["ring_id"])
    assert again["call"]["call_id"] == call["call_id"] and again["changed"] is False
    with pytest.raises(calls.CallError) as err:
        rings.answer("device_other", ring["ring_id"])
    assert err.value.status == 409 and err.value.error == "the call was answered elsewhere"


def test_the_answered_agent_hears_why_it_called_in_its_call_header(env):
    ring = _ring()["ring"]
    call = rings.answer(PHONE, ring["ring_id"])["call"]
    calls.routed(PHONE, call["call_id"], "theo-97e5")
    header = calls.prompt_header("theo-97e5")
    assert "You called the user" in header and "Possible water leak at pump 01" in header
    assert "Camera 2 shows a possible water leak" in header


def test_declining_and_cancelling_end_the_ring(env):
    ring = _ring()["ring"]
    out = rings.decline(PHONE, ring["ring_id"])
    assert out["ring"]["state"] == "declined" and rings.current() is None
    with pytest.raises(calls.CallError) as err:
        rings.answer(PHONE, ring["ring_id"])
    assert err.value.status == 409 and err.value.error == "the call is over"
    env.clock.advance(rings.AGENT_INTERVAL_MS / 1000 + 1)
    other = _ring()["ring"]
    with pytest.raises(calls.CallError) as err:
        rings.cancel("a-mike", other["ring_id"])
    assert err.value.status == 403
    assert rings.cancel("a-theo", other["ring_id"])["ring"]["state"] == "cancelled"
    assert rings.cancel("", other["ring_id"])["changed"] is False
    assert [kind for kind, _ in env.pushes] == ["ring", "declined", "ring", "cancelled"]


def test_an_unanswered_ring_becomes_missed_and_tells_the_phone(env):
    ring = _ring()["ring"]
    env.clock.advance(rings.RING_TIMEOUT_MS / 1000 - 1)
    assert rings.expire_due() == [] and rings.current()["state"] == "ringing"
    env.clock.advance(2)
    assert rings.expire_due() == [ring["ring_id"]]
    assert rings.get(ring["ring_id"])["state"] == "missed" and rings.current() is None
    assert [kind for kind, _ in env.pushes] == ["ring", "missed"]
    with pytest.raises(calls.CallError) as err:
        rings.answer(PHONE, ring["ring_id"])
    assert err.value.error == "the call is over"


def test_reads_expire_a_stale_ring_without_a_timer(env):
    ring = _ring()["ring"]
    env.clock.advance(rings.RING_TIMEOUT_MS / 1000 + 1)
    assert rings.get(ring["ring_id"])["state"] == "missed"


def test_the_agent_gets_a_notice_of_how_its_call_went(env):
    ring = _ring()["ring"]
    rings.decline(PHONE, ring["ring_id"])
    texts = [r["text"] for r in db.conn().execute(
        "SELECT text FROM messages WHERE agent_id='a-theo' ORDER BY updated_at, message_id").fetchall()]
    assert any("declined" in t and "Possible water leak" in t for t in texts)


def test_the_audit_trail_keeps_every_outcome_newest_first(env):
    ring = _ring()["ring"]
    rings.answer(PHONE, ring["ring_id"])
    trail = rings.audit()
    assert [e["outcome"] for e in trail[:2]] == ["answered", "ringing"]
    assert all(e["ring_id"] == ring["ring_id"] for e in trail[:2])
    for _ in range(rings.KEEP_AUDIT + 5):
        rings.update_settings({"enabled": False})
        with pytest.raises(calls.CallError):
            _ring()
    assert len(rings.audit()) == rings.KEEP_AUDIT


def test_rings_survive_a_restart(env):
    ring = _ring()["ring"]
    rings._reset_for_tests()
    assert rings.current()["ring_id"] == ring["ring_id"]


# ---- the push ---------------------------------------------------------------

def test_the_ring_push_is_time_sensitive_with_answer_actions():
    from lib import apns
    ring = {"ring_id": "rng_1", "session": "theo-97e5", "agent_id": "a-theo", "persona": "Theo",
            "reason": "Possible water leak at pump 01", "opening_line": "Camera 2 …", "state": "ringing",
            "opening_audio_url": "/audio/x.mp3", "expires_at": 5}
    payload = apns.agent_ring_payload(ring, "ring")
    aps = payload["aps"]
    assert aps["interruption-level"] == "time-sensitive" and aps["category"] == "agent-call"
    assert aps["sound"] and aps["alert"]["title"] == "Theo is calling"
    assert aps["alert"]["body"] == "Possible water leak at pump 01"
    assert payload["kind"] == "agent-ring" and payload["ring_id"] == "rng_1"
    assert payload["session"] == "theo-97e5" and payload["ring_state"] == "ringing"
    missed = apns.agent_ring_payload({**ring, "state": "missed"}, "missed")
    assert missed["aps"]["alert"]["title"] == "Missed call from Theo"
    assert missed["aps"]["interruption-level"] == "active" and "category" not in missed["aps"]
    assert apns.agent_ring_collapse_id(ring) == apns.agent_ring_collapse_id({**ring, "state": "missed"})


# ---- routes -----------------------------------------------------------------

_SERVER = []


def _server():
    import importlib.util
    import sys
    module = sys.modules.get("server")
    if module is not None and pathlib.Path(module.__file__).resolve() == ROOT / "server/server.py":
        return module
    if not _SERVER:
        spec = importlib.util.spec_from_file_location("clarp_server_under_test_rings", ROOT / "server/server.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _SERVER.append(loaded)
    return _SERVER[0]


class _Handler:
    def __init__(self, body, env, principal=PHONE, scope="full", query=None, turn_agent=""):
        self.body, self.sent = body, []
        self._request_auth_validated, self._request_device_scope = True, scope
        self._request_principal = principal
        self.ctx = type("Ctx", (), {"stream": env.stream, "tts": env.tts, "herald": None})()
        self.headers = {"X-Clarp-Turn": ""}
        self._q = query or {}
        self._turn_agent = turn_agent

    def __getattr__(self, name):
        if name.startswith(("_handle_calls", "_calls_")):
            return getattr(_server().Handler, name).__get__(self)
        raise AttributeError(name)

    def _turn_caller(self):
        return self._turn_agent

    def _read_json(self):
        return self.body

    def _json(self, status, value):
        self.sent.append((status, value))

    def _json_ok(self, value):
        self.sent.append((200, value))

    def _json_error(self, status, message):
        self.sent.append((status, {"error": message}))

    def _query(self):
        return self._q


def test_the_ring_routes(env):
    server = _server()
    limited = _Handler({"agent": "Theo", "reason": "r", "opening_line": "o"}, env, scope="limited")
    server.Handler._handle_calls_ring(limited)
    assert limited.sent[0][0] == 401
    wrong = _Handler({"agent": "Mike", "reason": "r", "opening_line": "o"}, env, turn_agent="a-theo")
    server.Handler._handle_calls_ring(wrong)
    assert wrong.sent[0][0] == 403
    made = _Handler({"reason": "r", "opening_line": "o"}, env, turn_agent="a-theo")
    server.Handler._handle_calls_ring(made)
    status, body = made.sent[0]
    assert status == 200 and body["ring"]["session"] == "theo-97e5"
    rid = body["ring"]["ring_id"]
    getter = _Handler(None, env, query={"ring_id": [rid]})
    server.Handler._handle_calls_ring_get(getter)
    assert getter.sent[0][1]["ring"]["state"] == "ringing"
    current = _Handler(None, env)
    server.Handler._handle_calls_ring_get(current)
    assert current.sent[0][1]["ring"]["ring_id"] == rid
    answer = _Handler({"ring_id": rid}, env)
    server.Handler._handle_calls_ring_answer(answer)
    status, body = answer.sent[0]
    assert status == 200 and body["call"]["floor"] == "theo-97e5" and body["opening"]["text"] == "o"
    late = _Handler({"ring_id": rid}, env)
    server.Handler._handle_calls_ring_decline(late)
    assert late.sent[0][0] == 409
    settings = _Handler({"enabled": False}, env)
    server.Handler._handle_calls_ring_settings_post(settings)
    assert settings.sent[0][1]["settings"]["enabled"] is False
    read = _Handler(None, env)
    server.Handler._handle_calls_ring_settings_get(read)
    body = read.sent[0][1]
    assert body["settings"]["enabled"] is False
    assert [e["outcome"] for e in body["audit"][:3]] == ["settings", "answered", "ringing"]


def test_the_routes_are_registered():
    server = _server()
    posts = server.Handler.POST_ROUTES if hasattr(server.Handler, "POST_ROUTES") else None
    source = (ROOT / "server/server.py").read_text()
    for route, handler in (('"/calls/ring"', "_handle_calls_ring"),
                           ('"/calls/ring/answer"', "_handle_calls_ring_answer"),
                           ('"/calls/ring/decline"', "_handle_calls_ring_decline"),
                           ('"/calls/ring/cancel"', "_handle_calls_ring_cancel"),
                           ('"/calls/ring/settings"', "_handle_calls_ring_settings_post")):
        assert f'{route}: "{handler}"' in source, route
    assert posts is None or "/calls/ring" in posts


# ---- CLI ----------------------------------------------------------------------

def _admin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("clarp_admin_rings", ROOT / "bin/clarp-admin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_rings_waits_for_the_answer_and_cancels(monkeypatch, capsys):
    admin = _admin()
    sent, states = [], iter(["ringing", "ringing", "answered"])

    def api(method, path, body=None, **kwargs):
        sent.append((method, path, body))
        if path == "/calls/ring":
            return {"ok": True, "ring": {"ring_id": "rng_1", "state": "ringing"}, "summary": "Calling."}
        if path.startswith("/calls/ring?"):
            return {"ring": {"ring_id": "rng_1", "state": next(states)}}
        return {"ok": True}
    monkeypatch.setattr(admin, "api_request", api)
    monkeypatch.setattr(admin.time, "sleep", lambda s: None)
    run = lambda argv: admin.parser().parse_args(argv).func(admin.parser().parse_args(argv))  # noqa: E731
    assert run(["call", "ring", "--reason", "Leak at pump 01", "--say", "Camera 2 shows a leak.", "--wait"]) == 0
    assert sent[0] == ("POST", "/calls/ring", {"reason": "Leak at pump 01", "opening_line": "Camera 2 shows a leak."})
    assert [p for _, p, _ in sent[1:]] == ["/calls/ring?ring_id=rng_1"] * 3
    assert '"answered"' in capsys.readouterr().out
    sent.clear()
    assert run(["call", "ring", "--agent", "Theo", "--reason", "r", "--say", "s"]) == 0
    assert sent == [("POST", "/calls/ring", {"agent": "Theo", "reason": "r", "opening_line": "s"})]
    sent.clear()
    assert run(["call", "ring-cancel", "rng_1"]) == 0
    assert run(["call", "ring-status"]) == 0
    assert run(["call", "rings", "off"]) == 0
    assert run(["call", "rings", "status"]) == 0
    assert sent == [("POST", "/calls/ring/cancel", {"ring_id": "rng_1"}), ("GET", "/calls/ring", None),
                    ("POST", "/calls/ring/settings", {"enabled": False}), ("GET", "/calls/ring/settings", None)]
