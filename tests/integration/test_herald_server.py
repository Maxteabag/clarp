"""End-to-end HTTP test for the herald + permission flow.

Builds the real ThreadingHTTPServer with FakeTTSEngine, simulates
a non-focused agent emitting audio, then drives /transcribe with the user's
permission utterance and asserts the held buffer flushes.
"""
from contextlib import contextmanager
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
from lib.herald import HeraldManager  # noqa: E402

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("claude_pwa_server", _SERVER_DIR / "server.py")
assert _spec and _spec.loader
server_module = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(server_module)
build_server = server_module.build_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_with_herald(tmp_path):
    static = pathlib.Path(__file__).resolve().parents[2] / "static"
    audio = tmp_path / "audio"; audio.mkdir()
    agents_path = tmp_path / "agents.json"  # legacy field; DB is source of truth
    from lib.agents import create_agent, session_dict
    create_agent(persona="Mike",   voice_id="V_MIKE",
                 cwd=str(tmp_path), session="claude")
    create_agent(persona="Rachel", voice_id="V_RACHEL",
                 cwd=str(tmp_path), session="rachel")

    stream = AudioStream(audio)
    tts = FakeTTSEngine(audio)
    stt = StubSTT(text="", ends_terminal=False)   # we override per-test

    ctx = ServerContext(
        root=tmp_path, static=static, audio_dir=audio,
        agents_path=agents_path,
        default_session="claude",
        tts=tts,
        stream=stream,
        stt=stt,
        roster_names=("Mike", "Rachel"),
    )
    # Wire a HeraldManager and expose it on ctx so handlers can find it.
    ctx.herald = HeraldManager(  # type: ignore[attr-defined]
        stream=stream, tts=tts,
        agents=session_dict,
    )

    port = _free_port()
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.02)
    yield base, ctx, srv
    srv.shutdown()
    srv.server_close()


def _post(url, body: dict | None = None, raw: bytes | None = None,
          content_type: str = "application/json",
          headers: dict[str, str] | None = None):
    data = raw if raw is not None else json.dumps(body or {}).encode()
    request_headers = {"Content-Type": content_type}
    request_headers.update(headers or {})
    req = urllib.request.Request(
        url, data=data, headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=2) as r:
        return r.status, r.read()


def test_settings_handler_rejects_invalid_bool_without_partial_write(
    server_with_herald,
):
    base, _ctx, _srv = server_with_herald
    status, body = _post(base + "/herald/settings", body={
        "disabled": False,
        "speak_if_short_chars": 321,
        "short_reply_bypass_enabled": True,
    })
    assert status == 200
    before = json.loads(body)["settings"]

    with pytest.raises(urllib.error.HTTPError) as error:
        _post(base + "/herald/settings", body={
            "disabled": "false",
            "speak_if_short_chars": 999,
            "short_reply_bypass_enabled": False,
        })
    assert error.value.code == 400
    assert "disabled must be a boolean" in error.value.read().decode()

    with urllib.request.urlopen(base + "/herald/settings", timeout=2) as response:
        assert response.status == 200
        after = json.loads(response.read())["settings"]
    assert after == before


def test_focus_handler_holds_authority_guard_through_herald_update(
    server_with_herald, monkeypatch,
):
    from lib import agents as agents_db

    base, ctx, _srv = server_with_herald
    active = {"count": 0}

    @contextmanager
    def tracked_guard():
        active["count"] += 1
        try:
            yield
        finally:
            active["count"] -= 1

    original_set_focus = ctx.herald.set_focus

    def checked_set_focus(session):
        assert active["count"] > 0
        original_set_focus(session)

    monkeypatch.setattr(agents_db, "focus_guard", tracked_guard)
    monkeypatch.setattr(ctx.herald, "set_focus", checked_set_focus)

    status, _body = _post(base + "/focus", body={"session": "rachel"})

    assert status == 200
    assert agents_db.get_focus_session() == "rachel"


