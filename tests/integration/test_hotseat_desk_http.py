"""The Hotseat desk's routes over real HTTP: a real Host, a paired full device
and a fake `hotseat` executable that logs when each invocation starts and ends.

Nothing here reads a real account or runs the installed Hotseat."""
from __future__ import annotations

import json
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
server_module.BIND_ADDR = "127.0.0.1"
build_server = server_module.build_server

ADMIN = "hotseat-desk-admin-token"
STOPPED_AT_S = 1_800_000_000.5

FAKE_HOTSEAT = r'''#!{python}
"""Fake hotseat: its state is a JSON file; every invocation logs START and END."""
import json, pathlib, sys, time
home = pathlib.Path({home!r})
state = json.loads((home / "state.json").read_text())
args = sys.argv[1:]
log = home / "calls.log"
with log.open("a") as fh:
    fh.write("START %.6f %s\n" % (time.time(), " ".join(args)))
code, out = 0, ""
def accounts(provider):
    return [{{"alias": a, "is_active": a == state["active"][provider], "error": None,
             "usage": {{"status": "allowed", "limited": False, "used_5h": 0.1, "used_7d": 0.1,
                       "usable": True, "blocked": False, "worst_used": 0.1}}}}
            for a in state["aliases"][provider]]
if args == ["tui", "--snapshot"]:
    time.sleep(state.get("tui_sleep", 0))
    if state.get("tui_fails"):
        code = 1
    out = json.dumps({{"sessions": 1, "errors": [], "accounts": [
        {{"provider": p, "alias": a, "active": a == state["active"][p], "can_switch": True, "windows": []}}
        for p in ("claude", "codex") for a in state["aliases"][p]]}})
elif args == ["list", "--json"]:
    if state.get("list_fails"):
        code = 1
    out = json.dumps({{"accounts": accounts("claude"), "codex": {{"accounts": accounts("codex")}},
                      "extensions": {{"clarp": {{"stopped": state.get("stopped", [])}}}}}})
elif args in (["codex", "--no-quota", "--json"], ["codex", "--json"]):
    out = json.dumps({{"accounts": accounts("codex")}})
elif args == ["bridge", "work"]:
    out = json.dumps({{"items": []}})
elif args[:1] == ["switch"] or args[:2] == ["codex-account", "switch"]:
    provider = "claude" if args[0] == "switch" else "codex"
    alias = args[1] if provider == "claude" else args[2]
    time.sleep(state.get("switch_sleep", 0))
    state = json.loads((home / "state.json").read_text())
    if state.get("switch_fails"):
        code = 1
    else:
        state["active"][provider] = alias
        (home / "state.json").write_text(json.dumps(state))
        out = json.dumps({{"switched": True}}) if provider == "claude" else ""
else:
    sys.stderr.write("unexpected %r\n" % (args,))
    code = 2
with log.open("a") as fh:
    fh.write("END %.6f %s\n" % (time.time(), " ".join(args)))
sys.stdout.write(out)
sys.exit(code)
'''


