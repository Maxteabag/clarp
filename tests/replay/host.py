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

HARNESS = Path(__file__).resolve().parents[2]
# tests/qa/fake_codex.py behind the harness's tool-item proxy (fake_provider.py).
FAKE_PROVIDER = HARNESS / 'tests/replay/fake_provider.py'
TOKEN = 'replay-host-token'
SESSIONS = ('rachel', 'mike')


def isolate(root: Path) -> None:
    for key, suffix in {
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
    }.items():
        os.environ[key] = str(root / suffix)
    for sub in ('home', 'tmp'):
        (root / sub).mkdir(exist_ok=True)
    (root / 'provider').mkdir(exist_ok=True)
    (root / 'config.toml').write_text('[tts]\nprovider = "none"\n[network]\nadvertise_lan = false\n')
    for key in tuple(os.environ):
        if key.endswith('_API_KEY') or key in (
                'ELEVEN_API_KEY', 'CLAUDE_PWA_TTS_PROVIDER', 'CLAUDE_PWA_SESSION',
                'CLARP_PROVIDER_TURN', 'CLARP_BACKGROUND_ORIGIN', 'CLARP_CODE_ROOT'):
            os.environ.pop(key, None)
    os.environ['CLARP_HEARTBEATS_DISABLED'] = '1'
    os.environ.setdefault('CLARP_QA_STREAM_DELAY', '0.02')


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
    from lib import provider_capabilities
    provider_capabilities._resolve_executable = (
        lambda provider_id: str(fake) if provider_id == 'codex' else None)
    provider_spawn = backends.spawn_turn

    def deterministic_spawn(backend, **kwargs):
        if backends.normalize(backend) != 'codex':
            raise RuntimeError('replay host supports only the deterministic Codex provider')
        if not Path(kwargs['cwd']).resolve().is_relative_to(root):
            raise RuntimeError('replay provider workspace escaped the state dir')
        return provider_spawn(backend, **kwargs)
    backends.spawn_turn = deterministic_spawn
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    for session in SESSIONS:
        if not agents.get_by_session(session):
            aid = agents.create_agent(persona=session.title(), session=session,
                                      voice_id='', cwd=str(workspace), backend='codex')
            agents.start_runtime(aid, session)
    audio = root / 'audio'
    audio.mkdir(exist_ok=True)
    ctx = ServerContext(root=root, static=build / 'static', audio_dir=audio,
                        agents_path=root / 'agents.json', default_session=SESSIONS[0],
                        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
                        stt=StubSTT(text='replay voice prompt', ends_terminal=True),
                        auth_token=TOKEN, roster_names=tuple(s.title() for s in SESSIONS))
    spec = importlib.util.spec_from_file_location('replay_server', build / 'server/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
