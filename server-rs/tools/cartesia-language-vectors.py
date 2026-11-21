#!/usr/bin/env python3
"""Run the reference's Cartesia language rules in an isolated process (R168)."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/cartesia-language-vectors.json'


@scratch.case('clarp-cartesia-language-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import cartesia_tts, log, settings_store, stt_language, tts_registry

    models = [
        'sonic-latest', ' SONIC-LATEST ', 'sonic-latest-other', 'sonic-3.6',
        'sonic-3.6-2026-01', ' SONIC-3.6-2026-01 ', 'sonic-3', 'sonic-3.5',
        'sonic-3-2025-01', 'sonic-3.60', 'sonic', 'sonic-2024', 'sonic-2024-12',
        'sonic-2025', 'sonic-2', 'sonic-2-2025', 'sonic-turbo', 'sonic-turbo-2025',
        '', 'unknown',
    ]
    declarations = {model: sorted(cartesia_tts.model_languages(model)) for model in models}
    codes = [None, '', ' ', 'en', 'EN_us', 'nb', 'nn', ' no-NO ', 'NB_no',
             'nn-NO', 'or', 'ur-PK', 'hi', 'hi_IN', 'fr', 'ka', 'pa', 'xx', '-no', 'no,en']
    requests = []
    for model in models:
        for code in codes:
            events = []
            with patch.object(log, 'log', lambda event, text: events.append([event, text])):
                language = cartesia_tts.language_for(code, model)
            requests.append({'model': model, 'code': code, 'language': language, 'logs': events})

    agent = {'name': ' Display ', 'persona': 'Persona', 'session': 'session-1'}
    agents = []
    inputs = [
        ('name before persona and session', agent, '{"display":" NB,no","persona":"fr","session-1":"de"}', 'en', ''),
        ('persona before session', agent, '{" Persona ":"nn,pt","session-1":"de"}', 'en', ''),
        ('session alone', agent, '{"session-1":"de,fr"}', 'en', ''),
        ('unmatched override', agent, '{"else":"no"}', 'pt', ''),
        ('missing default and map', agent, None, None, ''),
        ('invalid default', agent, None, 'invalid', ''),
        ('no agent', None, '{"display":"no"}', ' DE ', ''),
        ('malformed map', agent, '{', 'fr', ''),
        ('non-map', agent, '[]', 'fr', ''),
        ('invalid own set', agent, '{"display":"pt,,en"}', 'de', ''),
        ('failed agent settings read', agent, None, 'no', 'agent'),
        ('failed default read', agent, None, 'no', 'default'),
        ('matched agent does not read default', agent, '{"display":"no,de"}', 'pt', 'default'),
    ]
    for case, who, raw, default, fail in inputs:
        events = []

        def read(key):
            reading_own = key == stt_language.AGENT_LANGUAGES_KEY
            if (reading_own and fail == 'agent') or (not reading_own and fail == 'default'):
                raise OSError('scratch setting unreadable')
            return raw if reading_own else default

        with patch.object(settings_store, 'get_text', read), \
                patch.object(log, 'log_exception', lambda event, _error: events.append(event)):
            language = tts_registry.agent_speech_language(who)
        agents.append({'case': case, 'agent': who, 'map': raw, 'default': default, 'read_error': fail,
                       'language': language, 'logs': events})
    return {'model_languages': declarations, 'requests': requests, 'agents': agents}


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(), ensure_ascii=True))
        return
    ran = subprocess.run([sys.executable, '-I', __file__, '--child'], capture_output=True, text=True,
                         env=scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode:
        print(ran.stderr[-3000:], file=sys.stderr)
        raise SystemExit(ran.returncode)
    cases = json.loads(ran.stdout.splitlines()[-1])
    ordered = sorted(cases.items())
    lines = ['{']
    for index, (key, value) in enumerate(ordered):
        encoded = lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True)
        suffix = ',' if index + 1 < len(ordered) else ''
        if isinstance(value, list):
            lines.append(f' {encoded(key)}: [')
            lines.append(',\n'.join(f'  {encoded(row)}' for row in value))
            lines.append(f' ]{suffix}')
        else:
            lines.append(f' {encoded(key)}: {encoded(value)}{suffix}')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print('R168 vectors:', {key: len(value) for key, value in cases.items()})


if __name__ == '__main__':
    main()