class FakeHotseat:
    def __init__(self, home: pathlib.Path):
        self.home = home
        self.path = home / "hotseat"
        self.path.write_text(FAKE_HOTSEAT.format(python=sys.executable, home=str(home)))
        self.path.chmod(0o755)
        self.set(active={"claude": "ai13", "codex": "cx1"},
                 aliases={"claude": ["ai13", "ai12"], "codex": ["cx1", "cx2"]}, stopped=[])

    def state(self) -> dict:
        path = self.home / "state.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def set(self, **changes) -> None:
        (self.home / "state.json").write_text(json.dumps({**self.state(), **changes}))

    def calls(self, kind: str = "START") -> list[str]:
        path = self.home / "calls.log"
        if not path.exists():
            return []
        return [line.split(" ", 2)[2] for line in path.read_text().splitlines()
                if line.startswith(kind + " ")]

    def spans(self, prefix: str) -> list[tuple[float, float]]:
        starts, spans = [], []
        for line in (self.home / "calls.log").read_text().splitlines():
            kind, at, args = line.split(" ", 2)
            if not args.startswith(prefix):
                continue
            if kind == "START":
                starts.append(float(at))
            else:
                spans.append((starts.pop(0), float(at)))
        return spans

    def stop(self, session: str, *, ready: bool = True, hard: str | None = None,
             stopped_at: float = STOPPED_AT_S, cause: str = "usage_limit") -> None:
        self.set(stopped=[{"kind": "clarp", "cause": cause, "pending": 0, "id": session,
                           "name": session.capitalize(), "backend": "claude",
                           "stopped_at": stopped_at,
                           "readiness": {"ready": ready, "hard": hard, "warning": None}}])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """(base url, full-device token, fake hotseat, turns started, ctx)."""
    from lib import device_pairing, hotseat_desk
    from lib.agents import create_agent
    from lib.backend.registry import by_id
    from lib.turn_dispatch import clear_for_agent
    home = tmp_path / "hotseat-home"
    home.mkdir()
    fake = FakeHotseat(home)
    monkeypatch.setattr(hotseat_desk, "hotseat_command", lambda: str(fake.path))
    turns: list[dict] = []

    class _Handle:
        pid = 99

    def start_turn(*, text, **metadata):
        turns.append({"text": text, **metadata})
        return _Handle()

    monkeypatch.setattr(by_id("claude"), "start_turn", start_turn)
    audio = tmp_path / "audio"
    audio.mkdir()
    agent_ids = [create_agent(persona=name.capitalize(), voice_id="", cwd=str(tmp_path),
                              session=name, backend="claude") for name in ("koko", "rachel")]
    ctx = ServerContext(
        root=tmp_path, static=pathlib.Path(__file__).resolve().parents[2] / "static",
        audio_dir=audio, agents_path=tmp_path / "agents.json", default_session="koko",
        uploads_dir=tmp_path / "uploads", media_dir=tmp_path / "media",
        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
        stt=StubSTT(text="hi", ends_terminal=True), roster_names=("Koko", "Rachel"),
        auth_token=ADMIN,
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + "/agents/snapshot", headers={"Authorization": "Bearer " + ADMIN}),
                timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    token = device_pairing.exchange(device_pairing.issue(scope="full")["code"])["token"]
    fake.server = srv
    yield base, token, fake, turns, ctx
    srv.shutdown()
    srv.server_close()
    for agent_id in agent_ids:
        clear_for_agent(agent_id)


def _call(base, token, method, path, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw or b"{}")
        except ValueError:
            return error.code, {"raw": raw.decode("utf-8", "replace")}


def _live_read(base, token):
    status, body = _call(base, token, "GET", "/hotseat/desk?refresh=1")
    assert status == 200 and body["error"] is None, body
    return body


def _resume_prompts(session: str) -> list[dict]:
    from lib import db
    agent_id = db.conn().execute(
        "SELECT agent_id FROM agents WHERE session = ?", (session,)).fetchone()["agent_id"]
    return [dict(row) for row in db.conn().execute(
        "SELECT message_id, origin, text FROM messages WHERE agent_id = ? AND role = 'user'"
        " AND message_id LIKE 'u-hotseat-resume-%'", (agent_id,))]


# --- resume ---------------------------------------------------------------

def test_two_resumes_of_one_stop_start_exactly_one_turn(desk):
    base, token, fake, turns, _ctx = desk
    from lib import hotseat_desk
    fake.stop("koko")
    _live_read(base, token)

    first = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})
    second = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert first[0] == 200 and first[1]["ok"] is True and first[1]["continued"] is True
    assert first[1].get("already_sent") is False
    assert second[0] == 200 and second[1]["ok"] is True and second[1]["already_sent"] is True
    prompts = _resume_prompts("koko")
    assert len(prompts) == 1 and prompts[0]["origin"] == "automation"
    assert [t["text"] for t in turns] == [prompts[0]["text"]]
    # The prompt says what the Host knows, not that quota is back (issue 263, point 2).
    assert prompts[0]["text"].startswith("Your work was interrupted by a usage limit.")
    assert "quota again" not in prompts[0]["text"] and "Nothing was checked again just now" in prompts[0]["text"]


