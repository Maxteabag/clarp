"""Core contract conformance: every core endpoint response and SSE event,
validated against contract/schemas, on the real server.

Boots build_server in-process against a FakeClaude-seeded DB (same pattern
as tests/integration/test_clip_ack_contract.py), drives a turn with the
existing fake Claude AFTER boot so the state watcher broadcasts, and
checks the negative paths the prose promises (missing conversation,
replace_required after a transcript rebuild, conversation change after a
re-bind, delta paging, Last-Event-ID replay).

Shape disagreements fail here. Fix the server or the prose, not the schema.
"""
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

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = REPO / "contract" / "schemas"
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO / "tests" / "integration"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fake_claude import FakeClaude  # noqa: E402
from lib import agents as agents_db  # noqa: E402
from lib import message_store  # noqa: E402
from lib.transcript_log import parse_turns  # noqa: E402

from schema_check import validate  # noqa: E402  (tests/contract dir, see sys.path above)


def _load(name: str) -> dict:
    return json.loads((CONTRACT / name).read_text())


SCHEMAS = {
    "server-info": _load("server-info.json"),
    "snapshot": _load("agents-snapshot.json"),
    "log": _load("log.json"),
    "send": _load("send.json"),
    "stop": _load("stop.json"),
    "select": _load("select.json"),
    "clips-ack": _load("clips-ack.json"),
    "transcribe": _load("transcribe.json"),
    "recoverable": _load("clips-recoverable.json"),
}
SSE = _load("sse.json")
SSE_DEFS = SSE["$defs"]
TYPE_TO_DEF = {
    "transcript-updated": "transcript-updated",
    "agent-state": "agent-state",
    "agent-activity": "agent-activity",
    "agent-roster": "agent-roster",
    "agent-focus": "agent-focus",
    "queue-updated": "queue-updated",
    "user-notification": "user-notification",
    "audio": "audio",
    "tts-error": "tts-error",
    "server-version": "server-version",
    "remote-action": "remote-action",
}


def check(name: str, body: dict) -> dict:
    validate(body, SCHEMAS[name], SCHEMAS[name])
    return body


def check_event(ev: dict) -> None:
    """Validate one SSE data payload. Unknown types are ignored, exactly
    like a client must ignore them (additive-only policy)."""
    assert isinstance(ev, dict) and "type" in ev, f"event without type: {ev!r:.200}"
    name = TYPE_TO_DEF.get(ev["type"])
    if name is None:
        return
    validate(ev, SSE_DEFS[name], SSE)


# ---------- HTTP helpers -------------------------------------------------


def _get(base: str, path: str) -> tuple[int, dict]:
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, json.loads(r.read() or b"{}")


def _post(base: str, path: str, body: object,
          raw: bool = False) -> tuple[int, bytes]:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_json(base: str, path: str, body: object) -> tuple[int, dict]:
    status, raw = _post(base, path, body)
    try:
        return status, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return status, {"_raw": raw.decode("utf-8", "replace")}


# ---------- SSE collector -------------------------------------------------


class Collector(threading.Thread):
    """Reads an SSE stream on a raw socket until closed. Parses id:/data:
    blocks; :ping and :connected comments are dropped."""

    def __init__(self, port: int, last_event_id: int | None = None):
        super().__init__(daemon=True)
        self.port = port
        self.last_event_id = last_event_id
        self.events: list[tuple[int | None, dict]] = []
        self._stop = threading.Event()

    def run(self) -> None:
        head = ("GET /events HTTP/1.1\r\nHost: x\r\n"
                "Accept: text/event-stream\r\nConnection: close\r\n")
        if self.last_event_id is not None:
            head += f"Last-Event-ID: {self.last_event_id}\r\n"
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.settimeout(0.5)
        try:
            s.sendall((head + "\r\n").encode())
            buf = b""
            # Skip HTTP response headers.
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    return
                buf += chunk
            buf = buf.split(b"\r\n\r\n", 1)[1]
            while not self._stop.is_set():
                try:
                    chunk = s.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                if not chunk:
                    return
                buf += chunk
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    self._parse(block.decode("utf-8", "replace"))
        except OSError:
            return
        finally:
            try:
                s.close()
            except OSError:
                pass

    def _parse(self, block: str) -> None:
        eid: int | None = None
        payload: dict | None = None
        for line in block.splitlines():
            if line.startswith("id:"):
                try:
                    eid = int(line[3:].strip())
                except ValueError:
                    eid = None
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    payload = None
        if payload is not None:
            self.events.append((eid, payload))

    def kinds(self) -> set[str]:
        return {str(ev.get("type")) for _, ev in self.events
                if isinstance(ev, dict)}

    def close(self) -> None:
        self._stop.set()
        self.join(timeout=5)


