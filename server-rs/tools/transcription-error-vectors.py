#!/usr/bin/env python3
"""Record the reference's transcription error words, status, and voice event detail."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/transcription-error-vectors.json'


@scratch.case('clarp-transcription-error-vectors-')
def record(place):
    spec = importlib.util.spec_from_file_location('host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (place / 'scenario-config.toml').write_text('')
    host.isolate(place)
    host.loopback_only()
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import heard_audio, transcription_pipeline as reference

    class DerivedLoading(reference.STTModelLoadingError):
        pass

    class Engine:
        def __init__(self, error):
            self.error = error

        def transcribe_bytes(self, *args, **kwargs):
            raise self.error

    cases = []
    for kind, error_type in [('unknown_model', reference.STTUnknownModelError),
                             ('loading', reference.STTModelLoadingError),
                             ('loading', DerivedLoading),
                             ('busy', reference.STTBusyError), ('failed', RuntimeError)]:
        for detail in ['', 'provider output named /fixture/private/model.bin']:
            error = error_type(detail)
            voice = []
            with (patch.object(reference, 'log_exception'), patch.object(heard_audio, 'retain'),
                  patch.object(reference.health, 'mark_error'),
                  patch.object(reference, '_cache_dir', return_value=place)):
                result = reference._run_engines(
                    Engine(error), call_stt=None, call_steps=[], requested_model='',
                    agent_fallbacks=[], focus='', audio_bytes=b'fixture', ctype='audio/webm',
                    prompt=None, trace_id='fixture', vocab_run_id=0, utterance_id='fixture',
                    client_ts=None, record_voice=lambda event, **kw: voice.append(kw['detail']),
                )
            word = reference.client_words(error)
            assert result.payload['error'] == word
            cases.append(dict(kind=kind, type=error_type.__name__, status=result.status,
                              word=word, voice=voice))
    return cases


if __name__ == '__main__':
    OUT.write_text(json.dumps(record(), ensure_ascii=True, indent=2) + '\n', encoding='ascii')