def test_concurrent_resumes_of_one_stop_start_exactly_one_turn(desk):
    base, token, fake, turns, _ctx = desk
    fake.stop("koko")
    _live_read(base, token)
    gate = threading.Barrier(4)
    answers: list[tuple[int, dict]] = []

    def tap():
        gate.wait(timeout=30)
        answers.append(_call(base, token, "POST", "/hotseat/resume", {"id": "koko"}))

    threads = [threading.Thread(target=tap) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert [status for status, _ in answers] == [200] * 4
    assert sorted(body["already_sent"] for _, body in answers) == [False, True, True, True]
    assert len(turns) == 1 and len(_resume_prompts("koko")) == 1


def test_a_new_stop_of_the_same_agent_can_be_continued_again(desk):
    base, token, fake, turns, _ctx = desk
    from lib import agents as agents_db
    from lib.protocol import AgentState
    from lib.turn_dispatch import clear_for_agent
    fake.stop("koko")
    _live_read(base, token)
    assert _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})[0] == 200
    # That turn runs into the limit again: a later stop of the same agent.
    agent_id = agents_db.get_by_session("koko")["agent_id"]
    agents_db.record_state(agent_id, AgentState.INTERRUPTED, {"reason": "usage_limit"})
    clear_for_agent(agent_id)
    fake.stop("koko", stopped_at=STOPPED_AT_S + 3600)
    _ctx.hotseat_desk.refresh(force=True)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert status == 200 and body["already_sent"] is False
    assert len(turns) == 2


def test_resume_refuses_an_agent_that_is_not_in_the_stopped_list(desk):
    base, token, fake, turns, _ctx = desk
    fake.stop("koko")
    _live_read(base, token)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "rachel"})

    assert status == 409 and body["ok"] is False and body["error"] == "not_stopped"
    assert "Rachel" in body["message"]
    assert turns == [] and _resume_prompts("rachel") == []


def test_resume_refuses_while_readiness_says_it_cannot_continue(desk):
    base, token, fake, turns, _ctx = desk
    fake.stop("koko", ready=False, hard="ai13 is still rate limited")
    _live_read(base, token)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert status == 409 and body["error"] == "not_ready"
    assert body["message"] == "Koko cannot continue yet: ai13 is still rate limited."
    assert turns == []


def test_resume_refuses_an_agent_that_completed_a_turn_after_the_stop(desk):
    base, token, fake, turns, _ctx = desk
    from lib import agents as agents_db
    from lib.protocol import AgentState
    fake.stop("koko", stopped_at=time.time() - 600)
    _live_read(base, token)
    agents_db.record_state(agents_db.get_by_session("koko")["agent_id"], AgentState.DONE)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert status == 409 and body["error"] == "not_stopped"
    assert turns == []


def test_resume_refuses_an_agent_that_is_working_now(desk):
    base, token, fake, turns, _ctx = desk
    from lib import agents as agents_db
    from lib.protocol import AgentState
    fake.stop("koko", stopped_at=time.time() - 600)
    _live_read(base, token)
    agents_db.record_state(agents_db.get_by_session("koko")["agent_id"], AgentState.THINKING)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert status == 409 and body["error"] == "agent_busy" and "Koko" in body["message"]
    assert turns == []


