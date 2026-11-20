#!/usr/bin/env python3
"""Record the reference's table, playback keys and cached audio events."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/router-phrases.json'


def record(directory):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    (directory / 'scenario-config.toml').write_text('')
    replay.isolate(directory)
    replay.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import orchestrator as router
    from lib.orchestrator_phrases import PHRASES
    rows, events = [], []
    for text in [*PHRASES.values(), '', 'Other words', 'Was that for me? ']:
        for supplied in ['', 'clarify_target', 'unrelated']:
            rows.append([text, supplied, *router.OrchestratorService._clip_key(text, supplied)])
    service = SimpleNamespace(ctx=SimpleNamespace(stream=None))
    key, text = next(iter(PHRASES.items()))
    for session, agent in [('', None), ('agent-a', 'id-a'), ('agent-b', 'id-b')]:
        with patch.object(router, 'conn') as conn, patch.object(router.identity, 'lookup', return_value={'agent_id': agent}), patch.object(router.events, 'broadcast', side_effect=lambda _, e: events.append(e)):
            conn.return_value.execute.return_value.fetchone.return_value = {'text': text, 'audio_path': '/audio/fixture.mp3'}
            assert router.OrchestratorService._play_cached_phrase(service, phrase_key=key, session=session, voice_id='app-voice', expected_text=text)
    return {'keys': rows, 'table': PHRASES, 'events': events}


if __name__ == '__main__':
    if '--isolated' in sys.argv:
        with scratch.directory('router-phrases-') as place, contextlib.redirect_stderr(io.StringIO()):
            print(json.dumps(record(Path(place)), sort_keys=True))
    else:
        env = scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        out = subprocess.check_output([sys.executable, '-I', __file__, '--isolated'], env=env, text=True)
        OUT.write_text(json.dumps(json.loads(out.splitlines()[-1]), indent=2) + '\n')
