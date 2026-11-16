#!/usr/bin/env python3
"""Writes the vectors the Rust Host's rules of speech in are tested against.

A person speaks, an app records a clip and hands it to the Host with a name for the job
(`POST /transcribe`). Before and after an engine turns the clip into text, the Python Host
applies rules that are no engine's: what a job may be called and when two requests are the same
job (`server/lib/transcription_results.py`), which engine runs a clip (`stt_router.py`), which
of an engine's segments are kept (`stt.py`), when a text is something an engine invented on
silence (`hallucinations.py`), what a clip is called when it is sent on (`eleven_stt.py`), how
a header's yes and a header's number are read, and which engines there are
(`stt_providers.py`, `transcription_catalog.py`). This calls those functions for many made-up
inputs and writes what they answered to `crates/clarp-kernel/tests/fixtures/speech-vectors.json`.

    python3 server-rs/tools/speech-vectors.py

Nothing of a Host is touched or started. A file that leans on nothing of the server is run as
the module it is; from a file that does, each function is taken out by its name and called by
itself, so that no other module of the server runs. No engine is called and no clip is real: the
"audio" is a few made-up bytes.
"""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
LIB = REPO / 'server' / 'lib'
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'speech-vectors.json'


def taken(path: pathlib.Path, names: list[str], given: dict | None = None) -> dict:
    """The named functions and values of a file, by themselves: every top-level definition
    or assignment with one of these names is run in a room of its own."""
    tree = ast.parse(path.read_text())
    wanted = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            wanted.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names:
            wanted.append(node)
    found = {n.name if hasattr(n, 'name') else (n.targets[0].id if isinstance(n, ast.Assign) else n.target.id)
             for n in wanted}
    missing = set(names) - found
    assert not missing, f'{path.name} has no {sorted(missing)}'
    room = {'re': re, 'hashlib': hashlib, '__name__': 'taken', **(given or {})}
    module = ast.Module(body=wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), room)  # noqa: S102 - the repo's own code
    return room


def whole(path: pathlib.Path) -> dict:
    """A file that leans on nothing of the server, run as the module it is."""
    import importlib.util
    name = f'taken_{path.stem}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return vars(module)


def said(function, *args, **kwargs):
    """What a function answered, or what it refused with."""
    try:
        return {'answer': function(*args, **kwargs)}
    except Exception as error:  # noqa: BLE001 - the refusal is the vector
        return {'refused': type(error).__name__, 'message': str(error)}


def job_ids(results: dict) -> list:
    names = [
        None, '', '   ', 'a', 'job-1', 'A.b_c-9', ' padded ', '\tjob\n', 'x' * 128, 'x' * 129, 'has space',
        'slash/in', 'ünï', 'job:1', 'job+1', 'job=1', '.', '..', '-', '_', 'UPPER', '0', 'a\nb', 'a.b.c',
        '4b0c5e0e-7a53-4d31-9c58-4f0f3b5b2f11', 'utt_1794800000000_3', '😀', 'tab\tinside', '%41', 'a,b',
    ]
    return [{'given': name, **said(results['normalize_job_id'], name)} for name in names]


def fingerprints(results: dict) -> list:
    clips = [b'', b'\x00', b'RIFF....WAVEfmt ', bytes(range(256)), b'clip one', b'clip two']
    kinds = ['audio/webm', 'audio/wav', '', 'audio/mp4; codecs=mp4a', 'ünï/cödé']
    models = ['', 'server-default', 'elevenlabs:scribe_v2', 'faster-whisper:small.en']
    out = []
    for at, clip in enumerate(clips):
        for kind in kinds[: 2 + at % 4]:
            for model in models[: 1 + (at + len(kind)) % 4]:
                for hands_free in (False, True):
                    out.append({'audio_hex': clip.hex(), 'content_type': kind, 'model': model,
                                'hands_free': hands_free,
                                'fingerprint': results['request_fingerprint'](clip, kind, model, hands_free)})
    return out


def plans(router: dict) -> list:
    out = []
    for requested in ('', 'server-default', 'elevenlabs:scribe_v2', 'faster-whisper:medium'):
        for agent in ([], ['google:chirp_3'], ['elevenlabs:scribe_v2', 'deepgram:nova-3', 'local']):
            for cloud in ('', 'deepgram:nova-3'):
                for long_model in ('', 'elevenlabs:scribe_v2', 'google:chirp_3'):
                    for in_call in (False, True):
                        plan = router['plan_engines'](requested_model=requested, agent_engines=list(agent),
                                                     cloud_engine=cloud, long_model=long_model, in_call=in_call)
                        wants = router['wants_cloud_engine'](requested_model=requested, agent_engines=list(agent),
                                                            in_call=in_call)
                        out.append({'requested': requested, 'agent_engines': agent, 'cloud_engine': cloud,
                                    'long_model': long_model, 'in_call': in_call, 'wants_cloud_engine': wants,
                                    'model': plan.model, 'agent_engine': plan.agent_engine,
                                    'fallbacks': list(plan.agent_fallbacks),
                                    'escalated_from': plan.escalated_from})
    return out