def _wait_for(collector: Collector, kinds: set[str], timeout: float = 15.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if kinds <= collector.kinds():
            return
        time.sleep(0.1)
    missing = kinds - collector.kinds()
    raise AssertionError(f"timed out waiting for SSE kinds {sorted(missing)}; "
                         f"got {sorted(collector.kinds())}")


# ---------- helpers -------------------------------------------------------
# core_server lives in tests/contract/conftest.py (shared with the
# reference-client test).


def _agent_id() -> str:
    from lib.agents import conn
    row = conn().execute(
        "SELECT agent_id FROM agents WHERE session = 'rachel'").fetchone()
    return str(row["agent_id"])


def _drive_turn(agent: FakeClaude, backend_session_id: str = "backend-1") -> None:
    """A full turn through the real paths: hooks write state_log, the real
    transcript parser + store write messages, runtime is bound so the read
    model has a live conversation."""
    agent.user_prompt("hello there", source="pwa")
    agent.assistant_text("Hi! I'm Rachel and I am ready.")
    r = agent.stop()
    assert r.returncode == 0, r.stderr
    agent_id = _agent_id()
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, backend_session_id)
    turns = parse_turns(agent.transcript)
    assert turns, "fake transcript parsed to zero turns"
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id=backend_session_id,
        source_file=str(agent.transcript), turns=turns,
    )


# ---------- tests ---------------------------------------------------------


def test_server_info_shape(core_server):
    status, body = _get(core_server["base"], "/server-info")
    assert status == 200
    check("server-info", body)
    assert body["min_app_version"] and body["clarp_version"]
    assert isinstance(body["capabilities"]["features"], list)


def test_snapshot_and_log_shapes(core_server):
    base, agent = core_server["base"], core_server["agent"]
    status, missing_body = _get(base, "/log?session=rachel&limit=5")
    assert status == 200
    check("log", missing_body)
    assert missing_body["missing"] is True
    assert missing_body["turns"] == []

    _drive_turn(agent)
    status, snap = _get(base, "/agents/snapshot")
    assert status == 200
    check("snapshot", snap)
    row = next(a for a in snap["agents"] if a["session"] == "rachel")
    assert row["conversation_id"] == "backend-1"
    assert row["head_revision"] > 0

    status, tail = _get(base, "/log?session=rachel&limit=100")
    assert status == 200
    check("log", tail)
    assert tail["missing"] is False
    assert tail["conversation_id"] == "backend-1"
    assert len(tail["turns"]) >= 2
    texts = [t["text"] for t in tail["turns"]]
    assert any("hello there" in t for t in texts)
    assert any("Rachel" in t for t in texts)

    cursor = tail["latest_revision"]
    status, delta = _get(base, f"/log?session=rachel&after_revision={cursor}&limit=100")
    assert status == 200
    check("log", delta)
    assert delta["turns"] == []
    assert delta["has_more"] is False

    oldest = tail["turns"][0]["id"]
    status, page = _get(base, f"/log?session=rachel&limit=1&before={oldest}")
    assert status == 200
    check("log", page)


def test_delta_paging_holds_cursor_back(core_server):
    base = core_server["base"]
    agent_id = _agent_id()
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, "backend-1")

    def _store(texts):
        agents_db.store_transcript_turns(
            agent_id=agent_id, backend_session_id="backend-1",
            source_file="/tmp/paging.jsonl",
            turns=[{"role": "user" if i % 2 == 0 else "assistant",
                    "text": text,
                    "timestamp": f"2026-01-01T00:00:{i:02d}Z"}
                   for i, text in enumerate(texts)],
        )

    _store([f"message {i}" for i in range(2)])
    _, tail = _get(base, "/log?session=rachel&limit=100")
    cursor = tail["latest_revision"]
    assert len(tail["turns"]) == 2
    # A backlog lands after the cursor; a small limit must page through it
    # oldest-first without ever skipping ahead. Re-storing upserts the first
    # two rows in place, so only the four new/changed rows follow the cursor.
    _store([f"message {i}" for i in range(6)])
    seen: list[str] = []
    pages = 0
    for _ in range(4):
        status, page = _get(
            base, f"/log?session=rachel&after_revision={cursor}&limit=2")
        assert status == 200
        check("log", page)
        seen.extend(t["text"] for t in page["turns"])
        cursor = page["latest_revision"]
        pages += 1
        if not page["has_more"]:
            break
    assert pages == 2, "four changed rows at limit=2 must take two pages"
    assert seen == [f"message {i}" for i in range(2, 6)], seen