def test_background_agent_clip_emits_herald_and_holds_via_endpoint(server_with_herald):
    """A non-focused agent's clip → server publishes a herald, not the original."""
    base, ctx, _srv = server_with_herald
    ctx.herald.set_focus("claude")
    ctx.herald.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    # Stream's recent buffer is what new SSE clients would replay.
    audio_urls = [e["url"] for e in ctx.stream.recent() if e.get("type") == "audio"]
    assert any("herald" in u or "/audio/" in u for u in audio_urls)
    assert "/audio/r1.mp3" not in audio_urls
    assert "rachel" in ctx.herald.pending_heralds()


def test_transcribe_grant_releases_held_buffer(server_with_herald):
    base, ctx, _srv = server_with_herald
    ctx.herald.set_focus("claude")
    ctx.herald.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    ctx.herald.ingest_clip("rachel", url="/audio/r2.mp3", ts=2)
    assert "rachel" in ctx.herald.pending_heralds()

    # the user speaks "Sure, Rachel" — /transcribe runs intent against pending
    # heralds and releases Rachel's buffer.
    ctx.stt.text = "Sure, Rachel, what is it?"
    status, body = _post(base + "/transcribe", raw=b"\x00" * 100,
                         content_type="audio/webm")
    assert status == 200
    payload = json.loads(body)
    # Regression ("Yes Domi?" got sent to Domi as a message): a grant is a
    # COMMAND that releases the buffer — it must be CONSUMED, not returned for
    # dispatch, or the agent replies to the grant phrase itself.
    assert payload.get("herald_consumed") is True
    assert payload.get("text") == "", "grant phrase must not be dispatched"
    assert "rachel" not in ctx.herald.pending_heralds()
    audio_urls = [e["url"] for e in ctx.stream.recent() if e.get("type") == "audio"]
    assert "/audio/r1.mp3" in audio_urls
    assert "/audio/r2.mp3" in audio_urls


def test_hands_free_transcribe_grant_uses_herald_fallback_when_orchestrator_enabled(
    server_with_herald,
):
    base, ctx, _srv = server_with_herald
    ctx.herald.set_focus("claude")
    ctx.herald.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)

    ctx.stt.text = "Yes, Rachel."
    status, body = _post(
        base + "/transcribe",
        raw=b"\x00" * 100,
        content_type="audio/webm",
        headers={"X-Hands-Free": "true"},
    )

    assert status == 200
    payload = json.loads(body)
    assert payload.get("hands_free") is True
    assert payload.get("herald_consumed") is True
    assert payload.get("orchestrator_skip_herald") is False
    assert payload.get("text") == ""
    assert "rachel" not in ctx.herald.pending_heralds()


def test_transcribe_mere_mention_does_not_release(server_with_herald):
    base, ctx, _srv = server_with_herald
    ctx.herald.set_focus("claude")
    ctx.herald.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    ctx.stt.text = "tell rachel I said hi"
    _post(base + "/transcribe", raw=b"\x00" * 100, content_type="audio/webm")
    assert "rachel" in ctx.herald.pending_heralds()
    audio_urls = [e["url"] for e in ctx.stream.recent() if e.get("type") == "audio"]
    assert "/audio/r1.mp3" not in audio_urls


def test_focus_endpoint_updates_sqlite_focus_for_sticky_routing(server_with_herald):
    base, _ctx, _srv = server_with_herald
    from lib import agents as agents_db

    status, _body = _post(base + "/focus", {"session": "rachel"})

    assert status == 200
    focused = agents_db.get_by_agent_id(agents_db.get_focus())
    assert focused["session"] == "rachel"


def test_send_to_held_agent_releases_them(server_with_herald):
    base, ctx, _srv = server_with_herald
    ctx.herald.set_focus("claude")
    ctx.herald.ingest_clip("rachel", url="/audio/r1.mp3", ts=1)
    # the user addresses Rachel directly with /send → herald should clear.
    _post(base + "/send", {"text": "Rachel, anything to add?", "session": "rachel"})
    assert "rachel" not in ctx.herald.pending_heralds()
    audio_urls = [e["url"] for e in ctx.stream.recent() if e.get("type") == "audio"]
    assert "/audio/r1.mp3" in audio_urls