def test_resume_refuses_archived_and_janitor_agents(desk):
    base, token, fake, turns, _ctx = desk
    from lib import agents as agents_db, db
    fake.set(stopped=[
        {"kind": "clarp", "cause": "usage_limit", "id": name, "name": name.capitalize(),
         "backend": "claude", "stopped_at": STOPPED_AT_S,
         "readiness": {"ready": True, "hard": None, "warning": None}}
        for name in ("koko", "rachel")])
    _live_read(base, token)
    con = db.conn()
    con.execute("UPDATE agents SET archived_at = ? WHERE session = 'koko'", (db.now_ms(),))
    con.execute("UPDATE agents SET is_janitor = 1 WHERE session = 'rachel'")
    con.commit()
    assert agents_db.get_by_session("rachel")["is_janitor"]

    for action in ("continue", "unpause"):
        archived = _call(base, token, "POST", "/hotseat/resume", {"id": "koko", "action": action})
        janitor = _call(base, token, "POST", "/hotseat/resume", {"id": "rachel", "action": action})
        assert archived[0] == 409 and archived[1]["error"] == "agent_archived", archived
        assert janitor[0] == 409 and janitor[1]["error"] == "janitor_inspection_only", janitor
        assert archived[1]["message"] and janitor[1]["message"]
    assert turns == []


def test_resume_dispatches_in_process_without_calling_the_host_or_its_admin_token(desk, monkeypatch):
    base, token, fake, turns, _ctx = desk
    from lib import db
    fake.stop("koko")
    _live_read(base, token)
    real_urlopen = urllib.request.urlopen
    host_calls: list[str] = []

    def urlopen(request, *args, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.endswith("/send"):
            host_calls.append(url)
            raise AssertionError("resume must not call the Host's own /send")
        return real_urlopen(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})

    assert status == 200 and body["continued"] is True, body
    assert host_calls == [] and len(turns) == 1
    admitted = db.conn().execute("SELECT COUNT(*) AS n FROM prompt_admissions").fetchone()["n"]
    assert admitted == 0, "no prompt is admitted under the administrator's credential"


def test_unpause_lifts_the_pause_and_lets_the_waiting_turns_run(desk, monkeypatch):
    base, token, _fake, _turns, _ctx = desk
    from lib import agents as agents_db, turn_queue
    from lib.turn_dispatch import TurnDispatchService
    agent_id = agents_db.get_by_session("koko")["agent_id"]
    turn_queue.set_paused(agent_id, True)
    recovered: list[int] = []
    monkeypatch.setattr(TurnDispatchService, "recover_queued",
                        lambda self, *a, **k: recovered.append(1))

    status, body = _call(base, token, "POST", "/hotseat/resume", {"id": "koko", "action": "unpause"})
    again = _call(base, token, "POST", "/hotseat/resume", {"id": "koko", "action": "unpause"})

    assert status == 200 and body["queue_paused"] is False
    assert turn_queue.is_paused(agent_id) is False
    assert recovered == [1], "the waiting turns are handed to whoever owns turns, once"
    assert again[0] == 200


# --- switch ---------------------------------------------------------------

def _last_switch(provider: str):
    from lib import settings_store
    raw = settings_store.get(f"hotseat-switcher.{provider}.last-switch")
    return json.loads(raw) if raw else None


def test_switch_to_the_active_alias_is_a_noop_that_leaves_the_flip_back_record(desk):
    base, token, fake, _turns, _ctx = desk
    from lib import settings_store
    record = json.dumps({"from": "ai12", "to": "ai13", "at": 1})
    settings_store.set_text("hotseat-switcher.claude.last-switch", record)

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai13"})

    assert status == 200 and body["ok"] is True and body["switched"] is False
    assert body["reason"] == "already_active" and body["from"] == "ai13"
    assert not [c for c in fake.calls() if c.startswith("switch")]
    assert settings_store.get("hotseat-switcher.claude.last-switch") == record


def test_switch_records_the_alias_hotseat_reports_as_active_without_any_snapshot(desk):
    base, token, fake, _turns, _ctx = desk

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})

    assert status == 200 and body["switched"] is True and body["from"] == "ai13"
    assert fake.calls().count("switch ai12 --yes --json") == 1
    record = _last_switch("claude")
    assert record["from"] == "ai13" and record["to"] == "ai12"


