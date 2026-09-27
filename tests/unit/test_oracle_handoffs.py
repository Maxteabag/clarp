"""Oracle handoffs: the Host side of docs/oracle-handoff.md (revision 4).

A fake phone drives the real ``oracle_handoffs`` state machine and the real
stable ``Conversation`` (fake upstream, fake agent tools, no network): offers,
phase acks, deadlines, rollbacks, returns, new-call rules, the route and the
CLI. Nothing here says anything to the Host in words; a handoff starts only
through ``connect``.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import re
import threading
import time
import urllib.error

import pytest

from lib import agents as agents_db
from lib import db, oracle_contact, oracle_handoffs as handoffs, oracle_live_stable as mod

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = "device_phone1"


def _agent(agent_id, persona, session, *, archived=False, deleted=False, helper=False, janitor=False):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, archived_at, deleted_at,"
        " is_janitor, role, parent_agent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, persona, "v", "/tmp", session, db.now_ms(), db.now_ms() if archived else None,
         db.now_ms() if deleted else None, 1 if janitor else 0,
         "helper" if helper else "agent", "a-theo" if helper else None))


class Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(dict(event))

    def handoffs(self):
        return [e for e in self.events if e["type"] == "oracle-handoff"]


class Upstream:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        pass


class Tools:
    def __init__(self):
        self.lock = threading.Lock()
        self.delegations = set()
        self.fallback = "theo-97e5"
        self.dispatched = []

    def results(self):
        return []

    def resolve(self, name):
        return {"session": "theo-97e5", "persona": "Theo", "agent_id": "a-theo"}

    def execute(self, name, arguments, call_id):
        if name == "list_agents":
            return {"agents": [], "oracle_contact": "theo-97e5"}
        self.dispatched.append(dict(arguments))
        return {"status": "accepted", "operation_id": "op-" + str(len(self.dispatched)), "session": "theo-97e5"}


class Call:
    """One Oracle v2 call on the phone: the real Conversation plus its socket."""

    def __init__(self, token, *, capable=True, handoff_id=""):
        self.down, self.upstream = [], Upstream()
        self.now = [100.0]
        self.conv = mod.Conversation(self.upstream, self.down.append, Tools(), "key",
                                     clock=lambda: self.now[0], delegation_strategy="direct_contact")
        self.conv.principal, self.conv.voice_session_id = PHONE, token
        self.conv.handoff_capable, self.conv.handoff_id = capable, handoff_id
        handoffs.register(self.conv)

    def close(self):
        handoffs.unregister(self.conv)
        self.conv.stop.set()
        self.conv.pool.shutdown(wait=True)

    def user(self, text):
        self.conv.receive({"type": "session.input_transcript.delta", "delta": text, "start_ms": 0, "end_ms": 800})
        self.now[0] += 1.1
        with self.conv.lock:
            self.conv.routing += 1
        self.conv.route("item")

    def mirrored(self):
        return [e for e in self.down if e["type"] == "oracle_v2.handoff"]

    def cues(self):
        return [e["name"] for e in self.down if e["type"] == "oracle_v2.cue"]

    def notes(self):
        return [e["content"] for e in self.upstream.sent if e["type"].endswith(".append")
                and e["type"] != "session.input_audio.append"]


@pytest.fixture
def host(monkeypatch):
    for name in ("pending_decisions", "pending_completion_notifications"):
        monkeypatch.setattr(mod.oracle_attention, name, lambda: [])
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-mike", "Mike", "mike-86db")
    stream = Stream()
    handoffs.bind(type("Ctx", (), {"stream": stream, "herald": None})())
    calls = []

    def open_call(token="vs-1", **kwargs):
        call = Call(token, **kwargs)
        calls.append(call)
        return call
    yield stream, open_call
    for call in calls:
        call.close()
    for timer in list(handoffs._TIMERS.values()):
        timer.cancel()
    handoffs._TIMERS.clear()
    handoffs._LIVE.clear()


def _offer(target="Mike"):
    return handoffs.connect(PHONE, target, wait=None)["handoff"]


def _ack(record, phase, reason=""):
    return handoffs.ack(PHONE, {"handoff_id": record["handoff_id"], "generation": record["generation"],
                                "phase": phase, "reason": reason})["handoff"]


def _until(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---- resolving the target -------------------------------------------------

def test_a_persona_resolves_to_the_one_visible_contact_despite_hidden_duplicates():
    _agent("a-live", "Mike", "mike-86db")
    for index in range(8):
        _agent(f"a-gone-{index}", "Mike", f"mike-old{index}", deleted=True)
    _agent("a-arch", "Mike", "mike-archived", archived=True)
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-helper", "Mike", "mike-helper", helper=True)
    _agent("a-jan", "Mike", "mike-janitor", janitor=True)
    assert oracle_contact.resolve_visible("mike")["session"] == "mike-86db"
    assert oracle_contact.resolve_visible("MIKE-HELPER")["session"] == "mike-helper"   # a session always works
    with pytest.raises(ValueError, match="unknown agent"):
        oracle_contact.resolve_visible("mike-old3")
    with pytest.raises(ValueError, match="unknown agent"):
        oracle_contact.resolve_visible("Zorblax")
    _agent("a-mike2", "Mike", "mike-second")
    with pytest.raises(ValueError, match="ambiguous agent"):
        oracle_contact.resolve_visible("Mike")


# ---- connect errors ---------------------------------------------------------

def test_connect_without_a_live_call_or_to_an_unknown_agent_is_an_error(host):
    stream, open_call = host
    with pytest.raises(handoffs.HandoffError, match="no live Oracle call") as missing:
        handoffs.connect(PHONE, "Mike", wait=None)
    assert missing.value.status == 404
    open_call()
    with pytest.raises(handoffs.HandoffError, match="unknown agent"):
        handoffs.connect(PHONE, "Zorblax", wait=None)
    with pytest.raises(handoffs.HandoffError, match="no active handoff"):
        handoffs.connect(PHONE, "oracle", wait=None)
    assert stream.handoffs() == []


def test_a_call_that_did_not_advertise_handoff_is_never_offered_one(host):
    stream, open_call = host
    call = open_call(capable=False)
    with pytest.raises(handoffs.HandoffError, match="handoff_unsupported") as refused:
        handoffs.connect(PHONE, "Mike", wait=None)
    assert refused.value.status == 409
    assert stream.handoffs() == [] and call.mirrored() == [] and call.cues() == []


def test_the_administrator_acts_on_the_one_live_call_and_refuses_to_guess_between_two(host):
    stream, open_call = host
    open_call()
    record = handoffs.connect(handoffs.ADMINISTRATOR, "mike-86db", wait=None)["handoff"]
    assert record["principal"] == PHONE
    other = Call("vs-9")
    other.conv.principal = "device_other"
    handoffs.register(other.conv)
    try:
        with pytest.raises(handoffs.HandoffError, match="several live calls"):
            handoffs.connect(handoffs.ADMINISTRATOR, "Mike", wait=None)
    finally:
        other.close()


# ---- Oracle -> agent ----------------------------------------------------------

def test_offer_goes_to_sse_and_the_oracle_socket_with_the_cue_first(host):
    stream, open_call = host
    call = open_call()
    record = _offer()
    [event] = stream.handoffs()
    assert set(event) == {"type", *handoffs.events.FIELDS["oracle-handoff"]}
    assert event["handoff_id"] == record["handoff_id"] and re.fullmatch(r"hof_[0-9a-f]{32}", event["handoff_id"])
    assert (event["direction"], event["mode"], event["state"], event["revision"]) == (
        "oracle_to_agent", "hands_free", "offered", 1)
    assert event["principal"] == PHONE and event["host_id"] and event["generation"] == 1
    assert event["agent"] == {"session": "mike-86db", "agent_id": "a-mike", "persona": "Mike"}
    assert event["oracle"]["voice_session_id"] == "vs-1"
    assert event["oracle_call"] == "running" and 9000 < event["ttl_ms"] <= 10_000
    [mirror] = call.mirrored()
    assert mirror["handoff_id"] == record["handoff_id"]
    types = [e["type"] for e in call.down]
    assert types.index("oracle_v2.cue") < types.index("oracle_v2.handoff")
    assert call.cues() == ["connected"] and call.down[types.index("oracle_v2.cue")]["agent"] == "Mike"


def test_the_full_transfer_holds_oracle_and_commits_only_on_ready(host):
    stream, open_call = host
    call = open_call()
    result = {}

    def connect():
        result["value"] = handoffs.connect(PHONE, "Mike")
    worker = threading.Thread(target=connect)
    worker.start()
    assert _until(lambda: bool(stream.handoffs()))
    offered = stream.handoffs()[0]
    preparing = _ack(offered, "preparing")
    assert preparing["state"] == "preparing" and preparing["oracle_call"] == "unknown"
    notes_before = len(call.notes())
    call.user("and also check the deploy")
    assert call.conv.tools.dispatched == [], "no later turn is routed once the handoff holds Oracle"
    assert len(call.notes()) == notes_before, "Oracle is sent nothing new"
    assert _ack(offered, "activating")["state"] == "activating"
    assert worker.is_alive(), "activating is not success: capture may still fail"
    active = _ack(offered, "ready")
    worker.join(5)
    assert active["state"] == "active" and result["value"]["ok"] is True and result["value"]["state"] == "active"
    assert agents_db.get_focus_session() == "mike-86db"
    assert any(e["type"] == "agent-focus" and e["session"] == "mike-86db" for e in stream.events)
    states = [e["state"] for e in stream.handoffs()]
    assert states == ["offered", "preparing", "activating", "active"]
    assert [e["revision"] for e in stream.handoffs()] == [1, 2, 3, 4]
    assert handoffs.snapshot(PHONE)["active"]["handoff_id"] == offered["handoff_id"]


def test_a_repeated_ack_answers_the_current_record_without_repeating_effects(host):
    stream, open_call = host
    open_call()
    offered = _offer()
    _ack(offered, "preparing")
    _ack(offered, "activating")
    _ack(offered, "ready")
    events_before = len(stream.events)
    again = _ack(offered, "preparing")
    assert again["state"] == "active" and again["revision"] == 4
    assert len(stream.events) == events_before


def test_an_illegal_phase_is_409_with_the_current_record(host):
    stream, open_call = host
    open_call()
    offered = _offer()
    with pytest.raises(handoffs.HandoffError, match="illegal_transition") as illegal:
        _ack(offered, "ready")
    assert illegal.value.status == 409 and illegal.value.record["state"] == "offered"
    with pytest.raises(handoffs.HandoffError, match="illegal_transition"):
        _ack(offered, "rollback_failed")     # only legal from activating


def test_stale_generation_only_for_a_phase_not_applied_before(host):
    stream, open_call = host
    open_call()
    first = _offer()
    _ack(first, "preparing")
    second = _offer()               # supersedes the first
    assert handoffs.snapshot(PHONE)["handoff"]["handoff_id"] == second["handoff_id"]
    assert _ack(first, "preparing")["state"] == "cancelled", "a repeat still answers 200"
    with pytest.raises(handoffs.HandoffError, match="stale_generation"):
        _ack(first, "activating")


def test_a_rollback_resumes_oracle_and_gives_it_one_neutral_fact(host):
    stream, open_call = host
    call = open_call()
    offered = _offer()
    _ack(offered, "preparing")
    _ack(offered, "activating")
    failed = _ack(offered, "rolled_back")
    assert (failed["state"], failed["reason"], failed["oracle_call"]) == ("failed", "rolled_back", "running")
    assert call.conv.held_for_handoff is False
    assert call.cues() == ["connected", "switch_failed"]
    [note] = [n for n in call.notes() if "host_event" in n]
    fact = json.loads(note.split(":", 1)[1])
    assert fact == {"host_event": "handoff_failed", "agent": "Mike", "reason": "rolled_back"}
    call.user("check the deploy")
    assert len(call.conv.tools.dispatched) == 1, "the call works as before"


def test_rollback_failed_is_broken_and_never_claims_the_call_resumed(host):
    stream, open_call = host
    call = open_call()
    offered = _offer()
    _ack(offered, "preparing")
    _ack(offered, "activating")
    broken = _ack(offered, "rollback_failed")
    assert broken["state"] == "broken" and broken["oracle_call"] != "running"
    assert not [n for n in call.notes() if "host_event" in n]


def test_no_ack_fails_the_offer_and_leaves_oracle_running(host, monkeypatch):
    monkeypatch.setitem(handoffs.TTL_MS, "offered", 50)
    stream, open_call = host
    call = open_call()
    with pytest.raises(handoffs.HandoffError, match="failed: no_ack"):
        handoffs.connect(PHONE, "Mike")
    record = handoffs.snapshot(PHONE)["handoff"]
    assert (record["state"], record["reason"], record["oracle_call"]) == ("failed", "no_ack", "running")
    assert call.cues() == ["connected", "switch_failed"]


def test_prepare_timeout_resumes_feeding_and_a_late_rollback_confirms_it(host, monkeypatch):
    monkeypatch.setitem(handoffs.TTL_MS, "preparing", 50)
    stream, open_call = host
    call = open_call()
    offered = _offer()
    _ack(offered, "preparing")
    assert _until(lambda: handoffs.snapshot(PHONE)["handoff"]["state"] == "failed")
    record = handoffs.snapshot(PHONE)["handoff"]
    assert (record["reason"], record["oracle_call"]) == ("prepare_timeout", "unknown")
    assert call.conv.held_for_handoff is False
    with pytest.raises(handoffs.HandoffError, match="illegal_transition"):
        _ack(offered, "activating")
    late = _ack(offered, "rolled_back")
    assert (late["state"], late["oracle_call"], late["revision"]) == ("failed", "running", record["revision"] + 1)


def test_activation_timeout_is_broken_and_a_late_ready_is_accepted(host, monkeypatch):
    monkeypatch.setitem(handoffs.TTL_MS, "activating", 50)
    stream, open_call = host
    call = open_call()
    offered = _offer()
    _ack(offered, "preparing")
    _ack(offered, "activating")
    assert _until(lambda: handoffs.snapshot(PHONE)["handoff"]["state"] == "broken")
    assert call.conv.held_for_handoff is True, "the Host never resumes Oracle on its own here"
    assert _ack(offered, "ready")["state"] == "active"


def test_a_new_oracle_call_supersedes_an_offer_and_ends_an_active_handoff(host):
    stream, open_call = host
    open_call("vs-1")
    offered = _offer()
    open_call("vs-2")
    assert handoffs.snapshot(PHONE)["handoff"]["state"] == "cancelled"
    assert handoffs.snapshot(PHONE)["handoff"]["reason"] == "superseded"
    second = _offer()
    _ack(second, "preparing"); _ack(second, "activating"); _ack(second, "ready")
    open_call("vs-3")
    record = handoffs.snapshot(PHONE)["handoff"]
    assert (record["state"], record["reason"]) == ("ended", "client_returned")
    assert offered["handoff_id"] != second["handoff_id"]


def test_abandon_is_bookkeeping_only(host):
    stream, open_call = host
    call = open_call()
    offered = _offer()
    _ack(offered, "preparing")
    cancelled = _ack(offered, "abandon", "local_stop")
    assert (cancelled["state"], cancelled["reason"]) == ("cancelled", "local_stop")
    assert call.conv.held_for_handoff is True, "abandon resumes nothing"
    with pytest.raises(handoffs.HandoffError, match="illegal_transition"):
        _ack(offered, "rolled_back")         # cancelled is terminal: no recovery ack


# ---- agent -> Oracle ----------------------------------------------------------

def _through(open_call):
    call = open_call("vs-1")
    offered = _offer()
    _ack(offered, "preparing"); _ack(offered, "activating"); _ack(offered, "ready")
    call.close()                               # the phone closes Oracle after active
    return offered


def test_the_return_completes_only_when_the_new_oracle_session_is_ready(host):
    stream, open_call = host
    parent = _through(open_call)
    back = handoffs.connect(handoffs.ADMINISTRATOR, "oracle", wait=None)["handoff"]
    assert back["direction"] == "agent_to_oracle" and back["parent_handoff_id"] == parent["handoff_id"]
    assert back["oracle"]["thread_id"] == parent["oracle"]["thread_id"]
    assert back["agent"]["session"] == "mike-86db" and back["generation"] == parent["generation"] + 1
    _ack(back, "preparing")
    _ack(back, "activating")
    call = open_call("vs-2", handoff_id=back["handoff_id"])
    assert handoffs.snapshot(PHONE)["handoff"]["state"] == "activating", "opening the socket is not success"
    assert call.mirrored() == [], "a return is announced on SSE only"
    done = _ack(back, "ready")
    assert (done["state"], done["reason"]) == ("ended", "returned")
    snap = handoffs.snapshot(PHONE)
    assert snap["active"] is None
    parent_now = next(e for e in reversed(stream.handoffs()) if e["handoff_id"] == parent["handoff_id"])
    assert (parent_now["state"], parent_now["reason"]) == ("ended", "returned")
    assert call.cues() == ["back_to_oracle"]
    assert any('"host_event": "returned_from_agent"' in n for n in call.notes())


def test_a_failed_return_keeps_the_parent_active_and_a_broken_one_ends_it(host):
    stream, open_call = host
    _through(open_call)
    back = _offer("oracle")
    _ack(back, "preparing")
    rolled = _ack(back, "rolled_back")
    assert rolled["state"] == "failed" and rolled["oracle_call"] == "closed"
    assert handoffs.snapshot(PHONE)["active"] is not None
    again = _offer("oracle")
    _ack(again, "preparing"); _ack(again, "activating")
    assert _ack(again, "rollback_failed")["state"] == "broken"
    snap = handoffs.snapshot(PHONE)
    assert snap["active"] is None


def test_a_return_the_phone_no_longer_intends_ends_the_parent(host):
    stream, open_call = host
    _through(open_call)
    back = _offer("oracle")
    failed = _ack(back, "failed", "local_intent_changed")
    assert (failed["state"], failed["reason"]) == ("failed", "local_intent_changed")
    assert handoffs.snapshot(PHONE)["active"] is None


# ---- restart, wire, route, CLI -----------------------------------------------

def test_a_restart_runs_the_phase_deadline_again(host, monkeypatch):
    stream, open_call = host
    open_call()
    offered = _offer()
    _ack(offered, "preparing")
    monkeypatch.setattr(handoffs, "_BOOT", "after-restart")
    monkeypatch.setitem(handoffs.TTL_MS, "preparing", 50)
    handoffs.recover()
    assert _until(lambda: handoffs.snapshot(PHONE)["handoff"]["reason"] == "prepare_timeout")


def test_session_close_may_carry_its_handoff_id():
    ident = "hof_" + "a" * 32
    assert mod.client_event(json.dumps({"type": "session.close", "handoff_id": ident})) == {
        "type": "session.close", "handoff_id": ident}
    assert mod.client_event(json.dumps({"type": "session.close"})) == {"type": "session.close"}
    assert mod.client_event(json.dumps({"type": "session.close", "handoff_id": "nope"})) is None


_SERVER = []


def _server():
    """This checkout's server.py. Loading clarp-admin (here and in other tests)
    puts the installed Host's share directory on sys.path, so a bare
    ``import server`` can find a different server.py."""
    import sys
    module = sys.modules.get("server")
    if module is not None and pathlib.Path(module.__file__).resolve() == ROOT / "server/server.py":
        return module
    if not _SERVER:
        spec = importlib.util.spec_from_file_location("clarp_server_under_test", ROOT / "server/server.py")
        assert spec and spec.loader
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _SERVER.append(loaded)
    return _SERVER[0]


class _Handler:
    def __init__(self, body, principal=PHONE, scope="full"):
        self.body, self.sent = body, []
        self._request_auth_validated, self._request_device_scope = True, scope
        self._request_principal = principal
        self.ctx = type("Ctx", (), {"stream": Stream(), "herald": None})()

    def _oracle_handoff_principal(self):
        return _server().Handler._oracle_handoff_principal(self)

    def _read_json(self):
        return self.body

    def _json(self, status, value):
        self.sent.append((status, value))

    def _json_ok(self, value):
        self.sent.append((200, value))

    def _json_error(self, status, message):
        self.sent.append((status, {"error": message}))

    def _query(self):
        return {}


def test_the_route_needs_full_scope_and_reports_errors(host):
    server = _server()
    limited = _Handler({"agent": "Mike"}, scope="limited")
    server.Handler._handle_oracle_connect(limited)
    assert limited.sent[0][0] == 401
    handler = _Handler({"agent": "Mike"})
    server.Handler._handle_oracle_connect(handler)
    assert handler.sent == [(404, {"ok": False, "error": "no live Oracle call"})]
    getter = _Handler(None)
    server.Handler._handle_oracle_handoff_get(getter)
    status, snap = getter.sent[0]
    assert status == 200 and snap["principal"] == PHONE and snap["handoff"] is None and "server_now" in snap


def test_the_route_puts_the_live_call_through(host):
    server = _server()
    stream, open_call = host
    open_call()
    handler = _Handler({"agent": "Mike"})
    worker = threading.Thread(target=server.Handler._handle_oracle_connect, args=(handler,))
    worker.start()
    assert _until(lambda: handoffs.snapshot(PHONE)["handoff"] is not None)
    offered = handoffs.snapshot(PHONE)["handoff"]
    for phase in ("preparing", "activating", "ready"):
        acker = _Handler({"handoff_id": offered["handoff_id"], "generation": offered["generation"], "phase": phase})
        server.Handler._handle_oracle_handoff_ack(acker)
        assert acker.sent[0][0] == 200
    worker.join(5)
    assert handler.sent[0][0] == 200 and handler.sent[0][1]["state"] == "active"


def _admin():
    spec = importlib.util.spec_from_file_location("clarp_admin_oracle", ROOT / "bin/clarp-admin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_posts_connect_and_fails_with_the_hosts_error(monkeypatch, capsys):
    admin = _admin()
    calls = []

    def ok(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs.get("timeout")))
        return {"ok": True, "state": "active"}
    monkeypatch.setattr(admin, "api_request", ok)
    args = admin.argparse.Namespace(agent="mike", principal="")
    assert admin.cmd_oracle(args) == 0
    assert calls == [("POST", "/oracle/connect", {"agent": "mike"}, admin.ORACLE_CONNECT_TIMEOUT)]
    assert admin.ORACLE_CONNECT_TIMEOUT > handoffs.CONNECT_WAIT_SECONDS

    def refused(method, path, body=None, **kwargs):
        raise urllib.error.HTTPError(path, 404, "Not Found", {},
                                     io.BytesIO(b'{"ok": false, "error": "no live Oracle call"}'))
    monkeypatch.setattr(admin, "api_request", refused)
    assert admin.cmd_oracle(admin.argparse.Namespace(agent="oracle", principal="")) == 1
    assert "no live Oracle call" in capsys.readouterr().out


def test_the_cli_parses_oracle_connect():
    admin = _admin()
    args = admin.parser().parse_args(["oracle", "connect", "mike", "--principal", "device_x"])
    assert args.func is admin.cmd_oracle and (args.agent, args.principal) == ("mike", "device_x")


# ---- no phrase matching, no scripted lines ---------------------------------

ORACLE_PATH = ["oracle_live_stable.py", "oracle_relay.py", "oracle_strategy.py", "oracle_prompt.py",
               "oracle_handoffs.py", "oracle_earcons.py", "oracle_contact.py"]


def test_no_regex_phrase_matching_remains_in_the_oracle_path():
    """The only regexes left: handoff ids, and the agent's own <speak> markup."""
    found = []
    for name in ORACLE_PATH:
        text = (ROOT / "server/lib" / name).read_text()
        for line in text.splitlines():
            if re.search(r"\bre\.(compile|search|match|fullmatch|findall|finditer|sub)\(|__import__\(\"re\"\)", line):
                found.append((name, line.strip()))
    assert found == [
        ("oracle_relay.py", 'spoken = re.findall(r"<speak>(.*?)</speak>", raw, flags=re.S)'),
        ("oracle_relay.py", 'return re.sub(r"<[^>]+>", "", " ".join(spoken)) if spoken else raw'),
        ("oracle_handoffs.py", 'ID_PATTERN = re.compile(r"hof_[0-9a-f]{32}")'),
    ]
    assert not (ROOT / "server/lib/oracle_voices.py").exists()


def test_the_host_never_scripts_what_oracle_says():
    for name in ORACLE_PATH:
        text = (ROOT / "server/lib" / name).read_text()
        for scripted in ("Host note", "Tell the user", "in one short sentence", "Connecting you",
                         "Say only this"):
            assert scripted not in text, (name, scripted)


def test_speech_about_switching_starts_nothing(host):
    """Direct mode: "put me through to Mike" is just a turn for the primary."""
    stream, open_call = host
    call = open_call()
    call.user("Put me through to Mike")
    assert len(call.conv.tools.dispatched) == 1
    assert stream.handoffs() == [] and call.mirrored() == []
    assert call.cues() == ["handed_off"]
