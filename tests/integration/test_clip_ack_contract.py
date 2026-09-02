"""Contract test for POST /clips/ack and the clip lifecycle.

Pins the invariant that the client's playback acknowledgements (broadcast
→ queued → play-start → play-ok|fail) correctly advance the clip row's
status columns. Without this test, the ACK contract could rot silently
because the only way you'd notice is by writing a DuckDB query at 2 AM.

Also asserts the failure modes: unknown status → 400, missing clip → no
crash, malformed body → 400.
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

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))
from lib.audio_stream import AudioStream                # noqa: E402
from lib.context import ServerContext, StubSTT          # noqa: E402
from lib.tts_engine import FakeTTSEngine                 # noqa: E402

import importlib.util as _ilu                            # noqa: E402
_spec = _ilu.spec_from_file_location(
    "claude_pwa_server_for_ack", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
_srv_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_srv_mod)
build_server = _srv_mod.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post(url: str, body: dict | str, *, status_ok: tuple[int, ...] = (200,)
          ) -> tuple[int, dict]:
    """POST JSON, return (status, parsed-body). Unlike urllib's default,
    don't raise on non-2xx — we WANT to assert on 4xx behaviour here."""
    raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=raw, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body_bytes = e.read() or b"{}"
        try:
            return e.code, json.loads(body_bytes)
        except json.JSONDecodeError:
            return e.code, {"_raw": body_bytes.decode("utf-8", "replace")}


# ---------- fixtures -----------------------------------------------------


@pytest.fixture
def server_with_clip(tmp_path):
    """Boot the real server, register an agent, drop a fake mp3 with a
    clips row in `synthesized` status. Yields (base_url, clip_id, url)."""
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"; audio.mkdir()
    agents_path = tmp_path / "agents.json"

    from lib.agents import create_agent, record_clip, conn
    from lib.protocol import ClipStatus
    create_agent(persona="Mike", voice_id="V_MIKE",
                 cwd=str(tmp_path), session="claude")
    agent = conn().execute(
        "SELECT agent_id FROM agents WHERE session = 'claude'"
    ).fetchone()
    agent_id = agent["agent_id"]

    # Drop a fake mp3 on disk + DB row, status=synthesized.
    clip_path = audio / "1700000000000__claude.mp3"
    clip_path.write_bytes(b"\xff\xfb" * 100)
    record_clip(agent_id=agent_id, path=f"/audio/{clip_path.name}",
                voice_id="V_MIKE", trace_id="test-trace",
                byte_count=clip_path.stat().st_size)
    # record_clip seeds status=synthesized by default — verify.
    row = conn().execute(
        "SELECT clip_id, status FROM clips WHERE path = ?",
        (f"/audio/{clip_path.name}",),
    ).fetchone()
    clip_id = row["clip_id"]
    assert row["status"] == ClipStatus.SYNTHESIZED

    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio,
        agents_path=agents_path,
        default_session="claude",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="", ends_terminal=False),
        roster_names=("Mike",),
    )
    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    base = f"http://127.0.0.1:{port}"
    # Wait for accept.
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    try:
        yield base, clip_id, f"/audio/{clip_path.name}"
    finally:
        srv.shutdown()
        srv.server_close()


def _clip_row(clip_id: int) -> dict:
    from lib.agents import conn
    row = conn().execute(
        """SELECT status, broadcast_at, queued_at, play_started_at,
                  played_at, error
             FROM clips WHERE clip_id = ?""",
        (clip_id,),
    ).fetchone()
    return dict(row) if row else {}


# ---------- happy-path lifecycle ----------------------------------------