def test_a_repeated_switch_does_not_run_twice_or_rewrite_the_record(desk):
    base, token, fake, _turns, _ctx = desk
    assert _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})[0] == 200
    record = _last_switch("claude")

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})

    assert status == 200 and body["switched"] is False and body["reason"] == "already_active"
    assert fake.calls().count("switch ai12 --yes --json") == 1
    assert _last_switch("claude") == record


def test_concurrent_switches_never_overlap_and_the_record_names_the_last_one(desk):
    base, token, fake, _turns, _ctx = desk
    fake.set(switch_sleep=0.6, aliases={"claude": ["ai13", "ai12", "ai11"], "codex": ["cx1"]})
    answers: dict[str, tuple[int, dict]] = {}

    def switch(alias):
        answers[alias] = _call(base, token, "POST", "/hotseat/switch",
                               {"provider": "claude", "alias": alias})

    threads = [threading.Thread(target=switch, args=(alias,)) for alias in ("ai12", "ai11")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    spans = sorted(fake.spans("switch "))
    assert len(spans) == 2 and spans[0][1] <= spans[1][0], spans
    assert all(status == 200 and body["switched"] is True for status, body in answers.values())
    record = _last_switch("claude")
    assert record["to"] == fake.state()["active"]["claude"]
    assert record["from"] in ("ai12", "ai11") and record["from"] != record["to"]


def test_a_codex_switch_tells_the_backend_and_leaves_its_record_on_the_desk(desk, monkeypatch):
    base, token, fake, _turns, _ctx = desk
    from lib import backends, hotseat_status
    hooks: list[str] = []
    monkeypatch.setattr(type(backends.by_id("codex")), "on_credential_change",
                        lambda self: hooks.append(self.id))
    hotseat_status.record_action("codex", {"action": "noop", "active": "cx1", "remaining": 80},
                                 mode="notify", switched=None, now=1, floor_pct=10, cadence_s=900)
    before = hotseat_status.payload()

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "codex", "alias": "cx2"})

    assert status == 200 and body["switched"] is True and body["from"] == "cx1"
    assert hooks == ["codex"]
    assert "codex-account switch cx2" in fake.calls()
    assert _last_switch("codex")["from"] == "cx1" and _last_switch("codex")["to"] == "cx2"
    assert "desk" not in body
    record = next(s for s in _call(base, token, "GET", "/hotseat/desk")[1]["switches"] if s["provider"] == "codex")
    assert (record["state"], record["from"], record["alias"]) == ("switched", "cx1", "cx2")
    assert record["finished_at"] >= record["requested_at"]
    # The automatic switcher's own receipt and lane are its own: a manual switch
    # is none of its branches and must not rewrite its mode, floor or cadence.
    after = hotseat_status.payload()
    assert after["actions"] == before["actions"]
    assert after["switcher"]["lanes"] == before["switcher"]["lanes"] and before["switcher"]["lanes"]


def test_a_failed_switch_says_so_and_writes_no_flip_back_record(desk, monkeypatch):
    base, token, fake, _turns, _ctx = desk
    from lib import backends
    hooks: list[str] = []
    monkeypatch.setattr(type(backends.by_id("claude")), "on_credential_change",
                        lambda self: hooks.append(self.id))
    fake.set(switch_fails=True)

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})

    assert status == 409 and body["ok"] is False and body["error"] == "switch_failed"
    assert body["from"] == "ai13" and body["message"]
    assert hooks == [] and _last_switch("claude") is None
    assert "desk" not in body
    assert _call(base, token, "GET", "/hotseat/desk")[1]["switches"][0]["state"] == "failed"
    assert fake.calls().count("switch ai12 --yes --json") == 1


