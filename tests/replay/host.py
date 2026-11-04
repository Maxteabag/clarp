#!/usr/bin/env python3
"""Replay host: one build's server code behind the harness's own fakes.

The harness tree owns this launcher and the fake provider; ``--build`` names
the checkout whose ``server/`` and ``static/`` are under test. Base and head
therefore run the same launcher, fakes and corpus, and only the server code
differs. Everything the Host writes stays under ``--state-dir``.

capture.py starts this with a minimal environment (no inherited provider homes,
sockets or proxies); ``isolate`` then points every home, data, config, temp and
provider directory into the state dir.

Adapted from tests/qa/host.py (the deterministic turn lane), without its route
allow-list: the replay corpus is meant to reach every route it names.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import threading
import time

HARNESS = Path(__file__).resolve().parents[2]
# tests/qa/fake_codex.py behind the harness's tool-item proxy (fake_provider.py).
FAKE_PROVIDER = HARNESS / 'tests/replay/fake_provider.py'
TOKEN = 'replay-host-token'
SESSIONS = ('rachel', 'mike')
# A non-steerable backend for preemption scenarios: claude, served by fake_claude.py.
CLAUDE_SESSIONS = ('claire',)
FAKE_CLAUDE = HARNESS / 'tests/replay/fake_claude.py'


# Every home, data, config, temp and provider directory, inside the state dir.
STATE_DIRS = {
        'HOME': 'home',
        'CLAUDE_PWA_DB': 'state.sqlite', 'CLAUDE_PWA_LOG_DIR': 'logs',
        'CLAUDE_PWA_CONFIG': 'config.toml', 'CLARP_CONFIG_DIR': 'config',
        'CLARP_SHARE_DIR': 'share', 'CLARP_DATA_DIR': 'share', 'CLARP_CACHE_DIR': 'cache',
        'CLARP_TELEMETRY_DB': 'telemetry.sqlite',
        'XDG_CONFIG_HOME': 'xdg-config', 'XDG_DATA_HOME': 'xdg-data',
        'XDG_CACHE_HOME': 'xdg-cache', 'XDG_STATE_HOME': 'xdg-state',
        'CLARP_QA_PROVIDER_ROOT': 'provider', 'CODEX_HOME': 'provider',
        'CLAUDE_CONFIG_DIR': 'home/.claude', 'TMPDIR': 'tmp',
        'CLARP_MEDIA_DIR': 'media', 'CLARP_UPLOADS_DIR': 'uploads',
        'CLARP_WORKSPACE_ROOT': 'workspace',
}


def isolate(root: Path) -> None:
    for key, suffix in STATE_DIRS.items():
        os.environ[key] = str(root / suffix)
    for sub in ('home', 'tmp'):
        (root / sub).mkdir(exist_ok=True)
    (root / 'provider').mkdir(exist_ok=True)
    scenario_config = root / 'scenario-config.toml'
    (root / 'config.toml').write_text(scenario_config.read_text() if scenario_config.is_file()
                                      else '[tts]\nprovider = "none"\n[network]\nadvertise_lan = false\n')
    for key in tuple(os.environ):
        if key.endswith('_API_KEY') or key in (
                'ELEVEN_API_KEY', 'CLAUDE_PWA_TTS_PROVIDER', 'CLAUDE_PWA_SESSION',
                'CLARP_PROVIDER_TURN', 'CLARP_BACKGROUND_ORIGIN', 'CLARP_CODE_ROOT'):
            os.environ.pop(key, None)
    os.environ['CLARP_HEARTBEATS_DISABLED'] = '1'
    os.environ.setdefault('CLARP_QA_STREAM_DELAY', '0.02')


def loopback_only() -> None:
    """No connection leaves the machine: a scenario that routes TTS or STT to a
    cloud provider must fail like a missing key, never reach the provider."""
    import ipaddress
    import socket

    def allowed(address) -> bool:
        if not isinstance(address, tuple):
            return True  # AF_UNIX
        host = address[0]
        if host in ('localhost', ''):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        if not allowed(address):
            raise ConnectionRefusedError('replay host: network access refused')
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(address):
            return 111
        return real_connect_ex(self, address)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, 'localhost', '127.0.0.1', '::1'):
            raise socket.gaierror('replay host: name resolution refused')
        return real_getaddrinfo(host, *args, **kwargs)
    socket.getaddrinfo = getaddrinfo


# 100 silent MPEG-1 Layer III frames (128 kbit/s, 44.1 kHz, 417 bytes each): a
# synthetic stream that splits into three of the adapter's 16 KiB reads.
SYNTHETIC_MP3 = (b'\xff\xfb\x90\x00' + bytes(413)) * 100


def synthetic_cartesia() -> None:
    """The real Cartesia adapter (lib.cartesia_tts.synthesize) streams a fixed
    synthetic response instead of calling the vendor: only its transport is
    replaced, and only for its own URL. Anything else still meets the loopback
    guard. Used by the scenarios that route TTS to Cartesia in their config."""
    import io
    import urllib.request
    from lib import cartesia_tts
    real = urllib.request.urlopen

    pace_file = Path(os.environ['CLARP_QA_PROVIDER_ROOT']) / 'cartesia-pace'

    class Response(io.BytesIO):
        """The synthetic body. With provider/cartesia-pace (seconds), every read
        waits that long first, so a client can subscribe to the clip's live
        stream while it is still being produced."""
        status = 200

        def read(self, size=-1):
            if pace_file.is_file():
                time.sleep(float(pace_file.read_text().strip() or 0))
            return super().read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    def urlopen(request, *args, **kwargs):
        url = getattr(request, 'full_url', request)
        if url == cartesia_tts._URL:
            return Response(SYNTHETIC_MP3)
        return real(request, *args, **kwargs)
    cartesia_tts.urllib.request.urlopen = urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    build, root = args.build.resolve(), args.state_dir.resolve()
    marker = root / '.clarp-replay-owned'
    if root.is_symlink() or not marker.is_file():
        parser.error('state directory was not prepared by the replay harness')
    isolate(root)
    loopback_only()
    sys.path.insert(0, str(build / 'server'))

    from lib import host_generation
    backups = host_generation.default_backups_dir()
    if not backups.resolve().is_relative_to(root):
        raise SystemExit(f'replay instance generation escaped the state dir: {backups}')
    if not host_generation.ensure_instance_generation():
        raise SystemExit('replay host could not provision its instance generation')
    from lib import agents, backends, codex_transcript, db, telemetry
    codex_transcript._codex_home = lambda: root / 'provider'
    for path in (db.DB_PATH, telemetry.TELEMETRY_PATH):
        if not path.resolve().is_relative_to(root):
            raise SystemExit(f'replay database escaped the state dir: {path}')
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.tts_engine import FakeTTSEngine
    # The fake runs under this interpreter, whatever python3 the minimal PATH finds.
    fake = root / 'provider' / 'fake-codex'
    fake.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_PROVIDER))} "$@"\n')
    fake.chmod(0o755)
    backends.by_id('codex').required_binary = str(fake)
    # `claude` resolves on PATH (backend/claude.py executable()): a shim in the state
    # dir, first on the Host's PATH, runs the fake under this interpreter.
    shims = root / 'provider' / 'bin'
    shims.mkdir(exist_ok=True)
    claude_shim = shims / 'claude'
    claude_shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_CLAUDE))} "$@"\n')
    claude_shim.chmod(0o755)
    os.environ['PATH'] = f"{shims}{os.pathsep}{os.environ.get('PATH', '')}" 
    from lib import provider_capabilities
    # Only codex resolves for the capability probes: the cache warm-up runs every
    # resolvable CLI for the model catalogue, and a claude fake started by it lands
    # in the capture or not depending on teardown timing. claire's turns run the
    # `claude` shim through PATH, which does not depend on this probe.
    provider_capabilities._resolve_executable = (
        lambda provider_id: str(fake) if provider_id == 'codex' else None)
    provider_spawn = backends.spawn_turn

    def deterministic_spawn(backend, **kwargs):
        if backends.normalize(backend) not in ('codex', 'claude'):
            raise RuntimeError('replay host supports only the deterministic Codex and Claude fakes')
        if not Path(kwargs['cwd']).resolve().is_relative_to(root):
            raise RuntimeError('replay provider workspace escaped the state dir')
        return provider_spawn(backend, **kwargs)
    backends.spawn_turn = deterministic_spawn
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    for session, backend in [*((s, 'codex') for s in SESSIONS), *((s, 'claude') for s in CLAUDE_SESSIONS)]:
        if not agents.get_by_session(session):
            aid = agents.create_agent(persona=session.title(), session=session,
                                      voice_id='', cwd=str(workspace), backend=backend)
            agents.start_runtime(aid, session)
    audio = root / 'audio'
    audio.mkdir(exist_ok=True)
    ctx = ServerContext(root=root, static=build / 'static', audio_dir=audio,
                        agents_path=root / 'agents.json', default_session=SESSIONS[0],
                        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
                        stt=StubSTT(text='replay voice prompt', ends_terminal=True),
                        auth_token=TOKEN, roster_names=tuple(s.title() for s in (*SESSIONS, *CLAUDE_SESSIONS)))
    spec = importlib.util.spec_from_file_location('replay_server', build / 'server/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    synthetic_cartesia()
    # Never a real interactive CLI: a scenario only sends plain GETs to /terminal/
    # (answered before any spawn), and this makes an accidental upgrade fail loudly.
    from lib import terminal_ws

    def no_pty(*_args, **_kwargs):
        raise RuntimeError('the replay host never spawns a terminal')
    terminal_ws._serve_pty = no_pty
    server = module.build_server(ctx, 0, bind_addr='127.0.0.1')
    metadata = {'url': f'http://127.0.0.1:{server.server_address[1]}', 'pid': os.getpid()}
    pending = root / f'.host.json.{os.getpid()}.tmp'
    pending.write_text(json.dumps(metadata) + '\n')
    os.replace(pending, root / 'host.json')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close_local()


if __name__ == '__main__':
    main()