def invented(blocklist: dict) -> list:
    phrases = sorted(blocklist['HALLUCINATION_PHRASES'])
    texts = [
        '', ' ', '.', '...', '. . .', 'Thank you.', 'thank you', 'THANK YOU!', ' Thank you very much. ',
        'Thank you very much. I hope you enjoyed it.', 'Thank you. Please open the file.', 'Okay.', 'Okay, do it.',
        'okay okay', 'Yeah.', 'Yeah, that one.', 'you', 'You.', 'You?', 'you know', 'You know what I mean?',
        'Bye!', 'Bye bye.', 'bye-bye', 'Goodbye. See you next time.', 'See you.', 'See you tomorrow.',
        '[Music]', '[music]', '(music)', '♪', '♪♪', '♪ ♪', '[ Applause ]', '(laughter)', '[BLANK_AUDIO]',
        '(speaking in foreign language)', '[lyd fra mikrofon]', '(a) (b)', '(a)(b)', '(unclosed', 'unopened)',
        '(nested (inside))', '[mixed)', '(line\nbreak)', 'Run the tests (all of them).', '(Run the tests.)',
        '(Run the tests)', '[Run] the tests', 'Please.', 'please', 'Please transfer.', 'Please run it.',
        'Subscribe.', "Don't forget to subscribe.", 'Like, comment and subscribe.', 'like comment and subscribe',
        'Mm-hmm.', 'Uh-huh', 'Um...', 'Hmm.', 'hm', 'Mmm', 'and', 'And.', 'The.', 'A', 'a.', 'and the a',
        'And. The. A.', 'Thanks!', 'Thanks a lot!', 'Thanks so much. Bye.', 'Good night.', 'Goodnight!',
        'Good morning. Open the log.', 'Good morning.', 'Good evening?', "I'll see you in the next one.",
        'I hope you enjoyed this video. Thanks for watching!', 'Thanks for watching — bye.', '— thank you —',
        '–okay–', '-ok-', 'ok!?', 'OK?!', '  Thank you  .  ', 'Thank you\n', 'thank you,', ', thank you ,',
        'Thank  you.', 'thank\tyou', 'Tack.', 'Takk.', 'Ja.', 'ünï cödé', 'İ', 'Maximus, status.', 'Status?',
        'One. Two. Three.', 'one', '1', '!', '?', '!?', '?!.', ' . ', '\x1cthank you\x1f', ' okay ',
        ' thanks ', 'thank you​', 'Thank you. [Music]', '[Music] Thank you.', '[Music] Run it.',
        '(music) (applause)', 'music', 'Music.', 'The music is too loud.', 'Yeah. Okay. Thanks.', 'Yeah okay thanks',
    ]
    return {'phrases': phrases,
            'texts': [{'text': text, 'invented': blocklist['is_pure_hallucination'](text)} for text in texts]}


def joined(speech: dict) -> list:
    cases = [
        [],
        [(' Hello there.', 0.0)],
        [(' Hello', 0.1), (' there.', 0.2)],
        [(' kept', 0.59), (' dropped', 0.6), (' kept too.', 0.5999)],
        [(' dropped', 0.9), (' also dropped', 1.0)],
        [('  spaces  ', 0.0), ('  around  ', 0.0)],
        [('no leading space', 0.0), ('joined', 0.0)],
        [(' ünï', 0.0), (' cödé.', 0.3)],
        [('', 0.0), (' ', 0.0), ('\n', 0.0)],
        [(' One.', 0.61), (' Two.', 0.0), (' Three.', 0.7)],
    ]
    out = []
    for case in cases:
        segments = [types.SimpleNamespace(text=text, no_speech_prob=chance) for text, chance in case]
        out.append({'segments': [{'text': text, 'no_speech_prob': chance} for text, chance in case],
                    'joined': speech['_join_confident_segments'](segments)})
    # A segment that does not say how sure it is of speech is kept.
    out.append({'segments': [{'text': ' unsure of nothing.'}],
                'joined': speech['_join_confident_segments']([types.SimpleNamespace(text=' unsure of nothing.')])})
    return out


