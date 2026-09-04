#!/usr/bin/env python3
"""Disposable real HTTP/SSE host using a deterministic provider executable.

Never installed or shipped in the runtime. A marked state directory can be
reused to exercise restart recovery; unmarked nonempty directories are refused.
"""
import argparse
import faulthandler
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]


def main():
    faulthandler.register(signal.SIGUSR1)
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--token', default='qa-host-test')
    args = parser.parse_args()
    root = args.state_dir.resolve()
    marker = root / '.clarp-qa-owned'
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        parser.error('state directory is nonempty and does not belong to the QA harness')
    root.mkdir(parents=True, exist_ok=True)
    marker.touch()
    for key, suffix in {
        'CLAUDE_PWA_DB': 'state.sqlite', 'CLAUDE_PWA_LOG_DIR': 'logs',
        'CLAUDE_PWA_CONFIG': 'config.toml', 'CLARP_CONFIG_DIR': 'config',
        'CLARP_SHARE_DIR': 'share', 'CLARP_DATA_DIR': 'share', 'CLARP_CACHE_DIR': 'cache',
        'CLARP_TELEMETRY_DB': 'telemetry.sqlite',
        'XDG_CONFIG_HOME': 'xdg-config', 'XDG_DATA_HOME': 'xdg-data',
        'XDG_CACHE_HOME': 'xdg-cache', 'XDG_STATE_HOME': 'xdg-state',
        'CLARP_QA_PROVIDER_ROOT': 'provider',
        'CLARP_MEDIA_DIR': 'media', 'CLARP_UPLOADS_DIR': 'uploads',
        'CLARP_WORKSPACE_ROOT': 'workspace',
    }.items():
        os.environ[key] = str(root / suffix)
    (root / 'config.toml').write_text('[tts]\nprovider = "none"\n[network]\nadvertise_lan = false\n')
    for key in tuple(os.environ):
        if key.endswith('_API_KEY') or key in ('ELEVEN_API_KEY', 'CLAUDE_PWA_TTS_PROVIDER'):
            os.environ.pop(key, None)
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, backends, codex_runner, codex_transcript, db, telemetry
    # Storage location is injected; discovery and parsing remain production code.
    codex_transcript._codex_home = lambda: root / 'provider'
    for path in (db.DB_PATH, telemetry.TELEMETRY_PATH):
        if not path.resolve().is_relative_to(root):
            raise SystemExit(f'QA database escaped the disposable root: {path}')
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.tts_engine import FakeTTSEngine
    codex_runner.CODEX_BIN = str(REPO / 'tests/qa/fake_codex.py')
    provider_spawn = backends.spawn_turn
    def deterministic_spawn(backend, **kwargs):
        if backends.normalize(backend) != 'codex':
            raise RuntimeError('QA host supports only the deterministic Codex provider')
        if not Path(kwargs['cwd']).resolve().is_relative_to(root):
            raise RuntimeError('QA provider workspace escaped the disposable root')
        return provider_spawn(backend, **kwargs)
    backends.spawn_turn = deterministic_spawn
    for session in ('rachel', 'mike'):
        if not agents.get_by_session(session):
            aid = agents.create_agent(persona=session.title(), session=session,
                                      voice_id='', cwd=str(root), backend='codex')
            agents.start_runtime(aid, session)
    if any(agent['backend'] != 'codex' for agent in agents.list_agents()):
        raise SystemExit('QA state contains an unsupported provider')
    audio = root / 'audio'
    audio.mkdir(exist_ok=True)
    ctx = ServerContext(root=root, static=REPO / 'static', audio_dir=audio,
                        agents_path=root / 'agents.json', default_session='rachel',
                        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
                        stt=StubSTT(text='QA voice prompt', ends_terminal=True),
                        auth_token=args.token, roster_names=('Rachel', 'Mike'))
    spec = importlib.util.spec_from_file_location('qa_server', REPO / 'server/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The turn lane does not exercise installation, provider login, arbitrary
    # agent creation, file browsing or terminal shells. Keep those capabilities
    # unavailable, even if another CLI is authenticated on the developer's Mac.
    allowed_get = {'/status', '/agents/snapshot', '/artifacts', '/attention',
        '/automation-settings', '/clips/recoverable', '/events', '/log', '/server-info',
        '/task-plan', '/teams', '/transcription-capabilities', '/transcription-providers',
        '/voice-catalog', '/turn-queue'}
    allowed_post = {'/send', '/select', '/focus', '/clog', '/clips/ack', '/devices', '/stop'}
    for method, allowed in [('GET', allowed_get), ('POST', allowed_post), ('PUT', set()), ('DELETE', set())]:
        original = getattr(module.Handler, 'do_' + method)
        def scoped(self, original=original, allowed=allowed):
            if urlsplit(self.path).path not in allowed:
                return self._send(403, b'QA route is outside the deterministic turn lane')
            return original(self)
        setattr(module.Handler, 'do_' + method, scoped)
    server = module.build_server(ctx, args.port, bind_addr='127.0.0.1')
    metadata = {'url': f'http://127.0.0.1:{server.server_address[1]}', 'pid': os.getpid(),
                'state_dir': str(root), 'provider': 'deterministic-codex-process'}
    (root / 'host.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(json.dumps(metadata), flush=True)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        server.server_close()
        db.close_local()


if __name__ == '__main__':
    main()
