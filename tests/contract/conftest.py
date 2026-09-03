"""Shared in-process core server for the contract tests.

Same pattern as tests/integration/test_clip_ack_contract.py: a
FakeClaude-seeded DB plus build_server with fakes. The turn itself is
driven by each test AFTER boot so broadcasts land on live subscribers.
"""
from __future__ import annotations

import pathlib
import socket
import sys
import threading
import time
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))
sys.path.insert(0, str(REPO / "tests" / "integration"))

from fake_claude import FakeClaude  # noqa: E402
from lib.audio_stream import AudioStream  # noqa: E402
from lib.context import ServerContext, StubSTT  # noqa: E402
from lib.tts_engine import FakeTTSEngine  # noqa: E402

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "claude_pwa_server_for_contract", REPO / "server" / "server.py")
assert _spec and _spec.loader
_srv_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_srv_mod)
build_server = _srv_mod.build_server


@pytest.fixture
def core_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    agent = FakeClaude(home=home, session="rachel",
                       persona="Rachel", voice_id="v_r")
    agent.__enter__()
    from lib import db
    db.reset_for_tests(home / ".local/share/clarp/state.sqlite")
    # Backend codex: the INV2 ghost-session reconcile (a bound Claude
    # session must have a transcript file) only applies to claude
    # backends. The core protocol is backend-agnostic.
    db.conn().execute("UPDATE agents SET backend = 'codex' WHERE session = 'rachel'")
    db.conn().commit()

    static = REPO / "static"
    audio = home / ".cache" / "clarp" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    ctx = ServerContext(
        root=home, static=static, audio_dir=audio,
        agents_path=tmp_path / "agents.json",
        default_session="rachel",
        tts=FakeTTSEngine(audio),
        stream=AudioStream(audio),
        stt=StubSTT(text="hello from stub", ends_terminal=True),
        roster_names=("Rachel",),
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = build_server(ctx, port, bind_addr="127.0.0.1")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/agents/snapshot", timeout=0.2).read()
            break
        except Exception:
            time.sleep(0.05)
    try:
        yield {"base": base, "port": port, "ctx": ctx,
               "home": home, "audio": audio, "agent": agent}
    finally:
        srv.shutdown()
        srv.server_close()