def test_clip_status_advances_broadcast_queued_play_ok(server_with_clip):
    base, clip_id, url = server_with_clip
    from lib.protocol import ClipStatus

    # 1) broadcast → broadcast_at filled
    code, body = _post(base + "/clips/ack",
                       {"clip_id": clip_id, "url": url,
                        "status": ClipStatus.BROADCAST})
    assert (code, body.get("ok"), body.get("updated")) == (200, True, True)
    row = _clip_row(clip_id)
    assert row["status"] == ClipStatus.BROADCAST
    assert row["broadcast_at"] is not None

    # 2) queued → queued_at filled, prior columns preserved
    prior_broadcast_at = row["broadcast_at"]
    _post(base + "/clips/ack",
          {"clip_id": clip_id, "url": url, "status": ClipStatus.QUEUED})
    row = _clip_row(clip_id)
    assert row["status"] == ClipStatus.QUEUED
    assert row["queued_at"] is not None
    assert row["broadcast_at"] == prior_broadcast_at, (
        "broadcast_at must be preserved when advancing to queued"
    )

    # 3) play-start → play_started_at filled
    _post(base + "/clips/ack",
          {"clip_id": clip_id, "url": url, "status": ClipStatus.PLAY_START})
    row = _clip_row(clip_id)
    assert row["status"] == ClipStatus.PLAY_START
    assert row["play_started_at"] is not None

    # 4) play-ok → played_at filled, no error
    _post(base + "/clips/ack",
          {"clip_id": clip_id, "url": url, "status": ClipStatus.PLAY_OK})
    row = _clip_row(clip_id)
    assert row["status"] == ClipStatus.PLAY_OK
    assert row["played_at"] is not None
    assert not row["error"]


def test_play_fail_records_error_text(server_with_clip):
    base, clip_id, url = server_with_clip
    from lib.protocol import ClipStatus

    _post(base + "/clips/ack",
          {"clip_id": clip_id, "url": url,
           "status": ClipStatus.PLAY_FAIL,
           "error": "NotAllowedError: user gesture required"})
    row = _clip_row(clip_id)
    assert row["status"] == ClipStatus.PLAY_FAIL
    assert row["error"] and "NotAllowedError" in row["error"]


def test_lookup_by_url_when_clip_id_omitted(server_with_clip):
    """The client may not always know the clip_id (e.g. it was reconnected
    after the broadcast). The ACK falls back to url-matching."""
    base, clip_id, url = server_with_clip
    from lib.protocol import ClipStatus

    code, body = _post(base + "/clips/ack",
                       {"url": url, "status": ClipStatus.BROADCAST})
    assert code == 200, body
    assert body["updated"] is True
    assert _clip_row(clip_id)["status"] == ClipStatus.BROADCAST


# ---------- failure modes ------------------------------------------------


def test_bogus_status_rejected_with_400(server_with_clip):
    base, clip_id, url = server_with_clip
    code, body = _post(base + "/clips/ack",
                       {"clip_id": clip_id, "url": url,
                        "status": "totally-made-up"})
    assert code == 400
    assert "bad status" in (body.get("error") or "")


def test_unknown_clip_id_returns_200_updated_false(server_with_clip):
    """Missing clip → not a 4xx (clients can race against janitor cleanup).
    The server reports updated=false so callers can detect the no-op."""
    base, _clip_id, url = server_with_clip
    from lib.protocol import ClipStatus
    code, body = _post(base + "/clips/ack",
                       {"clip_id": 999_999, "url": url,
                        "status": ClipStatus.BROADCAST})
    assert code == 200
    assert body["ok"] is True
    assert body["updated"] is False


def test_malformed_json_body_rejected_with_400(server_with_clip):
    base, _clip_id, _url = server_with_clip
    code, body = _post(base + "/clips/ack", "{not-json")
    assert code == 400
    assert "bad json" in (body.get("error") or "")


def test_non_integer_clip_id_rejected_with_400(server_with_clip):
    base, _clip_id, url = server_with_clip
    from lib.protocol import ClipStatus
    code, body = _post(base + "/clips/ack",
                       {"clip_id": "not-a-number", "url": url,
                        "status": ClipStatus.BROADCAST})
    assert code == 400
    assert "bad clip_id" in (body.get("error") or "")
