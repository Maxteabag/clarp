#!/usr/bin/env python3
"""Replay the Python Host's R152 selection, fallback and refusal with isolated engines."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs/crates/clarp-kernel/tests/fixtures/language-set-vectors.json'
CHIRP = 'google:chirp_3'
SCRIBE = 'elevenlabs:scribe_v2'
NOVA = 'deepgram:nova-3'
INK = 'cartesia:ink-2'


@scratch.case('clarp-language-set-vectors-')
def reference(root):
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, custom_stt_adapters, stt_language, stt_providers as stt
    from lib import transcription_catalog, transcription_pipeline as pipe

    def configured(model='small', enabled=True):
        return SimpleNamespace(whisper_provider='faster-whisper', whisper_model=model,
                               whisper_enabled=enabled)

    def declarations(codes):
        def declared(_):
            if codes == 'unavailable':
                raise RuntimeError('/scratch/internal-model-path')
            return None if codes is None else {'languages': codes}
        return declared

    def own_result(data):
        with patch.object(custom_stt_adapters, 'get', return_value=None), \
                patch.object(transcription_catalog, 'model_by_id', declarations(data['declared'])):
            return stt.own_engine_refusal(configured(data['model'], data['enabled']), data['languages'])

    def selected(model, keyed):
        with patch.object(stt, 'selected_engine', return_value=model), \
                patch.object(config, 'load', return_value=configured()), \
                patch.object(stt, '_key_for', return_value='stand-in' if keyed else ''):
            return stt.language_set_engine()

    def fallback(data):
        calls, logs = [], []
        actual = configured(data['model'], data['enabled'])

        def hosted(model, *args, **kwargs):
            calls.append(model)
            if data['hosted_error']:
                raise RuntimeError('stand-in hosted failure')
            stt_language.note_handed(None if stt.provider_of(model) == 'elevenlabs' else data['language'])
            return 'heard', True, 1.0

        def local(*args, **kwargs):
            calls.append('server-default')
            error = data['own_error']
            if error == 'busy':
                raise pipe.STTBusyError('busy')
            if error == 'loading':
                raise pipe.STTModelLoadingError('loading')
            if error:
                raise RuntimeError('/scratch/internal-model-path')
            return 'heard', True, 1.0

        own = SimpleNamespace(default_model_id=f"faster-whisper:{data['model']}",
                              transcribe_bytes=local)
        with patch.object(config, 'load', return_value=actual), \
                patch.object(stt_language, 'current_language', return_value=data['language']), \
                patch.object(stt, 'selected_engine', return_value=data['selected']), \
                patch.object(stt, '_key_for', return_value='stand-in' if data['keyed'] else ''), \
                patch.object(custom_stt_adapters, 'get', return_value=None), \
                patch.object(transcription_catalog, 'model_by_id', declarations(data['declared'])), \
                patch.object(pipe, 'log', lambda *a: None), \
                patch.object(pipe, 'log_exception', lambda *a, **k: logs.append(str(a[1]))), \
                patch('lib.log.log_exception', lambda *a, **k: logs.append(str(a[1]))):
            try:
                _, _, _, target, spoken = pipe._language_set_fallback(
                    own, hosted, data['from'], b'stand-in', 'audio/webm', None, 'agent',
                    failed=data['attempted'])
            except stt.LanguageSetRefused as e:
                result = {'error': 'language_set_unsupported', 'message': str(e), 'engine': e.engine,
                          'languages': e.languages, 'engines': e.engines, 'reasons': e.reasons}
            except pipe.STTBusyError:
                result = {'status': 429}
            except pipe.STTModelLoadingError:
                result = {'status': 503}
            except RuntimeError:
                result = {'status': 500}
            else:
                result = {'from': data['from'], 'to': target, 'reason': 'language_set', 'languages': spoken}
        assert all('/scratch/' not in str(value) for value in result.values())
        return {'result': result, 'calls': calls, 'logged_internal_detail': bool(logs)}

    def reference_repeats_provider():
        calls = []

        def transcribe(model, *_a, **_kw):
            if model == NOVA:
                raise stt.LanguageSetUnsupported('language set')
            calls.append(model)
            stt_language.note_handed(None)
            raise RuntimeError('stand-in outage')

        own = SimpleNamespace(transcribe_model_bytes=transcribe)
        with patch.object(stt, 'language_set_engine', return_value=''), \
                patch.object(stt, 'own_engine_refusal', return_value="the Host's own engine is off"), \
                patch.object(config, 'load', return_value=configured()), \
                patch.object(stt_language, 'current_language', return_value='no,en'), \
                patch.object(pipe, 'log', lambda *a: None), \
                patch.object(pipe, 'log_exception', lambda *a, **kw: None):
            result = pipe._run_engines(
                own, call_stt=None, call_steps=[], requested_model=SCRIBE,
                agent_fallbacks=[SCRIBE, NOVA], focus='agent', audio_bytes=b'stand-in',
                ctype='audio/webm', prompt=None, trace_id='trace', vocab_run_id=0,
                utterance_id='', client_ts=None, record_voice=lambda *a, **kw: None)
        assert result.status == 422
        return {'order': [SCRIBE, SCRIBE, NOVA], 'sent': calls, 'language': 'no,en'}

    selections = [{'model': model, 'keyed': keyed, 'selected': selected(model, keyed)}
                  for model in (CHIRP, SCRIBE, NOVA, INK, 'google:unknown', 'local', '')
                  for keyed in (False, True)]
    own = []
    for model, enabled, declared, languages in (
        ('small', False, ['multilingual'], ['no', 'en']),
        ('small', True, 'unavailable', ['no', 'en']),
        ('small.en', True, None, ['no', 'en']),
        ('small.en', True, None, ['en-GB']),
        ('custom', True, [], ['nb', 'en']),
        ('custom', True, ['  NN-no ', 'EN_us'], ['no', 'en-GB']),
        ('custom', True, ['nb', 'pt'], ['nn', 'pt_BR']),
        ('custom', True, ['nb', 'pt'], ['en', 'no']),
        ('custom.en', True, ['MULTILINGUAL'], ['no', 'en']),
        ('custom.en', True, ['  ', ' EN '], ['no', 'en']),
    ):
        data = dict(model=model, enabled=enabled, declared=declared, languages=languages)
        own.append({**data, 'refusal': own_result(data)})

    fallbacks = []
    for case, patch_values in (
        ('selected Chirp', dict(selected=CHIRP)),
        ('selected Scribe', dict(selected=SCRIBE)),
        ('selected not known', dict(selected='google:unknown')),
        ('selected unkeyed', dict(selected=CHIRP, keyed=False)),
        ('selected incompatible', dict(selected=NOVA)),
        ('selected already failed', dict(selected=SCRIBE, attempted=[SCRIBE])),
        ('selected and own already failed', dict(selected=SCRIBE, attempted=[SCRIBE, 'local'])),
        ('selected is rejecting provider', dict(selected=CHIRP, **{'from': CHIRP})),
        ('selected failed terminally', dict(selected=CHIRP, hosted_error=True)),
        ('own alias blank already failed', dict(attempted=[''])),
        ('own alias named already failed', dict(attempted=['server-default'])),
        ('own id already failed', dict(attempted=['faster-whisper:small'])),
        ('own model too narrow', dict(model='small.en', declared=['en'])),
        ('own model multilingual by declaration', dict(model='small.en', declared=['multilingual'])),
        ('own engine off', dict(enabled=False)),
        ('own engine unavailable', dict(declared='unavailable')),
        ('own engine failed', dict(own_error='failed')),
        ('own engine busy', dict(own_error='busy')),
        ('own engine loading', dict(own_error='loading')),
        ('ordered reasons', dict(selected=SCRIBE, attempted=[SCRIBE], enabled=False)),
    ):
        data = {
            'from': NOVA, 'selected': '', 'keyed': True, 'attempted': [],
            'model': 'small', 'enabled': True, 'declared': None, 'language': 'no,en',
            'own_error': '', 'hosted_error': False, **patch_values,
        }
        fallbacks.append({'case': case, **data, **fallback(data)})
    return {'selections': selections, 'own': own, 'fallbacks': fallbacks,
            'reference_repeats_provider': reference_repeats_provider()}


def main():
    if sys.argv[1:] == ['--child']:
        print(json.dumps(reference(), sort_keys=True, ensure_ascii=True))
        return
    ran = subprocess.run([sys.executable, '-I', __file__, '--child'], capture_output=True, text=True,
                         env=scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=True)
    vectors = json.loads(ran.stdout.splitlines()[-1])
    OUT.write_text(json.dumps(vectors, sort_keys=True, ensure_ascii=True, indent=2) + '\n', encoding='ascii')
    print(f"{len(vectors['selections'])} selections, {len(vectors['own'])} own declarations, "
          f"{len(vectors['fallbacks'])} fallbacks and one reference duplication")


if __name__ == '__main__':
    main()