def small_rules(providers: dict, eleven: dict, helpers: dict, server: dict) -> dict:
    kinds = ['audio/webm', 'audio/mp4', 'audio/m4a', 'audio/x-m4a', 'audio/wav', 'audio/x-wav', 'audio/ogg',
             'audio/ogg; codecs=opus', 'audio/mpeg', 'audio/mp3', 'AUDIO/WAV', 'Audio/MP4', '', 'application/octet-stream',
             'audio/webm;codecs=opus', 'video/mp4', 'audio/wave', 'audio/flac']
    texts = ['', ' ', 'Done.', 'Done', 'Done!', 'Done?', 'Done. ', 'Done.\n', 'Done…', 'Done."', 'ok?!', '.', 'a.b',
             'ünï.', 'Done. And more', '3.', '...', 'Done ! ']
    models = ['', 'local', 'server-default', 'elevenlabs:scribe_v2', 'ElevenLabs:Scribe_v2', ' deepgram :nova-3',
              'faster-whisper:small.en', 'whisper.cpp:base.en', 'google:chirp_3', 'cartesia:ink-2', 'unknown:model',
              ':', 'a:b:c', 'nocolon']
    payloads = ['', 'a', 'a, b, c', ' a ,b,, c ,', ',', 'Clarp, Maximus, ünï cödé', 'one term only', 'a,b', ' , , ']
    yes = [None, '', '1', 'true', 'TRUE', ' yes ', 'On', 'on', '0', 'false', 'no', 'off', 'y', 't', '2', 'True\n', 'ja']
    numbers = [None, '', '0', '12', ' 12 ', '12.9', '-12.9', '1e3', '1794800000000', '1794800000000.75', 'abc', '1,5',
               '0x10', '+7', '-0', '1_000', 'nan', '  ', '12abc', '.5', '5.']
    return {
        'eleven_file_names': [{'content_type': kind, 'name': eleven['_filename_for'](kind)} for kind in kinds],
        'ends_terminal': [{'text': text, 'ends': providers['ends_terminal'](text)} for text in texts],
        'providers': [{'model': model, 'provider': providers['provider_of'](model),
                       'cloud': providers['is_cloud_model'](model)} for model in models],
        'terms': [{'payload': payload, 'terms': providers['split_terms'](payload)} for payload in payloads],
        'truthy': [{'given': value, 'yes': helpers['_truthy_header'](value)} for value in yes],
        'whole': [{'given': value, **said(server['_header_int'], value)} for value in numbers],
    }


def catalogue(providers: dict, local: dict) -> dict:
    """Which engines there are: the cloud ones, each with what it needs and can do, and the
    local models a Host can be given."""
    cloud = []
    for provider in providers['CATALOG']:
        cloud.append({**{key: value for key, value in provider.items() if key != 'models'},
                      'models': [dict(model) for model in provider['models']]})
    return {'cloud': cloud, 'local': [dict(model) for model in local['CATALOG']],
            'recommended': local['RECOMMENDED_MODEL_ID']}


def main() -> int:
    results = taken(LIB / 'transcription_results.py', ['_VALID_JOB_ID', 'normalize_job_id', 'request_fingerprint'])
    router = whole(LIB / 'stt_router.py')
    blocklist = whole(LIB / 'hallucinations.py')
    speech = taken(LIB / 'stt.py', ['_join_confident_segments'], {'Iterable': __import__('typing').Iterable})
    providers = taken(LIB / 'stt_providers.py',
                      ['CATALOG', 'VALID_IDS', 'provider_of', 'is_cloud_model', 'split_terms', 'ends_terminal'])
    eleven = whole(LIB / 'eleven_stt.py')
    helpers = taken(LIB / 'routes' / 'helpers.py', ['_truthy_header'])
    server = taken(REPO / 'server' / 'server.py', ['_header_int'])
    local = whole(LIB / 'transcription_catalog.py')
    vectors = {
        'written_by': f'server-rs/tools/speech-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',
        'job_ids': job_ids(results),
        'fingerprints': fingerprints(results),
        'plans': plans(router),
        'invented': invented(blocklist),
        'joined': joined(speech),
        **small_rules(providers, eleven, helpers, server),
        'catalogue': catalogue(providers, local),
    }
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    counts = {key: (len(value) if isinstance(value, list) else len(value.get('texts', value)))
              for key, value in vectors.items() if isinstance(value, (list, dict))}
    print(f'wrote {OUT.relative_to(REPO)}: {counts}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