def test_rebuild_requires_replace_and_rebind_changes_conversation(core_server):
    base, agent = core_server["base"], core_server["agent"]
    _drive_turn(agent)
    agent_id = _agent_id()
    _, tail = _get(base, "/log?session=rachel&limit=100")
    cursor = tail["latest_revision"]
    assert cursor > 0

    # Transcript rewrite (compaction/rebuild): re-store truncated turns the
    # way the import path does, then the old cursor must get replace_required.
    short = parse_turns(agent.transcript)[:1]
    assert short
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file=str(agent.transcript), turns=short,
    )
    _, delta = _get(base, f"/log?session=rachel&after_revision={cursor}&limit=100")
    check("log", delta)
    assert delta.get("replace_required") is True

    # Fork/relaunch: a new backend session is a new conversation.
    agents_db.bind_backend_session(agent_id, "backend-2")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-2",
        source_file="/tmp/other.jsonl",
        turns=[{"role": "user", "text": "fresh start",
                "timestamp": "2026-02-01T00:00:00Z"}],
    )
    _, tail2 = _get(base, "/log?session=rachel&limit=100")
    check("log", tail2)
    assert tail2["conversation_id"] == "backend-2"
    assert tail2["conversation_id"] != tail["conversation_id"]


def test_send_accepts_and_delivery_is_identity(core_server):
    base, agent = core_server["base"], core_server["agent"]
    _drive_turn(agent)
    from lib.turn_dispatch import DispatchResult, TurnDispatchService

    calls: list[dict] = []
    real_dispatch = TurnDispatchService.dispatch

    def fake_dispatch(self, *, text, requested_session, trace_id,
                      client_msg_id="", **kwargs):
        row = agents_db.get_by_session(requested_session)
        message_store.record_user_message(
            agent_id=row["agent_id"], backend_session_id="backend-1",
            client_msg_id=client_msg_id or trace_id, text=text,
        )
        calls.append({"text": text, "client_msg_id": client_msg_id})
        return DispatchResult(session=requested_session, backend="codex")

    TurnDispatchService.dispatch = fake_dispatch  # type: ignore[method-assign]
    try:
        status, body = _post_json(base, "/send", {
            "session": "rachel", "text": "second question",
            "client_msg_id": "probe-id-1",
        })
    finally:
        TurnDispatchService.dispatch = real_dispatch  # type: ignore[method-assign]
    assert status == 200, body
    check("send", body)
    assert body["session"] == "rachel" and calls

    # 200 was only acceptance: delivery is the u- id in /log.
    _, tail = _get(base, "/log?session=rachel&limit=100")
    assert "u-probe-id-1" in [t["id"] for t in tail["turns"]]

    # Error paths stay JSON shaped where the server promises JSON.
    status, body = _post_json(base, "/send", {"session": "rachel", "text": "   "})
    assert status == 400
    status, raw = _post(base, "/send", b"{nope")
    assert status == 400
    status, raw = _post(base, "/send", json.dumps("{nope").encode())
    assert status == 400, "a JSON string body must not kill the connection"
    # The guard lives in _read_json, so every JSON handler gets it.
    status, raw = _post(base, "/select", json.dumps("rachel").encode())
    assert status == 400, "/select must reject a non-object JSON body"
    status, raw = _post(base, "/clips/ack", json.dumps(["queued"]).encode())
    assert status == 400, "/clips/ack must reject a non-object JSON body"