def test_switch_refuses_when_hotseat_cannot_say_which_account_is_active(desk):
    base, token, fake, _turns, _ctx = desk
    fake.set(list_fails=True)

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})

    assert status == 503 and body["ok"] is False and body["error"] == "hotseat_unavailable"
    assert body["message"]
    assert not [c for c in fake.calls() if c.startswith("switch")]
    assert _last_switch("claude") is None


def test_switch_answers_when_the_switch_is_done_and_the_desk_then_shows_the_truth(desk):
    base, token, fake, _turns, _ctx = desk
    before = _live_read(base, token)
    assert before["refreshed"] is True
    fake.set(tui_sleep=2.0)
    started = time.monotonic()

    status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})

    assert time.monotonic() - started < 1.8, "the answer must not wait for the live read"
    assert status == 200
    assert body == {"ok": True, "switched": True, "from": "ai13", "provider": "claude", "alias": "ai12"}, (
        "no desk in the answer: none has been read since the switch (issue 263, point 4)")
    _, now = _call(base, token, "GET", "/hotseat/desk")
    assert now["refreshed"] is False and now["observed_at"] == before["observed_at"]
    assert [a["alias"] for a in now["accounts"] if a["provider"] == "claude" and a["active"]] == ["ai12"], (
        "the stored desk already names the account Hotseat confirmed")
    assert now["switches"][0]["state"] == "switched"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        _, later = _call(base, token, "GET", "/hotseat/desk")
        if not later["refreshing"] and later["observed_at"] != before["observed_at"]:
            break
        time.sleep(0.1)
    assert later["refreshing"] is False and later["observed_at"] != before["observed_at"]
    assert [a["alias"] for a in later["accounts"] if a["provider"] == "claude" and a["active"]] == ["ai12"]
    assert later["switches"] == [{**later["switches"][0], "provider": "claude", "alias": "ai12",
                                  "from": "ai13", "state": "switched"}]


def test_no_switch_answer_carries_a_desk(desk):
    base, token, fake, _turns, _ctx = desk
    noop = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai13"})
    assert noop == (200, {"ok": True, "switched": False, "reason": "already_active", "from": "ai13",
                          "provider": "claude", "alias": "ai13",
                          "message": "ai13 is already the account in use."})
    fake.set(list_fails=True)
    unreadable = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})
    assert unreadable[0] == 503 and set(unreadable[1]) == {"ok", "error", "provider", "alias", "message"}
    fake.set(list_fails=False, switch_fails=True)
    failed = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"})
    assert failed[0] == 409 and set(failed[1]) == {"ok", "error", "from", "provider", "alias", "message"}


def test_the_desk_says_whether_the_live_read_it_was_asked_for_happened(desk):
    base, token, fake, _turns, _ctx = desk
    first = _live_read(base, token)
    assert (first["refreshed"], first["refresh_throttled"]) == (True, False)
    plain = _call(base, token, "GET", "/hotseat/desk")[1]
    assert (plain["refreshed"], plain["refresh_throttled"]) == (False, False)
    held = _call(base, token, "GET", "/hotseat/desk?refresh=1")[1]
    assert (held["refreshed"], held["refresh_throttled"]) == (False, True)

    fake.set(list_fails=False, tui_fails=True)
    _ctx.hotseat_desk.refresh(force=True)
    failed_state = _call(base, token, "GET", "/hotseat/desk")[1]
    assert failed_state["error"] == "cli_failed" and failed_state["observed_at"] == first["observed_at"]


def test_switch_never_passes_an_option_shaped_alias_to_hotseat(desk):
    base, token, fake, _turns, _ctx = desk

    for alias in ("--help", "-x", "a b", "a;rm", "../x", ""):
        status, body = _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": alias})
        assert status == 400 and body["error"] == "invalid_alias", (alias, body)

    assert fake.calls() == []


