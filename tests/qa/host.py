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
import urllib.request
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]


def recording_stt(root, stub):
    """Cloud engines without a paid provider (CLARP_QA_RECORDING_STT=1).

    A cloud model id goes through the production handoff,
    stt_providers.transcribe, exactly as WhisperSTT routes it; only the
    provider adapters behind it are replaced by recorders. Each appends the
    language it was handed to provider/stt-handoffs.jsonl. provider/stt-control.json
    ({"no_key": [...], "fail_after": [...]}) makes a provider's key check
    fail (its handoff is then unobserved) or the provider fail after it. The local default stays the stub.
    """
    from lib import cartesia_stt, cartesia_stt_ws, deepgram_stt, eleven_stt, google_stt, stt_providers
    from lib.stt import STTUnknownModelError
    log_path = root / 'provider' / 'stt-handoffs.jsonl'
    control_path = root / 'provider' / 'stt-control.json'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def control():
        return json.loads(control_path.read_text()) if control_path.is_file() else {}

    def recorder(provider):
        def run(**kwargs):
            with log_path.open('a') as log:
                log.write(json.dumps({'provider': provider, 'model': kwargs.get('model'),
                                      'language': kwargs.get('language')}) + '\n')
            if provider in control().get('fail_after', []):
                raise RuntimeError(f'{provider} failed after the handoff (QA)')
            return 'QA cloud transcript', 1.0
        return run
    for module, provider in ((eleven_stt, 'elevenlabs'), (google_stt, 'google'),
                             (deepgram_stt, 'deepgram'), (cartesia_stt, 'cartesia'), (cartesia_stt_ws, 'cartesia')):
        module.transcribe = recorder(provider)
    stt_providers._key_for = lambda cfg, provider: (
        '' if provider in control().get('no_key', []) else 'qa-recording-key')

    class RecordingSTT(type(stub)):
        def transcribe_model_bytes(self, model_id, audio_bytes, content_type, vocab_prompt, *, wait=0.0):
            if stt_providers.is_cloud_model(model_id):
                return stt_providers.transcribe(model_id, audio_bytes, content_type, vocab_prompt)
            raise STTUnknownModelError(f'transcription model not installed: {model_id}')
    return RecordingSTT(text=stub.text, ends_terminal=stub.ends_terminal)


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
    from lib import agents, backends, codex_transcript, db, telemetry
    # Storage location is injected; discovery and parsing remain production code.
    codex_transcript._codex_home = lambda: root / 'provider'
    for path in (db.DB_PATH, telemetry.TELEMETRY_PATH):
        if not path.resolve().is_relative_to(root):
            raise SystemExit(f'QA database escaped the disposable root: {path}')
    from lib.audio_stream import AudioStream
    from lib.context import ServerContext, StubSTT
    from lib.tts_engine import FakeTTSEngine
    backends.by_id('codex').required_binary = str(REPO / 'tests/qa/fake_codex.py')
    # Capability probes would otherwise run the developer's real provider CLIs
    # (opencode's wrapper rewrites global mise config and outlives the Host).
    from lib import provider_capabilities
    provider_capabilities._resolve_executable = (
        lambda provider_id: str(REPO / 'tests/qa/fake_codex.py') if provider_id == 'codex' else None)
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
    stt = StubSTT(text='QA voice prompt', ends_terminal=True)
    if os.environ.get('CLARP_QA_RECORDING_STT') == '1':
        stt = recording_stt(root, stt)
    ctx = ServerContext(root=root, static=REPO / 'static', audio_dir=audio,
                        agents_path=root / 'agents.json', default_session='rachel',
                        tts=FakeTTSEngine(audio), stream=AudioStream(audio),
                        stt=stt,
                        auth_token=args.token, roster_names=('Rachel', 'Mike'))
    spec = importlib.util.spec_from_file_location('qa_server', REPO / 'server/server.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The turn lane does not exercise installation, provider login, arbitrary
    # agent creation, file browsing or terminal shells. Keep those capabilities
    # unavailable, even if another CLI is authenticated on the developer's Mac.
    allowed_get = {'/status', '/agents/snapshot', '/artifacts', '/attention',
        '/automation-settings', '/clips/recoverable', '/events', '/log', '/server-info',
        '/task-plan', '/task-plans', '/task-plan/document', '/teams', '/transcription-capabilities', '/transcription-providers',
        '/voice-catalog', '/turn-queue', '/goal-ledger', '/goal-ledger/delegations', '/calls/ring'}
    allowed_post = {'/send', '/select', '/focus', '/clog', '/clips/ack', '/devices', '/stop', '/turn-queue/resume', '/task-plan/action',
        '/goal-ledger/delegations', '/goal-ledger/delegations/stop',
        # Agent rings and listen-first openings (docs/agent-rings.md).
        '/calls/ring', '/calls/ring/answer', '/calls/ring/opening', '/calls/end',
        # Call-scoped transcription (docs/group-calls.md); the QA STT is a stub.
        '/transcribe', '/calls'}
    if os.environ.get('CLARP_QA_RECORDING_STT') == '1':
        # Call policy and agent overrides for the recording engines below.
        allowed_post |= {'/transcription-providers'}
    for method, allowed in [('GET', allowed_get), ('POST', allowed_post), ('PUT', set()), ('DELETE', set())]:
        original = getattr(module.Handler, 'do_' + method)
        def scoped(self, original=original, allowed=allowed):
            if urlsplit(self.path).path not in allowed:
                return self._send(403, b'QA route is outside the deterministic turn lane')
            return original(self)
        setattr(module.Handler, 'do_' + method, scoped)
    metadata = {}
    # Bookkeeping listeners run in this process, never as real user services:
    # a unit left behind would outlive this disposable root.
    from lib import bookkeeping_listener
    bookkeeping_listener.POLL_SECONDS, bookkeeping_listener.QUIET_MS = .1, 500
    bookkeeping_listener.MAX_DELAY_MS = 3000

    def self_send(session, text, wake_id):
        request = urllib.request.Request(
            metadata['url'] + '/send', method='POST',
            data=json.dumps({'session': session, 'text': text, 'client_msg_id': wake_id,
                             'origin': 'automation', 'queue_if_busy': True,
                             'synthesize_audio': False}).encode(),
            headers={'Authorization': f'Bearer {args.token}', 'Content-Type': 'application/json'})
        urllib.request.urlopen(request, timeout=10).read()

    def launch_in_process(delegation_id):
        threading.Thread(target=bookkeeping_listener.main, args=(delegation_id,),
                         kwargs={'send': self_send}, daemon=True).start()
        return {'unit': 'in-process', 'started': True, 'error': ''}
    bookkeeping_listener.launch = launch_in_process
    # The deterministic lane runs every agent on the fake Codex provider; tests
    # present Host turn tokens explicitly, so delegations may name these agents.
    from lib import turn_identity
    turn_identity.IDENTITY_BACKENDS = turn_identity.IDENTITY_BACKENDS | {'codex'}
    bookkeeping_listener.unit_active = lambda delegation_id: False
    bookkeeping_listener.stop_unit = lambda delegation_id: {'unit': 'in-process', 'stopped': True}
    # Exercise the production goal reconciler promptly in disposable tests.
    schedule_runner = module.AgentScheduleRunner
    module.AgentScheduleRunner = lambda **kwargs: schedule_runner(check_interval_sec=.05, **kwargs)
    server = module.build_server(ctx, args.port, bind_addr='127.0.0.1')
    metadata.update({'url': f'http://127.0.0.1:{server.server_address[1]}', 'pid': os.getpid(),
                'state_dir': str(root), 'provider': 'deterministic-codex-process'})
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
