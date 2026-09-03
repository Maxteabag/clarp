"""The reference client must stay small and actually work.

Runs contract/reference-client/clarp-client.mjs through one turn against
the harness: an existing transcript, a stubbed dispatch that files the
durable user row (no backend spawn), and a broadcast audio clip. Asserts
delivery by identity, transcript equality with /log, and the ack row.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
CLIENT = REPO / "contract" / "reference-client" / "clarp-client.mjs"
sys.path.insert(0, str(REPO / "server"))

from lib import agents as agents_db  # noqa: E402
from lib import message_store  # noqa: E402


def _agent_id() -> str:
    from lib.agents import conn
    row = conn().execute(
        "SELECT agent_id FROM agents WHERE session = 'rachel'").fetchone()
    return str(row["agent_id"])


def _iso_now() -> str:
    # Transcript timestamps must bracket the server-filed user row (which
    # carries real now): m0 strictly before the run, m1 strictly after the
    # send lands, or display order will not match insertion order.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_reference_client_under_line_budget():
    lines = CLIENT.read_text().splitlines()
    assert len(lines) < 250, f"reference client grew to {len(lines)} lines"


def test_reference_client_full_turn(core_server):
    base = core_server["base"]
    agent_id = _agent_id()
    agents_db.start_runtime(agent_id, "codex")
    agents_db.bind_backend_session(agent_id, "backend-1")
    first = [
        {"role": "assistant", "text": "welcome",
         "timestamp": _iso_now()},
    ]
    agents_db.store_transcript_turns(
        agent_id=agent_id, backend_session_id="backend-1",
        source_file="/tmp/refclient.jsonl", turns=first,
    )

    # A clip waiting on the replay backlog: the client connects after the
    # broadcast and must still pick it up, fetch it, and ack it.
    audio = core_server["audio"]
    clip_path = audio / "1700000000000__rachel.mp3"
    clip_path.write_bytes(b"\xff\xfb" * 100)
    from lib.agents import record_clip
    record_clip(agent_id=agent_id, path=f"/audio/{clip_path.name}",
                voice_id="v_r", trace_id="ref-trace",
                byte_count=clip_path.stat().st_size)
    from lib.agents import conn
    clip_id = int(conn().execute(
        "SELECT clip_id FROM clips WHERE path = ?",
        (f"/audio/{clip_path.name}",)).fetchone()["clip_id"])
    core_server["ctx"].stream.broadcast(
        {"type": "audio", "clip_id": clip_id,
         "url": f"/audio/{clip_path.name}",
         "session": "rachel", "agent_id": agent_id,
         "persona": "Rachel", "trace_id": "ref-trace",
         "streamable": False})

    # Stub the spawn: file the durable user row exactly like dispatch does,
    # return without launching a backend.
    from lib.turn_dispatch import DispatchResult, TurnDispatchService
    real_dispatch = TurnDispatchService.dispatch

    def fake_dispatch(self, *, text, requested_session, trace_id,
                      client_msg_id="", **kwargs):
        row = agents_db.get_by_session(requested_session)
        message_store.record_user_message(
            agent_id=row["agent_id"], backend_session_id="backend-1",
            client_msg_id=client_msg_id or trace_id, text=text,
        )
        return DispatchResult(session=requested_session, backend="codex")

    TurnDispatchService.dispatch = fake_dispatch  # type: ignore[method-assign]
    try:
        proc = subprocess.Popen(
            ["node", str(CLIENT), f"--base={base}",
             "--session=rachel", "--text=reference question"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            # Wait until the client's send landed, then grow the transcript
            # mid-run: the client's poll loop must pick the reply up.
            deadline = time.time() + 15
            while time.time() < deadline:
                n = conn().execute(
                    "SELECT COUNT(*) AS n FROM messages "
                    "WHERE agent_id = ? AND message_id LIKE 'u-%'",
                    (agent_id,)).fetchone()["n"]
                if int(n) >= 1:
                    break
                time.sleep(0.2)
            agents_db.store_transcript_turns(
                agent_id=agent_id, backend_session_id="backend-1",
                source_file="/tmp/refclient.jsonl",
                turns=[*first, {"role": "assistant", "text": "reference answer",
                                "timestamp": _iso_now()}],
            )
            out = proc.communicate(timeout=60)[0]
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    finally:
        TurnDispatchService.dispatch = real_dispatch  # type: ignore[method-assign]

    assert proc.returncode == 0, f"client exited {proc.returncode}: {out[-2000:]}"
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["delivered"] is True, summary
    assert summary["acked"] == [clip_id], summary
    assert summary["turns"] == summary["logIds"], summary
    assert len(summary["turns"]) >= 3, summary  # welcome + u- row + answer

    with urllib.request.urlopen(
            base + "/clips/recoverable?session=rachel", timeout=10) as r:
        recoverable = json.loads(r.read())
    assert recoverable["events"] == [], (
        "fully acked clip must not be recoverable")