def test_the_host_builds_one_desk_and_the_automatic_switcher_takes_its_lock(desk):
    _base, _token, fake, _turns, ctx = desk
    from lib import hotseat_desk
    assert isinstance(ctx.hotseat_desk, hotseat_desk.HotseatDesk)
    switcher = fake.server.workers.get("autonomy-janitors")
    for provider in ("claude", "codex"):
        assert switcher.hotseat_switch_lock(provider) is ctx.hotseat_desk.switch_lock(provider)
    assert switcher.hotseat_observe == ctx.hotseat_desk.absorb_switcher_read
    assert switcher.hotseat_switched == ctx.hotseat_desk.mark_switched
    assert ctx.hotseat_desk.switch_lock("claude") is not ctx.hotseat_desk.switch_lock("codex")


# --- live reads -----------------------------------------------------------

def test_live_reads_keep_a_minimum_interval_and_say_when_the_next_is_allowed(desk):
    base, token, fake, _turns, _ctx = desk
    from lib import hotseat_desk

    bodies = [_call(base, token, "GET", "/hotseat/desk?refresh=1")[1] for _ in range(5)]

    assert sorted(fake.calls()) == ["bridge work", "list --json", "tui --snapshot"]
    assert bodies[0]["refresh_throttled"] is False
    assert [b["refresh_throttled"] for b in bodies[1:]] == [True] * 4
    for body in bodies:
        assert body["next_refresh_at"] == bodies[0]["next_refresh_at"]
        assert body["observed_at"] == bodies[0]["observed_at"]
    assert (bodies[0]["next_refresh_at"] - bodies[0]["observed_at"]
            == hotseat_desk.MIN_REFRESH_INTERVAL_MS)
    assert len(bodies[4]["accounts"]) == 4


def test_only_a_full_device_reaches_the_desk(desk):
    base, token, _fake, _turns, _ctx = desk
    from lib import device_pairing
    limited = device_pairing.exchange(device_pairing.issue(scope="limited")["code"])["token"]
    routes = (("GET", "/hotseat/desk", None), ("POST", "/hotseat/switch", {"provider": "x", "alias": "y"}),
              ("POST", "/hotseat/resume", {"id": "missing"}))

    assert [_call(base, limited, m, p, b)[0] for m, p, b in routes] == [403, 403, 403]
    assert [_call(base, "wrong", m, p, b)[0] for m, p, b in routes] == [401, 401, 401]
    assert [_call(base, token, m, p, b)[0] for m, p, b in routes] == [200, 400, 404]


def test_with_the_desk_switched_off_the_routes_are_gone_and_the_feature_is_not_advertised(desk, monkeypatch):
    base, token, fake, turns, _ctx = desk
    import dataclasses
    from lib import config
    fake.stop("koko")
    _live_read(base, token)
    ran = len(fake.calls())
    _, info = _call(base, token, "GET", "/server-info")
    assert {"hotseat_desk", "hotseat_desk_actions"} <= set(info["capabilities"]["features"])
    current = config.load()
    monkeypatch.setattr(config, "load", lambda *a, **k: dataclasses.replace(current, hotseat_desk_enabled=False))

    answers = [_call(base, token, "GET", "/hotseat/desk?refresh=1"),
               _call(base, token, "POST", "/hotseat/switch", {"provider": "claude", "alias": "ai12"}),
               _call(base, token, "POST", "/hotseat/resume", {"id": "koko"})]
    _, info = _call(base, token, "GET", "/server-info")

    assert [status for status, _ in answers] == [404, 404, 404]
    assert all(body == {"raw": "not found"} for _, body in answers), "exactly what an unknown route answers"
    assert len(fake.calls()) == ran and turns == []
    features = set(info["capabilities"]["features"])
    assert not {"hotseat_desk", "hotseat_desk_actions"} & features and "accounts_hotseat" in features
    assert not {"hotseat_desk", "hotseat_desk_actions"} & set(info["contract"]["features"])
    assert _call(base, token, "GET", "/hotseat/status")[0] == 200, "the read-only view the app falls back to"
    assert _call(base, token, "GET", "/hotseat/nothing-here")[1] == {"raw": "not found"}
