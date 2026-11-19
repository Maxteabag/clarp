#!/usr/bin/env python3
"""Record the WAV header probe and the configured duration switch in an isolated reference process."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/long-recordings-vectors.json'


def record():
    spec = importlib.util.spec_from_file_location('host', ROOT / 'tests/replay/host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / 'scenario-config.toml').write_text('')
        host.isolate(p)
        host.loopback_only()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib import audio_duration, stt_providers, settings_store
        def chunk(name, data, length=None):
            return name + struct.pack('<I',len(data) if length is None else length) + data + bytes(len(data)%2)
        def fmt(tag=1, channels=1, rate=8000, bits=16, extra=b''):
            return chunk(b'fmt ', struct.pack('<HHIIHH',tag,channels,rate,0,0,bits)+extra)
        def wave(parts):
            return chunk(b'RIFF',b'WAVE'+b''.join(parts))
        pcm = bytes.fromhex('0100000000001000800000aa00389b71')
        ext = struct.pack('<HHI',22,16,0)
        data = chunk(b'data',b'',16000)
        samples = [wave([fmt(),data]),wave([fmt(),chunk(b'data',b'',1)]),
                   wave([chunk(b'JUNK',b'x'),fmt(),data]),wave([data,fmt()]),
                   wave([fmt(),fmt(rate=16000),data]),wave([fmt(),chunk(b'LIST',b'abc'),data]),
                   b'', b'not a RIFF', b'RIFF'+bytes(4)+b'WAVE',wave([fmt()]),wave([chunk(b'fmt ',b'bad'),data]),
                   wave([fmt(0xfffe,extra=ext+pcm),data]),wave([fmt(0xfffe,extra=ext+bytes(16)),data]),
                   wave([fmt(0xfffe,extra=ext+pcm[:15]),data])]
        for channels in (0,1,2):
            for rate in (0,1,8000):
                for bits in (0,1,8,9,16,24):
                    samples.append(wave([fmt(channels=channels,rate=rate,bits=bits),chunk(b'data',b'',19)]))
        samples.extend([wave([fmt(tag=3),data]), wave([fmt(),data]).replace(b'WAVE',b'xxxx'),
                        wave([fmt(),data]).replace(b'RIFF',b'RIFX')])
        normal=wave([fmt(),data])
        samples.extend(normal[:n] for n in range(len(normal)))
        wav=[{'audio':base64.b64encode(raw).decode(),'seconds':audio_duration._wav_seconds(raw)} for raw in samples]
        switches=[]
        for model in ('','deepgram:nova-3',' custom:model '):
            for threshold in (1,30,3600):
                settings_store.set_text(stt_providers.LONG_FORM_MODEL_KEY,model)
                settings_store.set_text(stt_providers.LONG_FORM_THRESHOLD_KEY,str(threshold))
                for duration in (0,-1,threshold-.001,threshold,threshold+.001,7200):
                    switches.append(dict(model=model,threshold=threshold,seconds=duration,
                                         selected=stt_providers.long_form_model_for(duration)))
        from unittest.mock import patch
        from lib import call_transcription, transcription_pipeline as pipe
        from lib import config, stt_language
        from types import SimpleNamespace
        calls = []
        local = 'faster-whisper:small.en'
        for own in ([], ['deepgram:nova-3']):
            for policy_engines in ([], ['elevenlabs:scribe_v2']):
                for language in ('', 'en', 'no'):
                    for requested in ('', 'local', 'elevenlabs:scribe_v2'):
                        for duration in (29.9,30.0):
                            for long in ('deepgram:nova-3',local):
                                policy = dict(languages=language,engines=policy_engines,global_engine='deepgram:nova-3',
                                              configured=bool(language or policy_engines),error='')
                                with patch.object(call_transcription,'policy_snapshot',return_value=policy), \
                                     patch.object(stt_language,'agent_languages',return_value={}), \
                                     patch.object(stt_language,'engines_for',return_value=own), \
                                     patch.object(stt_language,'selected',return_value='en'), \
                                     patch.object(config,'load',return_value=SimpleNamespace(whisper_provider='faster-whisper',whisper_model='small.en')), \
                                     patch.object(stt_providers,'long_form_model',return_value=long), \
                                     patch.object(stt_providers,'long_form_threshold_sec',return_value=30), \
                                     patch.object(audio_duration,'seconds',return_value=duration):
                                    planned = pipe._plan_call({'session':'agent'}, requested, b'clip')
                                case=dict(own=own,policy=policy,requested=requested,duration=duration,long=long)
                                if isinstance(planned,pipe.TranscriptionOutcome):
                                    case['refusal']=planned.payload
                                else:
                                    _, resolved, steps, first = planned
                                    case.update(steps=steps,first=first,engines=resolved['engines'],device=resolved['requested_model'])
                                calls.append(case)
        containers=[]
        for fields in [
            dict(container=1_000_000,duration=1,base=.1),
            dict(container=500_000,duration=4,base=1),
            dict(streams=['video','audio'],duration=2,base=.5),
            dict(duration=1,base=.25,rate=1,frames=[8]),
            dict(rate=1000,frames=[499,501]),
            dict(rate=1000,frames=[499,500]),
            dict(streams=['video'],container=2_000_000),
            dict(rate=0,frames=[500]),dict(rate=1,frames=[]),
        ]:
            class Container:
                duration=fields.get('container')
                streams=[SimpleNamespace(type=t,duration=fields.get('duration'),time_base=fields.get('base'),
                                         rate=fields.get('rate')) for t in fields.get('streams',['audio'])]
                def __enter__(self):
                    return self
                def __exit__(self,*_args):
                    pass
                def decode(self,_stream):
                    return [SimpleNamespace(samples=n) for n in fields['frames']]
            av=SimpleNamespace(time_base=1_000_000, open=lambda *_args,**_kw:Container())
            with patch.dict(sys.modules, av=av):
                measured=audio_duration.seconds(b'synthetic container')
            containers.append(dict(fields=fields,seconds=measured))
        return dict(wav=wav,switches=switches,calls=calls,containers=containers)


if __name__=='__main__':
    if sys.argv[1:] == ['--record']:
        print(json.dumps(record(),indent=1,ensure_ascii=True))
    else:
        OUT.write_bytes(subprocess.check_output([sys.executable,'-IB',__file__,'--record'],
                                                env={'PATH':os.defpath,'LANG':'C.UTF-8'}))
