"""Group calls: the Host side of docs/group-calls.md.

The real ``group_calls`` module against a temporary database: the record and
its participant states, floor passing, idempotency, restarts, concurrency,
spoken-name resolution, call routing for /send, the routes and the CLI.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import pathlib
import threading
import time
import urllib.error

import pytest

from lib import agents as agents_db
from lib import db, group_calls as calls, message_store, settings_store

ROOT = pathlib.Path(__file__).resolve().parents[2]
PHONE = "device_phone1"
OTHER = "device_tablet"
ADMIN = calls.ADMINISTRATOR


def _agent(agent_id, persona, session, *, archived=False, janitor=False, helper=False, runtime=True):
    db.conn().execute(
        "INSERT INTO agents(agent_id, persona, voice_id, cwd, session, created_at, archived_at,"
        " is_janitor, role, parent_agent_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (agent_id, persona, "v", "/tmp", session, db.now_ms(), db.now_ms() if archived else None,
         1 if janitor else 0, "helper" if helper else "agent", "a-theo" if helper else None))
    if runtime:
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

    def calls(self):
        return [e for e in self.events if e["type"] == "group-call"]


@pytest.fixture
def stream():
    _agent("a-theo", "Theo", "theo-97e5")
    _agent("a-mike", "Mike", "mike-86db")
    _agent("a-nadia", "Nadia", "nadia-1a2b")
    _agent("a-solu", "Solu", "solu-3c4d")
    _agent("a-omar", "Omar", "omar-5e6f")
    _agent("a-jan", "Janitor", "janitor-0000", janitor=True)
    _agent("a-old", "Oldie", "oldie-0000", archived=True)
    stream = Stream()
    calls.bind(type("Ctx", (), {"stream": stream})())
    return stream


def _states(call):
    return {p["persona"]: p["state"] for p in call["participants"]}


def _notices(agent_id):
    rows = db.conn().execute(
        "SELECT text, origin, role FROM messages WHERE agent_id=? ORDER BY updated_at, message_id",
        (agent_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- starting and the record -------------------------------------------

def test_start_makes_a_live_call_with_the_first_agent_on_the_floor(stream):
    out = calls.start(PHONE, ["Theo", "mike"])
    call = out["call"]
    assert call["call_id"].startswith("gcl_") and call["state"] == "live"
    assert call["principal"] == PHONE and call["floor"] == "theo-97e5"
    assert _states(call) == {"Theo": "active", "Mike": "invited"}
    assert call["revision"] == 1 and call["ended_at"] is None and call["started_at"] > 0
    [event] = stream.calls()
    assert event["call_id"] == call["call_id"] and event["revision"] == 1
    assert event["change"] == {"action": "start", "session": "theo-97e5", "by": ""}
    assert calls.snapshot(PHONE)["call"]["call_id"] == call["call_id"]
    assert "Theo" in out["summary"] and "Mike" in out["summary"]


def test_start_needs_one_to_eight_reachable_agents(stream):
    for bad in ([], ["Janitor"], ["Oldie"], ["nobody-at-all"]):
        with pytest.raises(calls.CallError):
            calls.start(PHONE, bad)
    many = []
    for i in range(9):
        _agent(f"a-x{i}", f"Extra{i}", f"extra{i}-0000")
        many.append(f"extra{i}-0000")
    with pytest.raises(calls.CallError) as err:
        calls.start(PHONE, many)
    assert err.value.status == 409
    assert calls.snapshot(PHONE)["call"] is None


def test_a_retried_start_returns_the_same_call_and_a_new_one_supersedes(stream):
    first = calls.start(PHONE, ["Theo"], request_id="req-1")["call"]
    again = calls.start(PHONE, ["Theo"], request_id="req-1")
    assert again["call"]["call_id"] == first["call_id"] and again["changed"] is False
    second = calls.start(PHONE, ["Mike"])["call"]
    assert second["call_id"] != first["call_id"]
    old = calls.get_call(first["call_id"])
    assert old["state"] == "ended" and old["reason"] == "superseded"
    assert all(p["state"] == "left" for p in old["participants"])


def test_calls_are_per_principal(stream):
    mine = calls.start(PHONE, ["Theo"])["call"]
    theirs = calls.start(OTHER, ["Mike"])["call"]
    assert calls.snapshot(PHONE)["call"]["call_id"] == mine["call_id"]
    with pytest.raises(calls.CallError) as err:
        calls.change(PHONE, "add", "Nadia", call_id=theirs["call_id"])
    assert err.value.status == 404
    with pytest.raises(calls.CallError) as err:
        calls.change(ADMIN, "add", "Nadia")
    assert err.value.status == 409 and err.value.error == "several live calls"
    out = calls.change(ADMIN, "add", "Nadia", call_id=theirs["call_id"])
    assert _states(out["call"])["Nadia"] == "invited"


# ---- participant changes -------------------------------------------------

def test_add_is_idempotent_and_readding_resumes(stream):
    calls.start(PHONE, ["Theo"])
    out = calls.change(PHONE, "add", "Mike")
    assert out["changed"] and _states(out["call"])["Mike"] == "invited"
    rev = out["call"]["revision"]
    again = calls.change(PHONE, "add", "mike-86db")
    assert again["changed"] is False and again["call"]["revision"] == rev
    calls.change(PHONE, "hold", "Mike")
    back = calls.change(PHONE, "add", "Mike")
    assert _states(back["call"])["Mike"] == "active"
    calls.change(PHONE, "remove", "Mike")
    back = calls.change(PHONE, "add", "Mike")
    assert _states(back["call"])["Mike"] == "active"
    sessions = [p["session"] for p in back["call"]["participants"]]
    assert sessions == ["theo-97e5", "mike-86db"]


def test_janitors_and_archived_agents_never_join(stream):
    calls.start(PHONE, ["Theo"])
    for name in ("Janitor", "janitor-0000", "Oldie"):
        with pytest.raises(calls.CallError) as err:
            calls.change(PHONE, "add", name)
        assert err.value.status == 404


def test_holding_the_floor_holder_passes_the_floor_to_the_last_one_who_had_it(stream):
    calls.start(PHONE, ["Theo", "Mike", "Nadia"])
    calls.change(PHONE, "switch", "Nadia")
    calls.change(PHONE, "switch", "Mike")
    out = calls.change(PHONE, "hold", "Mike")
    assert out["call"]["floor"] == "nadia-1a2b"
    assert _states(out["call"])["Mike"] == "on_hold"
    calls.change(PHONE, "hold", "Nadia")
    out = calls.change(PHONE, "hold", "Theo")
    assert out["call"]["floor"] == "" and out["call"]["state"] == "live"
    out = calls.change(PHONE, "resume", "Mike")
    assert out["call"]["floor"] == "mike-86db" and _states(out["call"])["Mike"] == "active"


def test_removing_the_active_speaker_passes_the_floor_and_the_last_one_ends_the_call(stream):
    calls.start(PHONE, ["Theo", "Mike"])
    out = calls.change(PHONE, "remove", "Theo")
    assert out["call"]["floor"] == "mike-86db" and _states(out["call"])["Theo"] == "left"
    out = calls.change(PHONE, "remove", "Mike")
    assert out["call"]["state"] == "ended" and out["call"]["reason"] == "empty"
    assert calls.snapshot(PHONE)["call"] is None


def test_removing_the_last_reachable_one_keeps_the_call_while_others_are_on_hold(stream):
    calls.start(PHONE, ["Theo", "Mike"])
    calls.change(PHONE, "hold", "Mike")
    out = calls.change(PHONE, "remove", "Theo")
    # Mike is still in the call, on hold: the call stays up with no floor.
    assert out["call"]["state"] == "live" and out["call"]["floor"] == ""
    out = calls.change(PHONE, "remove", "Mike")
    assert out["call"]["state"] == "ended"


def test_transfer_holds_the_current_one_and_calls_someone_else(stream):
    calls.start(PHONE, ["Theo"])
    out = calls.change(PHONE, "transfer", "Omar")
    assert _states(out["call"]) == {"Theo": "on_hold", "Omar": "active"}
    assert out["call"]["floor"] == "omar-5e6f"
    assert stream.calls()[-1]["change"]["action"] == "transfer"
    # Transferring back resumes Theo and holds Omar.
    out = calls.change(PHONE, "transfer", "Theo")
    assert _states(out["call"]) == {"Theo": "active", "Omar": "on_hold"}


def test_switch_resumes_or_adds_without_holding_anyone(stream):
    calls.start(PHONE, ["Theo", "Mike"])
    calls.change(PHONE, "hold", "Mike")
    out = calls.change(PHONE, "switch", "Mike")
    assert out["call"]["floor"] == "mike-86db"
    assert _states(out["call"]) == {"Theo": "active", "Mike": "active"}
    out = calls.change(PHONE, "switch", "Nadia")
    assert out["call"]["floor"] == "nadia-1a2b" and _states(out["call"])["Nadia"] == "active"


def test_changes_to_someone_not_in_the_call_are_refused(stream):
    calls.start(PHONE, ["Theo"])
    for action in ("remove", "hold", "resume"):
        with pytest.raises(calls.CallError) as err:
            calls.change(PHONE, action, "Mike")
        assert err.value.status == 409 and err.value.error == "not in the call"


def test_end_is_idempotent_and_nothing_changes_afterwards(stream):
    call = calls.start(PHONE, ["Theo", "Mike"])["call"]
    out = calls.end(PHONE)
    assert out["call"]["state"] == "ended" and out["call"]["reason"] == "ended"
    assert out["call"]["ended_at"] and all(p["state"] == "left" for p in out["call"]["participants"])
    again = calls.end(PHONE, call_id=call["call_id"])
    assert again["changed"] is False
    with pytest.raises(calls.CallError) as err:
        calls.change(PHONE, "add", "Nadia")
    assert err.value.status == 404 and err.value.error == "no live call"
    with pytest.raises(calls.CallError):
        calls.change(PHONE, "add", "Nadia", call_id=call["call_id"])


def test_unknown_actions_are_refused(stream):
    calls.start(PHONE, ["Theo"])
    with pytest.raises(calls.CallError) as err:
        calls.change(PHONE, "mute", "Theo")
    assert err.value.status == 400


# ---- events and agent logs ---------------------------------------------

def test_every_change_emits_one_event_with_a_rising_revision(stream):
    calls.start(PHONE, ["Theo"])
    calls.change(PHONE, "add", "Mike")
    calls.change(PHONE, "add", "Mike")          # no change, no event
    calls.change(PHONE, "hold", "Theo")
    calls.end(PHONE)
    revisions = [e["revision"] for e in stream.calls()]
    assert revisions == [1, 2, 3, 4]
    assert [e["change"]["action"] for e in stream.calls()] == ["start", "add", "hold", "end"]
    assert stream.calls()[-1]["state"] == "ended"


def test_changes_by_an_agent_are_attributed(stream):
    calls.start(PHONE, ["Theo"])
    calls.change(PHONE, "add", "Mike", by="theo-97e5")
    assert stream.calls()[-1]["change"] == {"action": "add", "session": "mike-86db", "by": "theo-97e5"}


def test_changes_leave_quiet_system_notices_in_the_agents_logs(stream):
    calls.start(PHONE, ["Theo"])
    calls.change(PHONE, "add", "Mike")
    calls.change(PHONE, "hold", "Theo")
    calls.end(PHONE)
    theo = _notices("a-theo")
    mike = _notices("a-mike")
    assert theo and mike
    assert all(row["origin"] == "system" and row["role"] == "assistant" for row in theo + mike)
    assert any("Mike joined" in row["text"] for row in theo)
    assert any("on hold" in row["text"] for row in theo)
    assert any("ended" in row["text"] for row in theo + mike)
    assert not _notices("a-nadia")


def test_a_notice_for_an_agent_without_a_runtime_is_skipped(stream):
    _agent("a-new", "Newbie", "newbie-0000", runtime=False)
    out = calls.start(PHONE, ["Newbie"])
    assert out["call"]["state"] == "live" and not _notices("a-new")


# ---- names ---------------------------------------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("Mike", "mike-86db"), ("mike.", "mike-86db"), ("MIKE", "mike-86db"),
    ("mike-86db", "mike-86db"), ("Mikey", "mike-86db"), ("nadja", "nadia-1a2b"),
    ("Omar?", "omar-5e6f"), ("Sulu", "solu-3c4d"), ("qq", None), ("zebra", None), ("", None),
])
def test_spoken_names_resolve(stream, spoken, expected):
    if expected is None:
        with pytest.raises(calls.CallError) as err:
            calls.resolve_name(spoken)
        assert err.value.status == 404 and err.value.error == "unknown agent"
    else:
        assert calls.resolve_name(spoken)["session"] == expected


def test_ambiguous_spoken_names_list_the_candidates(stream):
    _agent("a-mikael", "Mikael", "mikael-7777")
    with pytest.raises(calls.CallError) as err:
        calls.resolve_name("Mik")
    assert err.value.status == 409 and err.value.error == "ambiguous agent"
    names = {c["persona"] for c in err.value.extra["candidates"]}
    assert names == {"Mike", "Mikael"}
    # An exact name is never ambiguous.
    assert calls.resolve_name("Mike")["session"] == "mike-86db"


def test_two_contacts_with_one_persona_are_ambiguous(stream):
    _agent("a-mike2", "Mike", "mike-9999")
    with pytest.raises(calls.CallError) as err:
        calls.resolve_name("Mike")
    assert err.value.error == "ambiguous agent"
    assert calls.resolve_name("mike-9999")["session"] == "mike-9999"


def test_call_participants_win_a_tie_with_the_roster(stream):
    _agent("a-mike2", "Mike", "mike-9999")
    calls.start(PHONE, ["mike-9999"])
    out = calls.change(PHONE, "hold", "Mike")
    assert _states(out["call"]) == {"Mike": "on_hold"}
    assert out["call"]["participants"][0]["session"] == "mike-9999"


def test_helper_sub_agents_are_not_called_by_persona(stream):
    _agent("a-helper", "Helpy", "helpy-0000", helper=True)
    with pytest.raises(calls.CallError):
        calls.resolve_name("Helpy")


# ---- routing speech --------------------------------------------------------

def test_routing_follows_the_record(stream):
    call = calls.start(PHONE, ["Theo", "Mike"])["call"]
    cid = call["call_id"]
    assert calls.route(PHONE, cid, "theo-97e5")["session"] == "theo-97e5"
    # Speaking to an invited participant activates it and gives it the floor.
    out = calls.route(PHONE, cid, "mike-86db")
    assert out["session"] == "mike-86db" and out["reachable"] == ["theo-97e5", "mike-86db"]
    call = calls.snapshot(PHONE)["call"]
    assert call["floor"] == "mike-86db" and _states(call)["Mike"] == "active"
    assert stream.calls()[-1]["change"]["action"] == "route"
    # Routing to the floor again changes nothing.
    rev = call["revision"]
    calls.route(PHONE, cid, "mike-86db")
    assert calls.snapshot(PHONE)["call"]["revision"] == rev


def test_a_held_or_absent_target_is_redirected_to_the_floor(stream):
    cid = calls.start(PHONE, ["Theo", "Mike"])["call"]["call_id"]
    calls.change(PHONE, "hold", "Mike")
    assert calls.route(PHONE, cid, "mike-86db")["session"] == "theo-97e5"
    assert calls.route(PHONE, cid, "nadia-1a2b")["session"] == "theo-97e5"
    assert calls.route(PHONE, cid, "")["session"] == "theo-97e5"
    assert _states(calls.snapshot(PHONE)["call"])["Mike"] == "on_hold"


def test_routing_refuses_an_ended_call_or_nobody_to_talk_to(stream):
    cid = calls.start(PHONE, ["Theo", "Mike"])["call"]["call_id"]
    calls.change(PHONE, "hold", "Theo")
    calls.change(PHONE, "hold", "Mike")
    with pytest.raises(calls.CallError) as err:
        calls.route(PHONE, cid, "theo-97e5")
    assert err.value.status == 409 and err.value.error == "nobody to talk to"
    calls.end(PHONE)
    with pytest.raises(calls.CallError) as err:
        calls.route(PHONE, cid, "theo-97e5")
    assert err.value.status == 409 and err.value.error == "call ended"
    with pytest.raises(calls.CallError) as err:
        calls.route(OTHER, cid, "theo-97e5")
    assert err.value.error == "call ended"


def test_hold_during_a_reply_takes_effect_for_the_next_utterance(stream):
    """Mike is mid-turn (thinking) when an agent holds him: the hold lands
    immediately, his turn is left alone, and the next utterance addressed to
    him goes to the floor instead."""
    cid = calls.start(PHONE, ["Mike", "Theo"])["call"]["call_id"]
    calls.route(PHONE, cid, "mike-86db")
    agents_db.record_state("a-mike", "thinking", {"trace_id": "t-1"})
    calls.change(PHONE, "hold", "Mike", by="theo-97e5")
    assert agents_db.latest_state("a-mike")["kind"] == "thinking"
    assert calls.route(PHONE, cid, "mike-86db")["session"] == "theo-97e5"


def test_ending_while_agents_are_mid_turn_ends_routing_but_not_their_work(stream):
    cid = calls.start(PHONE, ["Mike", "Theo"])["call"]["call_id"]
    agents_db.record_state("a-mike", "thinking", {"trace_id": "t-1"})
    agents_db.record_state("a-theo", "tool", {"trace_id": "t-2"})
    calls.end(PHONE)
    assert agents_db.latest_state("a-mike")["kind"] == "thinking"
    assert agents_db.latest_state("a-theo")["kind"] == "tool"
    with pytest.raises(calls.CallError):
        calls.route(PHONE, cid, "mike-86db")


# ---- restarts, staleness, concurrency --------------------------------------

def test_a_live_call_survives_an_http_restart(stream):
    call = calls.start(PHONE, ["Theo", "Mike"])["call"]
    calls.change(PHONE, "hold", "Mike")
    restarted = importlib.reload(calls)
    restarted.bind(type("Ctx", (), {"stream": stream})())
    after = restarted.snapshot(PHONE)["call"]
    assert after["call_id"] == call["call_id"] and after["revision"] == 2
    assert _states(after) == {"Theo": "active", "Mike": "on_hold"}
    out = restarted.change(PHONE, "resume", "Mike")
    assert out["call"]["revision"] == 3
    assert restarted.route(PHONE, call["call_id"], "mike-86db")["session"] == "mike-86db"


def test_a_call_untouched_for_twelve_hours_ends_as_stale(stream, monkeypatch):
    call = calls.start(PHONE, ["Theo"])["call"]
    later = calls.now_ms() + calls.STALE_MS + 1
    monkeypatch.setattr(calls, "now_ms", lambda: later)
    assert calls.snapshot(PHONE)["call"] is None
    old = calls.get_call(call["call_id"])
    assert old["state"] == "ended" and old["reason"] == "stale"


def test_ended_history_is_bounded(stream):
    for _ in range(calls.KEEP_ENDED + 5):
        calls.start(PHONE, ["Theo"])
    data = json.loads(settings_store.get(calls.KEY + PHONE))
    assert len(data["calls"]) == calls.KEEP_ENDED + 1
    assert sum(c["state"] == "live" for c in data["calls"]) == 1


def test_concurrent_adds_and_removes_stay_consistent(stream):
    for i in range(6):
        _agent(f"a-c{i}", f"Crew{i}", f"crew{i}-0000")
    cid = calls.start(PHONE, ["Theo"])["call"]["call_id"]
    errors = []

    def churn(i):
        try:
            for _ in range(5):
                calls.change(PHONE, "add", f"crew{i}-0000", call_id=cid)
                calls.change(PHONE, "hold", f"crew{i}-0000", call_id=cid)
                calls.change(PHONE, "remove", f"crew{i}-0000", call_id=cid)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    call = calls.snapshot(PHONE)["call"]
    assert call["state"] == "live" and call["floor"] == "theo-97e5"
    sessions = [p["session"] for p in call["participants"]]
    assert len(sessions) == len(set(sessions)) == 7
    assert all(p["state"] == "left" for p in call["participants"] if p["session"] != "theo-97e5")
    revisions = [e["revision"] for e in stream.calls()]
    assert revisions == list(range(1, len(revisions) + 1)) == list(range(1, call["revision"] + 1))
    assert call["revision"] == 1 + 6 * 5 * 3


def test_concurrent_add_and_remove_of_the_same_agent_never_duplicates(stream):
    cid = calls.start(PHONE, ["Theo"])["call"]["call_id"]
    barrier = threading.Barrier(8)
    errors = []

    def flip(i):
        barrier.wait()
        for _ in range(10):
            try:
                calls.change(PHONE, "add" if i % 2 else "remove", "Mike", call_id=cid)
            except calls.CallError as exc:
                if exc.error != "not in the call":
                    errors.append(exc)

    threads = [threading.Thread(target=flip, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors
    call = calls.get_call(cid)
    assert [p["session"] for p in call["participants"]].count("mike-86db") == 1
    assert call["state"] == "live"


# ---- orchestrator restriction ---------------------------------------------

def test_the_orchestrator_only_sees_reachable_participants(stream):
    from lib import orchestrator
    settings = orchestrator.get_legacy_settings()
    packet = orchestrator.build_context_packet(
        utterance="Mike, what do you think?", requested_session="theo-97e5",
        trace_id="t", hands_free=True, settings=settings, context_scope="all",
        only_sessions=("theo-97e5", "nadia-1a2b"))
    assert {a["session"] for a in packet["agents"]} == {"theo-97e5", "nadia-1a2b"}
    assert packet["candidate_name_matches"] == [] or all(
        c["session"] in {"theo-97e5", "nadia-1a2b"} for c in packet["candidate_name_matches"])
    full = orchestrator.build_context_packet(
        utterance="Mike?", requested_session="theo-97e5", trace_id="t",
        hands_free=True, settings=settings, context_scope="all")
    assert "mike-86db" in {a["session"] for a in full["agents"]}


# ---- routes ----------------------------------------------------------------

_SERVER = []


def _server():
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
    def __init__(self, body, stream, principal=PHONE, scope="full", query=None, turn=""):
        self.body, self.sent = body, []
        self._request_auth_validated, self._request_device_scope = True, scope
        self._request_principal = principal
        self.ctx = type("Ctx", (), {"stream": stream, "herald": None})()
        self.headers = {"X-Clarp-Turn": turn}
        self._q = query or {}

    def __getattr__(self, name):
        if name.startswith(("_handle_calls", "_calls_")):
            return getattr(_server().Handler, name).__get__(self)
        raise AttributeError(name)

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


def test_the_routes_need_full_scope(stream):
    server = _server()
    limited = _Handler({"agents": ["Theo"]}, stream, scope="limited")
    server.Handler._handle_calls_start(limited)
    assert limited.sent[0][0] == 401
    getter = _Handler(None, stream, scope="limited")
    server.Handler._handle_calls_get(getter)
    assert getter.sent[0][0] == 401


def test_the_routes_start_change_get_and_end(stream):
    server = _server()
    start = _Handler({"agents": ["Theo", "Mike"], "request_id": "r1"}, stream)
    server.Handler._handle_calls_start(start)
    status, body = start.sent[0]
    assert status == 200 and body["ok"] and body["call"]["floor"] == "theo-97e5"
    cid = body["call"]["call_id"]
    for action, agent in (("hold", "Mike"), ("resume", "Mike"), ("switch", "Mike"),
                          ("transfer", "Nadia"), ("remove", "Nadia"), ("add", "Omar")):
        handler = _Handler({"agent": agent, "call_id": cid}, stream)
        server.Handler._handle_calls_action(handler, action)
        assert handler.sent[0][0] == 200, (action, handler.sent)
        assert handler.sent[0][1]["summary"]
    getter = _Handler(None, stream)
    server.Handler._handle_calls_get(getter)
    assert getter.sent[0][1]["call"]["call_id"] == cid
    unknown = _Handler({"agent": "zebra"}, stream)
    server.Handler._handle_calls_action(unknown, "add")
    assert unknown.sent[0] == (404, {"ok": False, "error": "unknown agent"})
    missing = _Handler({}, stream)
    server.Handler._handle_calls_action(missing, "add")
    assert missing.sent[0][0] == 400
    ender = _Handler({}, stream)
    server.Handler._handle_calls_action(ender, "end")
    assert ender.sent[0][1]["call"]["state"] == "ended"
    getter = _Handler(None, stream)
    server.Handler._handle_calls_get(getter)
    assert getter.sent[0][1]["call"] is None


def test_the_administrator_acts_on_the_one_live_call_and_names_the_principal(stream):
    server = _server()
    calls.start(PHONE, ["Theo"])
    admin = _Handler({"agent": "Mike"}, stream, principal=ADMIN)
    server.Handler._handle_calls_action(admin, "add")
    assert admin.sent[0][0] == 200
    getter = _Handler(None, stream, principal=ADMIN)
    server.Handler._handle_calls_get(getter)
    assert getter.sent[0][1]["call"]["principal"] == PHONE
    # Starting as the administrator reuses the principal of the newest call.
    starter = _Handler({"agents": ["Omar"]}, stream, principal=ADMIN)
    server.Handler._handle_calls_start(starter)
    assert starter.sent[0][1]["call"]["principal"] == PHONE


def test_the_administrator_cannot_start_without_any_known_principal(stream):
    server = _server()
    starter = _Handler({"agents": ["Omar"]}, stream, principal=ADMIN)
    server.Handler._handle_calls_start(starter)
    assert starter.sent[0][0] == 409 and "principal" in starter.sent[0][1]["error"]


class _SendHandler:
    """Just enough of Handler for the /send call gate."""

    def __init__(self, stream, principal=PHONE):
        self.sent = []
        self._request_auth_validated, self._request_device_scope = True, "full"
        self._request_principal = principal
        self.ctx = type("Ctx", (), {"stream": stream})()

    def _json(self, status, value):
        self.sent.append((status, value))


def _send_request(**payload):
    from lib.send_request import SendRequest
    return SendRequest.from_payload(
        {"text": "hello", "hands_free": True, **payload}, default_session="theo-97e5",
        trace_id_factory=lambda: "trace", authenticated=True)


def test_send_reads_call_id(stream):
    assert _send_request(call_id="gcl_" + "0" * 32).call_id == "gcl_" + "0" * 32
    assert _send_request().call_id == ""


def test_the_send_gate_redirects_and_refuses(stream):
    server = _server()
    cid = calls.start(PHONE, ["Theo", "Mike"])["call"]["call_id"]
    calls.change(PHONE, "hold", "Mike")
    handler = _SendHandler(stream)
    req, only = server.Handler._apply_group_call(handler, _send_request(session="mike-86db", call_id=cid))
    assert req.session == "theo-97e5" and only == ("theo-97e5",) and not handler.sent
    plain = _send_request(session="mike-86db")
    assert server.Handler._apply_group_call(handler, plain) == (plain, None)
    calls.end(PHONE)
    req, only = server.Handler._apply_group_call(handler, _send_request(session="theo-97e5", call_id=cid))
    assert req is None and handler.sent == [(409, {"ok": False, "error": "call ended", "call_id": cid})]


# ---- CLI -------------------------------------------------------------------

def _admin():
    spec = importlib.util.spec_from_file_location("clarp_admin_calls", ROOT / "bin/clarp-admin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cli_parses_every_call_command():
    admin = _admin()
    args = admin.parser().parse_args(["call", "add", "mike", "--call", "gcl_x", "--principal", "device_x"])
    assert args.func is admin.cmd_call and (args.call_command, args.agents) == ("add", ["mike"])
    assert (args.call, args.principal) == ("gcl_x", "device_x")
    for command in ("remove", "hold", "resume", "switch", "transfer"):
        assert admin.parser().parse_args(["call", command, "x"]).call_command == command
    assert admin.parser().parse_args(["call", "start", "theo", "mike"]).agents == ["theo", "mike"]
    assert admin.parser().parse_args(["call", "status"]).call_command == "status"
    assert admin.parser().parse_args(["call", "end"]).call_command == "end"


def test_the_cli_posts_and_reports_the_hosts_error(monkeypatch, capsys):
    admin = _admin()
    sent = []

    def ok(method, path, body=None, **kwargs):
        sent.append((method, path, body))
        return {"ok": True, "summary": "Mike joined the call.", "call": {}}
    monkeypatch.setattr(admin, "api_request", ok)
    monkeypatch.setenv("CLAUDE_PWA_SESSION", "theo-97e5")
    run = lambda argv: admin.cmd_call(admin.parser().parse_args(argv))  # noqa: E731
    assert run(["call", "add", "Mike"]) == 0
    assert run(["call", "status", "--principal", "device_x"]) == 0
    assert run(["call", "start", "Theo", "Mike"]) == 0
    assert run(["call", "end", "--call", "gcl_y"]) == 0
    assert sent == [
        ("POST", "/calls/add", {"agent": "Mike", "by": "theo-97e5"}),
        ("GET", "/calls?principal=device_x", None),
        ("POST", "/calls", {"agents": ["Theo", "Mike"], "by": "theo-97e5"}),
        ("POST", "/calls/end", {"call_id": "gcl_y", "by": "theo-97e5"}),
    ]

    def refused(method, path, body=None, **kwargs):
        raise urllib.error.HTTPError(path, 409, "Conflict", {}, io.BytesIO(
            b'{"ok": false, "error": "ambiguous agent", "candidates": [{"persona": "Mike"}]}'))
    monkeypatch.setattr(admin, "api_request", refused)
    assert run(["call", "add", "Mik"]) == 1
    assert "ambiguous agent" in capsys.readouterr().out


def test_the_skill_is_managed_and_names_the_cli():
    manifest = json.loads((ROOT / "skills/manifest.json").read_text())
    text = json.dumps(manifest)
    assert "clarp-calls" in text
    skill = (ROOT / "skills/clarp-calls/SKILL.md").read_text()
    assert skill.startswith("---\nname: clarp-calls\n")
    for command in ("call status", "call add", "call hold", "call switch", "call transfer", "call remove"):
        assert command in skill


# ---- shared context: the call header in the agent's prompt -----------------

def _said(agent_id, text, *, at, role="user", origin="user"):
    """A chat row at a pinned activity time (ms)."""
    msg_id = f"m-{agent_id}-{at}-{role}"
    db.conn().execute(
        "INSERT INTO messages(message_id, agent_id, backend_session_id, seq, role, text, timestamp,"
        " updated_at, origin) VALUES (?,?,?,?,?,?,?,?,?)",
        (msg_id, agent_id, "bs-" + agent_id, at % 100000, role, text,
         time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(at / 1000)) + f".{at % 1000:03d}Z", at, origin))


def _in_call():
    call = calls.start(PHONE, ["Theo", "Mike", "Nadia"])["call"]
    base = call["started_at"] + 1000
    _said("a-theo", "Theo, what's the plan for Friday?", at=base)
    _said("a-theo", "Ship the <speak>beta on Friday</speak> and keep the flag off.", at=base + 1000,
          role="assistant")
    _said("a-nadia", "Nadia, any risk?", at=base + 2000)
    _said("a-nadia", "The migration is the only risk.", at=base + 3000, role="assistant")
    _said("a-theo", "Group call: Mike joined.", at=base + 3500, role="assistant", origin="system")
    _said("a-mike", "Mike, what do you think of Theo's idea?", at=base + 4000)
    calls.route(PHONE, call["call_id"], "mike-86db")
    return call, base


def test_the_call_header_quotes_the_turns_since_the_agent_last_spoke(stream):
    _in_call()
    header = calls.prompt_header("mike-86db")
    assert header.startswith("[Group call]")
    assert "Theo" in header and "Nadia" in header and "talking to you now" in header
    assert "User -> Theo: Theo, what's the plan for Friday?" in header
    assert "Theo: Ship the beta on Friday and keep the flag off." in header
    assert "Nadia: The migration is the only risk." in header
    # The utterance this turn answers is the prompt itself, not a quote; and
    # the Host's own notices are not part of the conversation.
    assert "what do you think of Theo's idea" not in header
    assert "Mike joined" not in header and "<speak>" not in header


def test_the_header_starts_after_the_agents_own_last_reply(stream):
    call, base = _in_call()
    _said("a-mike", "I like it, but test the migration first.", at=base + 5000, role="assistant")
    _said("a-theo", "Theo, can you do that?", at=base + 6000)
    _said("a-theo", "Yes, today.", at=base + 7000, role="assistant")
    _said("a-mike", "Mike, anything else?", at=base + 8000)
    header = calls.prompt_header("mike-86db")
    assert "Theo, can you do that?" in header and "Theo: Yes, today." in header
    assert "plan for Friday" not in header and "only risk" not in header


def test_the_header_is_capped(stream):
    call = calls.start(PHONE, ["Theo", "Mike"])["call"]
    base = call["started_at"] + 1000
    for i in range(40):
        _said("a-theo", f"question {i} " + "x" * 300, at=base + i * 2000)
        _said("a-theo", f"answer {i} " + "y" * 300, at=base + i * 2000 + 1000, role="assistant")
    _said("a-mike", "Mike?", at=base + 100000)
    calls.route(PHONE, call["call_id"], "mike-86db")
    header = calls.prompt_header("mike-86db")
    quotes = [line for line in header.splitlines() if line.startswith(("User -> ", "Theo: "))]
    assert 0 < len(quotes) <= calls.HEADER_QUOTES
    assert sum(len(q) for q in quotes) <= calls.HEADER_CHARS
    assert "answer 39" in header and "answer 0 " not in header


def test_no_header_outside_a_call_on_hold_or_for_a_turn_not_routed_from_the_call(stream):
    assert calls.prompt_header("mike-86db") == ""
    call, _ = _in_call()
    assert calls.prompt_header("theo-97e5") == ""        # the call's last utterance went to Mike
    assert calls.prompt_header("omar-5e6f") == ""        # not in the call
    calls.change(PHONE, "hold", "Mike")
    assert calls.prompt_header("mike-86db") == ""
    calls.change(PHONE, "resume", "Mike")
    calls.route(PHONE, call["call_id"], "mike-86db")
    assert calls.prompt_header("mike-86db")
    calls.end(PHONE)
    assert calls.prompt_header("mike-86db") == ""


def test_the_header_reaches_the_model_but_never_the_users_message(stream):
    from lib import voice_preamble
    _in_call()
    prompt = voice_preamble.apply_voice_preamble("Mike, what do you think of Theo's idea?",
                                                 voice=True, persona="Mike", session="mike-86db")
    assert "[Group call]" in prompt and "Theo: Ship the beta" in prompt
    # The transcript parsers strip the preamble: the chat row is exactly what was said.
    assert voice_preamble.strip_voice_preamble(prompt) == "Mike, what do you think of Theo's idea?"
    context = voice_preamble.app_turn_instructions(voice=True, session="mike-86db")
    assert "[Group call]" in context
    assert "[Group call]" not in voice_preamble.app_turn_instructions(voice=True, session="theo-97e5")


def test_the_claude_hook_adds_the_header_as_context(stream):
    import sys
    sys.path.insert(0, str(ROOT / "plugin/hooks"))
    try:
        hook = importlib.import_module("pwa_source_flag")
    finally:
        sys.path.pop(0)
    _in_call()
    mike = agents_db.get_by_session("mike-86db")
    context = hook._build_additional_context(app_dispatched=True, voiced=True, agent=mike)
    assert "[Group call]" in context and "Nadia: The migration" in context
    theo = agents_db.get_by_session("theo-97e5")
    assert "[Group call]" not in hook._build_additional_context(app_dispatched=True, voiced=True, agent=theo)
    assert hook._build_additional_context(app_dispatched=False, voiced=False, agent=mike) == ""


def test_the_header_never_changes_the_record(stream, monkeypatch):
    """The runtime process builds prompts without an event stream: reading the
    header must not end a stale call (or emit anything)."""
    call, _ = _in_call()
    before = settings_store.get(calls.KEY + PHONE)
    events = len(stream.events)
    later = calls.now_ms() + calls.STALE_MS + 1
    monkeypatch.setattr(calls, "now_ms", lambda: later)
    assert calls.prompt_header("mike-86db") == ""
    assert settings_store.get(calls.KEY + PHONE) == before and len(stream.events) == events


def test_the_skill_lets_agents_bring_others_in_only_aloud_and_when_asked_or_clearly_helpful():
    skill = (ROOT / "skills/clarp-calls/SKILL.md").read_text()
    section = skill[skill.index("## Bringing someone in yourself"):skill.index("## The call header")]
    assert "clarp-admin call add" in section and "clarp-admin call switch" in section
    assert "aloud" in section and "asked for" in section and "clearly" in section
    assert "[Group call]" in skill