def test_stop_select_ack_recoverable_transcribe(core_server):
    base, agent = core_server["base"], core_server["agent"]
    _drive_turn(agent)
    ctx, audio = core_server["ctx"], core_server["audio"]

    status, body = _post_json(base, "/stop", {"session": "rachel"})
    assert status == 200
    check("stop", body)

    status, body = _post_json(base, "/select", {"session": "rachel"})
    assert status == 200
    check("select", body)
    status, body = _post_json(base, "/select", {"session": "no-such-session"})
    assert status == 404

    clip_path = audio / "1700000000000__rachel.mp3"
    clip_path.write_bytes(b"\xff\xfb" * 100)
    from lib.agents import record_clip
    record_clip(agent_id=_agent_id(), path=f"/audio/{clip_path.name}",
                voice_id="v_r", trace_id="contract-trace",
                byte_count=clip_path.stat().st_size)
    from lib.agents import conn
    clip_id = int(conn().execute(
        "SELECT clip_id FROM clips WHERE path = ?",
        (f"/audio/{clip_path.name}",)).fetchone()["clip_id"])
    for ack_status in ("broadcast", "queued", "play-start", "play-ok"):
        status, body = _post_json(base, "/clips/ack", {
            "clip_id": clip_id, "url": f"/audio/{clip_path.name}",
            "status": ack_status,
        })
        assert status == 200, (ack_status, body)
        check("clips-ack", body)
        assert body["updated"] is True
    status, body = _post_json(base, "/clips/ack",
                              {"clip_id": clip_id, "status": "bogus"})
    assert status == 400

    status, body = _get(base, "/clips/recoverable?session=rachel")
    assert status == 200
    check("recoverable", body)

    req = urllib.request.Request(
        base + "/transcribe", data=b"\xff\xfb" * 500, method="POST",
        headers={"Content-Type": "audio/webm",
                 "X-Transcription-ID": "contract-tid-1"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tstatus, tbody = r.status, json.loads(r.read())
    assert tstatus == 200
    check("transcribe", tbody)
    assert tbody["text"] == "hello from stub"
    # Idempotent retry returns the cached result.
    with urllib.request.urlopen(req, timeout=15) as r:
        assert json.loads(r.read())["cached"] is True
    _ = ctx  # ctx used by other tests via fixture


def test_sse_replay_and_event_shapes(core_server):
    base, port = core_server["base"], core_server["port"]
    ctx, agent = core_server["ctx"], core_server["agent"]
    sub = Collector(port)
    sub.start()
    try:
        time.sleep(0.5)  # let the :connected + replay flush land first
        agent.user_prompt("hello there", source="pwa")
        agent.assistant_text("Hi! I'm Rachel and I am ready.")
        assert agent.stop().returncode == 0
        _drive_turn_messages_only(agent)
        _post_json(base, "/stop", {"session": "rachel"})
        _post_json(base, "/select", {"session": "rachel"})
        from lib.agents import record_clip
        clip_path = core_server["audio"] / "1700000000001__rachel.mp3"
        clip_path.write_bytes(b"\xff\xfb" * 100)
        record_clip(agent_id=_agent_id(), path=f"/audio/{clip_path.name}",
                    voice_id="v_r", trace_id="sse-trace",
                    byte_count=clip_path.stat().st_size)
        from lib.agents import conn
        clip_id = int(conn().execute(
            "SELECT clip_id FROM clips WHERE path = ?",
            (f"/audio/{clip_path.name}",)).fetchone()["clip_id"])
        # Server-produced audio broadcast (also flips the clip to BROADCAST).
        ctx.stream.broadcast({"type": "audio", "clip_id": clip_id,
                              "url": f"/audio/{clip_path.name}",
                              "session": "rachel", "agent_id": _agent_id(),
                              "persona": "Rachel", "trace_id": "sse-trace",
                              "streamable": False})
        # remote-action through its real HTTP producer.
        with urllib.request.urlopen(
                base + "/remote-action?action=stop-agent", timeout=10) as r:
            assert r.status == 200
        # Injected events: transcript-updated, user-notification and tts-error
        # need a live backend turn, a classified completion, or a failing TTS
        # provider to occur naturally. These ride the real serialization and
        # replay path with server-shaped content (notification ids are
        # pn-<sha1> strings); only the trigger is faked.
        ctx.stream.broadcast({"type": "transcript-updated", "session": "rachel",
                              "agent_id": _agent_id(),
                              "backend_session_id": "backend-1"})
        ctx.stream.broadcast({"type": "user-notification",
                              "notification_id": "pn-contract0000000007",
                              "session": "rachel",
                              "agent_id": _agent_id(), "persona": "Rachel",
                              "preview": "done", "reason": "turn-complete"})
        ctx.stream.broadcast({"type": "tts-error", "session": "rachel",
                              "message": "quota exceeded"})
        _wait_for(sub, {"agent-state", "queue-updated", "agent-focus",
                        "audio", "transcript-updated", "user-notification",
                        "tts-error", "remote-action", "server-version"})
    finally:
        sub.close()
    assert sub.events, "expected SSE events, got none"
    # The connect preamble carries the bare roster nudge (live events keep
    # flowing after it, so it is not necessarily last on a live stream).
    assert any(ev == {"type": "agent-roster"} for _, ev in sub.events), (
        "connect preamble must include the bare agent-roster nudge")
    for eid, ev in sub.events:
        check_event(ev)

    durable = [(eid, ev) for eid, ev in sub.events if eid is not None]
    assert durable, "expected durable (id-stamped) events"
    first_id = durable[0][0]
    assert first_id is not None
    replay = Collector(port, last_event_id=first_id)
    replay.start()
    try:
        _wait_for(replay, {"audio"}, timeout=15.0)
        time.sleep(1.0)  # let the roster terminator land
    finally:
        replay.close()
        kinds = replay.kinds()
        assert "audio" in kinds, f"replay missed audio; got {sorted(kinds)}"
        assert any(ev == {"type": "agent-roster"} for _, ev in replay.events), (
            "replay must include its bare agent-roster terminator")
    for _, ev in replay.events:
        check_event(ev)


def _drive_turn_messages_only(agent: FakeClaude) -> None:
    """Bind runtime + import transcript without firing hooks twice (the
    SSE test already fired them above for state coverage)."""
    agent_id = _agent_id()
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, "backend-1")
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file=str(agent.transcript),
        turns=parse_turns(agent.transcript),
    )
